import asyncio

import stt


def test_parse_parakeet_words_prefers_word_timestamps():
    payload = {"words": [{"start": 0.0, "end": 0.4, "word": "hello"},
                         {"start": 0.5, "end": 0.9, "word": "world"}],
               "segments": [{"start": 0.0, "end": 0.9, "text": "hello world"}]}
    out = stt._parse_parakeet_words(payload)
    assert out == [{"start": 0.0, "end": 0.4, "word": "hello"},
                   {"start": 0.5, "end": 0.9, "word": "world"}]


def test_parse_parakeet_words_falls_back_to_segments():
    payload = {"words": None, "segments": [{"start": 1.0, "end": 2.0, "text": "fallback line"}]}
    out = stt._parse_parakeet_words(payload)
    assert out == [{"start": 1.0, "end": 2.0, "word": "fallback line"}]


def test_parse_diarizer_turns():
    payload = {"segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]}
    assert stt._parse_diarizer_turns(payload) == [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]


def test_assign_speakers_two_speakers_split():
    words = [{"start": 0.0, "end": 0.4, "word": "hi"},
             {"start": 0.5, "end": 0.9, "word": "there"},
             {"start": 2.0, "end": 2.4, "word": "yes"}]
    turns = [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
             {"start": 1.5, "end": 3.0, "speaker": "SPEAKER_01"}]
    out = stt._assign_speakers(words, turns)
    assert [s["speaker"] for s in out] == ["SPEAKER_00", "SPEAKER_01"]
    assert out[0]["text"] == "hi there" and out[1]["text"] == "yes"
    assert out[0]["start"] == 0.0 and out[0]["end"] == 0.9


def test_assign_speakers_empty_words():
    assert stt._assign_speakers([], [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}]) == []


def test_assign_speakers_no_turns_marks_unknown():
    words = [{"start": 0.0, "end": 0.4, "word": "alone"}]
    out = stt._assign_speakers(words, [])
    assert out[0]["speaker"] == "UNKNOWN" and out[0]["text"] == "alone"


def test_assign_speakers_no_overlap_uses_nearest_turn():
    words = [{"start": 5.0, "end": 5.4, "word": "late"}]
    turns = [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
             {"start": 4.0, "end": 4.9, "speaker": "SPEAKER_01"}]
    assert stt._assign_speakers(words, turns)[0]["speaker"] == "SPEAKER_01"


# ---------------------------------------------------------------------------
# B1 coverage additions
# ---------------------------------------------------------------------------

def test_assign_speakers_gap_breaks_same_speaker():
    words = [{"start": 0.0, "end": 0.4, "word": "first"},
             {"start": 2.0, "end": 2.4, "word": "second"}]   # gap 1.6s > 1.0
    turns = [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00"}]
    out = stt._assign_speakers(words, turns)
    assert len(out) == 2
    assert out[0]["speaker"] == out[1]["speaker"] == "SPEAKER_00"


def test_parse_diarizer_turns_defaults_unknown():
    out = stt._parse_diarizer_turns({"segments": [{"start": 0.0, "end": 1.0}]})
    assert out == [{"start": 0.0, "end": 1.0, "speaker": "UNKNOWN"}]


def test_parse_parakeet_words_empty_list_falls_back():
    payload = {"words": [], "segments": [{"start": 1.0, "end": 2.0, "text": "x"}]}
    assert stt._parse_parakeet_words(payload) == [{"start": 1.0, "end": 2.0, "word": "x"}]


# ---------------------------------------------------------------------------
# B2: HTTP client tests
# ---------------------------------------------------------------------------

def test_parakeet_transcribe_parses_words(monkeypatch):
    async def fake_post(url, path, data, **kw):
        assert url.endswith("/v1/audio/transcriptions")
        assert data["response_format"] == "verbose_json"
        assert data["timestamp_granularities"] == "word"
        assert data["model"] == stt.PARAKEET_MODEL
        return {"words": [{"start": 0.0, "end": 0.3, "word": "ok"}], "segments": []}
    monkeypatch.setattr(stt, "_retry_post_audio", fake_post)
    out = asyncio.run(stt._parakeet_transcribe("x.wav"))
    assert out == [{"start": 0.0, "end": 0.3, "word": "ok"}]


def test_diarize_audio_passes_speaker_bounds(monkeypatch):
    seen = {}
    async def fake_post(url, path, data, **kw):
        seen.update(data); assert url.endswith("/diarize")
        return {"segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]}
    monkeypatch.setattr(stt, "_retry_post_audio", fake_post)
    out = asyncio.run(stt._diarize_audio("x.wav", 2, 4))
    assert out == [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]
    assert seen["min_speakers"] == "2" and seen["max_speakers"] == "4"


# ---------------------------------------------------------------------------
# B3: step_transcribe backend branch
# ---------------------------------------------------------------------------

def test_step_transcribe_parakeet_contract(monkeypatch):
    async def fake_tx(path):
        return [{"start": 0.0, "end": 0.4, "word": "hello"},
                {"start": 0.5, "end": 0.9, "word": "world"}]
    async def fake_di(path, mn, mx):
        return [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]
    monkeypatch.setattr(stt, "_parakeet_transcribe", fake_tx)
    monkeypatch.setattr(stt, "_diarize_audio", fake_di)
    out = asyncio.run(stt.step_transcribe("x.wav", None, None, backend="parakeet"))
    assert set(out) >= {"language", "duration", "segments"}
    seg = out["segments"][0]
    assert set(seg) == {"start", "end", "text", "speaker"}
    assert seg["text"] == "hello world" and seg["speaker"] == "SPEAKER_00"
    assert out["language"] == "en"


def test_step_transcribe_parakeet_no_diarize(monkeypatch):
    async def fake_tx(path):
        return [{"start": 0.0, "end": 0.4, "word": "solo"}]
    async def boom(*a, **k):
        raise AssertionError("diarizer must not be called when diarize=False")
    monkeypatch.setattr(stt, "_parakeet_transcribe", fake_tx)
    monkeypatch.setattr(stt, "_diarize_audio", boom)
    out = asyncio.run(stt.step_transcribe("x.wav", None, None, backend="parakeet", diarize=False))
    assert out["segments"][0]["speaker"] == "UNKNOWN"


def test_step_transcribe_degrades_when_diarizer_fails(monkeypatch):
    async def fake_tx(path):
        return [{"start": 0.0, "end": 0.4, "word": "hello"}]
    async def fake_di_fail(path, mn, mx):
        raise Exception("diarizer down")
    monkeypatch.setattr(stt, "_parakeet_transcribe", fake_tx)
    monkeypatch.setattr(stt, "_diarize_audio", fake_di_fail)
    out = asyncio.run(stt.step_transcribe("x.wav", None, None, backend="parakeet", diarize=True))
    assert out["segments"][0]["speaker"] == "UNKNOWN"
    assert out["segments"][0]["text"] == "hello"
