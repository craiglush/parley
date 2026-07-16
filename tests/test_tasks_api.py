import asyncio
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


def test_task_full_crud(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "meetings", {})

    # CREATE with no note_id -> auto-creates the "Tasks" inbox note
    r = client.post("/api/tasks", json={"text": "buy milk", "owner": "alex",
                                        "due": "2026-07-01", "priority": "high"})
    assert r.status_code == 200
    inbox_id = r.json()["note_id"]

    t = next(t for t in client.get("/api/tasks").json()["tasks"] if t["text"] == "buy milk")
    assert (t["owner"], t["due"], t["priority"]) == ("alex", "2026-07-01", "high")
    assert t["source"] == "note" and t["source_id"] == inbox_id
    assert any(n["title"] == "Tasks" for n in client.get("/api/notes").json()["notes"])

    # CREATE again reuses the same inbox (no duplicate note)
    client.post("/api/tasks", json={"text": "second"})
    assert sum(1 for n in client.get("/api/notes").json()["notes"] if n["title"] == "Tasks") == 1

    # empty text -> 400
    assert client.post("/api/tasks", json={"text": "   "}).status_code == 400

    # UPDATE text + metadata
    r = client.patch("/api/tasks", json={"note_id": inbox_id, "line": t["line"],
                                         "expected_text": "buy milk", "text": "buy oat milk",
                                         "owner": "sam", "due": "", "priority": "low"})
    assert r.status_code == 200
    t2 = next(t for t in client.get("/api/tasks").json()["tasks"] if t["text"] == "buy oat milk")
    assert t2["owner"] == "sam" and t2["priority"] == "low" and not t2["due"]

    # UPDATE with stale expected_text -> 409
    assert client.patch("/api/tasks", json={"note_id": inbox_id, "line": t2["line"],
                                            "expected_text": "WRONG", "text": "x"}).status_code == 409

    # DELETE
    r = client.request("DELETE", "/api/tasks", json={"note_id": inbox_id, "line": t2["line"],
                                                     "expected_text": "buy oat milk"})
    assert r.status_code == 200
    assert not any(t["text"] == "buy oat milk" for t in client.get("/api/tasks").json()["tasks"])

    # DELETE with stale expected_text -> 409
    assert client.request("DELETE", "/api/tasks", json={"note_id": inbox_id, "line": 0,
                                                        "expected_text": "NOPE"}).status_code == 409


def test_meeting_task_overlay_crud(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    mdir = tmp_path / "_m"
    mdir.mkdir()
    (mdir / "summary.json").write_text(json.dumps({"action_items": [
        {"task": "Send report", "who": "Amy", "deadline": "2026-07-01", "priority": "high"},
        {"task": "Book venue", "who": "Bob", "priority": "low"},
    ]}))
    monkeypatch.setattr(app, "meetings", {"m1": {"title": "Sync", "status": app.MeetingStatus.complete, "output_dir": str(mdir)}})

    mt = [t for t in client.get("/api/tasks").json()["tasks"] if t["source"] == "meeting"]
    assert len(mt) == 2
    t0 = next(t for t in mt if t["text"] == "Send report")
    assert t0["done"] is False and t0["index"] == 0

    # complete in place -> persisted to a sidecar (no note created)
    assert client.post("/api/meetings/m1/tasks/toggle", json={"index": 0, "done": True}).status_code == 200
    assert (mdir / "task_overlay.json").exists()
    assert next(t for t in client.get("/api/tasks").json()["tasks"]
                if t["source"] == "meeting" and t["text"] == "Send report")["done"] is True

    # edit text + metadata (done state preserved; due cleared)
    assert client.patch("/api/meetings/m1/tasks", json={"index": 0, "text": "Send final report",
                                                        "owner": "Carol", "due": "", "priority": "low"}).status_code == 200
    edited = next(t for t in client.get("/api/tasks").json()["tasks"]
                  if t["source"] == "meeting" and t["text"] == "Send final report")
    assert edited["owner"] == "Carol" and edited["priority"] == "low" and not edited["due"] and edited["done"] is True

    # dismiss -> drops out of the list, other items keep their index
    assert client.request("DELETE", "/api/meetings/m1/tasks", json={"index": 0}).status_code == 200
    remaining = [t for t in client.get("/api/tasks").json()["tasks"] if t["source"] == "meeting"]
    assert len(remaining) == 1 and remaining[0]["text"] == "Book venue" and remaining[0]["index"] == 1

    # guards: bad index 404, empty edit 400, unknown meeting 404
    assert client.post("/api/meetings/m1/tasks/toggle", json={"index": 99, "done": True}).status_code == 404
    assert client.patch("/api/meetings/m1/tasks", json={"index": 1, "text": "  "}).status_code == 400
    assert client.post("/api/meetings/nope/tasks/toggle", json={"index": 0, "done": True}).status_code == 404


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


def test_read_json_cached_caches_until_file_changes(tmp_path):
    import app
    p = tmp_path / "summary.json"
    p.write_text(json.dumps({"action_items": [{"task": "one"}]}))
    first = app._read_json_cached(p)
    assert first == {"action_items": [{"task": "one"}]}
    assert app._read_json_cached(p) is first          # unchanged -> cached object, no re-parse
    # different content (and size) -> (mtime_ns, size) key misses -> re-read
    p.write_text(json.dumps({"action_items": [{"task": "one"}, {"task": "two"}]}))
    assert len(app._read_json_cached(p)["action_items"]) == 2
    p.unlink()
    assert app._read_json_cached(p) is None           # missing -> None


def test_tasks_collection_runs_off_event_loop(tmp_path, monkeypatch):
    # /api/tasks used to collect synchronously ON the event loop (unlike its
    # offloaded siblings), stalling the whole service for the full walk. Inside
    # run_in_executor there is no running loop -> RuntimeError.
    client, app = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "meetings", {})
    seen = {}

    def probe():
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return []

    monkeypatch.setattr(app, "_collect_note_tasks", probe)
    assert client.get("/api/tasks").status_code == 200
    assert seen["on_loop"] is False


def test_meeting_tasks_pick_up_summary_rewrite(tmp_path, monkeypatch):
    # Guards the cache KEY: a rewritten summary.json (new mtime/size) must be
    # re-read, never served stale from the JSON cache.
    client, app = _client(tmp_path, monkeypatch)
    mdir = tmp_path / "_m"
    mdir.mkdir()
    (mdir / "summary.json").write_text(json.dumps({"action_items": [{"task": "First", "who": ""}]}))
    monkeypatch.setattr(app, "meetings", {"m1": {"title": "Sync", "status": app.MeetingStatus.complete, "output_dir": str(mdir)}})
    texts = {t["text"] for t in client.get("/api/tasks?all_owners=1").json()["tasks"]}
    assert texts == {"First"}
    (mdir / "summary.json").write_text(json.dumps({"action_items": [
        {"task": "First", "who": ""}, {"task": "Second added later", "who": ""}]}))
    texts = {t["text"] for t in client.get("/api/tasks?all_owners=1").json()["tasks"]}
    assert "Second added later" in texts
