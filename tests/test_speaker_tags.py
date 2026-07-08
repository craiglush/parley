"""Unit tests for reconcile_speaker_tags — map human tap markers to pyannote clusters."""

import app


SEGMENTS = [
    {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"},
    {"start": 10.0, "end": 20.0, "speaker": "SPEAKER_01"},
    {"start": 20.0, "end": 30.0, "speaker": "SPEAKER_00"},
]


def test_single_tag_names_whole_cluster():
    # Tap Alex at 5s (inside SPEAKER_00). Both SPEAKER_00 segments become Alex.
    out = app.reconcile_speaker_tags([{"t": 5.0, "name": "Alex"}], SEGMENTS)
    assert out["speaker_map"] == {"SPEAKER_00": "Alex"}
    info = out["speaker_info"]["SPEAKER_00"]
    assert info["name"] == "Alex"
    assert info["confidence"] == "manual"
    assert info["auto_detected"] is False
    assert info["display_name"] == "Alex"


def test_two_people_two_clusters():
    out = app.reconcile_speaker_tags(
        [{"t": 5.0, "name": "Alex"}, {"t": 12.0, "name": "Sarah"}], SEGMENTS
    )
    assert out["speaker_map"] == {"SPEAKER_00": "Alex", "SPEAKER_01": "Sarah"}


def test_reaction_lag_tolerance():
    # Person starts at 10.0 (SPEAKER_01); user taps at 11.5s. Window catches SPEAKER_01.
    out = app.reconcile_speaker_tags([{"t": 11.5, "name": "Sarah"}], SEGMENTS)
    assert out["speaker_map"] == {"SPEAKER_01": "Sarah"}


def test_conflict_majority_wins():
    # Two taps land in SPEAKER_00; majority name wins.
    out = app.reconcile_speaker_tags(
        [{"t": 3.0, "name": "Alex"}, {"t": 7.0, "name": "Alex"}, {"t": 25.0, "name": "Dan"}],
        SEGMENTS,
    )
    # SPEAKER_00 has two Alex votes (t=3,7) and one Dan vote (t=25) -> Alex.
    assert out["speaker_map"]["SPEAKER_00"] == "Alex"


def test_genuine_tie_left_unresolved():
    # Alex and Dan both tapped fully inside one cluster with equal overlap -> tie.
    # Spec: never guess silently -> the cluster must NOT be assigned (left for LLM/manual).
    out = app.reconcile_speaker_tags(
        [{"t": 3.0, "name": "Alex"}, {"t": 7.0, "name": "Dan"}],
        [{"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"}],
    )
    assert "SPEAKER_00" not in out["speaker_map"]
    assert "SPEAKER_00" not in out["speaker_info"]


def test_roster_enriches_display_name():
    out = app.reconcile_speaker_tags(
        [{"t": 5.0, "name": "Alex"}],
        SEGMENTS,
        roster=[{"name": "Alex", "company": "Acme", "title": "CTO"}],
    )
    info = out["speaker_info"]["SPEAKER_00"]
    assert info["company"] == "Acme"
    assert info["title"] == "CTO"
    assert info["display_name"] == "Alex (CTO, Acme)"


def test_marker_in_silence_dropped():
    # Tag far past the last segment (no cluster within tolerance) -> nothing assigned.
    out = app.reconcile_speaker_tags([{"t": 999.0, "name": "Ghost"}], SEGMENTS)
    assert out["speaker_map"] == {}
    assert out["speaker_info"] == {}


def test_no_markers_returns_empty():
    assert app.reconcile_speaker_tags([], SEGMENTS) == {"speaker_map": {}, "speaker_info": {}}


def test_ignores_unknown_and_blank_names():
    out = app.reconcile_speaker_tags(
        [{"t": 5.0, "name": ""}, {"t": 6.0, "name": "  "}], SEGMENTS
    )
    assert out["speaker_map"] == {}
