"""POST /captures/{sid}/tags persists live speaker tags into meta.json,
and adopt carries them onto the new meeting."""

from tests.test_meeting_routes import _client

SID = "1720000000000-tagsid"


def _announce(client):
    return client.post("/captures", json={"sid": SID, "mimeType": "audio/webm", "startedAt": 1720000000000})


def test_tags_written_to_meta(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    assert _announce(client).status_code == 201
    r = client.post(f"/captures/{SID}/tags", json={
        "markers": [{"t": 5.0, "name": "Alex"}, {"t": 12.0, "name": "Sarah"}],
        "roster": [{"name": "Alex", "company": "Acme", "title": "CTO"}],
        "title": "Weekly sync", "context": "Planning",
    })
    assert r.status_code == 200, r.text
    meta = app._read_capture_meta(SID)
    assert len(meta["speaker_tags"]) == 2
    assert meta["speaker_roster"][0]["name"] == "Alex"
    assert meta["title"] == "Weekly sync"
    assert meta["context"] == "Planning"


def test_tags_unknown_capture_404(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    r = client.post(f"/captures/{SID}/tags", json={"markers": []})
    assert r.status_code == 404


def test_adopt_carries_tags(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    # Stub processing so adopt does not run the real pipeline.
    async def _noop(mid):
        return None
    monkeypatch.setattr(app, "process_meeting", _noop)
    assert _announce(client).status_code == 201
    client.post(f"/captures/{SID}/chunks/0", content=b"abcd",
                headers={"Content-Type": "application/octet-stream"})
    client.post(f"/captures/{SID}/tags", json={
        "markers": [{"t": 5.0, "name": "Alex"}],
        "roster": [{"name": "Alex", "company": "Acme", "title": "CTO"}],
        "title": "Weekly sync", "context": "Planning",
    })
    r = client.post(f"/captures/{SID}/adopt", json={})
    assert r.status_code == 202, r.text
    mid = r.json()["meeting_id"]
    m = app.meetings[mid]
    assert m["speaker_tags"] == [{"t": 5.0, "name": "Alex"}]
    assert m["speaker_roster"][0]["company"] == "Acme"
    assert m["meeting_context"] == "Planning"
    assert m["title"] == "Weekly sync"


def test_tags_note_id_stored_and_survives_tag_only_reposts(tmp_path, monkeypatch):
    """note_id is mirrored onto the capture meta ONLY when present in the body:
    the frequent tag-only re-posts from liveTags._flush must never wipe it."""
    client, app, _ = _client(tmp_path, monkeypatch)
    assert _announce(client).status_code == 201
    r = client.post(f"/captures/{SID}/tags", json={
        "markers": [], "roster": [], "note_id": "n_1a2b3c4d5e6f",
    })
    assert r.status_code == 200, r.text
    assert app._read_capture_meta(SID)["note_id"] == "n_1a2b3c4d5e6f"
    # tag-only re-post: no note_id key at all
    r = client.post(f"/captures/{SID}/tags", json={
        "markers": [{"t": 1.0, "name": "Alex"}], "roster": [],
    })
    assert r.status_code == 200
    meta = app._read_capture_meta(SID)
    assert meta["note_id"] == "n_1a2b3c4d5e6f"
    assert len(meta["speaker_tags"]) == 1   # roster/markers overwrite semantics unchanged
