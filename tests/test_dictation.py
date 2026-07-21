import asyncio
import app
import io
from fastapi.testclient import TestClient


def _client():
    return TestClient(app.app)


def test_join_segments_skips_empty_text():
    result = {"segments": [{"text": "hello"}, {"text": ""}, {"text": "world"}, {}]}
    assert app._join_segments(result) == "hello\nworld"


def test_transcribe_plain_preprocesses_and_joins(monkeypatch):
    import stt
    monkeypatch.setattr(stt, "preprocess_audio", lambda inp, out: 3.5)

    async def fake_transcribe(path, mn, mx, *, backend=None, diarize=True):
        assert diarize is False
        return {"segments": [{"text": "hello"}, {"text": "world"}]}
    monkeypatch.setattr(stt, "step_transcribe", fake_transcribe)

    out = asyncio.run(app._transcribe_plain("/tmp/whatever.m4a"))
    assert out == "hello\nworld"


def test_extract_stt_unchanged_behavior(monkeypatch):
    import stt
    monkeypatch.setattr(stt, "preprocess_audio", lambda inp, out: 1.0)

    async def fake_transcribe(path, mn, mx, *, backend=None, diarize=True):
        return {"segments": [{"text": "attachment text"}]}
    monkeypatch.setattr(stt, "step_transcribe", fake_transcribe)

    out = asyncio.run(app._extract_stt("/tmp/attachment.mp3"))
    assert out == "attachment text"


def test_dictate_endpoint_returns_transcript(monkeypatch):
    import stt
    monkeypatch.setattr(stt, "preprocess_audio", lambda inp, out: 4.0)

    async def fake_transcribe(path, mn, mx, *, backend=None, diarize=True):
        return {"segments": [{"text": "call Dave tomorrow"}]}
    monkeypatch.setattr(stt, "step_transcribe", fake_transcribe)

    r = _client().post("/api/dictate",
                        files={"audio": ("dictation.webm", io.BytesIO(b"fake bytes"), "audio/webm")})
    assert r.status_code == 200
    assert r.json() == {"text": "call Dave tomorrow"}


def test_dictate_endpoint_rejects_long_clip(monkeypatch):
    import stt
    monkeypatch.setattr(stt, "preprocess_audio", lambda inp, out: 121.0)

    async def fake_transcribe(path, mn, mx, *, backend=None, diarize=True):
        raise AssertionError("must not transcribe an over-length clip")
    monkeypatch.setattr(stt, "step_transcribe", fake_transcribe)

    r = _client().post("/api/dictate",
                        files={"audio": ("dictation.webm", io.BytesIO(b"fake bytes"), "audio/webm")})
    assert r.status_code == 400


def test_dictate_endpoint_400_on_preprocess_failure(monkeypatch):
    import stt

    def _boom(inp, out):
        raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(stt, "preprocess_audio", _boom)

    r = _client().post("/api/dictate",
                        files={"audio": ("dictation.webm", io.BytesIO(b"garbage"), "audio/webm")})
    assert r.status_code == 400


def test_dictate_endpoint_rejects_oversized_upload(monkeypatch):
    import stt
    calls = []

    def _track_preprocess(inp, out):
        calls.append("preprocess")
        return 4.0
    monkeypatch.setattr(stt, "preprocess_audio", _track_preprocess)

    async def _track_transcribe(path, mn, mx, *, backend=None, diarize=True):
        calls.append("transcribe")
        return {"segments": [{"text": "should never get here"}]}
    monkeypatch.setattr(stt, "step_transcribe", _track_transcribe)

    oversized = b"x" * (app.DICTATE_MAX_BYTES + 1)
    r = _client().post("/api/dictate",
                        files={"audio": ("dictation.webm", io.BytesIO(oversized), "audio/webm")})
    assert r.status_code == 413
    # The byte-size cap must reject before any ffmpeg preprocessing or STT runs --
    # not just return the right status code by coincidence.
    assert calls == []


def test_note_cleanup_prompt_and_schema_present():
    tmpl = app.DEFAULT_PROMPTS["note_cleanup"]
    assert "{text}" in tmpl
    sch = app.ANALYSIS_SCHEMAS["note_cleanup"]
    assert sch["type"] == "object"
    assert "text" in sch["properties"]
    assert sch["required"] == ["text"]


class _FakeCleanupResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_cleanup_note_text_returns_polished_text(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")

    async def _fake_call(method, url, *, json_body, **kw):
        assert "note_cleanup" not in json_body["prompt"]  # template was expanded
        assert "um so i think we should" in json_body["prompt"]
        assert json_body["format"] == app.ANALYSIS_SCHEMAS["note_cleanup"]
        return _FakeCleanupResp({"response": '{"text": "I think we should proceed."}'})

    monkeypatch.setattr(app, "_retry_ollama_call", _fake_call)
    out = asyncio.run(app.cleanup_note_text("um so i think we should proceed"))
    assert out == "I think we should proceed."


def test_cleanup_note_text_falls_back_to_original_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")

    async def _boom(method, url, *, json_body, **kw):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(app, "_retry_ollama_call", _boom)
    out = asyncio.run(app.cleanup_note_text("raw dictated text"))
    assert out == "raw dictated text"


def test_cleanup_note_text_falls_back_on_empty_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")

    async def _fake_call(method, url, *, json_body, **kw):
        return _FakeCleanupResp({"response": "not json"})

    monkeypatch.setattr(app, "_retry_ollama_call", _fake_call)
    out = asyncio.run(app.cleanup_note_text("raw dictated text"))
    assert out == "raw dictated text"


def test_enqueue_cleanup_coalesces_and_overwrites_text(monkeypatch):
    monkeypatch.setattr(app, "_cleanup_pending", set())
    monkeypatch.setattr(app, "_cleanup_status", {})
    monkeypatch.setattr(app, "_cleanup_text", {})
    q = app.asyncio.Queue()
    monkeypatch.setattr(app, "_cleanup_queue", q)
    app._enqueue_cleanup("n1", "first span")
    app._enqueue_cleanup("n1", "first span extended")  # still pending -> overwrite, no 2nd enqueue
    assert q.qsize() == 1
    assert app._cleanup_text["n1"] == "first span extended"
    assert app._cleanup_status["n1"] == "queued"


def _dictation_client(tmp_path, monkeypatch):
    import notes_store
    from fastapi.testclient import TestClient
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path)
    notes_store._index_cache.clear()
    monkeypatch.setattr(app, "get_qdrant", lambda: None)
    monkeypatch.setattr(app, "get_embedder", lambda: None)
    return TestClient(app.app), notes_store


def test_cleanup_span_post_enqueues(tmp_path, monkeypatch):
    client, _ = _dictation_client(tmp_path, monkeypatch)
    enqueued = []
    monkeypatch.setattr(app, "_enqueue_cleanup", lambda nid, text: enqueued.append((nid, text)))
    nid = client.post("/api/notes", json={"title": "N", "body": "hi"}).json()["id"]
    r = client.post(f"/api/notes/{nid}/cleanup-span", json={"text": "raw dictated text"})
    assert r.status_code == 200 and r.json()["queued"] is True
    assert enqueued == [(nid, "raw dictated text")]


def test_cleanup_span_post_404_for_missing_note(tmp_path, monkeypatch):
    client, _ = _dictation_client(tmp_path, monkeypatch)
    r = client.post("/api/notes/n_missing/cleanup-span", json={"text": "x"})
    assert r.status_code == 404


def test_cleanup_span_get_returns_result(tmp_path, monkeypatch):
    client, _ = _dictation_client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_cleanup_status", {})
    monkeypatch.setattr(app, "_cleanup_result", {})
    nid = client.post("/api/notes", json={"title": "N", "body": "hi"}).json()["id"]
    app._cleanup_status[nid] = "done"
    app._cleanup_result[nid] = "polished text"
    r = client.get(f"/api/notes/{nid}/cleanup-span")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"]["text"] == "polished text"


def test_cleanup_span_get_idle_when_never_run(tmp_path, monkeypatch):
    client, _ = _dictation_client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_cleanup_status", {})
    monkeypatch.setattr(app, "_cleanup_result", {})
    nid = client.post("/api/notes", json={"title": "N", "body": "hi"}).json()["id"]
    r = client.get(f"/api/notes/{nid}/cleanup-span")
    assert r.status_code == 200
    assert r.json() == {"status": "idle", "result": None}


def test_run_cleanup_job_calls_cleanup_note_text_and_stores_result(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_cleanup_text", {"n1": "raw text"})
    monkeypatch.setattr(app, "_cleanup_result", {})

    async def _fake_cleanup(text):
        assert text == "raw text"
        return "clean text"
    monkeypatch.setattr(app, "cleanup_note_text", _fake_cleanup)

    out = asyncio.run(app._run_cleanup_job("n1"))
    assert out == "clean text"
    assert app._cleanup_result["n1"] == "clean text"
