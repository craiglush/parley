"""Phase C: meeting-analysis context threading + gathering.

The analysis passes gain an optional `context` string (from notes the user
linked to a meeting) that is PREPENDED to the transcript before the existing
`{transcript}` template substitution. No template placeholder is added, so a
customised saved prompt still works and an empty context reproduces today's
prompt byte-for-byte.

Async internals are exercised with asyncio.run (mirrors tests/test_autotag.py);
`_retry_ollama_call` is monkeypatched so no live Ollama is needed and every
prompt sent is captured for assertion.
"""

import asyncio
import json

import app
import notes_store as ns


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


_PASS_KEYS = ["analysis_pass_a", "analysis_pass_b", "analysis_pass_c",
              "analysis_pass_d", "analysis_pass_e", "analysis_pass_f"]

TRANSCRIPT = "[00:00:00] Alex: hello world\n[00:00:05] Dana: goodbye"


def _capture_prompts(monkeypatch, tmp_path):
    """Force default prompts (absent settings file) and record every prompt sent
    to Ollama. Returns the list it accumulates into."""
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")  # absent -> defaults
    prompts = []

    async def _fake_retry(method, url, *, json_body, **kwargs):
        prompts.append(json_body["prompt"])
        return _Resp({"response": "{}"})

    monkeypatch.setattr(app, "_retry_ollama_call", _fake_retry)
    return prompts


def test_empty_context_reproduces_todays_prompt_byte_for_byte(tmp_path, monkeypatch):
    prompts = _capture_prompts(monkeypatch, tmp_path)
    asyncio.run(app._run_analysis_passes(TRANSCRIPT, "test-model", 0.3))
    expected = [app.DEFAULT_PROMPTS[k].replace("{transcript}", TRANSCRIPT) for k in _PASS_KEYS]
    assert prompts == expected

    # Explicit empty-string context must be identical to omitting it.
    prompts.clear()
    asyncio.run(app._run_analysis_passes(TRANSCRIPT, "test-model", 0.3, context=""))
    assert prompts == expected


def test_context_is_prepended_before_transcript(tmp_path, monkeypatch):
    prompts = _capture_prompts(monkeypatch, tmp_path)
    asyncio.run(app._run_analysis_passes(TRANSCRIPT, "test-model", 0.3, context="LINKED-NOTE-BODY"))
    assert len(prompts) == 6

    # Pin the exact prepend format (character-for-character, including em-dash)
    # The context is prepended to transcript_text before substitution into {transcript} placeholder
    expected_transcript_with_context = (
        "[Context from notes linked to this meeting — background only; "
        "base all analysis on the transcript that follows]\n\n"
        "LINKED-NOTE-BODY\n\n[Transcript]\n\n"
        "[00:00:00] Alex: hello world\n[00:00:05] Dana: goodbye"
    )

    for p in prompts:
        assert "LINKED-NOTE-BODY" in p
        # Context sits ahead of the transcript body within the prompt.
        assert p.index("LINKED-NOTE-BODY") < p.index("hello world")
        # Pin exact prepend format so accidental wording/spacing changes are caught
        assert expected_transcript_with_context in p


def test_step_summarize_threads_context_short_path(tmp_path, monkeypatch):
    prompts = _capture_prompts(monkeypatch, tmp_path)
    # duration <= 5400 -> the direct 6-pass path.
    asyncio.run(app.step_summarize(TRANSCRIPT, 100.0, context="ZZZCTX"))
    assert len(prompts) == 6
    assert all("ZZZCTX" in p for p in prompts)


def test_step_summarize_threads_context_hierarchical_path(tmp_path, monkeypatch):
    prompts = _capture_prompts(monkeypatch, tmp_path)
    # duration > 5400 -> hierarchical path: chunk summaries first, then the 6
    # analysis passes. Context is prepended inside _run_analysis_passes, so it
    # must appear in the analysis-pass prompts (identifiable by "meeting analyst").
    long_transcript = "\n".join(f"[{h:02d}:00:00] Alex: point {h}" for h in range(4))
    asyncio.run(app.step_summarize(long_transcript, 6000.0, context="ZZZCTX"))
    analysis_prompts = [p for p in prompts if "meeting analyst" in p]
    assert analysis_prompts, "the analysis passes should have run"
    assert all("ZZZCTX" in p for p in analysis_prompts)


def _link_note(tmp_path, meeting_id, title, body):
    rec = ns.create_note(tmp_path, title, body=body)
    ns.link_meeting(tmp_path, rec["id"], meeting_id, add=True)
    return rec


def _write_extracted(tmp_path, filename, text, status="done"):
    d = ns.attachments_dir(tmp_path) / ".extracted"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{filename}.json").write_text(json.dumps(
        {"text": text, "method": "vision", "chars": len(text),
         "extracted_at": "2026-07-15T00:00:00Z", "status": status}))


def test_gather_context_includes_linked_note_body_and_attachment_text(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path)
    ns._index_cache.clear()
    rec = _link_note(tmp_path, "m1", "Spec", "the linked note body")
    _write_extracted(tmp_path, "diagram-abc.png", "EXTRACTED-DIAGRAM-TEXT")
    # Phase A's body parser is consumed, not re-implemented here: stub it.
    monkeypatch.setattr(ns, "note_attachments",
                        lambda nd, nid: ["diagram-abc.png"] if nid == rec["id"] else [],
                        raising=False)
    out = app._gather_meeting_context("m1")
    assert "the linked note body" in out
    assert "EXTRACTED-DIAGRAM-TEXT" in out


def test_gather_context_ignores_unlinked_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path)
    ns._index_cache.clear()
    monkeypatch.setattr(ns, "note_attachments", lambda nd, nid: [], raising=False)
    _link_note(tmp_path, "m1", "Linked", "linked body text")
    ns.create_note(tmp_path, "Other", body="unlinked body text")  # not linked to m1
    out = app._gather_meeting_context("m1")
    assert "linked body text" in out
    assert "unlinked body text" not in out


def test_gather_context_empty_when_no_links(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path)
    ns._index_cache.clear()
    monkeypatch.setattr(ns, "note_attachments", lambda nd, nid: [], raising=False)
    ns.create_note(tmp_path, "Unrelated", body="nothing linked here")
    assert app._gather_meeting_context("m1") == ""


def test_gather_context_skips_non_done_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path)
    ns._index_cache.clear()
    _link_note(tmp_path, "m1", "Spec", "note body")
    _write_extracted(tmp_path, "scan-xyz.pdf", "", status="pending")
    monkeypatch.setattr(ns, "note_attachments",
                        lambda nd, nid: ["scan-xyz.pdf"], raising=False)
    out = app._gather_meeting_context("m1")
    assert "note body" in out          # the note is still included
    assert "scan-xyz.pdf" not in out   # a pending attachment contributes nothing


def test_gather_context_respects_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path)
    ns._index_cache.clear()
    monkeypatch.setattr(ns, "note_attachments", lambda nd, nid: [], raising=False)
    monkeypatch.setattr(app, "MEETING_CONTEXT_MAX", 50)
    _link_note(tmp_path, "m1", "Big", "x" * 500)
    out = app._gather_meeting_context("m1")
    assert len(out) == 50


def test_read_extracted_text_blocks_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path)
    # plant a decoy OUTSIDE .extracted that would be readable via traversal
    outside = tmp_path / "secrets.json"
    outside.write_text('{"status": "done", "text": "LEAKED"}', encoding="utf-8")
    assert app._read_extracted_text("../../secrets") == ""


from tests.test_meeting_routes import _client, _seed_complete


def test_reprocess_summarize_passes_linked_context(tmp_path, monkeypatch):
    client, appmod, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(appmod, tmp_path, mid="m1")
    (out / "transcript.json").write_text(json.dumps(
        {"segments": [{"start": 0.0, "end": 1.0, "speaker": "Alex", "text": "hi"}]}))

    monkeypatch.setattr(appmod, "_gather_meeting_context", lambda mid: f"CTX::{mid}")
    captured = {}

    async def _fake_summarize(transcript_text, duration, progress_callback=None, context=""):
        captured["context"] = context
        return {"title": "T", "summary": "s", "topics": [], "action_items": [],
                "decisions": [], "open_questions": [], "concerns": [],
                "figures": [], "sentiment": {}}

    monkeypatch.setattr(appmod, "step_summarize", _fake_summarize)
    monkeypatch.setattr(appmod, "build_summary_markdown", lambda summary, m: "md")

    r = client.post("/meetings/m1/reprocess", json={"step": "summarize"})
    assert r.status_code == 200, r.text
    # Let the scheduled background reprocess task run; each GET pumps the loop.
    for _ in range(50):
        if "context" in captured:
            break
        client.get("/meetings/m1/status")
    assert captured.get("context") == "CTX::m1"
