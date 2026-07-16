import asyncio
import json
import app


def test_note_analysis_prompt_and_schema_present():
    tmpl = app.DEFAULT_PROMPTS["note_analysis"]
    assert "{corpus}" in tmpl
    assert "{transcript}" not in tmpl  # note corpus, not a meeting transcript
    sch = app.ANALYSIS_SCHEMAS["note_analysis"]
    assert sch["type"] == "object"
    for key in ("summary", "key_points", "action_items", "insights"):
        assert key in sch["properties"]
    assert sch["properties"]["key_points"]["type"] == "array"
    assert sch["required"] == ["summary"]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_analyze_note_text_returns_structured_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")

    async def _fake_call(method, url, *, json_body, **kw):
        assert "note_analysis" not in json_body["prompt"]  # template was expanded
        assert "corpus goes here" in json_body["prompt"]
        assert json_body["format"] == app.ANALYSIS_SCHEMAS["note_analysis"]
        return _FakeResp({"response": json.dumps({
            "summary": "S", "key_points": ["a", "b"],
            "action_items": ["do x"], "insights": ["i1"],
        })})

    monkeypatch.setattr(app, "_retry_ollama_call", _fake_call)
    out = asyncio.run(app.analyze_note_text("corpus goes here"))
    assert out == {
        "summary": "S", "key_points": ["a", "b"],
        "action_items": ["do x"], "insights": ["i1"],
    }


def test_analyze_note_text_defaults_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")

    async def _boom(method, url, *, json_body, **kw):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(app, "_retry_ollama_call", _boom)
    out = asyncio.run(app.analyze_note_text("x"))
    assert out == {"summary": "", "key_points": [], "action_items": [], "insights": []}


def test_build_corpus_includes_body_and_attachment_text():
    note = {"title": "My Title", "body": "the note body"}
    exs = [{"text": "attachment one"}, {"text": "   "}, {"text": "attachment two"}]
    corpus = app._build_analysis_corpus(note, exs)
    assert "My Title" in corpus
    assert "the note body" in corpus
    assert "attachment one" in corpus and "attachment two" in corpus
    assert corpus.count("attachment") == 2  # the blank-text extraction is skipped


def test_build_corpus_caps_at_analysis_corpus_max(monkeypatch):
    monkeypatch.setattr(app, "ANALYSIS_CORPUS_MAX", 20)
    note = {"title": "", "body": "x" * 500}
    assert len(app._build_analysis_corpus(note, [])) == 20


def test_resolve_note_extractions_runs_vision_and_writes_sidecar(tmp_path, monkeypatch):
    import notes_store
    import extract
    import llm
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path)
    notes_store._index_cache.clear()

    fname = notes_store.save_attachment(tmp_path, "diagram.png", b"\x89PNG bytes")
    monkeypatch.setattr(notes_store, "note_attachments", lambda nd, nid: [fname])
    side = tmp_path / "sidecar.json"
    monkeypatch.setattr(extract, "extracted_sidecar_path", lambda nd, f: side)
    monkeypatch.setattr(extract, "extract_text",
                        lambda p, f: {"text": "", "method": "vision", "chars": 0, "status": "pending"})

    called = {}

    async def _fake_describe(path, *, prompt):
        called["path"] = path
        called["prompt"] = prompt
        return "TEXT FROM IMAGE"

    monkeypatch.setattr(llm, "describe_image", _fake_describe)

    out = asyncio.run(app._resolve_note_extractions("any-note-id"))
    assert len(out) == 1
    assert out[0]["text"] == "TEXT FROM IMAGE"
    assert out[0]["status"] == "done"
    assert called["prompt"] == app.VISION_EXTRACT_PROMPT
    saved = json.loads(side.read_text())
    assert saved["text"] == "TEXT FROM IMAGE" and saved["status"] == "done"
    assert "extracted_at" in saved


def test_resolve_note_extractions_reuses_terminal_sidecar(tmp_path, monkeypatch):
    import notes_store
    import extract
    import llm
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path)
    notes_store._index_cache.clear()

    fname = notes_store.save_attachment(tmp_path, "doc.txt", b"hi")
    monkeypatch.setattr(notes_store, "note_attachments", lambda nd, nid: [fname])
    side = tmp_path / "sidecar2.json"
    side.write_text(json.dumps({"text": "cached", "method": "text", "chars": 6, "status": "done"}))
    monkeypatch.setattr(extract, "extracted_sidecar_path", lambda nd, f: side)

    def _boom_extract(p, f):
        raise AssertionError("must not re-extract a terminal sidecar")

    async def _boom_describe(path, *, prompt):
        raise AssertionError("must not run vision on a terminal sidecar")

    monkeypatch.setattr(extract, "extract_text", _boom_extract)
    monkeypatch.setattr(llm, "describe_image", _boom_describe)

    out = asyncio.run(app._resolve_note_extractions("any"))
    assert out == [{"text": "cached", "method": "text", "chars": 6, "status": "done"}]


def test_run_analysis_job_writes_sidecar_and_returns_result(tmp_path, monkeypatch):
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path)
    notes_store._index_cache.clear()
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")
    rec = notes_store.create_note(tmp_path, "N", body="hello world")

    async def _fake_resolve(nid):
        return [{"text": "attachment text", "method": "text", "chars": 15, "status": "done"}]

    seen = {}

    async def _fake_analyze(corpus):
        seen["corpus"] = corpus
        return {"summary": "S", "key_points": ["k"], "action_items": [], "insights": []}

    monkeypatch.setattr(app, "_resolve_note_extractions", _fake_resolve)
    monkeypatch.setattr(app, "analyze_note_text", _fake_analyze)

    out = asyncio.run(app._run_analysis_job(rec["id"]))
    assert out["summary"] == "S"
    assert "hello world" in seen["corpus"] and "attachment text" in seen["corpus"]
    p = notes_store.attachments_dir(tmp_path) / ".analysis" / f'{rec["id"]}.json'
    assert p.exists()
    saved = json.loads(p.read_text())
    assert saved["summary"] == "S" and saved["key_points"] == ["k"]
    assert "analyzed_at" in saved


def test_enqueue_analysis_is_coalesced(monkeypatch):
    monkeypatch.setattr(app, "_analysis_pending", set())
    monkeypatch.setattr(app, "_analysis_status", {})
    q = app.asyncio.Queue()
    monkeypatch.setattr(app, "_analysis_queue", q)
    app._enqueue_analysis("n1")
    app._enqueue_analysis("n1")  # duplicate -> coalesced
    assert q.qsize() == 1
    assert app._analysis_status["n1"] == "queued"


def _api_client(tmp_path, monkeypatch):
    import notes_store
    from fastapi.testclient import TestClient
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path)
    notes_store._index_cache.clear()
    monkeypatch.setattr(app, "get_qdrant", lambda: None)
    monkeypatch.setattr(app, "get_embedder", lambda: None)
    return TestClient(app.app), notes_store


def test_analyze_endpoint_enqueues(tmp_path, monkeypatch):
    client, _ = _api_client(tmp_path, monkeypatch)
    enqueued = []
    monkeypatch.setattr(app, "_enqueue_analysis", lambda nid: enqueued.append(nid))
    nid = client.post("/api/notes", json={"title": "N", "body": "hi"}).json()["id"]
    r = client.post(f"/api/notes/{nid}/analyze")
    assert r.status_code == 200 and r.json()["queued"] is True
    assert nid in enqueued


def test_analyze_endpoint_404_for_missing_note(tmp_path, monkeypatch):
    client, _ = _api_client(tmp_path, monkeypatch)
    assert client.post("/api/notes/n_missing/analyze").status_code == 404


def test_get_analysis_returns_stored_result(tmp_path, monkeypatch):
    client, notes_store = _api_client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_analysis_status", {})
    nid = client.post("/api/notes", json={"title": "N", "body": "hi"}).json()["id"]
    d = notes_store.attachments_dir(tmp_path) / ".analysis"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nid}.json").write_text(json.dumps(
        {"summary": "S", "key_points": [], "action_items": [], "insights": []}))
    r = client.get(f"/api/notes/{nid}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"]["summary"] == "S"


def test_get_analysis_404_for_missing_note(tmp_path, monkeypatch):
    client, _ = _api_client(tmp_path, monkeypatch)
    assert client.get("/api/notes/n_missing/analysis").status_code == 404


def test_analysis_done_without_sidecar_is_error(tmp_path, monkeypatch):
    client, _ = _api_client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_analysis_status", {})
    nid = client.post("/api/notes", json={"title": "N", "body": "hi"}).json()["id"]
    # Simulate in-memory status as done but no sidecar file on disk
    app._analysis_status[nid] = "done"
    r = client.get(f"/api/notes/{nid}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error" and body["result"] is None
