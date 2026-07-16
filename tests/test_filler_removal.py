"""Filler-removal feature tests.

Spec: docs/superpowers/specs/2026-07-15-filler-removal-design.md
House idioms: bare TestClient(app.app) + monkeypatch of MEETINGS_DIR/SETTINGS_PATH
(settings & prompt tests, per tests/test_model_config.py::_settings_client); route
tests reuse tests.test_meeting_routes helpers. No live Ollama; no timing assertions.

(asyncio / re are used by the step_cleanup echo tests added in Task 3.)
"""
import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient


def _settings_client(tmp_path, monkeypatch):
    """Settings-on-tmp_path idiom from tests/test_model_config.py."""
    import app
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")
    return TestClient(app.app), app


# ------------------------------------------------------------- setting plumb (Task 1)

def test_remove_filler_roundtrip(tmp_path, monkeypatch):
    client, app = _settings_client(tmp_path, monkeypatch)
    # default ON with no settings.json on disk
    assert client.get("/api/settings").json()["settings"]["remove_filler"] is True
    # turn OFF; persists across a fresh GET (fresh load_settings from disk)
    r = client.put("/api/settings", json={"remove_filler": False})
    assert r.status_code == 200
    assert r.json()["settings"]["remove_filler"] is False
    assert client.get("/api/settings").json()["settings"]["remove_filler"] is False


def test_remove_filler_missing_key_defaults_on(tmp_path, monkeypatch):
    _, app = _settings_client(tmp_path, monkeypatch)
    # every settings.json written before this feature lacks the key -> ON
    (tmp_path / "settings.json").write_text(json.dumps({"diarize": True}))
    assert app.load_settings()["remove_filler"] is True


def test_remove_filler_non_bool_garbage_ignored(tmp_path, monkeypatch):
    _, app = _settings_client(tmp_path, monkeypatch)
    (tmp_path / "settings.json").write_text(json.dumps({"remove_filler": "nope"}))
    assert app.load_settings()["remove_filler"] is True


# ------------------------------------------------------------- regex pre-pass (Task 2)

@pytest.mark.parametrize("raw,expected", [
    ("Um, I think we should ship.", "I think we should ship."),
    ("I think, um, that works.", "I think, that works."),
    ("So, uh, yeah.", "So, yeah."),
    ("UM, UH, moving on", "Moving on"),               # case-insensitive; comma runs; recap
    ("Ummm... errr let me think.", "Let me think."),  # elongations; leading punctuation strip
    ("hmm, well, mm, fine", "Well, fine"),
])
def test_strip_fillers_vectors(raw, expected):
    import app
    assert app.strip_fillers(raw) == expected


@pytest.mark.parametrize("text", [
    "Take my umbrella.",         # um inside a word
    "This summer we go ahead.",  # mm / ah inside words
    "The ermine hid.",           # er inside a word
    "Uh-huh, agreed.",           # affirmation carries meaning - preserved
    "Mm-hmm.",                   # affirmation - preserved
])
def test_strip_fillers_leaves_real_words_and_affirmations(text):
    import app
    assert app.strip_fillers(text) == text


def test_prepass_drops_pure_filler_and_counts():
    import app
    segs = [
        {"start": 0.0, "end": 0.7, "speaker": "SPEAKER_00", "text": "Um."},
        {"start": 0.7, "end": 2.4, "speaker": "SPEAKER_01", "text": "Let's start."},
        {"start": 2.4, "end": 4.0, "speaker": "SPEAKER_00", "text": "Uh, agenda first."},
    ]
    out, changed = app.apply_filler_prepass(segs)
    assert [s["text"] for s in out] == ["Let's start.", "Agenda first."]
    # neighbours keep their timestamps and speaker labels
    assert out[0]["start"] == 0.7 and out[0]["speaker"] == "SPEAKER_01"
    assert out[1]["end"] == 4.0 and out[1]["speaker"] == "SPEAKER_00"
    assert changed == 2  # one dropped + one modified


def test_prepass_does_not_mutate_input():
    import app
    segs = [
        {"start": 0.0, "end": 0.7, "speaker": "SPEAKER_00", "text": "Um."},
        {"start": 0.7, "end": 2.4, "speaker": "SPEAKER_01", "text": "Um, hello."},
    ]
    before = json.dumps(segs, sort_keys=True)
    app.apply_filler_prepass(segs)
    assert json.dumps(segs, sort_keys=True) == before


def test_prepass_all_filler_guard_returns_input_unchanged():
    import app
    segs = [{"start": 0.0, "end": 1.0, "speaker": "S", "text": "Um."},
            {"start": 1.0, "end": 2.0, "speaker": "S", "text": "Hmm, mm."}]
    out, changed = app.apply_filler_prepass(segs)
    assert out == segs and changed == 0


def test_prepass_tolerates_none_text_segment():
    import app
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "A", "text": None},
        {"start": 1.0, "end": 2.0, "speaker": "B", "text": "um okay let's start"},
    ]
    out, changed = app.apply_filler_prepass(segs)
    # None-text segment is treated as empty (dropped as pure filler), no crash
    assert all(s.get("text") is not None for s in out)
    assert len(out) == 1  # the None segment was dropped
    assert out[0]["text"] == "Okay let's start"  # the second segment cleaned


# ------------------------------------- prompt directive + pre-pass wiring (Task 3)

_SEGS = [
    {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "Um."},
    {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_01", "text": "Um, let's start with the roadmap."},
    {"start": 2.0, "end": 3.0, "speaker": "SPEAKER_00", "text": "Take my umbrella."},
]


def test_cleanup_prompt_off_is_pre_feature_assembly(tmp_path, monkeypatch):
    _, app = _settings_client(tmp_path, monkeypatch)
    (tmp_path / "settings.json").write_text(json.dumps({"remove_filler": True}))
    prompt_on = app._build_cleanup_prompt(_SEGS[1:], [], [], "Roadmap sync")
    (tmp_path / "settings.json").write_text(json.dumps({"remove_filler": False}))
    prompt_off = app._build_cleanup_prompt(_SEGS[1:], [], [], "Roadmap sync")
    assert app.FILLER_DIRECTIVE not in prompt_off
    # regression guard: OFF is byte-for-byte the ON prompt minus the directive,
    # i.e. exactly the pre-feature assembly for identical inputs.
    assert prompt_on.count(app.FILLER_DIRECTIVE) == 1
    assert prompt_on.replace(app.FILLER_DIRECTIVE, "", 1) == prompt_off


def test_cleanup_prompt_on_directive_directly_after_preamble(tmp_path, monkeypatch):
    _, app = _settings_client(tmp_path, monkeypatch)  # no settings.json -> default ON
    prompt = app._build_cleanup_prompt(_SEGS[1:], [], [], "Roadmap sync")
    preamble = app.DEFAULT_PROMPTS["cleanup_system"].replace("{meeting_context}", "Roadmap sync")
    assert prompt.startswith(preamble + app.FILLER_DIRECTIVE + "\n")
    # directive sits after the preamble, before meeting-context and segments blocks
    assert prompt.index(app.FILLER_DIRECTIVE) < prompt.index("Meeting subject/context:")
    assert prompt.index(app.FILLER_DIRECTIVE) < prompt.index("--- Segments to clean")


def test_custom_cleanup_template_verbatim_both_modes(tmp_path, monkeypatch):
    _, app = _settings_client(tmp_path, monkeypatch)
    custom = "MY CUSTOM CLEANUP RULES v7 {meeting_context}"
    for toggle in (True, False):
        (tmp_path / "settings.json").write_text(json.dumps(
            {"prompts": {"cleanup_system": custom}, "remove_filler": toggle}))
        prompt = app._build_cleanup_prompt(_SEGS[1:], [], [], "Roadmap sync")
        assert prompt.startswith("MY CUSTOM CLEANUP RULES v7 Roadmap sync")
        assert (app.FILLER_DIRECTIVE in prompt) is toggle
        # the saved template is never edited at build time
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert saved["prompts"]["cleanup_system"] == custom


class _EchoResp:
    """Minimal fake httpx.Response carrying an Ollama /api/generate payload."""
    def __init__(self, text):
        self._text = text

    def json(self):
        return {"response": self._text}


def _install_echo_llm(monkeypatch, app):
    """LLM stub: echoes the prompt's numbered segment lines back verbatim.
    (_parse_cleanup_response strips the echoed 'SPEAKER_XX: ' prefixes itself.)"""
    async def fake_call(method, url, *, json_body, **kwargs):
        seg_block = json_body["prompt"].split("--- Segments to clean", 1)[1]
        echoed = [ln for ln in seg_block.splitlines() if re.match(r"\[\d+\]", ln)]
        return _EchoResp("\n".join(echoed))
    monkeypatch.setattr(app, "_retry_ollama_call", fake_call)


def test_step_cleanup_prepass_on_strips_and_drops(tmp_path, monkeypatch):
    _, app = _settings_client(tmp_path, monkeypatch)  # default ON
    _install_echo_llm(monkeypatch, app)
    out = asyncio.run(app.step_cleanup_transcript([dict(s) for s in _SEGS]))
    assert [s["text"] for s in out] == ["Let's start with the roadmap.", "Take my umbrella."]
    assert out[0]["start"] == 1.0 and out[0]["speaker"] == "SPEAKER_01"


def test_step_cleanup_off_is_unchanged(tmp_path, monkeypatch):
    _, app = _settings_client(tmp_path, monkeypatch)
    (tmp_path / "settings.json").write_text(json.dumps({"remove_filler": False}))
    _install_echo_llm(monkeypatch, app)
    out = asyncio.run(app.step_cleanup_transcript([dict(s) for s in _SEGS]))
    assert [s["text"] for s in out] == [s["text"] for s in _SEGS]


# ------------------------------------------------------------- reprocess path (Task 4)

from tests.test_meeting_routes import _client, _seed_complete


@pytest.mark.parametrize("before,after,expect", [
    ([{"text": "a"}, {"text": "b"}], [{"text": "a"}, {"text": "b"}], False),
    ([{"text": "a"}, {"text": "b"}], [{"text": "a"}], True),        # drop-only: zip() used to miss this
    ([{"text": "a"}, {"text": "b"}], [{"text": "a"}, {"text": "B"}], True),
])
def test_segment_texts_differ(before, after, expect):
    import app
    assert app._segment_texts_differ(before, after) is expect


def _seed_cleanup_fixture(appmod, tmp_path, with_raw):
    rec, out = _seed_complete(appmod, tmp_path, mid="m1")
    segs = [
        {"start": 0.0, "end": 0.7, "speaker": "SPEAKER_00", "text": "Um."},
        {"start": 0.7, "end": 2.0, "speaker": "SPEAKER_01", "text": "Morning all."},
        {"start": 2.0, "end": 3.5, "speaker": "SPEAKER_00", "text": "Agenda first."},
    ]
    data = {"meeting_id": "m1", "duration": 3.5, "language": "en", "segments": segs}
    if with_raw:
        (out / "raw_transcript.json").write_text(json.dumps(data, indent=2))
    (out / "transcript.json").write_text(json.dumps({**data, "cleaned": False}, indent=2))
    return rec, out


def _pump_until(client, cond, tries=50):
    """Drive the event loop until the background reprocess task lands its writes
    (each TestClient request pumps the loop — tests/test_meeting_context.py idiom)."""
    for _ in range(tries):
        if cond():
            return
        client.get("/meetings/m1/status")


def test_reprocess_cleanup_drop_only_rewrites_and_refreshes_count(tmp_path, monkeypatch):
    client, appmod, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_cleanup_fixture(appmod, tmp_path, with_raw=True)
    raw_before = (out / "raw_transcript.json").read_text()

    async def fake_cleanup(segments, meeting_context=None, batch_size=15, context_window=3):
        # emulate the pre-pass dropping the LAST segment; remaining texts identical
        return [dict(s) for s in segments[:-1]]

    monkeypatch.setattr(appmod, "step_cleanup_transcript", fake_cleanup)

    r = client.post("/meetings/m1/reprocess", json={"step": "cleanup"})
    assert r.status_code == 200, r.text
    _pump_until(client, lambda: appmod.meetings["m1"].get("segment_count") == 2)

    new_data = json.loads((out / "transcript.json").read_text())
    assert len(new_data["segments"]) == 2 and new_data["cleaned"] is True
    assert (out / "transcript.srt").exists() and (out / "transcript.md").exists()
    assert appmod.meetings["m1"]["segment_count"] == 2
    assert appmod.meetings["m1"]["transcript_cleaned"] is True
    # raw_transcript.json is never modified by a re-cleanup
    assert (out / "raw_transcript.json").read_text() == raw_before


def test_reprocess_cleanup_legacy_snapshot_before_rewrite(tmp_path, monkeypatch):
    client, appmod, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_cleanup_fixture(appmod, tmp_path, with_raw=False)
    original = (out / "transcript.json").read_text()

    async def fake_cleanup(segments, meeting_context=None, batch_size=15, context_window=3):
        edited = [dict(s) for s in segments]
        edited[1]["text"] = "Morning all, welcome."
        return edited

    monkeypatch.setattr(appmod, "step_cleanup_transcript", fake_cleanup)

    r = client.post("/meetings/m1/reprocess", json={"step": "cleanup"})
    assert r.status_code == 200, r.text
    _pump_until(client, lambda: (out / "raw_transcript.json").exists()
                and json.loads((out / "transcript.json").read_text()).get("cleaned") is True)

    # the only copy was snapshotted to raw_transcript.json BEFORE the rewrite,
    # byte-identical to the pre-run transcript.json
    assert (out / "raw_transcript.json").read_text() == original
    new_data = json.loads((out / "transcript.json").read_text())
    assert new_data["segments"][1]["text"] == "Morning all, welcome."
