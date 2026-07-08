"""The merge helper: human tags win, LLM fills the gaps."""

import app


def test_human_tags_override_llm():
    tag = {
        "speaker_map": {"SPEAKER_00": "Alex"},
        "speaker_info": {"SPEAKER_00": {"name": "Alex", "title": "", "company": "",
                                        "display_name": "Alex", "confidence": "manual",
                                        "auto_detected": False}},
    }
    llm = {
        "speaker_map": {"SPEAKER_00": "WrongGuess", "SPEAKER_01": "Sarah"},
        "speaker_info": {
            "SPEAKER_00": {"name": "WrongGuess", "title": "", "company": "",
                           "display_name": "WrongGuess", "confidence": "low", "auto_detected": True},
            "SPEAKER_01": {"name": "Sarah", "title": "", "company": "",
                           "display_name": "Sarah", "confidence": "medium", "auto_detected": True},
        },
    }
    merged = app.merge_speaker_identifications(tag, llm)
    # SPEAKER_00 keeps the human name; SPEAKER_01 keeps the LLM name.
    assert merged["speaker_map"] == {"SPEAKER_00": "Alex", "SPEAKER_01": "Sarah"}
    assert merged["speaker_info"]["SPEAKER_00"]["confidence"] == "manual"
    assert merged["speaker_info"]["SPEAKER_01"]["auto_detected"] is True


def test_merge_with_empty_llm():
    tag = {"speaker_map": {"SPEAKER_00": "Alex"}, "speaker_info": {"SPEAKER_00": {"name": "Alex"}}}
    empty = {"speaker_map": {}, "speaker_info": {}}
    assert app.merge_speaker_identifications(tag, empty)["speaker_map"] == {"SPEAKER_00": "Alex"}
