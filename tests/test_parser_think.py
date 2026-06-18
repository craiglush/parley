"""Belt-and-braces: the cleanup + speaker-id parsers must survive a stray
<think> block (qwen3.x thinking channel) even though we now set think:false."""

import app


def test_cleanup_parser_strips_think_block():
    raw = (
        "<think>The user wants me to fix two lines.\nLet me reason...</think>\n"
        "[0] We need to implement SQL fixes.\n"
        "[1] The WAF rollout is on track."
    )
    parsed = app._parse_cleanup_response(raw, expected_count=2)
    assert parsed == ["We need to implement SQL fixes.", "The WAF rollout is on track."]


def test_cleanup_parser_without_think_still_works():
    raw = "[0] First line\n[1] Second line"
    assert app._parse_cleanup_response(raw, expected_count=2) == ["First line", "Second line"]


def test_speaker_parser_strips_think_and_matches_real_labels():
    raw = (
        "<think>SPEAKER_00 introduces himself as Alex.</think>\n"
        '[{"label": "SPEAKER_00", "name": "Alex", "role": "Product Manager", '
        '"evidence": "Self-introduced as Alex"}]'
    )
    result = app._parse_speaker_identification(raw, ["SPEAKER_00"])
    assert result["speaker_map"].get("SPEAKER_00") == "Alex"
