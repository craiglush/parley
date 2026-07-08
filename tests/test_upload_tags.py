"""Upload accepts speaker_tags / speaker_roster JSON form fields."""

import io
import json
from tests.test_meeting_routes import _client


def test_upload_parses_tags(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    async def _noop(mid):
        return None
    monkeypatch.setattr(app, "process_meeting", _noop)
    files = {"file": ("rec.webm", io.BytesIO(b"abcd"), "audio/webm")}
    data = {
        "title": "Sync",
        "speaker_tags": json.dumps([{"t": 5.0, "name": "Alex"}]),
        "speaker_roster": json.dumps([{"name": "Alex", "company": "Acme", "title": "CTO"}]),
    }
    r = client.post("/meetings/upload", files=files, data=data)
    assert r.status_code == 202, r.text
    mid = r.json()["meeting_id"]
    m = app.meetings[mid]
    assert m["speaker_tags"] == [{"t": 5.0, "name": "Alex"}]
    assert m["speaker_roster"][0]["name"] == "Alex"


def test_upload_bad_tags_json_ignored(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    async def _noop(mid):
        return None
    monkeypatch.setattr(app, "process_meeting", _noop)
    files = {"file": ("rec.webm", io.BytesIO(b"abcd"), "audio/webm")}
    r = client.post("/meetings/upload", files=files, data={"speaker_tags": "not json"})
    assert r.status_code == 202
    m = app.meetings[r.json()["meeting_id"]]
    assert m["speaker_tags"] == []
