"""Tests for the [/] 'doing' checkbox state (T1: note-side pure tasks_store logic).
Meeting-overlay state and the HTTP endpoints are covered by the T2 additions to
this same file (see the bottom of the file after T2 lands)."""
import tasks_store as ts


# --- regex / state parsing ---------------------------------------------------

def test_checkbox_regex_accepts_doing_mark():
    tasks = ts.parse_tasks_from_body("- [/] in progress", "n1", "N")
    assert len(tasks) == 1
    assert tasks[0]["state"] == "doing" and tasks[0]["done"] is False


def test_parse_tasks_from_body_all_states():
    body = "- [ ] open one\n- [/] doing one\n- [x] done one\n- [X] done upper"
    tasks = ts.parse_tasks_from_body(body, "n1", "N")
    assert [t["state"] for t in tasks] == ["open", "doing", "done", "done"]
    assert [t["done"] for t in tasks] == [False, False, True, True]


# --- set_state_line ------------------------------------------------------------

def test_set_state_line_open_to_doing():
    body = "intro\n- [ ] do it @amy 📅 2026-07-01\noutro"
    new, ok = ts.set_state_line(body, 1, "doing")
    assert ok and new.split("\n")[1] == "- [/] do it @amy 📅 2026-07-01"


def test_set_state_line_doing_to_done_to_open():
    body = "- [/] task"
    doneb, ok1 = ts.set_state_line(body, 0, "done")
    assert ok1 and doneb == "- [x] task"
    openb, ok2 = ts.set_state_line(doneb, 0, "open")
    assert ok2 and openb == "- [ ] task"


def test_set_state_line_invalid_state_refuses():
    body = "- [ ] task"
    assert ts.set_state_line(body, 0, "bogus") == (body, False)


def test_set_state_line_guards():
    body = "- [ ] task one\nplain line"
    assert ts.set_state_line(body, 1, "doing")[1] is False   # not a checkbox
    assert ts.set_state_line(body, 9, "doing")[1] is False   # out of range
    assert ts.set_state_line(body, 0, "doing", expected_text="different")[1] is False
    assert ts.set_state_line(body, 0, "doing", expected_text="task one")[1] is True


# --- format_task_line / update_line round-trip --------------------------------

def test_format_task_line_state_takes_precedence_over_done():
    assert ts.format_task_line("x", done=True, state="doing") == "- [/] x"


def test_format_task_line_state_doing():
    assert ts.format_task_line("do it", state="doing") == "- [/] do it"


def test_format_task_line_legacy_done_bool_unaffected():
    assert ts.format_task_line("x", done=True).startswith("- [x] ")
    assert ts.format_task_line("x", done=False).startswith("- [ ] ")


def test_update_line_preserves_doing_state_on_unrelated_edit():
    body = "- [/] old @bob 📅 2026-01-01 ⏫"
    new, ok = ts.update_line(body, 0, "old", "new", owner="amy", due="2026-02-02", priority="low")
    assert ok and new == "- [/] new @amy 📅 2026-02-02 🔽"


def test_update_line_still_preserves_done_and_open():
    # regression: existing done/open behavior from before this feature
    body = "- [x] old @bob 📅 2026-01-01 ⏫"
    new, ok = ts.update_line(body, 0, "old", "new", owner="amy", due="2026-02-02", priority="low")
    assert ok and new == "- [x] new @amy 📅 2026-02-02 🔽"


# --- filter_tasks / sort_tasks --------------------------------------------------

def test_filter_tasks_doing_status():
    tasks = [
        {"text": "a", "done": False, "state": "doing"},
        {"text": "b", "done": False, "state": "open"},
        {"text": "c", "done": True, "state": "done"},
    ]
    assert [t["text"] for t in ts.filter_tasks(tasks, status="doing")] == ["a"]


def test_filter_tasks_doing_status_legacy_no_state_key():
    # tasks without a "state" key (defensive: pre-feature callers) never match "doing"
    tasks = [{"text": "a", "done": False}, {"text": "b", "done": True}]
    assert ts.filter_tasks(tasks, status="doing") == []


def test_sort_tasks_doing_first_within_open():
    tasks = [
        {"text": "open-hi", "done": False, "state": "open", "due": "2026-06-19", "priority": "high"},
        {"text": "doing-lo", "done": False, "state": "doing", "due": None, "priority": "low"},
        {"text": "done", "done": True, "state": "done", "due": None, "priority": None},
    ]
    out = ts.sort_tasks(tasks)
    assert [t["text"] for t in out] == ["doing-lo", "open-hi", "done"]


def test_sort_tasks_backward_compatible_no_state_key():
    # existing behavior (no "state" key at all) must be unchanged
    tasks = [
        {"text": "done", "done": True, "due": None, "priority": None},
        {"text": "later", "done": False, "due": "2026-07-01", "priority": "low"},
        {"text": "soon-hi", "done": False, "due": "2026-06-19", "priority": "high"},
    ]
    out = ts.sort_tasks(tasks)
    assert [t["text"] for t in out] == ["soon-hi", "later", "done"]


# ==============================================================================
# T2: state-change endpoints (note RMW+409, meeting overlay state)
# ==============================================================================
import json
from fastapi.testclient import TestClient
from tests.test_meeting_routes import _FakeEmbedder, _FakeQdrant


def _client(tmp_path, monkeypatch):
    """Combines the test_meeting_routes fakes (get_qdrant/get_embedder patched on
    BOTH app and vector, per that file's own docstring on why both are needed) with
    a notes_store.NOTES_DIR patch, since /api/tasks/state and /api/meetings/{id}/
    tasks/state both need exercising here."""
    import app
    import notes_store
    import vector
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(app, "meetings", {})
    fake_q = _FakeQdrant()
    monkeypatch.setattr(app, "get_qdrant", lambda: fake_q)
    monkeypatch.setattr(app, "get_embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(vector, "get_qdrant", lambda: fake_q)
    monkeypatch.setattr(vector, "get_embedder", lambda: _FakeEmbedder())
    return TestClient(app.app), app


def test_note_task_state_round_trip_and_409(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "T", "body": "- [ ] one"}).json()["id"]

    r = client.post("/api/tasks/state", json={"note_id": nid, "line": 0, "state": "doing", "expected_text": "one"})
    assert r.status_code == 200, r.text
    t = next(t for t in client.get("/api/tasks").json()["tasks"] if t["text"] == "one")
    assert t["state"] == "doing" and t["done"] is False

    r2 = client.post("/api/tasks/state", json={"note_id": nid, "line": 0, "state": "done", "expected_text": "one"})
    assert r2.status_code == 200
    t2 = next(t for t in client.get("/api/tasks").json()["tasks"] if t["text"] == "one")
    assert t2["state"] == "done" and t2["done"] is True

    # stale expected_text -> 409
    r3 = client.post("/api/tasks/state", json={"note_id": nid, "line": 0, "state": "open", "expected_text": "WRONG"})
    assert r3.status_code == 409

    # invalid state -> 400
    r4 = client.post("/api/tasks/state", json={"note_id": nid, "line": 0, "state": "bogus"})
    assert r4.status_code == 400


def test_note_task_state_404_unknown_note(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    r = client.post("/api/tasks/state", json={"note_id": "nope", "line": 0, "state": "doing"})
    assert r.status_code == 404


def _seed_meeting(app, tmp_path, mid="m1"):
    mdir = tmp_path / "_m"
    mdir.mkdir(exist_ok=True)
    (mdir / "summary.json").write_text(json.dumps({"action_items": [
        {"task": "Send report", "who": "", "deadline": "2026-07-01", "priority": "high"},
    ]}))
    app.meetings[mid] = {"title": "Sync", "status": app.MeetingStatus.complete, "output_dir": str(mdir)}


def test_meeting_task_state_round_trip(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    _seed_meeting(app, tmp_path)

    r = client.post("/api/meetings/m1/tasks/state", json={"index": 0, "state": "doing"})
    assert r.status_code == 200, r.text
    t = next(t for t in client.get("/api/tasks?all_owners=1").json()["tasks"] if t["source"] == "meeting")
    assert t["state"] == "doing" and t["done"] is False

    r2 = client.post("/api/meetings/m1/tasks/state", json={"index": 0, "state": "done"})
    assert r2.status_code == 200
    t2 = next(t for t in client.get("/api/tasks?all_owners=1").json()["tasks"] if t["source"] == "meeting")
    assert t2["state"] == "done" and t2["done"] is True

    # invalid state -> 400; unknown index -> 404; unknown meeting -> 404
    assert client.post("/api/meetings/m1/tasks/state", json={"index": 0, "state": "bogus"}).status_code == 400
    assert client.post("/api/meetings/m1/tasks/state", json={"index": 99, "state": "doing"}).status_code == 404
    assert client.post("/api/meetings/nope/tasks/state", json={"index": 0, "state": "doing"}).status_code == 404


def test_meeting_task_toggle_resets_state_to_open_or_done(tmp_path, monkeypatch):
    # Regression: a task marked 'doing' via the state endpoint, then completed and
    # un-completed via the OLD toggle endpoint, must land on done/open -- never
    # stay stuck on 'doing' (mirrors note toggle_line's full-overwrite semantics).
    client, app = _client(tmp_path, monkeypatch)
    _seed_meeting(app, tmp_path)
    client.post("/api/meetings/m1/tasks/state", json={"index": 0, "state": "doing"})
    client.post("/api/meetings/m1/tasks/toggle", json={"index": 0, "done": True})
    t = next(t for t in client.get("/api/tasks?all_owners=1").json()["tasks"] if t["source"] == "meeting")
    assert t["state"] == "done"
    client.post("/api/meetings/m1/tasks/toggle", json={"index": 0, "done": False})
    t2 = next(t for t in client.get("/api/tasks?all_owners=1").json()["tasks"] if t["source"] == "meeting")
    assert t2["state"] == "open"   # NOT back to 'doing'


def test_api_tasks_emits_state_for_notes_and_meetings(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    _seed_meeting(app, tmp_path)
    client.post("/api/notes", json={"title": "T", "body": "- [/] a doing note task"})
    tasks = client.get("/api/tasks?all_owners=1").json()["tasks"]
    assert all("state" in t for t in tasks)
    note_task = next(t for t in tasks if t["source"] == "note")
    meeting_task = next(t for t in tasks if t["source"] == "meeting")
    assert note_task["state"] == "doing"
    assert meeting_task["state"] == "open"
