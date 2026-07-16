import asyncio
import notes_store as ns


def test_apply_auto_tags_merges_without_dropping_user_tags(tmp_path):
    rec = ns.create_note(tmp_path, "N", body="x")
    ns.update_note(tmp_path, rec["id"], tags=["mine"])
    out = ns.apply_auto_tags(tmp_path, rec["id"], "planning", ["Roadmap", "OKRs", "mine"])
    assert "mine" in out["tags"]
    assert "roadmap" in out["tags"] and "okrs" in out["tags"]
    assert out["tags"].count("mine") == 1
    assert out["category"] == "planning"


def test_record_exposes_category(tmp_path):
    rec = ns.create_note(tmp_path, "N", body="x")
    ns.apply_auto_tags(tmp_path, rec["id"], "demo", [])
    assert ns.read_note(tmp_path, rec["id"])["category"] == "demo"


def test_pipeline_busy(monkeypatch):
    import app
    monkeypatch.setattr(app, "meetings", {"m": {"status": app.MeetingStatus.summarizing}})
    assert app._pipeline_busy() is True
    monkeypatch.setattr(app, "meetings", {"m": {"status": app.MeetingStatus.complete}})
    assert app._pipeline_busy() is False


def test_run_tag_job_defers_when_busy(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "meetings", {"m": {"status": app.MeetingStatus.transcribing}})
    rec = ns.create_note(tmp_path, "N", body="some content")
    assert asyncio.run(app._run_tag_job(rec["id"])) is False
    assert ns.read_note(tmp_path, rec["id"])["tags"] == []


def test_run_tag_job_tags_when_idle(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "meetings", {})
    async def _fake_tag(title, body, *, attachment_text=""):
        return {"category": "planning", "keywords": ["roadmap"], "entities": {}}
    monkeypatch.setattr(app, "auto_tag_note", _fake_tag)
    rec = ns.create_note(tmp_path, "N", body="content about the roadmap")
    assert asyncio.run(app._run_tag_job(rec["id"])) is True
    out = ns.read_note(tmp_path, rec["id"])
    assert "roadmap" in out["tags"] and out["category"] == "planning"
    assert asyncio.run(app._run_tag_job(rec["id"])) is False  # unchanged -> skipped


def test_retag_endpoint_enqueues(tmp_path, monkeypatch):
    import app
    from fastapi.testclient import TestClient
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "get_qdrant", lambda: None)
    monkeypatch.setattr(app, "get_embedder", lambda: None)
    enqueued = []
    monkeypatch.setattr(app, "_enqueue_tag", lambda nid: enqueued.append(nid))
    client = TestClient(app.app)
    nid = client.post("/api/notes", json={"title": "N", "body": "hi"}).json()["id"]
    r = client.post(f"/api/notes/{nid}/retag")
    assert r.status_code == 200 and r.json()["queued"] is True
    assert nid in enqueued


def test_auto_tag_includes_attachment_text_within_cap(tmp_path, monkeypatch):
    import app
    captured = {}

    class _Resp:
        def json(self):
            return {"response": '{"category": "planning", "keywords": ["merger"], "entities": {}}'}

    async def fake_call(method, url, *, json_body, timeout_seconds=180.0, max_retries=2):
        captured["prompt"] = json_body["prompt"]
        return _Resp()

    monkeypatch.setattr(app, "_retry_ollama_call", fake_call)
    out = asyncio.run(app.auto_tag_note("Title", "the body", attachment_text="MERGER MEMO details"))
    assert out["category"] == "planning"
    assert "the body" in captured["prompt"]
    assert "MERGER MEMO" in captured["prompt"]


def test_auto_tag_caps_at_16000(tmp_path, monkeypatch):
    import app
    captured = {}

    class _Resp:
        def json(self):
            return {"response": '{"category": "other", "keywords": [], "entities": {}}'}

    async def fake_call(method, url, *, json_body, timeout_seconds=180.0, max_retries=2):
        captured["prompt"] = json_body["prompt"]
        return _Resp()

    monkeypatch.setattr(app, "_retry_ollama_call", fake_call)
    big_attach = "A" * 40000
    asyncio.run(app.auto_tag_note("T", "body", attachment_text=big_attach))
    # analysis_pass_g template wraps {transcript}; the transcript slice itself is <=16000
    assert "A" * 40000 not in captured["prompt"]
