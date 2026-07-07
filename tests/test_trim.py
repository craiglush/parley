"""Tests for POST /meetings/{id}/trim — cut a span of a meeting's audio into a NEW meeting.

Reuses the test_meeting_routes harness: bare TestClient with MEETINGS_DIR /
meetings monkeypatched; ffmpeg-touching helpers (probe_duration / trim_audio)
and process_meeting stubbed so no live tools are needed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_meeting_routes import _client, _seed_complete


def _stub_pipeline(app, monkeypatch, duration=100.0):
    calls = {}

    def fake_probe(path):
        calls["probed"] = path
        return duration

    def fake_trim(src, dst, start, end):
        calls["trim"] = (src, dst, start, end)
        Path(dst).write_bytes(b"fake-trimmed-audio")

    def fake_process(meeting_id):
        calls["processed"] = meeting_id

        async def _noop():
            pass

        return _noop()

    monkeypatch.setattr(app, "probe_duration", fake_probe)
    monkeypatch.setattr(app, "trim_audio", fake_trim)
    monkeypatch.setattr(app, "process_meeting", fake_process)
    return calls


def test_trim_creates_new_queued_meeting(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(app, tmp_path, mid="m1", title="Weekly Sync")
    (out / "audio.webm").write_bytes(b"fake-audio")
    calls = _stub_pipeline(app, monkeypatch, duration=100.0)

    r = client.post("/meetings/m1/trim", json={"start": 10, "end": 30})
    assert r.status_code == 202, r.text
    body = r.json()
    new_id = body["meeting_id"]
    assert new_id != "m1"
    assert body["status"] == "queued"
    assert body["title"] == "Weekly Sync (trimmed)"

    nm = app.meetings[new_id]
    assert nm["status"] == app.MeetingStatus.queued
    assert nm["trimmed_from"] == {"meeting_id": "m1", "start": 10.0, "end": 30.0}
    src, dst, start, end = calls["trim"]
    assert src.endswith("audio.webm")
    assert f"_upload_{new_id}" in dst and dst.endswith(".webm")
    assert (start, end) == (10.0, 30.0)
    assert calls["processed"] == new_id
    assert Path(dst).exists()
    # Original meeting untouched.
    assert app.meetings["m1"]["status"] == app.MeetingStatus.complete


def test_trim_404s(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    calls = _stub_pipeline(app, monkeypatch)
    # Unknown meeting.
    assert client.post("/meetings/nope/trim", json={"start": 0, "end": 10}).status_code == 404
    # Known meeting but no stored audio file.
    _seed_complete(app, tmp_path, mid="m1")
    assert client.post("/meetings/m1/trim", json={"start": 0, "end": 10}).status_code == 404
    assert "trim" not in calls


def test_trim_validates_span(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(app, tmp_path, mid="m1")
    (out / "audio.webm").write_bytes(b"fake-audio")
    calls = _stub_pipeline(app, monkeypatch, duration=100.0)

    # End before start / sub-second span.
    assert client.post("/meetings/m1/trim", json={"start": 30, "end": 10}).status_code == 400
    assert client.post("/meetings/m1/trim", json={"start": 5, "end": 5.5}).status_code == 400
    # Start beyond the audio's real duration.
    assert client.post("/meetings/m1/trim", json={"start": 150, "end": 200}).status_code == 400
    assert "trim" not in calls


def test_trim_custom_title_and_end_clamped_to_duration(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(app, tmp_path, mid="m1")
    (out / "audio.webm").write_bytes(b"fake-audio")
    calls = _stub_pipeline(app, monkeypatch, duration=50.0)

    r = client.post("/meetings/m1/trim", json={"start": 40, "end": 500, "title": "Cut bit"})
    assert r.status_code == 202, r.text
    assert r.json()["title"] == "Cut bit"
    # end clamped from 500 to the probed 50.0s duration
    assert calls["trim"][2:] == (40.0, 50.0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_trim_audio_real_ffmpeg(tmp_path):
    import stt

    src = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-ac", "1", "-ar", "16000", str(src)],
        capture_output=True, check=True, timeout=120,
    )
    dst = tmp_path / "cut.wav"
    stt.trim_audio(str(src), str(dst), 1.0, 3.0)
    assert dst.exists() and dst.stat().st_size > 0
    assert abs(stt.probe_duration(str(dst)) - 2.0) < 0.3
