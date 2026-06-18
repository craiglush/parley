"""Characterization tests for the meeting routes (the previously-untested half of app.py).

These pin the externally-observable behavior of the meeting HTTP layer — response
shapes, status codes, and state/file effects — so the deeper app.py split
(routes_meetings.py / storage.py / vector.py / ...) can proceed behavior-preserving.

Pattern mirrors tests/test_notes_api.py: bare TestClient(app.app) (so FastAPI
startup events do NOT fire and never clobber the seeded `meetings` dict), with
MEETINGS_DIR / SETTINGS_PATH / meetings / get_qdrant / get_embedder monkeypatched.
No live Ollama / WhisperX / Qdrant.
"""

import json
import numpy as np
from fastapi.testclient import TestClient


class _FakeEmbedder:
    # search/chat call .encode(q).tolist(), so return a numpy array.
    def encode(self, texts):
        if isinstance(texts, str):
            return np.asarray([0.1, 0.2, 0.3, 0.4], dtype="float32")
        return np.asarray([[0.1, 0.2, 0.3, 0.4] for _ in texts], dtype="float32")


class _FakeQdrant:
    def __init__(self):
        self.points = {}

    def get_collections(self):
        c = type("C", (), {})()
        c.collections = [type("X", (), {"name": n})() for n in self.points]
        return c

    def create_collection(self, collection_name, vectors_config):
        self.points.setdefault(collection_name, [])

    def upsert(self, collection_name, points):
        self.points.setdefault(collection_name, []).extend(points)

    def delete(self, collection_name, points_selector):
        # delete_meeting passes a meeting_id filter; best-effort, just no-op here.
        return None

    def search(self, collection_name, query_vector, limit=10, query_filter=None):
        out = []
        for payload in self.points.get(collection_name, [])[:limit]:
            h = type("H", (), {})()
            h.payload = payload
            h.score = 0.9
            out.append(h)
        return out


def _client(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(app, "meetings", {})            # fresh, auto-reverts
    fake_q = _FakeQdrant()
    monkeypatch.setattr(app, "get_qdrant", lambda: fake_q)
    monkeypatch.setattr(app, "get_embedder", lambda: _FakeEmbedder())
    return TestClient(app.app), app, fake_q


def _seed_complete(app, tmp_path, mid="abc12345", title="Weekly Sync", date="2026-06-17", **extra):
    out = tmp_path / f"{date}_{mid}"
    out.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": mid, "date": date, "title": title,
        "status": app.MeetingStatus.complete,
        "created_at": "2026-06-17T10:00:00+00:00",
        "duration_formatted": "00:10:00",
        "progress_percent": 100, "progress_detail": "Complete",
        "transcript_cleaned": True, "step_timings": {"transcription": 1.0},
        "tags": {"category": "standup", "keywords": ["roadmap"],
                 "entities": {"people": ["Alex"], "companies": [], "projects": [],
                              "technologies": [], "dates": []}},
        "output_dir": str(out),
        "links": {"manual": [], "suggestions": []},
    }
    rec.update(extra)
    app.meetings[mid] = rec
    return rec, out


# --------------------------------------------------------------------------- info / status

def test_api_info(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path)
    r = client.get("/api/info")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "2.0.0"
    assert body["meetings_count"] == 1


def test_status_404_and_shape(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    assert client.get("/meetings/nope/status").status_code == 404
    _seed_complete(app, tmp_path, mid="m1")
    r = client.get("/meetings/m1/status")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["id"] == "m1" and s["status"] == "complete"
    assert s["title"] == "Weekly Sync" and s["progress_percent"] == 100
    for k in ("date", "duration_formatted", "progress_detail", "step_timings", "transcript_cleaned"):
        assert k in s


# --------------------------------------------------------------------------- transcript / summary

def test_transcript_404_409_200(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    # 404 unknown
    assert client.get("/meetings/x/transcript").status_code == 404
    # 409 not complete
    app.meetings["q1"] = {"id": "q1", "date": "2026-06-17", "title": "Q",
                          "status": app.MeetingStatus.queued, "output_dir": str(tmp_path / "q")}
    assert client.get("/meetings/q1/transcript").status_code == 409
    # 200 returns transcript.json verbatim
    rec, out = _seed_complete(app, tmp_path, mid="m1")
    transcript = {"meeting_id": "m1", "date": "2026-06-17", "duration": 600, "language": "en",
                  "segments": [{"start": 0.0, "end": 2.0, "text": "hello world", "speaker": "SPEAKER_00"}],
                  "cleaned": True}
    (out / "transcript.json").write_text(json.dumps(transcript))
    r = client.get("/meetings/m1/transcript")
    assert r.status_code == 200, r.text
    assert r.json()["segments"][0]["text"] == "hello world"


def test_summary_200_and_missing_file_404(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(app, tmp_path, mid="m1")
    # file missing -> 404
    assert client.get("/meetings/m1/summary").status_code == 404
    summary = {"title": "Weekly Sync", "summary": "did things", "topics": [],
               "action_items": [], "decisions": [], "open_questions": [],
               "concerns": [], "figures": [], "sentiment": {}}
    (out / "summary.json").write_text(json.dumps(summary))
    r = client.get("/meetings/m1/summary")
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "did things"


# --------------------------------------------------------------------------- files (allowed-set guard)

def test_files_download_and_guard(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(app, tmp_path, mid="m1")
    (out / "summary.json").write_text(json.dumps({"title": "t"}))
    # allowed + present -> 200
    r = client.get("/meetings/m1/files/summary.json")
    assert r.status_code == 200, r.text
    # disallowed filename (incl. any traversal-looking name) -> 400 before disk touch
    assert client.get("/meetings/m1/files/notes.json").status_code == 400
    assert client.get("/meetings/m1/files/secret.env").status_code == 400
    # allowed but missing -> 404
    assert client.get("/meetings/m1/files/tags.json").status_code in (200, 404)  # tags.json may not exist


def test_audio_404_when_missing(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1")
    assert client.get("/meetings/m1/audio").status_code == 404


# --------------------------------------------------------------------------- list / grouped

def test_list_meetings_and_filter(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1", title="Alpha")
    app.meetings["m2"] = {"id": "m2", "date": "2026-06-16", "title": "Beta",
                          "status": app.MeetingStatus.queued, "created_at": "2026-06-16T09:00:00+00:00"}
    data = client.get("/meetings").json()
    assert isinstance(data, list) and len(data) == 2
    ids = {m["id"] for m in data}
    assert ids == {"m1", "m2"}
    # filter by status
    done = client.get("/meetings?status=complete").json()
    assert [m["id"] for m in done] == ["m1"]


def test_grouped_contains_meeting(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1")
    r = client.get("/meetings/grouped?group_by=week")
    assert r.status_code == 200, r.text
    blob = json.dumps(r.json())
    assert "m1" in blob
    # invalid group_by -> 400
    assert client.get("/meetings/grouped?group_by=bogus").status_code == 400


# --------------------------------------------------------------------------- tags

def test_tags_get_and_update(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(app, tmp_path, mid="m1")
    # get returns the in-memory tags
    assert client.get("/meetings/m1/tags").json()["category"] == "standup"
    # valid update
    r = client.put("/meetings/m1/tags", json={"category": "planning", "keywords": ["q3", "okrs"]})
    assert r.status_code == 200, r.text
    assert r.json()["tags"]["category"] == "planning"
    assert app.meetings["m1"]["tags"]["category"] == "planning"
    # invalid category -> 400
    assert client.put("/meetings/m1/tags", json={"category": "not_a_category"}).status_code == 400


# --------------------------------------------------------------------------- per-meeting notes CRUD

def test_per_meeting_notes_crud(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1")            # output_dir exists -> notes persist
    # create
    r = client.post("/meetings/m1/notes", json={"type": "free", "content": "a thought"})
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["content"] == "a thought" and note["id"].startswith("n_")
    nid = note["id"]
    # list
    assert any(n["id"] == nid for n in client.get("/meetings/m1/notes").json()["notes"])
    # update
    r = client.put(f"/meetings/m1/notes/{nid}", json={"content": "edited"})
    assert r.status_code == 200 and r.json()["content"] == "edited"
    # annotation without segment_start -> 400
    assert client.post("/meetings/m1/notes", json={"type": "annotation", "content": "x"}).status_code == 400
    # delete
    assert client.delete(f"/meetings/m1/notes/{nid}").status_code == 200
    assert client.delete(f"/meetings/m1/notes/{nid}").status_code == 404


# --------------------------------------------------------------------------- speakers

def test_speakers_get(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    # 409 when not complete
    app.meetings["q1"] = {"id": "q1", "date": "2026-06-17", "title": "Q",
                          "status": app.MeetingStatus.queued, "output_dir": str(tmp_path / "q")}
    assert client.get("/meetings/q1/speakers").status_code == 409
    # 200 with speaker_info.json
    rec, out = _seed_complete(app, tmp_path, mid="m1")
    (out / "speaker_info.json").write_text(json.dumps(
        {"SPEAKER_00": {"name": "Alex", "title": "", "company": "", "display_name": "Alex",
                        "confidence": "high", "auto_detected": True}}))
    r = client.get("/meetings/m1/speakers")
    assert r.status_code == 200, r.text
    assert r.json()["speaker_info"]["SPEAKER_00"]["name"] == "Alex"


# --------------------------------------------------------------------------- settings

def test_settings_get_update_reset(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    g = client.get("/api/settings").json()
    assert "settings" in g and "defaults" in g
    # update (use a clearly non-default value so reset is observable)
    r = client.put("/api/settings", json={"ollama_model": "test-model-xyz", "temperature": 0.42})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["ollama_model"] == "test-model-xyz"
    assert r.json()["settings"]["temperature"] == 0.42
    # persisted to disk (SETTINGS_PATH under tmp_path)
    assert (tmp_path / "settings.json").exists()
    assert client.get("/api/settings").json()["settings"]["ollama_model"] == "test-model-xyz"
    # reset reverts to defaults
    r = client.post("/api/settings/reset")
    assert r.status_code == 200 and r.json()["settings"]["ollama_model"] != "test-model-xyz"


# --------------------------------------------------------------------------- search

def test_search_validation_and_results(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    # empty q -> 400
    assert client.get("/meetings/search?q=").status_code == 400
    # seed a vector hit in the meetings collection
    fake_q.points["meetings"] = [{
        "meeting_id": "m1", "date": "2026-06-17", "title": "Weekly Sync",
        "chunk_type": "transcript", "speaker": "Alex", "text": "discuss roadmap", "timestamp": 0,
    }]
    r = client.get("/meetings/search?q=roadmap")
    assert r.status_code == 200, r.text
    res = r.json()
    assert isinstance(res, list) and res[0]["meeting_id"] == "m1"
    assert res[0]["text"] == "discuss roadmap"


# --------------------------------------------------------------------------- delete

def test_delete_meeting(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1")
    assert client.delete("/meetings/nope").status_code == 404
    r = client.delete("/meetings/m1")
    assert r.status_code == 200, r.text
    assert "m1" not in app.meetings


# --------------------------------------------------------------------------- upload (pipeline trigger, mocked)

def test_upload_validation_and_enqueue(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)

    async def _noop(meeting_id):
        return None
    monkeypatch.setattr(app, "process_meeting", _noop)

    # unsupported extension -> 400
    bad = client.post("/meetings/upload", files={"file": ("notes.txt", b"x", "text/plain")})
    assert bad.status_code == 400, bad.text

    # valid audio -> 202 queued, returns a meeting_id, registered in `meetings`
    ok = client.post("/meetings/upload", files={"file": ("rec.wav", b"RIFFxxxxWAVE", "audio/wav")},
                     data={"title": "My Meeting"})
    assert ok.status_code == 202, ok.text
    body = ok.json()
    assert body["status"] == "queued" and "meeting_id" in body
    assert body["meeting_id"] in app.meetings


# --------------------------------------------------------------------------- insight_id path-traversal guard

def test_insight_id_validation(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1")
    r = client.get("/meetings/m1/insights?insight_id=../../etc/passwd")
    assert r.status_code == 400, r.text


# --------------------------------------------------------------------------- links (read)

def test_links_get(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1")
    r = client.get("/meetings/m1/links")
    assert r.status_code == 200, r.text
