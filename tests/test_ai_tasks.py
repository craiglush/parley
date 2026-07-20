import json
from tests.test_meeting_routes import _client


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def json(self):
        return self._payload


# --- POST /api/tasks/ai/parse -----------------------------------------------

def test_ai_parse_endpoint_happy_path(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod, "meetings", {})

    async def _fake_call(method, url, *, json_body, **kw):
        assert json_body["format"] == app_mod.ANALYSIS_SCHEMAS["task_parse"]
        return _FakeResp({"response": json.dumps(
            {"text": "chase John about the contract", "due": "2026-07-24", "priority": "high", "owner": "John"})})
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _fake_call)

    r = client.post("/api/tasks/ai/parse", json={"text": "chase John about the contract next Friday, high priority"})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "chase John about the contract", "due": "2026-07-24", "priority": "high", "owner": "John"}


def test_ai_parse_empty_text_short_circuits(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)

    async def _boom(method, url, *, json_body, **kw):
        raise AssertionError("must not call the LLM for empty text")
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _boom)

    r = client.post("/api/tasks/ai/parse", json={"text": "   "})
    assert r.status_code == 200
    assert r.json() == {"text": "", "due": "", "priority": "", "owner": ""}


def test_ai_parse_non_fatal_on_llm_failure(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)

    async def _boom(method, url, *, json_body, **kw):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _boom)

    r = client.post("/api/tasks/ai/parse", json={"text": "call Amy tomorrow"})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "call Amy tomorrow", "due": "", "priority": "", "owner": ""}


def test_ai_parse_rejects_bad_due_and_priority(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)

    async def _fake_call(method, url, *, json_body, **kw):
        return _FakeResp({"response": json.dumps({"text": "x", "due": "next Friday", "priority": "urgent", "owner": ""})})
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _fake_call)

    r = client.post("/api/tasks/ai/parse", json={"text": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["due"] == "" and body["priority"] == ""   # malformed date / non-enum priority dropped


# --- POST /api/tasks/ai/triage ----------------------------------------------
# Every triage test below patches notes_store.NOTES_DIR: the triage endpoint collects
# note tasks via app._collect_all_tasks() -> notes_store.NOTES_DIR, which _client
# (test_meeting_routes) does NOT patch. Without this, POSTed tasks land on the real
# on-disk vault and (worse) leak across test runs/tests -- test_ai_triage_no_open_
# tasks_short_circuits in particular needs a genuinely empty vault to prove its
# short-circuit fires for the right reason.

def test_ai_triage_maps_indices_back_to_refs(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod, "meetings", {})
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()
    client.post("/api/tasks", json={"text": "task A", "priority": "low"})
    client.post("/api/tasks", json={"text": "task B"})

    async def _fake_call(method, url, *, json_body, **kw):
        assert json_body["format"] == app_mod.ANALYSIS_SCHEMAS["task_triage"]
        return _FakeResp({"response": json.dumps({
            "suggestions": [{"index": 0, "priority": "high", "reason": "blocking release"}],
            "focus": [0],
        })})
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _fake_call)

    r = client.post("/api/tasks/ai/triage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["suggestions"]) == 1
    sug = body["suggestions"][0]
    assert sug["priority"] == "high" and sug["reason"] == "blocking release"
    assert sug["ref"]["source"] == "note" and "line" in sug["ref"] and "index" not in sug["ref"]
    assert len(body["focus"]) == 1
    assert body["focus"][0]["source"] == "note"


def test_ai_triage_no_open_tasks_short_circuits(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod, "meetings", {})
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()

    async def _boom(method, url, *, json_body, **kw):
        raise AssertionError("must not call the LLM with zero open tasks")
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _boom)

    r = client.post("/api/tasks/ai/triage")
    assert r.status_code == 200
    assert r.json() == {"suggestions": [], "focus": []}


def test_ai_triage_caps_at_80_tasks(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod, "meetings", {})
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()
    for i in range(90):
        client.post("/api/tasks", json={"text": f"task {i}"})

    seen = {}
    async def _fake_call(method, url, *, json_body, **kw):
        seen["prompt"] = json_body["prompt"]
        return _FakeResp({"response": json.dumps({"suggestions": [], "focus": []})})
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _fake_call)

    r = client.post("/api/tasks/ai/triage")
    assert r.status_code == 200
    assert "79:" in seen["prompt"]      # index 79 (the 80th item) present
    assert "80:" not in seen["prompt"]  # index 80 would be the 81st item -- capped out


def test_ai_triage_ignores_out_of_range_and_malformed_suggestions(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod, "meetings", {})
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()
    client.post("/api/tasks", json={"text": "only task"})

    async def _fake_call(method, url, *, json_body, **kw):
        return _FakeResp({"response": json.dumps({
            "suggestions": [
                {"index": 99, "priority": "high", "reason": "out of range"},
                {"index": 0, "priority": "not-a-priority", "reason": "bad priority"},
                {"index": "nope", "priority": "high", "reason": "bad index type"},
            ],
            "focus": [99, 0],
        })})
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _fake_call)

    r = client.post("/api/tasks/ai/triage")
    assert r.status_code == 200
    body = r.json()
    assert body["suggestions"] == []          # all three rejected
    assert len(body["focus"]) == 1            # only index 0 survives


def test_ai_triage_non_fatal_on_llm_failure(tmp_path, monkeypatch):
    client, app_mod, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod, "meetings", {})
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()
    client.post("/api/tasks", json={"text": "only task"})

    async def _boom(method, url, *, json_body, **kw):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(app_mod, "_retry_ollama_call", _boom)

    r = client.post("/api/tasks/ai/triage")
    assert r.status_code == 200
    assert r.json() == {"suggestions": [], "focus": []}
