"""upload/adopt note_id -> notes_store.link_meeting.

Covers: link-on-upload, unknown/malformed/temp ids ignored, sid-dedup
idempotency (linked_meetings can only ever contain the meeting id once),
dedup-path link REPAIR (retry carries the id the first attempt lacked), and
adopt's body/meta-fallback/dedup variants. Harness: test_meeting_routes._client
(bare TestClient, MEETINGS_DIR/meetings monkeypatched) + NOTES_DIR pointed at a
tmp subdir (test_notes_api pattern) + process_meeting stubbed (test_upload_tags
pattern)."""

import io

import notes_store
from tests.test_meeting_routes import _client

SID = "1720000000000-link01"   # matches ^[A-Za-z0-9-]{8,64}$


def _harness(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()

    async def _noop(mid):
        return None
    monkeypatch.setattr(app, "process_meeting", _noop)
    return client, app


def _upload(client, **data):
    files = {"file": ("rec.webm", io.BytesIO(b"abcd"), "audio/webm")}
    return client.post("/meetings/upload", files=files, data=data)


def _announce_with_chunk(client):
    client.post("/captures", json={"sid": SID, "mimeType": "audio/webm"})
    client.post(f"/captures/{SID}/chunks/0", content=b"abcd",
                headers={"Content-Type": "application/octet-stream"})


def _links(note_id):
    return notes_store.read_note(notes_store.NOTES_DIR, note_id)["linked_meetings"]


# ------------------------------------------------------------- upload

def test_upload_with_note_id_links_meeting(tmp_path, monkeypatch):
    client, app = _harness(tmp_path, monkeypatch)
    note = notes_store.create_note(notes_store.NOTES_DIR, "Sprint notes")
    r = _upload(client, title="Sync", note_id=note["id"])
    assert r.status_code == 202, r.text
    assert _links(note["id"]) == [r.json()["meeting_id"]]


def test_upload_unknown_malformed_temp_note_id_ignored(tmp_path, monkeypatch):
    """202 + no error for ids that can never link: unknown-but-real-shaped,
    temp (n_local_ never sent by a correct client, but the server must still
    shrug), and over the 64-char length cap."""
    client, app = _harness(tmp_path, monkeypatch)
    for bad in ("n_feedbeef0000", "n_local_ab12cd34ef", "x" * 65):
        r = _upload(client, note_id=bad)
        assert r.status_code == 202, r.text


def test_upload_sid_dedup_links_exactly_once(tmp_path, monkeypatch):
    client, app = _harness(tmp_path, monkeypatch)
    note = notes_store.create_note(notes_store.NOTES_DIR, "Sprint notes")
    r1 = _upload(client, sid=SID, note_id=note["id"])
    r2 = _upload(client, sid=SID, note_id=note["id"])   # retry after lost 202
    assert r1.json()["meeting_id"] == r2.json()["meeting_id"]
    assert _links(note["id"]) == [r1.json()["meeting_id"]]


def test_upload_sid_dedup_repairs_missing_link(tmp_path, monkeypatch):
    """First attempt predated the note's flush (no note_id); the retry carries
    the real id -> the dedup early-return path makes the link."""
    client, app = _harness(tmp_path, monkeypatch)
    note = notes_store.create_note(notes_store.NOTES_DIR, "Late note")
    r1 = _upload(client, sid=SID)
    assert _links(note["id"]) == []
    r2 = _upload(client, sid=SID, note_id=note["id"])
    assert r2.json()["meeting_id"] == r1.json()["meeting_id"]
    assert _links(note["id"]) == [r1.json()["meeting_id"]]


# ------------------------------------------------------------- adopt

def test_adopt_body_note_id_links(tmp_path, monkeypatch):
    client, app = _harness(tmp_path, monkeypatch)
    note = notes_store.create_note(notes_store.NOTES_DIR, "Adopt notes")
    _announce_with_chunk(client)
    r = client.post(f"/captures/{SID}/adopt", json={"note_id": note["id"]})
    assert r.status_code == 202, r.text
    assert _links(note["id"]) == [r.json()["meeting_id"]]


def test_adopt_meta_fallback_links(tmp_path, monkeypatch):
    """Dead-device path: the tags mirror carried note_id onto the SERVER
    capture meta; adopt posted {} (the existing recovery UI) still links."""
    client, app = _harness(tmp_path, monkeypatch)
    note = notes_store.create_note(notes_store.NOTES_DIR, "Dead device notes")
    _announce_with_chunk(client)
    client.post(f"/captures/{SID}/tags",
                json={"markers": [], "roster": [], "note_id": note["id"]})
    r = client.post(f"/captures/{SID}/adopt", json={})
    assert r.status_code == 202, r.text
    assert _links(note["id"]) == [r.json()["meeting_id"]]


def test_adopt_dedup_path_repairs_link(tmp_path, monkeypatch):
    """Blob-flush upload won the race (meeting exists, unlinked); a later adopt
    carrying note_id dedups to the same meeting AND repairs the link."""
    client, app = _harness(tmp_path, monkeypatch)
    note = notes_store.create_note(notes_store.NOTES_DIR, "Repair notes")
    _announce_with_chunk(client)
    r1 = _upload(client, sid=SID)               # meeting exists, no link
    r2 = client.post(f"/captures/{SID}/adopt", json={"note_id": note["id"]})
    assert r2.status_code == 202, r2.text
    assert r2.json()["meeting_id"] == r1.json()["meeting_id"]
    assert _links(note["id"]) == [r1.json()["meeting_id"]]
