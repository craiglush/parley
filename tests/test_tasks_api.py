import json
from fastapi.testclient import TestClient


class _FakeEmbedder:
    def encode(self, texts):
        if isinstance(texts, str):
            return [0.1, 0.2, 0.3, 0.4]
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _FakeQdrant:
    def __init__(self):
        self.points = {}
    def get_collections(self):
        c = type("C", (), {})(); c.collections = [type("X", (), {"name": n})() for n in self.points]
        return c
    def create_collection(self, collection_name, vectors_config):
        self.points.setdefault(collection_name, [])
    def upsert(self, collection_name, points):
        self.points.setdefault(collection_name, []).extend(points)
    def delete(self, collection_name, points_selector):
        tgt = points_selector.must[0].match.value
        self.points[collection_name] = [p for p in self.points.get(collection_name, []) if (p.payload or {}).get("note_id") != tgt]
    def search(self, collection_name, query_vector, limit=10, query_filter=None):
        out = []
        for p in self.points.get(collection_name, [])[:limit]:
            h = type("H", (), {})(); h.payload = p.payload; h.score = 0.9
            out.append(h)
        return out


def _client(tmp_path, monkeypatch):
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path)
    notes_store._index_cache.clear()
    import app
    fake_q = _FakeQdrant()
    fake_e = _FakeEmbedder()
    monkeypatch.setattr(app, "get_qdrant", lambda: fake_q)
    monkeypatch.setattr(app, "get_embedder", lambda: fake_e)
    return TestClient(app.app), app


def test_tasks_rollup_and_toggle(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "meetings", {})  # no meeting tasks for this test

    # a note with two checkboxes
    nid = client.post("/api/notes", json={"title": "Todos", "body": "- [ ] one\n- [x] two"}).json()["id"]

    tasks = client.get("/api/tasks").json()["tasks"]
    assert len(tasks) == 2
    assert len(client.get("/api/tasks?status=open").json()["tasks"]) == 1

    # toggle "one" -> done
    one = next(t for t in tasks if t["text"] == "one")
    r = client.post("/api/tasks/toggle", json={"note_id": nid, "line": one["line"], "done": True, "expected_text": "one"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(client.get("/api/tasks?status=open").json()["tasks"]) == 0

    # stale expected_text -> 409
    r2 = client.post("/api/tasks/toggle", json={"note_id": nid, "line": one["line"], "done": False, "expected_text": "WRONG"})
    assert r2.status_code == 409


def test_rename_and_push_action_items(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)

    # rename
    nid = client.post("/api/notes", json={"title": "Draft", "folder": "Inbox"}).json()["id"]
    r = client.post(f"/api/notes/{nid}/rename", json={"title": "Final", "folder": "Archive"})
    assert r.status_code == 200 and r.json()["path"] == "Archive/final.md"

    # push action items from a fake meeting
    mdir = tmp_path / "_m"
    mdir.mkdir()
    (mdir / "summary.json").write_text(json.dumps({"action_items": [
        {"task": "Follow up", "who": "Amy", "deadline": "2026-06-25", "priority": "high"}]}))
    monkeypatch.setattr(app, "meetings", {"m1": {"title": "Sync", "status": app.MeetingStatus.complete, "output_dir": str(mdir)}})

    r = client.post(f"/api/notes/{nid}/push-action-items", json={"meeting_id": "m1"})
    assert r.status_code == 200
    assert "- [ ] Follow up" in r.json()["body"] and "📅 2026-06-25" in r.json()["body"]
    # and it now shows up as a task
    assert any(t["text"] == "Follow up" for t in client.get("/api/tasks").json()["tasks"])
