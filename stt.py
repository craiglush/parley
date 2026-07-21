"""Speech-to-text: Parakeet+pyannote HTTP client + audio preprocessing/chunking.

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

PARAKEET_URL = os.getenv("PARAKEET_URL", "http://parakeet-asr:5092")
DIARIZER_URL = os.getenv("DIARIZER_URL", "http://pyannote-diarizer:8000")
PARAKEET_MODEL = os.getenv("PARAKEET_MODEL", "istupakov/parakeet-tdt-0.6b-v3-onnx")
MAX_AUDIO_CHUNK_SECONDS = 1800  # 30 min chunks for very long audio
OVERLAP_SECONDS = 30


async def _retry_post_audio(url, audio_path, data, *, file_field="file",
                            timeout_seconds=600.0, max_retries=3, base_delay=5.0) -> dict:
    """POST an audio file (multipart) with exponential backoff on transient failures."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
                with open(audio_path, "rb") as f:
                    resp = await client.post(
                        url, files={file_field: ("audio.wav", f, "audio/wav")}, data=data)
                resp.raise_for_status()
                return resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"POST {url} failed (attempt {attempt + 1}/{max_retries}): {exc}. "
                               f"Retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"POST {url} failed after {max_retries} attempts: {exc}")
    raise last_exc


async def _parakeet_transcribe(audio_path: str) -> list[dict]:
    data = {"response_format": "verbose_json", "timestamp_granularities": "word",
            "model": PARAKEET_MODEL}
    payload = await _retry_post_audio(f"{PARAKEET_URL}/v1/audio/transcriptions", audio_path, data)
    return _parse_parakeet_words(payload)


async def _diarize_audio(audio_path: str, min_speakers, max_speakers) -> list[dict]:
    data = {}
    if min_speakers is not None:
        data["min_speakers"] = str(min_speakers)
    if max_speakers is not None:
        data["max_speakers"] = str(max_speakers)
    payload = await _retry_post_audio(f"{DIARIZER_URL}/diarize", audio_path, data)
    return _parse_diarizer_turns(payload)


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


def _parse_parakeet_words(payload: dict) -> list[dict]:
    """Word-level timestamps from Parakeet verbose_json; fall back to segment-level."""
    out = []
    for w in (payload.get("words") or []):
        tok = (w.get("word") or "").strip()
        if not tok:
            continue
        start = float(w.get("start", 0.0))
        out.append({"start": start, "end": float(w.get("end", start)), "word": tok})
    if out:
        return out
    for s in payload.get("segments", []):
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        start = float(s.get("start", 0.0))
        out.append({"start": start, "end": float(s.get("end", start)), "word": txt})
    return out


def _parse_diarizer_turns(payload: dict) -> list[dict]:
    return [{"start": float(s["start"]), "end": float(s["end"]),
             "speaker": s.get("speaker", "UNKNOWN")}
            for s in payload.get("segments", [])]


def _finalize_segment(cur: dict) -> dict:
    text = " ".join(cur["_tokens"])
    text = re.sub(r"\s+([,.!?;:])", r"\1", text).strip()
    return {"start": round(cur["start"], 3), "end": round(cur["end"], 3),
            "text": text, "speaker": cur["speaker"]}


def _assign_speakers(words: list[dict], turns: list[dict], gap_seconds: float = 1.0) -> list[dict]:
    """Assign each word the max-overlap diarization speaker, then group consecutive
    same-speaker words (breaking on gaps > gap_seconds) into segments."""
    if not words:
        return []

    def speaker_for(w):
        ws, we = w["start"], w["end"]
        best, best_ov = None, 0.0
        for t in turns:
            ov = min(we, t["end"]) - max(ws, t["start"])
            if ov > best_ov:
                best_ov, best = ov, t["speaker"]
        if best is not None:
            return best
        if turns:
            mid = (ws + we) / 2.0
            nearest = min(turns, key=lambda t: min(abs(mid - t["start"]), abs(mid - t["end"])))
            return nearest["speaker"]
        return "UNKNOWN"

    segments, cur = [], None
    for w in words:
        token = (w.get("word") or "").strip()
        if not token:
            continue
        spk = speaker_for(w)
        if cur is not None and spk == cur["speaker"] and w["start"] - cur["end"] <= gap_seconds:
            cur["end"] = w["end"]
            cur["_tokens"].append(token)
        else:
            if cur is not None:
                segments.append(_finalize_segment(cur))
            cur = {"start": w["start"], "end": w["end"], "speaker": spk, "_tokens": [token]}
    if cur is not None:
        segments.append(_finalize_segment(cur))
    return segments


async def step_transcribe(audio_path: str, min_speakers: Optional[int],
                          max_speakers: Optional[int], *,
                          backend: Optional[str] = None, diarize: bool = True) -> dict:
    """Transcribe (+diarize) one audio chunk. Returns the frozen contract:
    {"language","duration","segments":[{start,end,text,speaker}]}."""
    words = await _parakeet_transcribe(audio_path)
    turns = []
    if diarize:
        try:
            turns = await _diarize_audio(audio_path, min_speakers, max_speakers)
        except Exception as e:
            logger.warning(f"Diarization failed, returning transcript without speakers: {e}")
    segments = _assign_speakers(words, turns)
    duration = segments[-1]["end"] if segments else 0.0
    return {"language": "en", "duration": duration, "segments": segments}
