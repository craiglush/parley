"""Speech-to-text: WhisperX HTTP client + audio preprocessing/chunking.

Extracted from app.py (Phase 4 §8.4). Self-contained — depends only on stdlib +
httpx and its own config (read from env), NOT on any of app.py's monkeypatched
globals, so app.py can re-import these names with no import cycle. Covered
indirectly via the meeting-route tests (process_meeting is mocked there).
"""

import asyncio
import logging
import os
import random
import re
import subprocess
from typing import Optional

import httpx

logger = logging.getLogger("meeting-service")

WHISPERX_URL = os.getenv("WHISPERX_URL", "http://whisperx:8000")
MAX_AUDIO_CHUNK_SECONDS = 1800  # 30 min chunks for very long audio
OVERLAP_SECONDS = 30


async def _retry_whisperx_call(
    audio_path: str,
    data: dict,
    *,
    timeout_seconds: float = 600.0,
    max_retries: int = 3,
    base_delay: float = 5.0,
) -> dict:
    """Call WhisperX with exponential backoff retry on transient failures."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
                with open(audio_path, "rb") as f:
                    resp = await client.post(
                        f"{WHISPERX_URL}/transcribe",
                        files={"file": ("audio.wav", f, "audio/wav")},
                        data=data,
                    )
                resp.raise_for_status()
                return resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    f"WhisperX call failed (attempt {attempt + 1}/{max_retries}): {exc}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"WhisperX call failed after {max_retries} attempts: {exc}")
    raise last_exc


def probe_duration(path: str) -> float:
    """Return an audio file's duration in seconds (0.0 if undeterminable)."""
    probe = subprocess.run(
        ["ffmpeg", "-i", path, "-f", "null", "-"],
        capture_output=True, text=True, timeout=900,
    )
    duration = 0.0
    for line in probe.stderr.splitlines():
        if "Duration:" in line:
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", line)
            if match:
                h, m, s, ms = match.groups()
                duration = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 100
    return duration


def preprocess_audio(input_path: str, output_path: str) -> float:
    """Convert any audio/video to 16 kHz mono WAV. Returns duration in seconds."""
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=900)
    return probe_duration(output_path)


def trim_audio(input_path: str, output_path: str, start: float, end: float) -> None:
    """Cut [start, end] out of an audio file without re-encoding (stream copy)."""
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ss", str(start), "-to", str(end),
        "-c", "copy", output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=900)


def split_audio(wav_path: str, duration: float) -> list[dict]:
    """Split long audio into overlapping chunks. Returns list of {path, offset}."""
    if duration <= MAX_AUDIO_CHUNK_SECONDS + 60:
        return [{"path": wav_path, "offset": 0.0}]

    chunks = []
    start = 0.0
    idx = 0
    while start < duration:
        end = min(start + MAX_AUDIO_CHUNK_SECONDS, duration)
        chunk_path = wav_path.replace(".wav", f"_chunk{idx}.wav")
        cmd = [
            "ffmpeg", "-y", "-i", wav_path,
            "-ss", str(start), "-to", str(end),
            "-c", "copy", chunk_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True, timeout=900)
        chunks.append({"path": chunk_path, "offset": start})
        start += MAX_AUDIO_CHUNK_SECONDS - OVERLAP_SECONDS
        idx += 1
    return chunks


def merge_chunk_segments(all_chunk_results: list[dict]) -> list[dict]:
    """Merge segments from multiple chunks, adjusting timestamps and deduplicating overlaps."""
    if len(all_chunk_results) == 1:
        return all_chunk_results[0].get("segments", [])

    merged = []
    for chunk in all_chunk_results:
        offset = chunk["offset"]
        for seg in chunk.get("segments", []):
            adjusted = {
                **seg,
                "start": seg["start"] + offset,
                "end": seg["end"] + offset,
            }
            # Skip if this segment overlaps with the last merged segment
            if merged and adjusted["start"] < merged[-1]["end"] - 1.0:
                continue
            merged.append(adjusted)
    return merged


async def step_transcribe(audio_path: str, min_speakers: Optional[int], max_speakers: Optional[int]) -> dict:
    """Send audio to WhisperX for transcription + diarization."""
    # English-only: force language so WhisperX skips auto-detection (faster, avoids misdetection).
    data = {"diarize": "true", "language": "en"}
    if min_speakers is not None:
        data["min_speakers"] = str(min_speakers)
    if max_speakers is not None:
        data["max_speakers"] = str(max_speakers)
    return await _retry_whisperx_call(audio_path, data)
