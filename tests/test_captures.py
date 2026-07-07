"""Tests for the streaming-capture failsafe — POST/GET/DELETE /captures.

Chunks stream to _captures/{sid}/chunks/{seq}.part while recording; adopt
assembles them in seq order into a new meeting. Reuses the test_meeting_routes
harness (bare TestClient, MEETINGS_DIR/meetings monkeypatched); process_meeting
and the ffmpeg helpers are never touched by these routes except adopt (stubbed).
"""

from pathlib import Path

from tests.test_meeting_routes import _client

SID = "1720000000000-abc123"   # matches the client sid format, passes ^[A-Za-z0-9-]{8,64}$


def _post_chunk(client, sid, seq, data):
    return client.post(f"/captures/{sid}/chunks/{seq}", content=data,
                       headers={"Content-Type": "application/octet-stream"})


def test_start_chunk_stop_flow(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    r = client.post("/captures", json={"sid": SID, "mimeType": "audio/webm", "startedAt": 1720000000000})
    assert r.status_code == 201, r.text
    # staging dir + meta created
    assert (tmp_path / "_captures" / SID / "meta.json").exists()

    assert _post_chunk(client, SID, 0, b"aaaa").status_code == 200
    assert _post_chunk(client, SID, 1, b"bbbbbb").status_code == 200
    r = client.post(f"/captures/{SID}/stop", json={"durationLabel": "0:05", "fileName": "rec.webm"})
    assert r.status_code == 200, r.text

    listing = client.get("/captures").json()
    assert len(listing) == 1
    c = listing[0]
    assert c["sid"] == SID and c["chunk_count"] == 2 and c["bytes"] == 10
    assert c["stopped"] is True and c["fileName"] == "rec.webm"


def test_re_announce_keeps_chunks(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    client.post("/captures", json={"sid": SID})
    _post_chunk(client, SID, 0, b"data")
    # A second POST /captures (e.g. a reconnect) must not wipe existing chunks.
    r = client.post("/captures", json={"sid": SID})
    assert r.status_code == 200 and r.json()["status"] == "exists"
    assert client.get("/captures").json()[0]["chunk_count"] == 1


def test_chunk_retry_is_idempotent(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    client.post("/captures", json={"sid": SID})
    _post_chunk(client, SID, 0, b"12345")
    _post_chunk(client, SID, 0, b"12345")   # resend same seq
    c = client.get("/captures").json()[0]
    assert c["chunk_count"] == 1 and c["bytes"] == 5


def test_out_of_order_chunks(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    client.post("/captures", json={"sid": SID})
    _post_chunk(client, SID, 2, b"cc")
    _post_chunk(client, SID, 0, b"a")
    _post_chunk(client, SID, 1, b"bb")
    c = client.get("/captures").json()[0]
    assert c["chunk_count"] == 3 and c["bytes"] == 5


def test_chunk_size_cap(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "CAPTURE_MAX_CHUNK_BYTES", 8)
    client.post("/captures", json={"sid": SID})
    assert _post_chunk(client, SID, 0, b"x" * 20).status_code == 413


def test_capture_total_size_cap(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "MAX_UPLOAD_SIZE", 6)
    client.post("/captures", json={"sid": SID})
    assert _post_chunk(client, SID, 0, b"aaaa").status_code == 200
    assert _post_chunk(client, SID, 1, b"bbbb").status_code == 413   # would exceed 6 total


def test_sid_validation(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    assert client.post("/captures", json={"sid": "short"}).status_code == 400
    assert client.post("/captures", json={"sid": "bad/../slash-xxxxx"}).status_code == 400
    # chunk / stop / delete on a bad sid also 400 (path-traversal safe)
    assert _post_chunk(client, "bad/../x", 0, b"a").status_code == 400
    assert client.delete("/captures/bad..slash").status_code == 400


def test_chunk_unknown_capture_404(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    assert _post_chunk(client, SID, 0, b"a").status_code == 404


def test_adopt_assembles_in_order_and_queues(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    processed = {}

    def fake_process(meeting_id):
        processed["id"] = meeting_id

        async def _noop():
            pass
        return _noop()

    monkeypatch.setattr(app, "process_meeting", fake_process)

    client.post("/captures", json={"sid": SID, "mimeType": "audio/webm"})
    _post_chunk(client, SID, 0, b"hello ")
    _post_chunk(client, SID, 2, b"world")
    _post_chunk(client, SID, 1, b"brave ")

    r = client.post(f"/captures/{SID}/adopt", json={"title": "Rescued"})
    assert r.status_code == 202, r.text
    new_id = r.json()["meeting_id"]
    assert r.json()["title"] == "Rescued"

    nm = app.meetings[new_id]
    assert nm["status"] == app.MeetingStatus.queued
    assert nm["recovered_from_capture"] == SID
    # bytes assembled in seq order (0,1,2), not arrival order
    assert Path(nm["original_path"]).read_bytes() == b"hello brave world"
    assert processed["id"] == new_id
    # staging removed after adopt
    assert not (tmp_path / "_captures" / SID).exists()


def test_adopt_never_stopped_capture(tmp_path, monkeypatch):
    """Crash case: the recorder died, so the capture was never /stop-ped — adopt still works."""
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "process_meeting", lambda mid: _acoro())
    client.post("/captures", json={"sid": SID})
    _post_chunk(client, SID, 0, b"partial")
    r = client.post(f"/captures/{SID}/adopt", json={})
    assert r.status_code == 202, r.text
    assert r.json()["title"].startswith("Recovered recording")


def test_adopt_empty_404(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    client.post("/captures", json={"sid": SID})
    assert client.post(f"/captures/{SID}/adopt", json={}).status_code == 404


def test_delete_removes_staging(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    client.post("/captures", json={"sid": SID})
    _post_chunk(client, SID, 0, b"x")
    assert (tmp_path / "_captures" / SID).exists()
    assert client.delete(f"/captures/{SID}").status_code == 200
    assert not (tmp_path / "_captures" / SID).exists()
    assert client.get("/captures").json() == []


def test_gc_prunes_only_stale(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    # Fresh capture
    client.post("/captures", json={"sid": SID})
    _post_chunk(client, SID, 0, b"fresh")
    # Stale capture: hand-write meta with an ancient updated_at
    stale = "9990000000000-old999"
    import json as _json
    sd = tmp_path / "_captures" / stale / "chunks"
    sd.mkdir(parents=True)
    (sd / "000000.part").write_bytes(b"old")
    (tmp_path / "_captures" / stale / "meta.json").write_text(_json.dumps({
        "sid": stale, "chunk_count": 1, "bytes": 3, "updated_at": 1.0,
    }))

    pruned = app._gc_captures()
    assert pruned == 1
    assert not (tmp_path / "_captures" / stale).exists()
    assert (tmp_path / "_captures" / SID).exists()   # fresh one survives


async def _acoro():
    pass
