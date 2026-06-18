"""
Meeting Service - Orchestrator for audio transcription, summarization, and search.

Pipeline: Upload audio -> WhisperX transcription -> LLM transcript cleanup -> Ollama summarization -> Qdrant storage -> file output
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    Range,
    VectorParams,
)
import numpy as np
import llm
import stt
import storage
import vector
import notes_store
import notes_vectors
import tasks_store
# Phase-4 modularization: pure LLM helpers live in llm.py. Re-bound here so
# existing app.py references (and tests that do `app._strip_think`) still work.
from llm import (
    _CTX_TIERS,
    _CTX_MAX,
    _ctx_for_text,
    _build_generate_body,
    _strip_think,
    _parse_json_object,
    _parse_json_array,
)
# WhisperX + audio helpers live in stt.py; re-bound so process_meeting's bare
# calls (preprocess_audio, split_audio, step_transcribe, merge_chunk_segments) resolve.
from stt import (
    _retry_whisperx_call,
    preprocess_audio,
    split_audio,
    merge_chunk_segments,
    step_transcribe,
)
# Pure file/format helpers live in storage.py (no monkeypatched-global deps).
from storage import (
    _validate_artifact_id,
    _atomic_write,
    _format_timestamp,
    _generate_srt,
    _srt_ts,
)
# Qdrant + embeddings live in vector.py. Re-bound so app.py route handlers' bare
# get_qdrant()/get_embedder() calls resolve here and stay monkeypatchable in tests.
from vector import (
    get_embedder,
    get_qdrant,
    _check_embedding_dim,
    store_in_qdrant,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("meeting-service")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WHISPERX_URL = os.getenv("WHISPERX_URL", "http://whisperx:8000")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:14b")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
OPENWEBUI_URL = os.getenv("OPENWEBUI_URL", "http://open-webui:8080")
OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY", "")
MEETINGS_DIR = Path(os.getenv("MEETINGS_DIR", "/data/meetings"))
COLLECTION_NAME = "meetings"
NOTES_COLLECTION = notes_vectors.NOTES_COLLECTION  # separate "notes" Qdrant collection
EMBEDDING_DIM = 1024  # qwen3-embedding:0.6b (also 1024-dim, served by Ollama)
SETTINGS_PATH = MEETINGS_DIR / "settings.json"
# qwen3-embedding:0.6b compresses short cross-type similarity into a narrow
# ~0.30-0.75 band, and the note<->meeting queries concatenate title+body (diluting
# the topical signal). 0.40 clipped genuine matches (observed empty "related"
# results); 0.30 sits just above the clearly-unrelated floor (~0.32).
RELATED_MIN_SCORE = 0.30

# ---------------------------------------------------------------------------
# Default LLM prompt templates (editable via /api/settings)
# ---------------------------------------------------------------------------

DEFAULT_PROMPTS = {
    "cleanup_system": (
        "You are a transcript editor. Clean up the following speech-to-text segments.\n"
        "\n"
        "Rules:\n"
        "- Fix misheard words, acronyms, technical terms, grammar, and proper nouns.\n"
        "- Do NOT change the meaning, merge segments, or split segments.\n"
        "- Return EXACTLY the same number of lines as segments to clean.\n"
        "- Use the format: [0] corrected text"
    ),
    "speaker_id": (
        "You are analyzing a meeting transcript to identify who each speaker is.\n"
        "\n"
        "The transcript uses generic labels: {speaker_list}\n"
        "\n"
        "Look for clues in the conversation such as:\n"
        "- Self-introductions: \"Hi, I'm Alex\" or \"This is Sarah from Acme Corp\"\n"
        "- Others addressing a speaker by name: \"Alex, what do you think?\" (the next speaker or the one being addressed is Alex)\n"
        "- Role/title mentions: \"As the project manager...\" or \"Speaking as CTO...\"\n"
        "- Company mentions: \"We at Acme Corp...\" or \"On behalf of TechStart...\"\n"
        "- Email signatures or references: \"I'll send it from alex@example.com\"\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with valid JSON (no markdown fences). For each speaker where you can identify at least a name, include an entry. Skip speakers you cannot identify.\n"
        "\n"
        "{json_example}"
    ),
    "analysis_pass_a": (
        "You are a meeting analyst. Read this meeting transcript and extract the title, a concise summary, and the main topics discussed.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with valid JSON, no other text:\n"
        "{\n"
        '  "title": "Short descriptive meeting title",\n'
        '  "summary": "2-3 sentence overview of the meeting",\n'
        '  "topics": [\n'
        '    {"topic": "Topic name", "summary": "Brief summary of discussion", "outcome": "What was concluded or next step"}\n'
        "  ]\n"
        "}"
    ),
    "analysis_pass_b": (
        "You are a meeting analyst. Read this meeting transcript and extract every action item, task, or commitment made.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with a valid JSON array, no other text:\n"
        "[\n"
        '  {"task": "What needs to be done", "who": "Person responsible or UNKNOWN", "deadline": "Deadline if mentioned or null", "priority": "high/medium/low"}\n'
        "]"
    ),
    "analysis_pass_c": (
        "You are a meeting analyst. Read this meeting transcript and extract all decisions that were made and any open questions that remain unanswered.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with valid JSON, no other text:\n"
        "{\n"
        '  "decisions": [\n'
        '    {"decision": "What was decided", "context": "Brief context for why"}\n'
        "  ],\n"
        '  "open_questions": [\n'
        '    {"question": "Question that was asked", "asked_by": "Who asked or UNKNOWN", "answered": false}\n'
        "  ]\n"
        "}"
    ),
    "analysis_pass_d": (
        "You are a meeting analyst. Read this meeting transcript and identify any concerns, risks, objections, or hesitations raised by participants.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with a valid JSON array, no other text:\n"
        "[\n"
        '  {"concern": "Description of the concern or risk", "raised_by": "Who raised it or UNKNOWN", "resolved": false, "notes": "Any resolution or follow-up discussed"}\n'
        "]"
    ),
    "analysis_pass_e": (
        "You are a meeting analyst. Read this meeting transcript and extract any specific numbers, dates, costs, metrics, deadlines, or quantitative data mentioned.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with a valid JSON array, no other text:\n"
        "[\n"
        '  {"figure": "The number, date, or metric", "context": "What it refers to", "said_by": "Who mentioned it or UNKNOWN"}\n'
        "]"
    ),
    "analysis_pass_f": (
        "Read this meeting transcript and describe the overall sentiment and emotional tone.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with valid JSON, no other text:\n"
        "{\n"
        '  "overall": "positive/neutral/negative/mixed",\n'
        '  "notable_moments": [\n'
        '    {"moment": "Description of a notable moment", "tone": "positive/negative/tense/humorous/etc"}\n'
        "  ]\n"
        "}"
    ),
    "analysis_pass_g": (
        "You are a meeting analyst. Read this meeting transcript and summary, then extract tags for categorization.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with valid JSON, no other text:\n"
        "{\n"
        '  "category": "one of: standup, planning, sprint_review, retrospective, sales, brainstorm, interview, training, one_on_one, all_hands, workshop, demo, other",\n'
        '  "keywords": ["3-5 relevant keyword tags"],\n'
        '  "entities": {\n'
        '    "people": ["names of people mentioned or participating"],\n'
        '    "companies": ["company or organization names"],\n'
        '    "projects": ["project or product names"],\n'
        '    "technologies": ["technologies, tools, or platforms mentioned"],\n'
        '    "dates": ["specific dates or deadlines mentioned"]\n'
        "  }\n"
        "}"
    ),
    "chunk_summary": (
        "Summarize this portion (segment {chunk_index}/{chunk_total}) of a meeting transcript in 3-5 bullet points:\n"
        "\n"
        "{chunk}"
    ),
}

# JSON Schemas for Ollama structured outputs (`format`). These MUST mirror the
# JSON shapes the prompt templates ask for (and that summary.json / the frontend
# consume) — Ollama ENFORCES `format`, so a wrong shape silently flattens output.
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}

ANALYSIS_SCHEMAS = {
    "analysis_pass_a": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "topics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "summary": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                    "required": ["topic", "summary", "outcome"],
                },
            },
        },
        "required": ["title", "summary", "topics"],
    },
    "analysis_pass_b": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "who": {"type": "string"},
                "deadline": {"type": "string"},
                "priority": {"type": "string"},
            },
            "required": ["task", "who", "priority"],
        },
    },
    "analysis_pass_c": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string"},
                        "context": {"type": "string"},
                    },
                    "required": ["decision"],
                },
            },
            "open_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "asked_by": {"type": "string"},
                        "answered": {"type": "boolean"},
                    },
                    "required": ["question"],
                },
            },
        },
    },
    "analysis_pass_d": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "concern": {"type": "string"},
                "raised_by": {"type": "string"},
                "resolved": {"type": "boolean"},
                "notes": {"type": "string"},
            },
            "required": ["concern"],
        },
    },
    "analysis_pass_e": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "figure": {"type": "string"},
                "context": {"type": "string"},
                "said_by": {"type": "string"},
            },
            "required": ["figure"],
        },
    },
    "analysis_pass_f": {
        "type": "object",
        "properties": {
            "overall": {"type": "string"},
            "notable_moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "moment": {"type": "string"},
                        "tone": {"type": "string"},
                    },
                    "required": ["moment"],
                },
            },
        },
        "required": ["overall"],
    },
    "analysis_pass_g": {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "keywords": _STR_ARRAY,
            "entities": {
                "type": "object",
                "properties": {
                    "people": _STR_ARRAY,
                    "companies": _STR_ARRAY,
                    "projects": _STR_ARRAY,
                    "technologies": _STR_ARRAY,
                    "dates": _STR_ARRAY,
                },
            },
        },
        "required": ["category"],
    },
}

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "You are a helpful meeting assistant. Answer questions about meetings using the provided context. "
    "Be concise and specific. Reference specific speakers, decisions, and action items when relevant. "
    "If the context doesn't contain enough information to answer, say so."
)

DEFAULT_SETTINGS = {
    "prompts": dict(DEFAULT_PROMPTS),
    "ollama_model": OLLAMA_MODEL,
    "temperature": 0.3,
    "chat": {
        "endpoint": "ollama",  # "ollama", "openwebui", or "custom"
        "custom_url": "",
        "custom_api_key": "",
        "model": "",  # empty = use main ollama_model
        "system_prompt": DEFAULT_CHAT_SYSTEM_PROMPT,
        "temperature": 0.5,
        "max_context_chunks": 15,
    },
}


def load_settings() -> dict:
    """Load settings from disk, filling in any missing keys from defaults."""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text())
            # Merge prompts (keep saved values, fill missing with defaults)
            if "prompts" in saved and isinstance(saved["prompts"], dict):
                for key in DEFAULT_PROMPTS:
                    if key in saved["prompts"]:
                        settings["prompts"][key] = saved["prompts"][key]
            if "ollama_model" in saved:
                settings["ollama_model"] = saved["ollama_model"]
            if "temperature" in saved:
                settings["temperature"] = saved["temperature"]
            # Merge chat settings
            if "chat" in saved and isinstance(saved["chat"], dict):
                for key in DEFAULT_SETTINGS["chat"]:
                    if key in saved["chat"]:
                        settings["chat"][key] = saved["chat"][key]
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")
    return settings


def save_settings(settings: dict):
    """Persist settings to disk."""
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(SETTINGS_PATH, json.dumps(settings, indent=2))


# Chunking / splitting
MAX_AUDIO_CHUNK_SECONDS = 1800  # 30 min chunks for very long audio
OVERLAP_SECONDS = 30

# Upload validation
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(500 * 1024 * 1024)))  # 500MB
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".mkv", ".ogg", ".webm", ".flac", ".aac"}

# Attachment handling
ATTACH_MAX_BYTES = 50 * 1024 * 1024
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_ATTACH_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
                 ".pdf": "application/pdf"}

# ---------------------------------------------------------------------------
# App & global state
# ---------------------------------------------------------------------------

app = FastAPI(title="Meeting Capture & Analysis Service", version="2.0.0")

ALLOWED_FRAME_ORIGINS = os.getenv("ALLOWED_FRAME_ORIGINS", "*")
ALLOWED_CORS_ORIGINS = os.getenv("ALLOWED_CORS_ORIGINS", "*")


def _cors_origins(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


class IFrameMiddleware(BaseHTTPMiddleware):
    """Allow embedding in iframes (e.g. Nextcloud External Sites)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if ALLOWED_FRAME_ORIGINS == "*":
            # Remove any restrictive X-Frame-Options and allow all origins
            if "X-Frame-Options" in response.headers:
                del response.headers["X-Frame-Options"]
            response.headers["Content-Security-Policy"] = "frame-ancestors *"
        else:
            response.headers["X-Frame-Options"] = f"ALLOW-FROM {ALLOWED_FRAME_ORIGINS}"
            response.headers["Content-Security-Policy"] = (
                f"frame-ancestors 'self' {ALLOWED_FRAME_ORIGINS}"
            )
        return response


app.add_middleware(IFrameMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(ALLOWED_CORS_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _verify_embedding_dim_on_startup():
    # Run the probe in the BACKGROUND: the embed call is a blocking, 300s-timeout
    # HTTP request to Ollama, so doing it inline would stall application startup
    # (and the healthcheck) whenever the embedding model is cold or the GPU is busy.
    async def _probe():
        try:
            loop = asyncio.get_event_loop()
            vec = await loop.run_in_executor(None, get_embedder().encode, "dimension probe")
            _check_embedding_dim(int(len(vec)), EMBEDDING_DIM)
        except Exception as e:
            logger.warning(f"Embedding dim check skipped (non-fatal): {e}")
    _probe_t = asyncio.create_task(_probe()); _bg_tasks.add(_probe_t); _probe_t.add_done_callback(_bg_tasks.discard)
    _tag_t = asyncio.create_task(_tag_worker()); _bg_tasks.add(_tag_t); _tag_t.add_done_callback(_bg_tasks.discard)


# In-memory meeting registry (survives container restarts via on-disk JSON index)
meetings: dict[str, dict] = {}
meetings_lock = asyncio.Lock()

# Background tag worker
TAG_IDLE_POLL = 30.0
_tag_queue: "asyncio.Queue[str]" = asyncio.Queue()
_tag_pending: set = set()
_bg_tasks: set = set()


class MeetingStatus(str, Enum):
    queued = "queued"
    preprocessing = "preprocessing"
    transcribing = "transcribing"
    cleaning_transcript = "cleaning_transcript"
    identifying_speakers = "identifying_speakers"
    summarizing = "summarizing"
    tagging = "tagging"
    storing = "storing"
    complete = "complete"
    error = "error"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------




def _meeting_dir(meeting: dict) -> Path:
    """Return the on-disk directory for a meeting."""
    date_str = meeting["date"]
    slug = re.sub(r"[^a-z0-9]+", "-", meeting.get("title", "meeting").lower()).strip("-")
    dirname = f"{date_str}_{slug}"
    path = MEETINGS_DIR / dirname
    path.mkdir(parents=True, exist_ok=True)
    return path




def _save_index():
    """Persist minimal meeting metadata to disk so it survives restarts.
    Keeps a one-generation backup so a corrupt write can't lose everything."""
    index_path = MEETINGS_DIR / "index.json"
    if index_path.exists():
        try:
            shutil.copy2(index_path, MEETINGS_DIR / "index.json.bak")
        except Exception as e:
            logger.warning(f"Failed to back up index.json (non-fatal): {e}")
    serializable = {}
    for mid, m in meetings.items():
        serializable[mid] = {k: v for k, v in m.items() if k not in ("_task",)}
    _atomic_write(index_path, json.dumps(serializable, indent=2, default=str))


def _load_index():
    """Load meeting index from disk on startup."""
    index_path = MEETINGS_DIR / "index.json"
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text())
            for mid, m in data.items():
                meetings[mid] = m
                # Load tags from disk if not in index
                if not m.get("tags") and m.get("output_dir"):
                    tags_path = Path(m["output_dir"]) / "tags.json"
                    if tags_path.exists():
                        try:
                            m["tags"] = json.loads(tags_path.read_text())
                        except Exception:
                            pass
                # Migrate: ensure links field exists
                if "links" not in m:
                    m["links"] = {"manual": [], "suggestions": []}
        except Exception as e:
            logger.warning(f"Failed to load meeting index: {e}")


def _update_progress(meeting: dict, percent: int, detail: str):
    """Update progress tracking fields on a meeting and persist."""
    meeting["progress_percent"] = percent
    meeting["progress_detail"] = detail
    _save_index()




# ---------------------------------------------------------------------------
# Retry wrappers for external calls
# ---------------------------------------------------------------------------


async def _retry_ollama_call(
    method: str,
    url: str,
    *,
    json_body: dict,
    timeout_seconds: float = 300.0,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> httpx.Response:
    """Call Ollama with exponential backoff retry on transient failures."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
                if method.upper() == "POST":
                    resp = await client.post(url, json=json_body)
                else:
                    resp = await client.get(url)
                resp.raise_for_status()
                return resp
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    f"Ollama call to {url} failed (attempt {attempt + 1}/{max_retries}): {exc}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"Ollama call to {url} failed after {max_retries} attempts: {exc}")
    raise last_exc


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


async def step_unload_ollama_models():
    """Ask Ollama to unload all models to free VRAM before transcription."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            # List running models
            resp = await client.get(f"{OLLAMA_URL}/api/ps")
            if resp.status_code == 200:
                running = resp.json().get("models", [])
                for m in running:
                    model_name = m.get("name", "")
                    if model_name:
                        await client.post(
                            f"{OLLAMA_URL}/api/generate",
                            json={"model": model_name, "keep_alive": 0},
                        )
    except Exception as e:
        logger.warning(f"Failed to unload Ollama models (non-critical): {e}")


async def step_summarize(transcript_text: str, duration: float, progress_callback=None) -> dict:
    """Use Ollama to generate a structured meeting summary via 6 focused passes."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)

    # For long meetings (>90 min), do hierarchical summarization
    if duration > 5400:
        return await _hierarchical_summarize(transcript_text, duration, model, temperature, progress_callback)

    return await _run_analysis_passes(transcript_text, model, temperature, progress_callback)


async def _run_analysis_passes(transcript_text: str, model: str, temperature: float, progress_callback=None) -> dict:
    """Run 6 sequential analysis passes against the transcript, each with a focused prompt."""
    settings = load_settings()
    prompts = settings.get("prompts", {})

    # Pass configs: (prompt_key, parser_type)
    pass_configs = [
        ("analysis_pass_a", "object"),
        ("analysis_pass_b", "array"),
        ("analysis_pass_c", "object"),
        ("analysis_pass_d", "array"),
        ("analysis_pass_e", "array"),
        ("analysis_pass_f", "object"),
    ]

    pass_results = {}
    for i, (key, parser_type) in enumerate(pass_configs):
        pass_label = key.split("_")[-1].upper()  # A, B, C, D, E, F
        logger.info(f"Running analysis pass {pass_label} ({i+1}/6)")

        if progress_callback:
            progress_callback(i, 6, f"Analyzing meeting (pass {i+1}/6: {pass_label})...")

        template = prompts.get(key, DEFAULT_PROMPTS.get(key, ""))
        prompt = template.replace("{transcript}", transcript_text)

        schema = ANALYSIS_SCHEMAS.get(key)
        # Array passes (B action_items / D concerns / E figures) can enumerate
        # many objects on a busy meeting; 2048 truncated the JSON mid-object ->
        # parse failure -> silent []. Object passes (A/C/F) fit comfortably in 2048.
        pass_num_predict = 3072 if parser_type == "array" else 2048
        t0 = time.monotonic()
        try:
            resp = await _retry_ollama_call(
                "POST",
                f"{OLLAMA_URL}/api/generate",
                json_body=_build_generate_body(
                    model, prompt, temperature=temperature, num_predict=pass_num_predict, schema=schema,
                ),
                timeout_seconds=300.0,
            )
            raw = resp.json().get("response", "")
            if parser_type == "array":
                pass_results[key] = _parse_json_array(raw, context=f"pass {pass_label}")
            else:
                pass_results[key] = _parse_json_object(raw, context=f"pass {pass_label}")
        except Exception as e:
            logger.warning(f"Analysis pass {pass_label} failed: {e}")
            pass_results[key] = [] if parser_type == "array" else {}

        elapsed = time.monotonic() - t0
        logger.info(f"Analysis pass {pass_label} completed in {elapsed:.1f}s")

    # Merge all pass results into a single summary dict
    pass_a = pass_results.get("analysis_pass_a", {})
    pass_b = pass_results.get("analysis_pass_b", [])
    pass_c = pass_results.get("analysis_pass_c", {})
    pass_d = pass_results.get("analysis_pass_d", [])
    pass_e = pass_results.get("analysis_pass_e", [])
    pass_f = pass_results.get("analysis_pass_f", {})

    return {
        "title": pass_a.get("title", "Meeting"),
        "summary": pass_a.get("summary", ""),
        "topics": pass_a.get("topics", []),
        "action_items": pass_b,
        "decisions": pass_c.get("decisions", []),
        "open_questions": pass_c.get("open_questions", []),
        "concerns": pass_d,
        "figures": pass_e,
        "sentiment": pass_f,
    }


VALID_CATEGORIES = {
    "standup", "planning", "sprint_review", "retrospective", "sales",
    "brainstorm", "interview", "training", "one_on_one", "all_hands",
    "workshop", "demo", "other",
}


async def step_auto_tag(transcript_text: str, summary: dict = None) -> dict:
    """Use Ollama to extract category, keywords, and entities from a transcript. Non-fatal on failure."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)

    template = settings["prompts"].get("analysis_pass_g", DEFAULT_PROMPTS.get("analysis_pass_g", ""))
    prompt = template.replace("{transcript}", transcript_text)

    default_tags = {
        "category": "other",
        "keywords": [],
        "entities": {"people": [], "companies": [], "projects": [], "technologies": [], "dates": []},
    }

    try:
        resp = await _retry_ollama_call(
            "POST",
            f"{OLLAMA_URL}/api/generate",
            json_body=_build_generate_body(
                model, prompt, temperature=temperature, num_predict=1536,
                schema=ANALYSIS_SCHEMAS.get("analysis_pass_g"),
            ),
            timeout_seconds=120.0,
            max_retries=2,
        )
        raw = resp.json().get("response", "")
        parsed = _parse_json_object(raw, context="auto-tag")

        if not parsed:
            logger.warning("Auto-tagging: LLM returned empty/unparseable response")
            return default_tags

        # Validate and normalize
        category = parsed.get("category", "other")
        if category not in VALID_CATEGORIES:
            category = "other"

        keywords = parsed.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(k).strip().lower() for k in keywords if k][:10]

        entities = parsed.get("entities", {})
        if not isinstance(entities, dict):
            entities = {}
        normalized_entities = {}
        for entity_type in ("people", "companies", "projects", "technologies", "dates"):
            vals = entities.get(entity_type, [])
            if not isinstance(vals, list):
                vals = []
            normalized_entities[entity_type] = [str(v).strip() for v in vals if v][:20]

        return {
            "category": category,
            "keywords": keywords,
            "entities": normalized_entities,
        }

    except Exception as e:
        logger.warning(f"Auto-tagging failed (non-fatal): {e}")
        return default_tags


_ACTIVE_MEETING_STATUSES = {
    MeetingStatus.preprocessing, MeetingStatus.transcribing, MeetingStatus.cleaning_transcript,
    MeetingStatus.identifying_speakers, MeetingStatus.summarizing, MeetingStatus.tagging, MeetingStatus.storing,
}


def _pipeline_busy() -> bool:
    return any(m.get("status") in _ACTIVE_MEETING_STATUSES for m in meetings.values())


def _enhance_state_path():
    return notes_store.NOTES_DIR / ".enhance_state.json"


def _enhance_state() -> dict:
    p = _enhance_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_enhance_state(d: dict) -> None:
    try:
        _atomic_write(_enhance_state_path(), json.dumps(d, indent=2))
    except Exception as e:
        logger.warning(f"enhance-state save failed (non-fatal): {e}")


def _body_sig(body: str) -> str:
    return hashlib.sha1((body or "").encode("utf-8")).hexdigest()


async def auto_tag_note(title: str, body: str) -> dict:
    """Tag a note the way meetings are tagged (analysis_pass_g). Non-fatal -> defaults."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)
    template = settings["prompts"].get("analysis_pass_g", DEFAULT_PROMPTS.get("analysis_pass_g", ""))
    text = (f"{title}\n\n{body}")[:16000]
    prompt = template.replace("{transcript}", text)
    default = {"category": "other", "keywords": [], "entities": {}}
    try:
        body = _build_generate_body(model, prompt, temperature=temperature, num_predict=1536,
            schema=ANALYSIS_SCHEMAS.get("analysis_pass_g"))  # think:False set by the helper
        resp = await _retry_ollama_call("POST", f"{OLLAMA_URL}/api/generate",
            json_body=body, timeout_seconds=180.0, max_retries=2)
        parsed = _parse_json_object(resp.json().get("response", ""), context="note-auto-tag")
        if not parsed:
            return default
        category = parsed.get("category", "other")
        if category not in VALID_CATEGORIES:
            category = "other"
        keywords = [str(k).strip().lower() for k in (parsed.get("keywords") or []) if k][:10]
        ents = parsed.get("entities") or {}
        if isinstance(ents, dict):
            for v in ents.values():
                if isinstance(v, list):
                    keywords += [str(x).strip().lower() for x in v if x]
        return {"category": category, "keywords": keywords, "entities": ents}
    except Exception as e:
        logger.warning(f"note auto-tag failed (non-fatal): {e}")
        return default


async def _run_tag_job(note_id: str) -> bool:
    if _pipeline_busy():
        return False
    note = notes_store.read_note(notes_store.NOTES_DIR, note_id)
    if not note:
        return False
    sig = _body_sig(note.get("body", ""))
    state = _enhance_state()
    if state.get(note_id, {}).get("tag_sig") == sig:
        return False
    tags = await auto_tag_note(note.get("title", ""), note.get("body", ""))
    kws = list(tags.get("keywords", []))
    if tags.get("category") and tags["category"] != "other":
        kws.append(tags["category"])
    notes_store.apply_auto_tags(notes_store.NOTES_DIR, note_id, tags.get("category", ""), kws)
    state.setdefault(note_id, {})["tag_sig"] = sig
    _save_enhance_state(state)
    return True


async def _hierarchical_summarize(transcript: str, duration: float, model: str = None, temperature: float = None, progress_callback=None) -> dict:
    """For very long meetings: summarize 15-min segments, then run multi-pass analysis on combined summaries."""
    if model is None or temperature is None:
        settings = load_settings()
        model = model or settings.get("ollama_model", OLLAMA_MODEL)
        temperature = temperature if temperature is not None else settings.get("temperature", 0.3)

    chunk_summary_template = load_settings()["prompts"].get("chunk_summary", DEFAULT_PROMPTS["chunk_summary"])

    lines = transcript.split("\n")
    # Split into ~15-minute chunks by timestamp parsing
    chunks = []
    current_chunk = []
    chunk_start = 0.0

    for line in lines:
        ts_match = re.match(r"\[(\d+:\d+:\d+)", line)
        if ts_match:
            parts = ts_match.group(1).split(":")
            current_time = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if current_time - chunk_start > 900 and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                chunk_start = current_time
        current_chunk.append(line)
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # If we couldn't parse timestamps, just split by line count
    if len(chunks) <= 1:
        chunk_size = len(lines) // max(1, int(duration / 900))
        chunks = [
            "\n".join(lines[i : i + chunk_size])
            for i in range(0, len(lines), max(1, chunk_size))
        ]

    # Summarize each chunk
    segment_summaries = []
    for i, chunk in enumerate(chunks):
        prompt = chunk_summary_template.replace(
            "{chunk_index}", str(i + 1)
        ).replace(
            "{chunk_total}", str(len(chunks))
        ).replace(
            "{chunk}", chunk
        )
        try:
            resp = await _retry_ollama_call(
                "POST",
                f"{OLLAMA_URL}/api/generate",
                json_body=_build_generate_body(
                    model, prompt, temperature=temperature, num_predict=1024,
                ),
                timeout_seconds=180.0,
            )
            segment_summaries.append(resp.json().get("response", ""))
        except Exception as e:
            logger.warning(f"Failed to summarize chunk {i+1}/{len(chunks)}: {e}")

    # Synthesize: combine segment summaries and run multi-pass analysis
    combined = "\n\n".join(
        f"--- Segment {i+1} ---\n{s}" for i, s in enumerate(segment_summaries)
    )
    prefix = f"[Synthesis of segment summaries from a {_format_timestamp(duration)} meeting]\n\n"
    return await _run_analysis_passes(prefix + combined, model, temperature, progress_callback)


def build_transcript_text(segments: list[dict]) -> str:
    """Build a human-readable transcript from segments."""
    lines = []
    for seg in segments:
        ts = _format_timestamp(seg["start"])
        speaker = seg.get("speaker", "UNKNOWN")
        lines.append(f"[{ts}] {speaker}: {seg['text']}")
    return "\n".join(lines)


def build_transcript_markdown(segments: list[dict], meeting: dict) -> str:
    """Build a readable markdown transcript with speaker headers and grouped blocks."""
    md = []
    md.append(f"# Transcript: {meeting.get('title', 'Meeting')}")
    md.append(f"\n**Date:** {meeting.get('date', 'Unknown')}")
    md.append(f"**Duration:** {meeting.get('duration_formatted', 'Unknown')}")

    # Collect unique speakers
    speakers = sorted({seg.get("speaker", "UNKNOWN") for seg in segments})
    md.append(f"**Speakers:** {', '.join(speakers)}")
    md.append("\n---\n")

    # Group consecutive same-speaker segments into blocks
    current_speaker = None
    block_texts = []
    block_start_ts = None

    for seg in segments:
        speaker = seg.get("speaker", "UNKNOWN")
        ts = _format_timestamp(seg["start"])

        if speaker != current_speaker:
            # Flush previous block
            if current_speaker is not None and block_texts:
                md.append(f"### {current_speaker} [{block_start_ts}]\n")
                md.append(" ".join(block_texts))
                md.append("")
            current_speaker = speaker
            block_texts = [seg["text"].strip()]
            block_start_ts = ts
        else:
            block_texts.append(seg["text"].strip())

    # Flush last block
    if current_speaker is not None and block_texts:
        md.append(f"### {current_speaker} [{block_start_ts}]\n")
        md.append(" ".join(block_texts))
        md.append("")

    return "\n".join(md)


# ---------------------------------------------------------------------------
# Transcript cleanup (Phase 3)
# ---------------------------------------------------------------------------


def _build_cleanup_prompt(
    batch_segments: list[dict],
    context_before: list[dict],
    context_after: list[dict],
    meeting_context: Optional[str] = None,
) -> str:
    """Construct the LLM prompt for transcript cleanup."""
    settings = load_settings()
    system_preamble = settings["prompts"].get("cleanup_system", DEFAULT_PROMPTS["cleanup_system"])

    parts = []
    # Editable system preamble (instructions/rules)
    parts.append(system_preamble.replace("{meeting_context}", meeting_context or ""))
    parts.append("")

    if meeting_context:
        parts.append(f"Meeting subject/context: {meeting_context}")
        parts.append("")

    if context_before:
        parts.append("--- Context (preceding, read-only) ---")
        for seg in context_before:
            speaker = seg.get("speaker", "UNKNOWN")
            parts.append(f"  {speaker}: {seg['text']}")
        parts.append("")

    parts.append(f"--- Segments to clean ({len(batch_segments)} lines) ---")
    for i, seg in enumerate(batch_segments):
        speaker = seg.get("speaker", "UNKNOWN")
        parts.append(f"[{i}] {speaker}: {seg['text']}")
    parts.append("")

    if context_after:
        parts.append("--- Context (following, read-only) ---")
        for seg in context_after:
            speaker = seg.get("speaker", "UNKNOWN")
            parts.append(f"  {speaker}: {seg['text']}")
        parts.append("")

    parts.append(f"Respond with exactly {len(batch_segments)} numbered lines:")
    return "\n".join(parts)


def _parse_cleanup_response(raw: str, expected_count: int) -> Optional[list[str]]:
    """Parse cleanup response. Returns list of cleaned texts or None on mismatch."""
    raw = _strip_think(raw)  # belt-and-braces: a stray <think> block breaks line counting
    lines = raw.strip().split("\n")
    results = {}
    pattern = re.compile(r"\[?(\d+)\]?[.:\s]\s*(.*)")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = pattern.match(line)
        if m:
            idx = int(m.group(1))
            text = m.group(2).strip()
            # Strip leading "SPEAKER: " prefix if the LLM echoed it back
            text = re.sub(r"^[A-Z_0-9]+:\s*", "", text)
            if text:
                results[idx] = text

    if len(results) != expected_count:
        return None

    return [results[i] for i in range(expected_count)]


async def step_cleanup_transcript(
    segments: list[dict],
    meeting_context: Optional[str] = None,
    batch_size: int = 15,
    context_window: int = 3,
) -> list[dict]:
    """Clean up transcript segments using LLM. Returns cleaned segments (or originals on failure)."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)

    cleaned_segments = copy.deepcopy(segments)
    total_batches = (len(segments) + batch_size - 1) // batch_size
    changes_made = 0

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(segments))
        batch = segments[start:end]

        ctx_before = segments[max(0, start - context_window):start]
        ctx_after = segments[end:min(len(segments), end + context_window)]

        prompt = _build_cleanup_prompt(batch, ctx_before, ctx_after, meeting_context)

        try:
            resp = await _retry_ollama_call(
                "POST",
                f"{OLLAMA_URL}/api/generate",
                json_body=_build_generate_body(
                    # temp 0.1: verbatim ASR correction is fidelity-critical, not
                    # creative; helper also sets think:false + a bounded num_ctx.
                    model, prompt, temperature=0.1, num_predict=4096,
                ),
                timeout_seconds=180.0,
                max_retries=2,
            )
            raw = resp.json().get("response", "")
            parsed = _parse_cleanup_response(raw, len(batch))

            if parsed:
                for i, text in enumerate(parsed):
                    if text != segments[start + i]["text"]:
                        cleaned_segments[start + i]["text"] = text
                        changes_made += 1
            else:
                logger.warning(
                    f"Cleanup batch {batch_idx + 1}/{total_batches}: "
                    f"response parse failed, keeping original text"
                )
        except Exception as e:
            logger.warning(
                f"Cleanup batch {batch_idx + 1}/{total_batches} failed: {e}. "
                f"Keeping original text for this batch."
            )

    logger.info(f"Transcript cleanup complete: {changes_made} segments modified out of {len(segments)}")
    return cleaned_segments


# ---------------------------------------------------------------------------
# Speaker identification
# ---------------------------------------------------------------------------


async def step_identify_speakers(transcript_text: str, segments: list[dict]) -> dict:
    """Use Ollama to identify real speaker names, titles, and companies from transcript content."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)

    # Collect unique speaker labels
    speaker_labels = sorted({seg.get("speaker", "UNKNOWN") for seg in segments} - {"UNKNOWN"})
    if not speaker_labels:
        return {"speaker_map": {}, "speaker_info": {}}

    speaker_list = ", ".join(speaker_labels)
    json_example = "{\n" + ",\n".join(
        f'  "{sp}": {{"name": "FirstName", "title": "JobTitle or null", "company": "CompanyName or null", "confidence": "high/medium/low"}}'
        for sp in speaker_labels
    ) + "\n}"

    template = settings["prompts"].get("speaker_id", DEFAULT_PROMPTS["speaker_id"])
    prompt = template.replace(
        "{speaker_list}", speaker_list
    ).replace(
        "{transcript}", transcript_text
    ).replace(
        "{json_example}", json_example
    ).replace(
        "{expected_participants}", ""
    )

    try:
        resp = await _retry_ollama_call(
            "POST",
            f"{OLLAMA_URL}/api/generate",
            json_body=_build_generate_body(
                # helper sets think:false (avoids a <think> block burning the
                # 1024 budget / breaking the JSON) + a bounded num_ctx.
                model, prompt, temperature=temperature, num_predict=1024,
            ),
            timeout_seconds=120.0,
        )
        raw = resp.json().get("response", "")
        return _parse_speaker_identification(raw, speaker_labels)

    except Exception as e:
        # Non-fatal: if identification fails, pipeline continues with generic labels
        logger.warning(f"Speaker identification failed (non-fatal): {e}")
        return {"speaker_map": {}, "speaker_info": {}}


def _parse_speaker_identification(raw: str, speaker_labels: list[str]) -> dict:
    """Parse LLM JSON response for speaker identification.

    Handles two formats:
    - Array: [{"label": "SPEAKER_00", "name": "Alex", "role": "...", "evidence": "..."}]
    - Object: {"SPEAKER_00": {"name": "Alex", "title": "...", "company": "...", ...}}
    """
    # Strip any <think> reasoning block, then markdown code fences if present
    cleaned = _strip_think(raw)
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try array pattern first, then object pattern
        match = re.search(r"\[[\s\S]*\]", cleaned) or re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return {"speaker_map": {}, "speaker_info": {}}
        else:
            return {"speaker_map": {}, "speaker_info": {}}

    speaker_map = {}
    speaker_info = {}

    if isinstance(data, list):
        # Array format: [{"label": "SPEAKER_00", "name": "Alex", "role": "...", ...}]
        for entry in data:
            if not isinstance(entry, dict):
                continue
            label = (entry.get("label") or "").strip()
            if label not in speaker_labels:
                continue
            name = (entry.get("name") or "").strip()
            if not name or name.lower() in ("null", "unknown", "none", ""):
                continue

            # "role" maps to title; array format may not have company
            title = (entry.get("role") or entry.get("title") or "").strip()
            if title.lower() in ("null", "none", ""):
                title = ""
            company = (entry.get("company") or "").strip()
            if company.lower() in ("null", "none", ""):
                company = ""
            confidence = entry.get("confidence", "medium")

            parts = []
            if title:
                parts.append(title)
            if company:
                parts.append(company)
            display_name = f"{name} ({', '.join(parts)})" if parts else name

            speaker_map[label] = name
            speaker_info[label] = {
                "name": name,
                "title": title,
                "company": company,
                "display_name": display_name,
                "confidence": confidence,
                "auto_detected": True,
            }
    elif isinstance(data, dict):
        # Object format: {"SPEAKER_00": {"name": "Alex", "title": "...", ...}}
        for label in speaker_labels:
            entry = data.get(label)
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            if not name or name.lower() in ("null", "unknown", "none", ""):
                continue

            title = (entry.get("title") or "").strip()
            if title.lower() in ("null", "none", ""):
                title = ""
            company = (entry.get("company") or "").strip()
            if company.lower() in ("null", "none", ""):
                company = ""
            confidence = entry.get("confidence", "medium")

            parts = []
            if title:
                parts.append(title)
            if company:
                parts.append(company)
            display_name = f"{name} ({', '.join(parts)})" if parts else name

            speaker_map[label] = name
            speaker_info[label] = {
                "name": name,
                "title": title,
                "company": company,
                "display_name": display_name,
                "confidence": confidence,
                "auto_detected": True,
            }

    return {"speaker_map": speaker_map, "speaker_info": speaker_info}


def build_summary_markdown(summary: dict, meeting: dict) -> str:
    """Build a markdown summary for file storage and RAG."""
    md = []
    md.append(f"# {summary.get('title', 'Meeting Summary')}")
    md.append(f"\n**Date:** {meeting.get('date', 'Unknown')}")
    md.append(f"**Duration:** {meeting.get('duration_formatted', 'Unknown')}")

    # Summary (new field) with fallback to executive_summary (legacy)
    summary_text = summary.get("summary") or summary.get("executive_summary", "N/A")
    md.append(f"\n## Summary\n\n{summary_text}")

    # Key Topics (new field: topics with outcome) with fallback to key_topics (legacy)
    topics = summary.get("topics") or summary.get("key_topics", [])
    if topics:
        md.append("\n## Key Topics\n")
        for t in topics:
            outcome = t.get("outcome", "")
            outcome_str = f" → _{outcome}_" if outcome else ""
            md.append(f"- **{t.get('topic', '')}**: {t.get('summary', '')}{outcome_str}")

    # Action Items (new fields: task, who, deadline) with fallback to legacy fields
    actions = summary.get("action_items", [])
    if actions:
        md.append("\n## Action Items\n")
        for a in actions:
            task = a.get("task") or a.get("description", "")
            who = a.get("who") or a.get("assigned_to", "Unassigned")
            deadline = a.get("deadline") or ""
            priority = a.get("priority", "medium")
            deadline_str = f", Deadline: {deadline}" if deadline else ""
            md.append(f"- [ ] {task} (Assigned: {who}, Priority: {priority}{deadline_str})")

    # Decisions
    decisions = summary.get("decisions", [])
    if decisions:
        md.append("\n## Decisions\n")
        for d in decisions:
            md.append(f"- **{d.get('decision', '')}** - {d.get('context', '')}")

    # Open Questions (new field) with fallback to questions_raised (legacy)
    questions = summary.get("open_questions") or summary.get("questions_raised", [])
    if questions:
        md.append("\n## Open Questions\n")
        for q in questions:
            asked_by = q.get("asked_by", "")
            asked_str = f" (asked by {asked_by})" if asked_by else ""
            status = "Answered" if q.get("answered") else "Open"
            md.append(f"- {q.get('question', '')}{asked_str} [{status}]")

    # Concerns & Risks (new section from Pass D)
    concerns = summary.get("concerns", [])
    if concerns:
        md.append("\n## Concerns & Risks\n")
        for c in concerns:
            raised = c.get("raised_by", "")
            raised_str = f" (raised by {raised})" if raised else ""
            resolved_str = " [Resolved]" if c.get("resolved") else " [Open]"
            notes = c.get("notes", "")
            notes_str = f" — {notes}" if notes else ""
            md.append(f"- {c.get('concern', '')}{raised_str}{resolved_str}{notes_str}")

    # Key Figures & Dates (new section from Pass E)
    figures = summary.get("figures", [])
    if figures:
        md.append("\n## Key Figures & Dates\n")
        for f in figures:
            said_by = f.get("said_by", "")
            said_str = f" (mentioned by {said_by})" if said_by else ""
            md.append(f"- **{f.get('figure', '')}**: {f.get('context', '')}{said_str}")

    # Sentiment (new field) with fallback to sentiment_overview (legacy)
    sentiment = summary.get("sentiment") or summary.get("sentiment_overview", {})
    if sentiment:
        md.append(f"\n## Sentiment\n\n**Overall:** {sentiment.get('overall', 'N/A')}")
        for moment in sentiment.get("notable_moments", []):
            if isinstance(moment, dict):
                md.append(f"- {moment.get('moment', '')} — _{moment.get('tone', '')}_")
            else:
                md.append(f"- {moment}")

    return "\n".join(md)


# ---------------------------------------------------------------------------
# Smart transcript chunking (Phase 2b)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Search context helper (Phase 5b)
# ---------------------------------------------------------------------------


def _get_search_context(meeting_id: str, timestamp: float) -> list[dict]:
    """Load transcript and find segments around a given timestamp."""
    m = meetings.get(meeting_id)
    if not m:
        return []
    out_dir = Path(m.get("output_dir", ""))
    transcript_path = out_dir / "transcript.json"
    if not transcript_path.exists():
        return []

    try:
        data = json.loads(transcript_path.read_text())
        segments = data.get("segments", [])
    except Exception:
        return []

    if not segments:
        return []

    # Find the segment closest to the timestamp
    closest_idx = 0
    min_diff = abs(segments[0].get("start", 0) - timestamp)
    for i, seg in enumerate(segments):
        diff = abs(seg.get("start", 0) - timestamp)
        if diff < min_diff:
            min_diff = diff
            closest_idx = i

    # Return 2 before and 2 after
    start = max(0, closest_idx - 2)
    end = min(len(segments), closest_idx + 3)
    return segments[start:end]


# ---------------------------------------------------------------------------
# Background processing pipeline
# ---------------------------------------------------------------------------


async def process_meeting(meeting_id: str):
    """Full async pipeline for a single meeting."""
    meeting = meetings[meeting_id]
    original_path = meeting["original_path"]
    step_timings = {}

    try:
        # Step 1: Pre-process audio
        meeting["status"] = MeetingStatus.preprocessing
        logger.info(f"[{meeting_id}] Starting preprocessing")
        _update_progress(meeting, 5, "Pre-processing audio...")

        t0 = time.monotonic()
        wav_path = original_path.rsplit(".", 1)[0] + "_processed.wav"
        duration = await asyncio.get_event_loop().run_in_executor(
            None, preprocess_audio, original_path, wav_path
        )
        meeting["duration"] = duration
        meeting["duration_formatted"] = _format_timestamp(duration)
        step_timings["preprocessing"] = round(time.monotonic() - t0, 1)
        logger.info(f"[{meeting_id}] Audio preprocessed: {duration:.0f}s, took {step_timings['preprocessing']}s")
        _update_progress(meeting, 10, "Audio preprocessed")

        # Step 2: Unload Ollama models to free VRAM
        await step_unload_ollama_models()

        # Step 3: Split if needed and transcribe
        meeting["status"] = MeetingStatus.transcribing
        logger.info(f"[{meeting_id}] Starting transcription")
        _update_progress(meeting, 15, "Starting transcription...")

        t0 = time.monotonic()
        chunks = await asyncio.get_event_loop().run_in_executor(
            None, split_audio, wav_path, duration
        )

        chunk_results = []
        total_chunks = len(chunks)
        for ci, chunk in enumerate(chunks):
            pct = 15 + int(((ci + 0.5) / max(1, total_chunks)) * 25)
            _update_progress(meeting, pct, f"Transcribing chunk {ci + 1}/{total_chunks}")
            logger.info(f"[{meeting_id}] Transcribing chunk {ci + 1}/{total_chunks}")

            result = await step_transcribe(
                chunk["path"],
                meeting.get("min_speakers"),
                meeting.get("max_speakers"),
            )
            result["offset"] = chunk["offset"]
            chunk_results.append(result)
            # Clean up chunk files (but not the main processed wav)
            if chunk["path"] != wav_path and os.path.exists(chunk["path"]):
                os.unlink(chunk["path"])

        segments = merge_chunk_segments(chunk_results)
        meeting["segment_count"] = len(segments)
        step_timings["transcription"] = round(time.monotonic() - t0, 1)
        logger.info(f"[{meeting_id}] Transcription complete: {len(segments)} segments, took {step_timings['transcription']}s")
        _update_progress(meeting, 40, f"Transcribed {len(segments)} segments")

        # Build text transcript
        transcript_text = build_transcript_text(segments)

        # Step 3.5: Transcript cleanup with LLM
        raw_segments = copy.deepcopy(segments)
        meeting["status"] = MeetingStatus.cleaning_transcript
        logger.info(f"[{meeting_id}] Starting transcript cleanup")
        _update_progress(meeting, 42, "Cleaning transcript...")

        t0 = time.monotonic()
        cleanup_settings = load_settings()
        cleanup_model = cleanup_settings.get("ollama_model", OLLAMA_MODEL)
        cleanup_temperature = cleanup_settings.get("temperature", 0.3)
        try:
            batch_size = 15
            total_cleanup_batches = (len(segments) + batch_size - 1) // batch_size
            cleaned_segments = copy.deepcopy(segments)
            changes_made = 0

            for batch_idx in range(total_cleanup_batches):
                pct = 42 + int(((batch_idx + 0.5) / max(1, total_cleanup_batches)) * 13)
                elapsed = round(time.monotonic() - t0)
                _update_progress(meeting, pct, f"Cleaning batch {batch_idx + 1}/{total_cleanup_batches} ({elapsed}s elapsed)")

                start_idx = batch_idx * batch_size
                end_idx = min(start_idx + batch_size, len(segments))
                batch = segments[start_idx:end_idx]

                ctx_before = segments[max(0, start_idx - 3):start_idx]
                ctx_after = segments[end_idx:min(len(segments), end_idx + 3)]

                prompt = _build_cleanup_prompt(batch, ctx_before, ctx_after, meeting.get("meeting_context"))

                try:
                    resp = await _retry_ollama_call(
                        "POST",
                        f"{OLLAMA_URL}/api/generate",
                        json_body=_build_generate_body(
                            # temp 0.1: verbatim ASR correction is fidelity-critical,
                            # not creative; helper also sets think:false + num_ctx.
                            cleanup_model, prompt, temperature=0.1, num_predict=4096,
                        ),
                        timeout_seconds=180.0,
                        max_retries=2,
                    )
                    raw = resp.json().get("response", "")
                    parsed = _parse_cleanup_response(raw, len(batch))

                    if parsed:
                        for i, text in enumerate(parsed):
                            if text != segments[start_idx + i]["text"]:
                                cleaned_segments[start_idx + i]["text"] = text
                                changes_made += 1
                    else:
                        logger.warning(
                            f"[{meeting_id}] Cleanup batch {batch_idx + 1}/{total_cleanup_batches}: "
                            f"response parse failed, keeping original"
                        )
                except Exception as e:
                    logger.warning(
                        f"[{meeting_id}] Cleanup batch {batch_idx + 1}/{total_cleanup_batches} failed: {e}. "
                        f"Keeping original text."
                    )

            if changes_made > 0:
                segments = cleaned_segments
                transcript_text = build_transcript_text(segments)
                meeting["transcript_cleaned"] = True
                logger.info(f"[{meeting_id}] Cleanup complete: {changes_made} segments modified")
            else:
                logger.info(f"[{meeting_id}] Cleanup complete: no changes made")

        except Exception as e:
            logger.warning(f"[{meeting_id}] Transcript cleanup failed entirely (non-fatal): {e}")
            # Continue with raw segments

        step_timings["cleanup"] = round(time.monotonic() - t0, 1)
        _update_progress(meeting, 55, "Transcript cleaned")

        # Step 4: Identify speakers using LLM
        meeting["status"] = MeetingStatus.identifying_speakers
        logger.info(f"[{meeting_id}] Starting speaker identification")
        _update_progress(meeting, 60, "Identifying speakers...")

        t0 = time.monotonic()
        identification = await step_identify_speakers(transcript_text, segments)
        speaker_map = identification.get("speaker_map", {})
        speaker_info = identification.get("speaker_info", {})

        if speaker_map:
            # Apply detected names to segments in-place
            for seg in segments:
                original = seg.get("speaker", "")
                if original in speaker_map:
                    seg["speaker"] = speaker_map[original]
            # NOTE: raw_segments intentionally left with original SPEAKER_XX labels
            # so raw_transcript.json preserves diarization output for re-identification
            # Rebuild transcript text with real names
            transcript_text = build_transcript_text(segments)
            # Store in meeting dict for later use
            meeting["speaker_map"] = speaker_map
            meeting["speaker_info"] = speaker_info

        step_timings["speaker_identification"] = round(time.monotonic() - t0, 1)
        logger.info(f"[{meeting_id}] Speaker identification complete, took {step_timings['speaker_identification']}s")
        _update_progress(meeting, 70, "Speakers identified")

        # Step 5: Summarize with Ollama (6 analysis passes)
        meeting["status"] = MeetingStatus.summarizing
        logger.info(f"[{meeting_id}] Starting multi-pass analysis")
        _update_progress(meeting, 72, "Analyzing meeting (pass 1/6)...")

        def _summarize_progress(pass_idx, total, detail):
            pct = 72 + int((pass_idx / max(1, total)) * 13)  # 72% -> 85%
            _update_progress(meeting, pct, detail)

        t0 = time.monotonic()
        summary = await step_summarize(transcript_text, duration, progress_callback=_summarize_progress)
        if "title" in summary and summary["title"] != "Meeting":
            meeting["title"] = summary["title"]
        step_timings["summarization"] = round(time.monotonic() - t0, 1)
        logger.info(f"[{meeting_id}] Multi-pass analysis complete, took {step_timings['summarization']}s")
        _update_progress(meeting, 82, "Analysis complete")

        # Step 5b: Auto-tag
        meeting["status"] = MeetingStatus.tagging
        logger.info(f"[{meeting_id}] Starting auto-tagging")
        _update_progress(meeting, 84, "Auto-tagging...")

        t0 = time.monotonic()
        tags = await step_auto_tag(transcript_text, summary)
        meeting["tags"] = tags
        step_timings["tagging"] = round(time.monotonic() - t0, 1)
        logger.info(f"[{meeting_id}] Auto-tagging complete: category={tags.get('category')}, took {step_timings['tagging']}s")
        _update_progress(meeting, 87, "Tagging complete")

        # Step 6: Store everything
        meeting["status"] = MeetingStatus.storing
        logger.info(f"[{meeting_id}] Storing results")
        _update_progress(meeting, 89, "Storing files...")

        t0 = time.monotonic()

        # Determine output directory
        out_dir = _meeting_dir(meeting)

        # Copy original audio
        original_ext = os.path.splitext(original_path)[1]
        shutil.copy2(original_path, out_dir / f"audio{original_ext}")

        # Write raw transcript JSON (original WhisperX output)
        raw_transcript_data = {
            "meeting_id": meeting_id,
            "date": meeting["date"],
            "duration": duration,
            "language": chunk_results[0].get("language", "en") if chunk_results else "en",
            "segments": raw_segments,
        }
        _atomic_write(out_dir / "raw_transcript.json", json.dumps(raw_transcript_data, indent=2))

        # Write cleaned transcript JSON
        transcript_data = {
            "meeting_id": meeting_id,
            "date": meeting["date"],
            "duration": duration,
            "language": chunk_results[0].get("language", "en") if chunk_results else "en",
            "segments": segments,
            "cleaned": meeting.get("transcript_cleaned", False),
        }
        _atomic_write(out_dir / "transcript.json", json.dumps(transcript_data, indent=2))

        # Write speaker identification files
        if speaker_info:
            _atomic_write(out_dir / "speaker_info.json", json.dumps(speaker_info, indent=2))
        if speaker_map:
            _atomic_write(out_dir / "speaker_map.json", json.dumps(speaker_map, indent=2))

        # Write SRT
        _atomic_write(out_dir / "transcript.srt", _generate_srt(segments))

        # Write transcript markdown
        transcript_md = build_transcript_markdown(segments, meeting)
        _atomic_write(out_dir / "transcript.md", transcript_md)

        # Write tags JSON
        if meeting.get("tags"):
            _atomic_write(out_dir / "tags.json", json.dumps(meeting["tags"], indent=2))

        # Write summary JSON
        _atomic_write(out_dir / "summary.json", json.dumps(summary, indent=2))

        # Write summary markdown
        summary_md = build_summary_markdown(summary, meeting)
        _atomic_write(out_dir / "summary.md", summary_md)

        _update_progress(meeting, 92, "Storing vectors...")

        # Store in Qdrant
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, store_in_qdrant, meeting_id, meeting, segments, summary
            )
        except Exception as e:
            # Non-fatal: files are saved, vector search just won't work for this meeting
            meeting["qdrant_error"] = str(e)
            logger.warning(f"[{meeting_id}] Qdrant storage failed (non-fatal): {e}")

        _update_progress(meeting, 95, "Uploading to OpenWebUI...")

        # Upload summary.md to OpenWebUI knowledge base (best-effort)
        try:
            await _upload_to_openwebui(meeting_id, summary_md, meeting)
        except Exception as e:
            logger.warning(f"[{meeting_id}] OpenWebUI upload failed (non-fatal): {e}")

        # Clean up temp files
        if os.path.exists(wav_path):
            os.unlink(wav_path)
        if os.path.exists(original_path) and str(out_dir) not in original_path:
            os.unlink(original_path)

        step_timings["storage"] = round(time.monotonic() - t0, 1)
        meeting["status"] = MeetingStatus.complete
        meeting["output_dir"] = str(out_dir)
        meeting["summary"] = summary
        meeting["transcript_text"] = transcript_text
        meeting["step_timings"] = step_timings
        logger.info(f"[{meeting_id}] Processing complete. Timings: {step_timings}")
        _update_progress(meeting, 100, "Complete")

        # Auto-compute link suggestions (non-fatal)
        try:
            _auto_compute_link_suggestions(meeting_id)
        except Exception as e:
            logger.warning(f"[{meeting_id}] Auto-linking failed (non-fatal): {e}")

    except Exception as e:
        meeting["status"] = MeetingStatus.error
        meeting["error"] = str(e)
        meeting["step_timings"] = step_timings
        logger.error(f"[{meeting_id}] Processing failed: {e}")
        _save_index()
        raise


async def _upload_to_openwebui(meeting_id: str, summary_md: str, meeting: dict):
    """Upload summary to OpenWebUI as a file in a knowledge base (best-effort)."""
    if not OPENWEBUI_API_KEY:
        return

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        # Upload as a file
        resp = await client.post(
            f"{OPENWEBUI_URL}/api/v1/files/",
            headers={"Authorization": f"Bearer {OPENWEBUI_API_KEY}"},
            files={
                "file": (
                    f"{meeting_id}_summary.md",
                    summary_md.encode(),
                    "text/markdown",
                )
            },
        )
        if resp.status_code in (200, 201):
            meeting["openwebui_file_id"] = resp.json().get("id")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json", media_type="application/json")


@app.get("/api/info")
async def api_info():
    return {
        "service": "Meeting Capture & Analysis",
        "version": "2.0.0",
        "meetings_count": len(meetings),
    }


@app.post("/meetings/upload")
async def upload_meeting(
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    min_speakers: Optional[int] = Form(default=None),
    max_speakers: Optional[int] = Form(default=None),
    meeting_context: Optional[str] = Form(default=None),
):
    """Upload an audio/video file for meeting processing."""
    # Validate file extension
    suffix = os.path.splitext(file.filename or ".wav")[1].lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Unsupported file type '{suffix}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            },
        )

    # Validate file size (read content and check)
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        return JSONResponse(
            status_code=413,
            content={
                "detail": f"File too large ({len(content) / (1024*1024):.1f} MB). Maximum: {MAX_UPLOAD_SIZE / (1024*1024):.0f} MB"
            },
        )

    meeting_id = str(uuid.uuid4())[:8]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Save uploaded file to temp location
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = str(MEETINGS_DIR / f"_upload_{meeting_id}{suffix}")
    with open(tmp_path, "wb") as f:
        f.write(content)

    meeting = {
        "id": meeting_id,
        "date": date_str,
        "title": title or "Meeting",
        "status": MeetingStatus.queued,
        "original_path": tmp_path,
        "original_filename": file.filename,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "min_speakers": min_speakers,
        "max_speakers": max_speakers,
        "meeting_context": meeting_context,
        "progress_percent": 0,
        "progress_detail": "Queued",
    }
    meetings[meeting_id] = meeting
    _save_index()

    logger.info(f"[{meeting_id}] Meeting uploaded: {file.filename} ({len(content) / (1024*1024):.1f} MB)")

    # Start processing in background
    task = asyncio.create_task(process_meeting(meeting_id))
    meeting["_task"] = task

    return JSONResponse(
        content={"meeting_id": meeting_id, "status": "queued"},
        status_code=202,
    )


# ---------------------------------------------------------------------------
# Grouped Views
# ---------------------------------------------------------------------------


def _compact_meeting_summary(mid: str, m: dict) -> dict:
    """Return a compact summary dict for grouped view items."""
    return {
        "id": mid,
        "date": m.get("date"),
        "title": m.get("title"),
        "status": m.get("status"),
        "duration_formatted": m.get("duration_formatted"),
        "tags": m.get("tags", {}),
    }


@app.get("/meetings/grouped")
async def get_meetings_grouped(group_by: str = Query(default="week")):
    """Return meetings grouped by week, speaker, keyword, category, or linked clusters."""
    valid_modes = {"week", "speaker", "keyword", "category", "linked"}
    if group_by not in valid_modes:
        raise HTTPException(status_code=400, detail=f"group_by must be one of: {', '.join(sorted(valid_modes))}")

    groups: list[dict] = []

    if group_by == "week":
        week_map: dict[str, list] = {}
        for mid, m in meetings.items():
            date_str = m.get("date", "")
            if date_str:
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    iso = dt.isocalendar()
                    week_key = f"{iso[0]}-W{iso[1]:02d}"
                    # Compute week-start label (Monday)
                    monday = dt - timedelta(days=dt.weekday())
                    label = f"Week of {monday.strftime('%b %d')}"
                except (ValueError, TypeError):
                    week_key = "unknown"
                    label = "Unknown Date"
            else:
                week_key = "unknown"
                label = "Unknown Date"

            if week_key not in week_map:
                week_map[week_key] = {"label": label, "meetings": []}
            week_map[week_key]["meetings"].append(_compact_meeting_summary(mid, m))

        # Sort groups reverse chronological (week key sorts naturally)
        for key in sorted(week_map.keys(), reverse=True):
            grp = week_map[key]
            grp["meetings"].sort(key=lambda x: x.get("date", ""), reverse=True)
            groups.append({"key": key, "label": grp["label"], "count": len(grp["meetings"]), "meetings": grp["meetings"]})

    elif group_by == "speaker":
        speaker_map_agg: dict[str, list] = {}
        for mid, m in meetings.items():
            if m.get("status") != MeetingStatus.complete:
                continue
            names = set()
            for v in m.get("speaker_map", {}).values():
                if isinstance(v, str) and v.strip():
                    names.add(v.strip())
            for v in m.get("speaker_info", {}).values():
                if isinstance(v, dict):
                    name = v.get("name", "")
                    if name and name.strip():
                        names.add(name.strip())
                elif isinstance(v, str) and v.strip():
                    names.add(v.strip())
            if not names:
                names = {"Unknown"}
            for name in names:
                if name not in speaker_map_agg:
                    speaker_map_agg[name] = []
                speaker_map_agg[name].append(_compact_meeting_summary(mid, m))

        for name in sorted(speaker_map_agg.keys(), key=lambda n: len(speaker_map_agg[n]), reverse=True):
            mtgs = speaker_map_agg[name]
            mtgs.sort(key=lambda x: x.get("date", ""), reverse=True)
            groups.append({"key": name, "label": name, "count": len(mtgs), "meetings": mtgs})

    elif group_by == "keyword":
        kw_map: dict[str, list] = {}
        for mid, m in meetings.items():
            if m.get("status") != MeetingStatus.complete:
                continue
            keywords = m.get("tags", {}).get("keywords", [])
            for kw in keywords:
                kw_str = str(kw).strip().lower()
                if not kw_str:
                    continue
                if kw_str not in kw_map:
                    kw_map[kw_str] = []
                kw_map[kw_str].append(_compact_meeting_summary(mid, m))

        for kw in sorted(kw_map.keys(), key=lambda k: len(kw_map[k]), reverse=True):
            mtgs = kw_map[kw]
            mtgs.sort(key=lambda x: x.get("date", ""), reverse=True)
            groups.append({"key": kw, "label": kw, "count": len(mtgs), "meetings": mtgs})

    elif group_by == "category":
        cat_map: dict[str, list] = {}
        for mid, m in meetings.items():
            if m.get("status") != MeetingStatus.complete:
                continue
            cat = m.get("tags", {}).get("category", "other")
            if cat not in cat_map:
                cat_map[cat] = []
            cat_map[cat].append(_compact_meeting_summary(mid, m))

        for cat in sorted(cat_map.keys(), key=lambda c: len(cat_map[c]), reverse=True):
            mtgs = cat_map[cat]
            mtgs.sort(key=lambda x: x.get("date", ""), reverse=True)
            groups.append({"key": cat, "label": cat.replace("_", " ").title(), "count": len(mtgs), "meetings": mtgs})

    elif group_by == "linked":
        # Union-Find to compute connected components from manual links
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(a: str, b: str):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Build union-find from manual links
        for mid, m in meetings.items():
            manual = m.get("links", {}).get("manual", [])
            for linked_id in manual:
                if linked_id in meetings:
                    union(mid, linked_id)

        # Group by root
        clusters: dict[str, list[str]] = {}
        unlinked = []
        for mid in meetings:
            manual = meetings[mid].get("links", {}).get("manual", [])
            if manual:
                root = find(mid)
                if root not in clusters:
                    clusters[root] = []
                clusters[root].append(mid)
            else:
                unlinked.append(mid)

        # Build groups from clusters
        for root, member_ids in clusters.items():
            # Label = title of earliest meeting in cluster
            earliest = min(member_ids, key=lambda mid: meetings[mid].get("date", "9999"))
            label = meetings[earliest].get("title", "Linked Meetings")
            mtgs = [_compact_meeting_summary(mid, meetings[mid]) for mid in member_ids]
            mtgs.sort(key=lambda x: x.get("date", ""), reverse=True)
            groups.append({"key": root, "label": label, "count": len(mtgs), "meetings": mtgs})

        groups.sort(key=lambda g: g["count"], reverse=True)

        return {
            "group_by": group_by,
            "groups": groups,
            "unlinked_count": len(unlinked),
        }

    return {"group_by": group_by, "groups": groups}


@app.get("/meetings")
async def list_meetings(
    status: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    title_search: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    speaker: Optional[str] = Query(default=None),
):
    """List all meetings with their status, with optional filtering."""
    result = []
    for mid, m in sorted(meetings.items(), key=lambda x: x[1].get("created_at", ""), reverse=True):
        # Apply filters
        if status and m.get("status") != status:
            continue
        if date_from and (m.get("date", "") < date_from):
            continue
        if date_to and (m.get("date", "") > date_to):
            continue
        if title_search and title_search.lower() not in (m.get("title", "") or "").lower():
            continue
        # Category filter: exact match
        if category:
            m_tags = m.get("tags", {})
            if m_tags.get("category") != category:
                continue
        # Tag filter: search keywords + all entity lists
        if tag:
            tag_lower = tag.lower()
            m_tags = m.get("tags", {})
            searchable = list(m_tags.get("keywords", []))
            for entity_list in m_tags.get("entities", {}).values():
                if isinstance(entity_list, list):
                    searchable.extend(entity_list)
            if not any(tag_lower in str(s).lower() for s in searchable):
                continue
        # Speaker filter: search speaker_map values and speaker_info values
        if speaker:
            speaker_lower = speaker.lower()
            speaker_names = []
            for v in m.get("speaker_map", {}).values():
                if isinstance(v, str):
                    speaker_names.append(v.lower())
            for v in m.get("speaker_info", {}).values():
                if isinstance(v, dict):
                    name = v.get("name", "")
                    if name:
                        speaker_names.append(name.lower())
                elif isinstance(v, str):
                    speaker_names.append(v.lower())
            if not any(speaker_lower in name for name in speaker_names):
                continue

        result.append({
            "id": mid,
            "date": m.get("date"),
            "title": m.get("title"),
            "status": m.get("status"),
            "duration_formatted": m.get("duration_formatted"),
            "created_at": m.get("created_at"),
            "progress_percent": m.get("progress_percent", 0),
            "progress_detail": m.get("progress_detail", ""),
            "step_timings": m.get("step_timings"),
            "tags": m.get("tags", {}),
        })
    return result


@app.get("/meetings/{meeting_id}/status")
async def meeting_status(meeting_id: str):
    """Get processing status for a meeting."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]
    result = {
        "id": meeting_id,
        "status": m.get("status"),
        "title": m.get("title"),
        "date": m.get("date"),
        "duration_formatted": m.get("duration_formatted"),
        "progress_percent": m.get("progress_percent", 0),
        "progress_detail": m.get("progress_detail", ""),
        "step_timings": m.get("step_timings"),
        "transcript_cleaned": m.get("transcript_cleaned", False),
    }
    if m.get("status") == MeetingStatus.error:
        result["error"] = m.get("error")
    return result


@app.get("/meetings/{meeting_id}/transcript")
async def meeting_transcript(meeting_id: str):
    """Get full transcript with speaker labels and timestamps."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    # Read from disk
    out_dir = Path(m.get("output_dir", ""))
    transcript_path = out_dir / "transcript.json"
    if transcript_path.exists():
        return json.loads(transcript_path.read_text())

    raise HTTPException(status_code=404, detail="Transcript file not found")


@app.get("/meetings/{meeting_id}/summary")
async def meeting_summary(meeting_id: str):
    """Get structured meeting summary."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    out_dir = Path(m.get("output_dir", ""))
    summary_path = out_dir / "summary.json"
    if summary_path.exists():
        return json.loads(summary_path.read_text())

    raise HTTPException(status_code=404, detail="Summary file not found")


@app.get("/meetings/{meeting_id}/files/{filename}")
async def meeting_file(meeting_id: str, filename: str):
    """Download a meeting output file."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    allowed = {
        "transcript.json", "transcript.srt", "transcript.md", "summary.md", "summary.json",
        "speaker_info.json", "speaker_map.json", "raw_transcript.json", "tags.json",
    }
    if filename not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid file. Allowed: {allowed}")

    out_dir = Path(m.get("output_dir", ""))
    file_path = out_dir / filename

    # Auto-generate markdown files for meetings processed before these features existed
    if not file_path.exists() and filename in ("transcript.md", "summary.md"):
        transcript_path = out_dir / "transcript.json"
        if filename == "transcript.md" and transcript_path.exists():
            data = json.loads(transcript_path.read_text())
            segments = data.get("segments", [])
            if segments:
                md = build_transcript_markdown(segments, m)
                _atomic_write(file_path, md)
        elif filename == "summary.md":
            summary_path = out_dir / "summary.json"
            if summary_path.exists():
                summary_data = json.loads(summary_path.read_text())
                md = build_summary_markdown(summary_data, m)
                _atomic_write(file_path, md)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    media_types = {
        "transcript.json": "application/json",
        "transcript.srt": "text/plain",
        "transcript.md": "text/markdown",
        "summary.md": "text/markdown",
        "summary.json": "application/json",
        "speaker_info.json": "application/json",
        "speaker_map.json": "application/json",
        "raw_transcript.json": "application/json",
        "tags.json": "application/json",
    }
    return FileResponse(file_path, media_type=media_types[filename], filename=filename)


@app.get("/meetings/{meeting_id}/audio")
async def meeting_audio(meeting_id: str):
    """Stream the original audio recording with Range request support for seeking."""
    m = meetings.get(meeting_id)
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    out_dir = Path(m.get("output_dir", ""))
    audio_path = None
    for ext in (".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".webm", ".flac", ".aac", ".mkv"):
        candidate = out_dir / f"audio{ext}"
        if candidate.exists():
            audio_path = candidate
            break
    if audio_path is None:
        raise HTTPException(status_code=404, detail="No audio file found")
    media_type = {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".mp4": "audio/mp4", ".ogg": "audio/ogg", ".webm": "audio/webm",
        ".flac": "audio/flac", ".aac": "audio/aac", ".mkv": "video/x-matroska",
    }.get(audio_path.suffix.lower(), "application/octet-stream")
    return FileResponse(audio_path, media_type=media_type,
                        headers={"Accept-Ranges": "bytes"})


@app.get("/meetings/search")
async def search_meetings(
    q: str,
    limit: int = 10,
    speaker: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    meeting_id: Optional[str] = Query(default=None),
    chunk_type: Optional[str] = Query(default=None),
    include_context: bool = Query(default=False),
):
    """Hybrid semantic search across all meeting transcripts and summaries with optional filters."""
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

    try:
        embedder = get_embedder()
        qdrant = get_qdrant()
        query_vec = embedder.encode(q).tolist()

        # Build Qdrant filter conditions
        must_conditions = []
        if speaker:
            must_conditions.append(
                FieldCondition(key="speaker", match=MatchValue(value=speaker))
            )
        if meeting_id:
            must_conditions.append(
                FieldCondition(key="meeting_id", match=MatchValue(value=meeting_id))
            )
        if chunk_type:
            must_conditions.append(
                FieldCondition(key="chunk_type", match=MatchValue(value=chunk_type))
            )
        if date_from:
            must_conditions.append(
                FieldCondition(key="date", range=Range(gte=date_from))
            )
        if date_to:
            must_conditions.append(
                FieldCondition(key="date", range=Range(lte=date_to))
            )

        query_filter = Filter(must=must_conditions) if must_conditions else None

        results = qdrant.search(
            collection_name=COLLECTION_NAME,
            query_vector=query_vec,
            query_filter=query_filter,
            limit=limit,
        )

        response = []
        for hit in results:
            item = {
                "score": hit.score,
                "meeting_id": hit.payload.get("meeting_id"),
                "date": hit.payload.get("date"),
                "title": hit.payload.get("title"),
                "chunk_type": hit.payload.get("chunk_type"),
                "speaker": hit.payload.get("speaker"),
                "text": hit.payload.get("text"),
                "timestamp": hit.payload.get("timestamp"),
            }
            if include_context and hit.payload.get("timestamp") is not None:
                item["context"] = _get_search_context(
                    hit.payload.get("meeting_id", ""),
                    hit.payload.get("timestamp", 0),
                )
            response.append(item)

        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search error: {e}")


@app.delete("/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str):
    """Delete a meeting: remove files, Qdrant vectors, and in-memory state."""
    async with meetings_lock:
        if meeting_id not in meetings:
            raise HTTPException(status_code=404, detail="Meeting not found")

        m = meetings[meeting_id]

        # Remove output directory from disk
        out_dir = m.get("output_dir")
        if out_dir and Path(out_dir).exists():
            shutil.rmtree(out_dir, ignore_errors=True)

        # Delete vectors from Qdrant (best-effort)
        try:
            qdrant = get_qdrant()
            qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=Filter(
                    must=[FieldCondition(key="meeting_id", match=MatchValue(value=meeting_id))]
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to delete Qdrant vectors for {meeting_id} (non-fatal): {e}")

        # Clean up links in other meetings that reference this one
        for other_id, other in meetings.items():
            if other_id == meeting_id:
                continue
            other_links = other.get("links")
            if not other_links:
                continue
            if meeting_id in other_links.get("manual", []):
                other_links["manual"].remove(meeting_id)
            other_links["suggestions"] = [
                s for s in other_links.get("suggestions", [])
                if s.get("meeting_id") != meeting_id
            ]

        del meetings[meeting_id]
        _save_index()

    return {"detail": f"Meeting {meeting_id} deleted"}


@app.get("/meetings/{meeting_id}/tags")
async def get_tags(meeting_id: str):
    """Get tags for a meeting."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]

    # Try memory first
    if m.get("tags"):
        return m["tags"]

    # Try disk
    out_dir = Path(m.get("output_dir", ""))
    tags_path = out_dir / "tags.json"
    if tags_path.exists():
        try:
            tags = json.loads(tags_path.read_text())
            m["tags"] = tags
            return tags
        except Exception:
            pass

    return {"category": "other", "keywords": [], "entities": {"people": [], "companies": [], "projects": [], "technologies": [], "dates": []}}


class TagsUpdateRequest(BaseModel):
    category: Optional[str] = None
    keywords: Optional[list[str]] = None
    entities: Optional[dict[str, list[str]]] = None


@app.put("/meetings/{meeting_id}/tags")
async def update_tags(meeting_id: str, body: TagsUpdateRequest):
    """Manually update tags for a meeting."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]
    out_dir = Path(m.get("output_dir", ""))

    # Load existing tags
    current_tags = m.get("tags", {})
    if not current_tags:
        tags_path = out_dir / "tags.json"
        if tags_path.exists():
            try:
                current_tags = json.loads(tags_path.read_text())
            except Exception:
                current_tags = {}

    # Apply updates
    if body.category is not None:
        if body.category in VALID_CATEGORIES:
            current_tags["category"] = body.category
        else:
            raise HTTPException(status_code=400, detail=f"Invalid category. Valid: {sorted(VALID_CATEGORIES)}")
    if body.keywords is not None:
        current_tags["keywords"] = [str(k).strip().lower() for k in body.keywords if k][:10]
    if body.entities is not None:
        entities = current_tags.get("entities", {})
        for entity_type in ("people", "companies", "projects", "technologies", "dates"):
            if entity_type in body.entities:
                entities[entity_type] = [str(v).strip() for v in body.entities[entity_type] if v][:20]
        current_tags["entities"] = entities

    # Save
    m["tags"] = current_tags
    if out_dir.exists():
        _atomic_write(out_dir / "tags.json", json.dumps(current_tags, indent=2))
    _save_index()

    return {"detail": "Tags updated", "tags": current_tags}


# ---------------------------------------------------------------------------
# Notes CRUD
# ---------------------------------------------------------------------------


class NoteCreateRequest(BaseModel):
    type: str = "free"  # "free" or "annotation"
    content: str
    segment_start: Optional[float] = None
    segment_index: Optional[int] = None


class NoteUpdateRequest(BaseModel):
    content: str


def _load_notes(meeting_id: str) -> dict:
    """Load notes.json for a meeting, returning empty structure if missing."""
    m = meetings.get(meeting_id)
    if not m:
        return {"notes": []}
    out_dir = Path(m.get("output_dir", ""))
    notes_path = out_dir / "notes.json"
    if notes_path.exists():
        try:
            return json.loads(notes_path.read_text())
        except Exception:
            pass
    return {"notes": []}


def _save_notes(meeting_id: str, notes_data: dict):
    """Write notes.json atomically for a meeting."""
    m = meetings.get(meeting_id)
    if not m:
        return
    out_dir = Path(m.get("output_dir", ""))
    if out_dir.exists():
        _atomic_write(out_dir / "notes.json", json.dumps(notes_data, indent=2))


@app.get("/meetings/{meeting_id}/notes")
async def get_notes(meeting_id: str):
    """List all notes for a meeting."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return _load_notes(meeting_id)


@app.post("/meetings/{meeting_id}/notes", status_code=201)
async def create_note(meeting_id: str, body: NoteCreateRequest):
    """Create a new note on a meeting."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if body.type not in ("free", "annotation"):
        raise HTTPException(status_code=400, detail="type must be 'free' or 'annotation'")
    if body.type == "annotation" and body.segment_start is None:
        raise HTTPException(status_code=400, detail="Annotations require segment_start")

    now = datetime.now(timezone.utc).isoformat()
    note = {
        "id": f"n_{uuid.uuid4().hex[:8]}",
        "type": body.type,
        "content": body.content,
        "created_at": now,
        "updated_at": now,
    }
    if body.type == "annotation":
        note["segment_start"] = body.segment_start
        note["segment_index"] = body.segment_index

    notes_data = _load_notes(meeting_id)
    notes_data["notes"].append(note)
    _save_notes(meeting_id, notes_data)
    return note


@app.put("/meetings/{meeting_id}/notes/{note_id}")
async def update_note(meeting_id: str, note_id: str, body: NoteUpdateRequest):
    """Update an existing note's content."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    notes_data = _load_notes(meeting_id)
    for note in notes_data["notes"]:
        if note["id"] == note_id:
            note["content"] = body.content
            note["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_notes(meeting_id, notes_data)
            return note
    raise HTTPException(status_code=404, detail="Note not found")


@app.delete("/meetings/{meeting_id}/notes/{note_id}")
async def delete_note(meeting_id: str, note_id: str):
    """Delete a note."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    notes_data = _load_notes(meeting_id)
    original_len = len(notes_data["notes"])
    notes_data["notes"] = [n for n in notes_data["notes"] if n["id"] != note_id]
    if len(notes_data["notes"]) == original_len:
        raise HTTPException(status_code=404, detail="Note not found")
    _save_notes(meeting_id, notes_data)
    return {"detail": "Note deleted"}


def _score_related_meetings(meeting_id: str, min_score: float = 0, limit: int = 5) -> list[dict]:
    """Score all other completed meetings against a given meeting by shared tags/keywords/entities.

    Returns a sorted list (highest score first) of dicts with meeting_id, title, date,
    category, score, shared_keywords, and shared_entities.
    """
    if meeting_id not in meetings:
        return []

    m = meetings[meeting_id]
    m_tags = m.get("tags", {})
    m_keywords = set(m_tags.get("keywords", []))
    m_entities = set()
    for entity_list in m_tags.get("entities", {}).values():
        if isinstance(entity_list, list):
            m_entities.update(str(e).lower() for e in entity_list)
    m_category = m_tags.get("category", "")

    if not m_keywords and not m_entities and not m_category:
        return []

    scored = []
    for other_id, other in meetings.items():
        if other_id == meeting_id:
            continue
        if other.get("status") != MeetingStatus.complete:
            continue

        o_tags = other.get("tags", {})
        score = 0.0

        # Category match
        if m_category and o_tags.get("category") == m_category:
            score += 2.0

        # Keyword overlap
        o_keywords = set(o_tags.get("keywords", []))
        keyword_overlap = m_keywords & o_keywords
        score += len(keyword_overlap) * 1.5

        # Entity overlap
        o_entities = set()
        for entity_list in o_tags.get("entities", {}).values():
            if isinstance(entity_list, list):
                o_entities.update(str(e).lower() for e in entity_list)
        entity_overlap = m_entities & o_entities
        score += len(entity_overlap) * 1.0

        if score >= min_score and score > 0:
            scored.append({
                "meeting_id": other_id,
                "title": other.get("title", "Meeting"),
                "date": other.get("date", ""),
                "category": o_tags.get("category", ""),
                "score": round(score, 2),
                "shared_keywords": sorted(keyword_overlap),
                "shared_entities": sorted(entity_overlap),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def _auto_compute_link_suggestions(meeting_id: str):
    """Compute and store link suggestions for a meeting. Non-fatal."""
    if meeting_id not in meetings:
        return

    m = meetings[meeting_id]
    if "links" not in m:
        m["links"] = {"manual": [], "suggestions": []}

    links = m["links"]
    manual_ids = set(links.get("manual", []))
    dismissed_ids = {
        s["meeting_id"] for s in links.get("suggestions", [])
        if s.get("status") == "dismissed"
    }
    exclude_ids = manual_ids | dismissed_ids

    scored = _score_related_meetings(meeting_id, min_score=2.0, limit=10)
    new_suggestions = []
    for item in scored:
        if item["meeting_id"] in exclude_ids:
            continue
        new_suggestions.append({
            "meeting_id": item["meeting_id"],
            "score": item["score"],
            "shared_keywords": item.get("shared_keywords", []),
            "shared_entities": item.get("shared_entities", []),
            "status": "pending",
        })
        if len(new_suggestions) >= 5:
            break

    # Preserve dismissed/accepted suggestions, replace pending ones
    kept = [s for s in links.get("suggestions", []) if s.get("status") != "pending"]
    links["suggestions"] = kept + new_suggestions
    _save_index()
    logger.info(f"[{meeting_id}] Auto-link: {len(new_suggestions)} suggestions computed")


@app.get("/meetings/{meeting_id}/related")
async def get_related_meetings(meeting_id: str, limit: int = Query(default=5)):
    """Find meetings related by shared tags, keywords, and entities."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return _score_related_meetings(meeting_id, min_score=0, limit=limit)


@app.get("/meetings/{meeting_id}/related-notes")
async def api_meeting_related_notes(meeting_id: str, limit: int = 5):
    m = meetings.get(meeting_id)
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    try:
        summary = m.get("summary") or {}
        query = (m.get("title", "") + "\n" + (summary.get("summary") or ""))[:4000]
        hits = notes_vectors.search_notes(get_qdrant(), get_embedder(), query,
                                          collection=NOTES_COLLECTION, dim=EMBEDDING_DIM, limit=limit * 3)
        seen, out = set(), []
        for h in hits:
            nid = h.get("note_id")
            if not nid or nid in seen:
                continue
            if h.get("score") is not None and h["score"] < RELATED_MIN_SCORE:
                continue
            seen.add(nid); out.append({"note_id": nid, "title": h.get("title", ""),
                                       "folder": h.get("folder", ""), "score": h.get("score")})
            if len(out) >= limit:
                break
        return {"related": out}
    except Exception as e:
        logger.warning(f"meeting related-notes failed (non-fatal): {e}")
        return {"related": []}


# ---------------------------------------------------------------------------
# Meeting Links API
# ---------------------------------------------------------------------------


class LinkRequest(BaseModel):
    target_meeting_id: str


@app.get("/meetings/{meeting_id}/links")
async def get_meeting_links(meeting_id: str):
    """Return manual links (enriched with title/date) and pending suggestions."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]
    links = m.get("links", {"manual": [], "suggestions": []})

    # Enrich manual links with metadata
    enriched_manual = []
    for linked_id in links.get("manual", []):
        if linked_id in meetings:
            other = meetings[linked_id]
            enriched_manual.append({
                "meeting_id": linked_id,
                "title": other.get("title", "Meeting"),
                "date": other.get("date", ""),
                "status": other.get("status", ""),
                "duration_formatted": other.get("duration_formatted", ""),
            })

    # Return suggestions (filter to pending only)
    pending_suggestions = [
        s for s in links.get("suggestions", [])
        if s.get("status") == "pending"
    ]

    return {
        "manual": enriched_manual,
        "suggestions": pending_suggestions,
    }


async def _auto_generate_insights(meeting_id: str):
    """Background task: auto-generate cross-meeting insights after linking."""
    try:
        cluster = _get_linked_cluster(meeting_id)
        completed = [mid for mid in cluster if meetings.get(mid, {}).get("status") == MeetingStatus.complete]
        if len(completed) < 2:
            return

        # Debounce: skip if an insight was generated in the last 60 seconds
        d = Path(meetings[meeting_id].get("output_dir", "")) / "insights"
        if d.exists():
            for f in d.glob("ins_*.json"):
                try:
                    entry = json.loads(f.read_text())
                    ts = datetime.fromisoformat(entry.get("timestamp", "2000-01-01"))
                    if (datetime.now() - ts).total_seconds() < 60:
                        logger.info(f"[{meeting_id}] Skipping auto-insights — recent insight exists")
                        return
                except Exception:
                    pass

        completed.sort(key=lambda mid: meetings[mid].get("date", ""))
        await _generate_and_store_insight(
            meeting_id, completed,
            label="Auto-generated (link added)",
            trigger="auto_link",
        )
    except Exception as e:
        logger.error(f"[{meeting_id}] Auto-insights generation failed: {e}")


@app.post("/meetings/{meeting_id}/links")
async def add_meeting_link(meeting_id: str, body: LinkRequest):
    """Add a bidirectional manual link between two meetings."""
    target_id = body.target_meeting_id
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if target_id not in meetings:
        raise HTTPException(status_code=404, detail="Target meeting not found")
    if meeting_id == target_id:
        raise HTTPException(status_code=400, detail="Cannot link a meeting to itself")

    # Ensure links structures exist
    for mid in (meeting_id, target_id):
        if "links" not in meetings[mid]:
            meetings[mid]["links"] = {"manual": [], "suggestions": []}

    src_links = meetings[meeting_id]["links"]
    tgt_links = meetings[target_id]["links"]

    # Add bidirectional manual link (idempotent)
    if target_id not in src_links["manual"]:
        src_links["manual"].append(target_id)
    if meeting_id not in tgt_links["manual"]:
        tgt_links["manual"].append(meeting_id)

    # If target was a pending suggestion, mark it accepted
    for s in src_links.get("suggestions", []):
        if s.get("meeting_id") == target_id and s.get("status") == "pending":
            s["status"] = "accepted"
    for s in tgt_links.get("suggestions", []):
        if s.get("meeting_id") == meeting_id and s.get("status") == "pending":
            s["status"] = "accepted"

    _save_index()
    # Auto-trigger cross-meeting insights in background
    asyncio.create_task(_auto_generate_insights(meeting_id))
    return {"detail": f"Linked {meeting_id} <-> {target_id}"}


@app.delete("/meetings/{meeting_id}/links/{target_id}")
async def remove_meeting_link(meeting_id: str, target_id: str):
    """Remove a bidirectional manual link between two meetings."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Remove from source
    src_links = meetings[meeting_id].get("links", {"manual": [], "suggestions": []})
    if target_id in src_links["manual"]:
        src_links["manual"].remove(target_id)

    # Remove from target (if it still exists)
    if target_id in meetings:
        tgt_links = meetings[target_id].get("links", {"manual": [], "suggestions": []})
        if meeting_id in tgt_links["manual"]:
            tgt_links["manual"].remove(meeting_id)

    _save_index()
    return {"detail": f"Unlinked {meeting_id} <-> {target_id}"}


@app.post("/meetings/{meeting_id}/links/suggestions/{target_id}/dismiss")
async def dismiss_link_suggestion(meeting_id: str, target_id: str):
    """Dismiss a link suggestion."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    links = meetings[meeting_id].get("links", {"manual": [], "suggestions": []})
    found = False
    for s in links.get("suggestions", []):
        if s.get("meeting_id") == target_id and s.get("status") == "pending":
            s["status"] = "dismissed"
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail="Pending suggestion not found")

    _save_index()
    return {"detail": f"Dismissed suggestion {target_id} for meeting {meeting_id}"}


# ---------------------------------------------------------------------------
# Cross-Meeting Insights
# ---------------------------------------------------------------------------


class InsightsRequest(BaseModel):
    custom_prompt: Optional[str] = None  # optional user instructions to focus the analysis
    meeting_ids: Optional[list[str]] = None  # if None, uses linked cluster


def _insights_dir(meeting_id: str) -> Path:
    """Return the insights directory for a meeting, creating it if needed."""
    out_dir = Path(meetings[meeting_id].get("output_dir", ""))
    d = out_dir / "insights"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_legacy_insights(meeting_id: str):
    """Move old cross_meeting_insights.json into the new insights/ dir."""
    out_dir = Path(meetings[meeting_id].get("output_dir", ""))
    legacy = out_dir / "cross_meeting_insights.json"
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text())
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        entry = {
            "id": f"ins_{ts}",
            "timestamp": datetime.now().isoformat(),
            "label": "General (migrated)",
            "trigger": "manual",
            "meeting_ids": _get_linked_cluster(meeting_id),
            "meetings_analyzed": len(_get_linked_cluster(meeting_id)),
            "insights": data,
        }
        d = _insights_dir(meeting_id)
        _atomic_write(d / f"{entry['id']}.json", json.dumps(entry, indent=2))
        legacy.unlink()
        logger.info(f"[{meeting_id}] Migrated legacy insights to {entry['id']}")
    except Exception as e:
        logger.warning(f"[{meeting_id}] Failed to migrate legacy insights: {e}")


def _list_insights(meeting_id: str) -> list[dict]:
    """List all insight entries for a meeting (metadata only), newest first."""
    out_dir = Path(meetings[meeting_id].get("output_dir", ""))
    d = out_dir / "insights"
    if not d.exists():
        return []
    results = []
    for f in sorted(d.glob("ins_*.json"), reverse=True):
        try:
            entry = json.loads(f.read_text())
            results.append({
                "id": entry["id"],
                "timestamp": entry.get("timestamp", ""),
                "label": entry.get("label", "General"),
                "trigger": entry.get("trigger", "manual"),
                "meetings_analyzed": entry.get("meetings_analyzed", 0),
            })
        except Exception:
            pass
    return results


def _build_meeting_context(target_ids: list[str]) -> str:
    """Build combined context string from multiple meetings for insights prompt."""
    meeting_contexts = []
    for mid in target_ids:
        m = meetings[mid]
        if m.get("status") != MeetingStatus.complete:
            continue
        out_dir = Path(m.get("output_dir", ""))
        if not out_dir.exists():
            continue

        ctx = f"## Meeting: {m.get('title', 'Untitled')} ({m.get('date', 'unknown date')})\n"

        summary_path = out_dir / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text())
                ctx += f"**Summary:** {summary.get('summary', '')}\n"
                topics = summary.get("topics", [])
                if topics:
                    ctx += "**Topics:**\n"
                    for t in topics:
                        ctx += f"- {t.get('topic', '')}: {t.get('summary', '')} (Outcome: {t.get('outcome', 'none')})\n"
                actions = summary.get("action_items", [])
                if actions:
                    ctx += "**Action Items:**\n"
                    for a in actions:
                        ctx += f"- [{a.get('priority', '?')}] {a.get('task', '')} → {a.get('who', 'TBD')} (deadline: {a.get('deadline', 'none')})\n"
                decisions = summary.get("decisions", [])
                if decisions:
                    ctx += "**Decisions:**\n"
                    for d_item in decisions:
                        ctx += f"- {d_item.get('decision', '')} ({d_item.get('context', '')})\n"
                open_qs = summary.get("open_questions", [])
                if open_qs:
                    ctx += "**Open Questions:**\n"
                    for q in open_qs:
                        ctx += f"- {q.get('question', '')} (asked by: {q.get('asked_by', 'unknown')})\n"
                concerns = summary.get("concerns", [])
                if concerns:
                    ctx += "**Concerns/Risks:**\n"
                    for c in concerns:
                        status = "resolved" if c.get("resolved") else "open"
                        ctx += f"- [{status}] {c.get('concern', '')} (raised by: {c.get('raised_by', 'unknown')})\n"
            except Exception:
                pass

        tags = m.get("tags", {})
        if tags:
            kws = tags.get("keywords", [])
            if kws:
                ctx += f"**Keywords:** {', '.join(kws)}\n"

        ctx += "\n"
        meeting_contexts.append(ctx)
    return "\n".join(meeting_contexts)


async def _generate_and_store_insight(
    meeting_id: str,
    target_ids: list[str],
    label: str = "General",
    trigger: str = "manual",
    custom_prompt: Optional[str] = None,
) -> dict:
    """Core insights generation. Returns the stored entry dict."""
    combined_context = _build_meeting_context(target_ids)
    custom_instruction = ""
    if custom_prompt:
        custom_instruction = f"\n\nADDITIONAL INSTRUCTIONS FROM USER:\n{custom_prompt}\n"

    prompt = (
        f"You are analyzing {len(target_ids)} related meetings to produce cross-meeting insights.\n\n"
        f"MEETING DATA:\n{combined_context}\n"
        f"{custom_instruction}\n"
        "Based on ALL these meetings together, provide a comprehensive cross-meeting analysis.\n\n"
        "Respond ONLY with valid JSON, no other text:\n"
        "{\n"
        '  "executive_summary": "3-5 sentence high-level summary spanning all meetings",\n'
        '  "recurring_themes": [{"theme": "Theme name", "details": "How it appeared across meetings", "meetings": ["meeting titles"]}],\n'
        '  "progress_tracking": [{"item": "Action item or topic", "status": "completed/in_progress/stalled/dropped", "history": "How it evolved across meetings"}],\n'
        '  "unresolved_items": [{"item": "Open question, concern, or action", "first_raised": "Which meeting", "current_status": "Latest status"}],\n'
        '  "key_relationships": [{"description": "Connection or dependency between meetings"}],\n'
        '  "recommendations": [{"recommendation": "Suggested next step based on patterns across meetings"}]\n'
        "}"
    )

    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)

    resp = await _retry_ollama_call(
        "POST",
        f"{OLLAMA_URL}/api/generate",
        json_body=_build_generate_body(
            # helper sets think:false + a bounded num_ctx for consistency.
            model, prompt, temperature=temperature, num_predict=4096,
        ),
        timeout_seconds=300.0,
    )
    raw = resp.json().get("response", "")
    insights = _parse_json_object(raw)

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    entry = {
        "id": f"ins_{ts}",
        "timestamp": datetime.now().isoformat(),
        "label": label,
        "trigger": trigger,
        "meeting_ids": target_ids,
        "meetings_analyzed": len(target_ids),
        "insights": insights,
    }
    d = _insights_dir(meeting_id)
    _atomic_write(d / f"{entry['id']}.json", json.dumps(entry, indent=2))
    logger.info(f"[{meeting_id}] Insights '{entry['id']}' generated across {len(target_ids)} meetings")
    return entry


@app.post("/meetings/{meeting_id}/insights")
async def generate_cross_meeting_insights(meeting_id: str, body: InsightsRequest):
    """Generate insights across linked meetings. Each call creates a NEW insight entry."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if body.meeting_ids:
        target_ids = [mid for mid in body.meeting_ids if mid in meetings]
    else:
        target_ids = _get_linked_cluster(meeting_id)

    if len(target_ids) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 linked meetings to generate cross-meeting insights")

    target_ids.sort(key=lambda mid: meetings[mid].get("date", ""))

    label = f"Custom: {body.custom_prompt[:60]}" if body.custom_prompt else "General"

    try:
        entry = await _generate_and_store_insight(meeting_id, target_ids, label=label, trigger="manual", custom_prompt=body.custom_prompt)
    except Exception as e:
        logger.error(f"Cross-meeting insights failed: {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}")

    return entry


@app.get("/meetings/{meeting_id}/insights")
async def get_cross_meeting_insights(meeting_id: str, insight_id: Optional[str] = None):
    """Get insights. Without insight_id: returns list. With insight_id: returns full detail."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    _migrate_legacy_insights(meeting_id)

    if insight_id:
        _validate_artifact_id(insight_id, kind="insight_id")
        d = _insights_dir(meeting_id)
        path = d / f"{insight_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Insight not found")
        return json.loads(path.read_text())

    return {"insights": _list_insights(meeting_id)}


@app.delete("/meetings/{meeting_id}/insights/{insight_id}")
async def delete_insight(meeting_id: str, insight_id: str):
    """Delete a specific insight entry."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    _validate_artifact_id(insight_id, kind="insight_id")
    d = _insights_dir(meeting_id)
    path = d / f"{insight_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Insight not found")
    path.unlink()
    logger.info(f"[{meeting_id}] Deleted insight {insight_id}")
    return {"detail": f"Deleted {insight_id}"}


@app.get("/meetings/{meeting_id}/speakers")
async def get_speakers(meeting_id: str):
    """Get speaker identification info for a meeting."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    out_dir = Path(m.get("output_dir", ""))

    # Try rich speaker_info first, fall back to flat speaker_map
    speaker_info_path = out_dir / "speaker_info.json"
    if speaker_info_path.exists():
        speaker_info = json.loads(speaker_info_path.read_text())
        return {"speaker_info": speaker_info}

    speaker_map_path = out_dir / "speaker_map.json"
    if speaker_map_path.exists():
        speaker_map = json.loads(speaker_map_path.read_text())
        # Convert flat map to speaker_info format
        speaker_info = {}
        for label, name in speaker_map.items():
            speaker_info[label] = {
                "name": name,
                "title": "",
                "company": "",
                "display_name": name,
                "confidence": "manual",
                "auto_detected": False,
            }
        return {"speaker_info": speaker_info}

    return {"speaker_info": {}}


class SpeakerMapRequest(BaseModel):
    speaker_map: dict[str, str]


@app.put("/meetings/{meeting_id}/speakers")
async def update_speakers(meeting_id: str, body: SpeakerMapRequest):
    """Rename speakers in a completed meeting's transcript and summary files."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    out_dir = Path(m.get("output_dir", ""))
    if not out_dir.exists():
        raise HTTPException(status_code=404, detail="Meeting output directory not found")

    incoming_map = body.speaker_map  # may have display-name keys OR SPEAKER_XX keys

    # Load existing speaker_map.json to resolve display-name keys to original labels
    existing_map_path = out_dir / "speaker_map.json"
    existing_map = {}
    if existing_map_path.exists():
        try:
            existing_map = json.loads(existing_map_path.read_text())
        except Exception:
            pass
    # reverse: {"Alex": "SPEAKER_00", ...}
    reverse_map = {v: k for k, v in existing_map.items()}

    # Build canonical speaker_map: SPEAKER_XX -> new_name
    canonical_map = {}
    for key, new_name in incoming_map.items():
        if key.startswith("SPEAKER_"):
            # Already an original label
            canonical_map[key] = new_name
        elif key in reverse_map:
            # Display-name key — resolve to original label
            canonical_map[reverse_map[key]] = new_name
        else:
            # Unknown key — store as-is (best effort)
            canonical_map[key] = new_name

    # Save canonical speaker_map.json (SPEAKER_XX -> name)
    _atomic_write(existing_map_path, json.dumps(canonical_map, indent=2))

    # Update transcript.json — revert to SPEAKER_XX labels then apply new names
    transcript_path = out_dir / "transcript.json"
    if transcript_path.exists():
        transcript_data = json.loads(transcript_path.read_text())
        segments = transcript_data.get("segments", [])
        # Revert any previously applied display names back to SPEAKER_XX
        for seg in segments:
            sp = seg.get("speaker", "")
            if sp in reverse_map:
                seg["speaker"] = reverse_map[sp]
        # Apply new names from canonical map
        for seg in segments:
            original = seg.get("speaker", "")
            if original in canonical_map:
                seg["speaker"] = canonical_map[original]
        _atomic_write(transcript_path, json.dumps(transcript_data, indent=2))

        # Regenerate transcript.srt with new names
        _atomic_write(
            out_dir / "transcript.srt",
            _generate_srt(segments),
        )

        # Regenerate transcript.md with new names
        _atomic_write(
            out_dir / "transcript.md",
            build_transcript_markdown(segments, m),
        )

    # Update summary.json — replace speaker labels/names in all fields
    summary_path = out_dir / "summary.json"
    if summary_path.exists():
        summary_data = json.loads(summary_path.read_text())
        # Build a combined replacement map: SPEAKER_XX -> new_name AND old_name -> new_name
        replace_map = {}
        for label, new_name in canonical_map.items():
            replace_map[label] = new_name  # SPEAKER_XX -> new_name
            old_name = existing_map.get(label)
            if old_name and old_name != new_name:
                replace_map[old_name] = new_name  # old display name -> new_name

        def _apply(text):
            """Replace speaker labels and old names in text."""
            if not text:
                return text
            for old, new in replace_map.items():
                text = text.replace(old, new)
            return text

        # Summary text
        for key in ("summary", "executive_summary"):
            if key in summary_data and summary_data[key]:
                summary_data[key] = _apply(summary_data[key])
        # Topics
        for t in summary_data.get("topics", []):
            t["summary"] = _apply(t.get("summary", ""))
        # Action items
        for a in summary_data.get("action_items", []):
            for field in ("who", "assigned_to"):
                a[field] = _apply(a.get(field, ""))
        # Decisions
        for d in summary_data.get("decisions", []):
            d["context"] = _apply(d.get("context", ""))
        # Open questions
        for q in summary_data.get("open_questions", []):
            q["asked_by"] = _apply(q.get("asked_by", ""))
        # Concerns & Risks
        for c in summary_data.get("concerns", []):
            c["raised_by"] = _apply(c.get("raised_by", ""))
            c["concern"] = _apply(c.get("concern", ""))
            c["notes"] = _apply(c.get("notes", ""))
        # Key Figures & Dates
        for f in summary_data.get("figures", []):
            f["said_by"] = _apply(f.get("said_by", ""))
            f["context"] = _apply(f.get("context", ""))
        # Sentiment notable moments
        sentiment = summary_data.get("sentiment")
        if isinstance(sentiment, dict):
            for i, moment in enumerate(sentiment.get("notable_moments", [])):
                if isinstance(moment, dict):
                    moment["moment"] = _apply(moment.get("moment", ""))
                elif isinstance(moment, str):
                    sentiment["notable_moments"][i] = _apply(moment)

        _atomic_write(summary_path, json.dumps(summary_data, indent=2))

        # Regenerate summary.md with new names
        summary_md = build_summary_markdown(summary_data, m)
        _atomic_write(out_dir / "summary.md", summary_md)

    # Update speaker_info.json — always keyed by SPEAKER_XX
    speaker_info_path = out_dir / "speaker_info.json"
    if speaker_info_path.exists():
        try:
            speaker_info = json.loads(speaker_info_path.read_text())
            for original_label, new_name in canonical_map.items():
                if original_label in speaker_info:
                    speaker_info[original_label]["name"] = new_name
                    speaker_info[original_label]["auto_detected"] = False
                    # Rebuild display_name
                    title = speaker_info[original_label].get("title", "")
                    company = speaker_info[original_label].get("company", "")
                    parts = []
                    if title:
                        parts.append(title)
                    if company:
                        parts.append(company)
                    speaker_info[original_label]["display_name"] = (
                        f"{new_name} ({', '.join(parts)})" if parts else new_name
                    )
                else:
                    speaker_info[original_label] = {
                        "name": new_name,
                        "title": "",
                        "company": "",
                        "display_name": new_name,
                        "confidence": "manual",
                        "auto_detected": False,
                    }
            _atomic_write(speaker_info_path, json.dumps(speaker_info, indent=2))
        except Exception as e:
            logger.warning(f"Failed to update speaker_info.json for {meeting_id}: {e}")

    # Store canonical mapping in memory
    m["speaker_map"] = canonical_map
    _save_index()

    return {"detail": "Speaker names updated", "speaker_map": canonical_map}


class MergeSpeakersRequest(BaseModel):
    speakers: list[str]  # list of speaker names/labels to merge
    target: str  # the name to keep (or new name)


@app.post("/meetings/{meeting_id}/speakers/merge")
async def merge_speakers(meeting_id: str, body: MergeSpeakersRequest):
    """Merge multiple speaker labels into one. Use when the system identified
    one person as two or more separate speakers."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    out_dir = Path(m.get("output_dir", ""))
    if not out_dir.exists():
        raise HTTPException(status_code=404, detail="Meeting output directory not found")

    if len(body.speakers) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 speakers to merge")
    if body.target not in body.speakers:
        # target can be a new name — that's fine
        pass

    sources = set(body.speakers) - {body.target}
    target = body.target
    merged_count = 0

    # Update transcript.json
    transcript_path = out_dir / "transcript.json"
    if transcript_path.exists():
        transcript_data = json.loads(transcript_path.read_text())
        segments = transcript_data.get("segments", [])
        for seg in segments:
            if seg.get("speaker") in sources:
                seg["speaker"] = target
                merged_count += 1
        _atomic_write(transcript_path, json.dumps(transcript_data, indent=2))
        _atomic_write(out_dir / "transcript.srt", _generate_srt(segments))
        _atomic_write(out_dir / "transcript.md", build_transcript_markdown(segments, m))

    # Update speaker_map.json
    speaker_map_path = out_dir / "speaker_map.json"
    if speaker_map_path.exists():
        speaker_map = json.loads(speaker_map_path.read_text())
        for label, name in list(speaker_map.items()):
            if name in sources:
                speaker_map[label] = target
        _atomic_write(speaker_map_path, json.dumps(speaker_map, indent=2))
        m["speaker_map"] = speaker_map

    # Update speaker_info.json — remove merged speakers
    speaker_info_path = out_dir / "speaker_info.json"
    if speaker_info_path.exists():
        try:
            speaker_info = json.loads(speaker_info_path.read_text())
            for label, info in list(speaker_info.items()):
                if info.get("name") in sources or info.get("display_name") in sources:
                    # Point this to the target
                    info["name"] = target
                    info["display_name"] = target
                    info["auto_detected"] = False
            _atomic_write(speaker_info_path, json.dumps(speaker_info, indent=2))
        except Exception as e:
            logger.warning(f"Failed to update speaker_info.json for merge: {e}")

    # Update summary — replace merged speaker names in text
    summary_path = out_dir / "summary.json"
    if summary_path.exists():
        summary_text = summary_path.read_text()
        for src in sources:
            summary_text = summary_text.replace(src, target)
        _atomic_write(summary_path, summary_text)
        try:
            summary_data = json.loads(summary_text)
            _atomic_write(out_dir / "summary.md", build_summary_markdown(summary_data, m))
        except Exception:
            pass

    _save_index()
    logger.info(f"[{meeting_id}] Merged speakers {list(sources)} -> {target} ({merged_count} segments)")
    return {"detail": f"Merged {list(sources)} into {target}", "segments_changed": merged_count}


class ReassignSegmentsRequest(BaseModel):
    segment_indices: list[int]  # indices into the transcript segments array
    new_speaker: str  # speaker name to assign these segments to


@app.post("/meetings/{meeting_id}/speakers/reassign")
async def reassign_segments(meeting_id: str, body: ReassignSegmentsRequest):
    """Reassign specific transcript segments to a different speaker. Use when
    the system grouped segments under the wrong speaker, or to split one
    speaker into two by reassigning some of their segments."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    out_dir = Path(m.get("output_dir", ""))
    if not out_dir.exists():
        raise HTTPException(status_code=404, detail="Meeting output directory not found")

    if not body.segment_indices:
        raise HTTPException(status_code=400, detail="No segment indices provided")

    transcript_path = out_dir / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=404, detail="Transcript not found")

    transcript_data = json.loads(transcript_path.read_text())
    segments = transcript_data.get("segments", [])
    changed = 0

    for idx in body.segment_indices:
        if 0 <= idx < len(segments):
            if segments[idx].get("speaker") != body.new_speaker:
                segments[idx]["speaker"] = body.new_speaker
                changed += 1

    _atomic_write(transcript_path, json.dumps(transcript_data, indent=2))
    _atomic_write(out_dir / "transcript.srt", _generate_srt(segments))
    _atomic_write(out_dir / "transcript.md", build_transcript_markdown(segments, m))

    _save_index()
    logger.info(f"[{meeting_id}] Reassigned {changed} segments to {body.new_speaker}")
    return {"detail": f"Reassigned {changed} segments to {body.new_speaker}", "segments_changed": changed}


# ---------------------------------------------------------------------------
# Retry & Reprocess endpoints (Phase 4)
# ---------------------------------------------------------------------------


@app.post("/meetings/{meeting_id}/retry")
async def retry_meeting(meeting_id: str):
    """Retry processing for a failed meeting from scratch."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.error:
        raise HTTPException(status_code=409, detail="Only error-status meetings can be retried")

    # Check if original audio still exists
    original_path = m.get("original_path", "")
    if not original_path or not os.path.exists(original_path):
        # Try to find audio in output dir
        out_dir = m.get("output_dir")
        if out_dir:
            out_path = Path(out_dir)
            audio_files = list(out_path.glob("audio.*"))
            if audio_files:
                original_path = str(audio_files[0])
                m["original_path"] = original_path
            else:
                raise HTTPException(status_code=404, detail="Original audio file not found")
        else:
            raise HTTPException(status_code=404, detail="Original audio file not found")

    # Clear error state
    m["status"] = MeetingStatus.queued
    m["error"] = None
    m["progress_percent"] = 0
    m["progress_detail"] = "Queued for retry"
    m["step_timings"] = {}
    _save_index()

    logger.info(f"[{meeting_id}] Retrying processing")

    # Start processing in background
    task = asyncio.create_task(process_meeting(meeting_id))
    m["_task"] = task

    return {"detail": f"Meeting {meeting_id} queued for retry", "status": "queued"}


class ReprocessRequest(BaseModel):
    step: str  # "cleanup", "identify_speakers", or "summarize"


@app.post("/meetings/{meeting_id}/reprocess")
async def reprocess_meeting(meeting_id: str, body: ReprocessRequest):
    """Reprocess a specific step for a completed meeting."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail="Only completed meetings can be reprocessed")

    out_dir = Path(m.get("output_dir", ""))
    if not out_dir.exists():
        raise HTTPException(status_code=404, detail="Meeting output directory not found")

    step = body.step
    if step not in ("cleanup", "identify_speakers", "summarize", "tagging"):
        raise HTTPException(status_code=400, detail="Invalid step. Allowed: cleanup, identify_speakers, summarize, tagging")

    logger.info(f"[{meeting_id}] Reprocessing step: {step}")

    async def _run_reprocess():
        try:
            if step == "cleanup":
                # Read raw transcript if available, else current transcript
                raw_path = out_dir / "raw_transcript.json"
                transcript_path = out_dir / "transcript.json"
                source_path = raw_path if raw_path.exists() else transcript_path

                data = json.loads(source_path.read_text())
                segments = data.get("segments", [])

                m["status"] = MeetingStatus.cleaning_transcript
                _update_progress(m, 42, "Re-cleaning transcript...")
                _save_index()

                cleaned = await step_cleanup_transcript(segments, m.get("meeting_context"))

                # Check if changes were made
                changes = sum(1 for a, b in zip(segments, cleaned) if a["text"] != b["text"])
                if changes > 0:
                    transcript_text = build_transcript_text(cleaned)
                    transcript_data = {**data, "segments": cleaned, "cleaned": True}
                    _atomic_write(transcript_path, json.dumps(transcript_data, indent=2))
                    _atomic_write(out_dir / "transcript.srt", _generate_srt(cleaned))
                    _atomic_write(out_dir / "transcript.md", build_transcript_markdown(cleaned, m))
                    m["transcript_cleaned"] = True

            elif step == "identify_speakers":
                transcript_path = out_dir / "transcript.json"
                raw_transcript_path = out_dir / "raw_transcript.json"

                # Prefer raw_transcript.json which has original SPEAKER_XX labels
                if raw_transcript_path.exists():
                    raw_data = json.loads(raw_transcript_path.read_text())
                    raw_segments = raw_data.get("segments", [])
                    # Check if labels are still original (SPEAKER_XX pattern)
                    sample_speaker = next((s.get("speaker", "") for s in raw_segments if s.get("speaker")), "")
                    if sample_speaker.startswith("SPEAKER_"):
                        source_segments = raw_segments
                    else:
                        # Legacy: raw_transcript was already corrupted, reverse-map from speaker_map.json
                        source_segments = raw_segments
                        old_map_path = out_dir / "speaker_map.json"
                        if old_map_path.exists():
                            old_map = json.loads(old_map_path.read_text())
                            reverse_map = {v: k for k, v in old_map.items()}
                            for seg in source_segments:
                                sp = seg.get("speaker", "")
                                if sp in reverse_map:
                                    seg["speaker"] = reverse_map[sp]
                else:
                    # No raw_transcript.json at all, fall back to transcript.json with reverse-map
                    data = json.loads(transcript_path.read_text())
                    source_segments = data.get("segments", [])
                    old_map_path = out_dir / "speaker_map.json"
                    if old_map_path.exists():
                        old_map = json.loads(old_map_path.read_text())
                        reverse_map = {v: k for k, v in old_map.items()}
                        for seg in source_segments:
                            sp = seg.get("speaker", "")
                            if sp in reverse_map:
                                seg["speaker"] = reverse_map[sp]

                transcript_text = build_transcript_text(source_segments)

                m["status"] = MeetingStatus.identifying_speakers
                _update_progress(m, 60, "Re-identifying speakers...")
                _save_index()

                identification = await step_identify_speakers(transcript_text, source_segments)
                speaker_map = identification.get("speaker_map", {})
                speaker_info = identification.get("speaker_info", {})

                if speaker_map:
                    # Read the current transcript.json for updating with new names
                    transcript_data = json.loads(transcript_path.read_text())
                    display_segments = transcript_data.get("segments", [])

                    # Build reverse map from old speaker_map to undo previous names
                    old_map_path = out_dir / "speaker_map.json"
                    if old_map_path.exists():
                        old_map = json.loads(old_map_path.read_text())
                        old_reverse = {v: k for k, v in old_map.items()}
                        # Revert display segments back to SPEAKER_XX labels first
                        for seg in display_segments:
                            sp = seg.get("speaker", "")
                            if sp in old_reverse:
                                seg["speaker"] = old_reverse[sp]

                    # Now apply the new speaker_map
                    for seg in display_segments:
                        original = seg.get("speaker", "")
                        if original in speaker_map:
                            seg["speaker"] = speaker_map[original]
                    _atomic_write(transcript_path, json.dumps({**transcript_data, "segments": display_segments}, indent=2))
                    _atomic_write(out_dir / "transcript.srt", _generate_srt(display_segments))
                    _atomic_write(out_dir / "transcript.md", build_transcript_markdown(display_segments, m))
                    if speaker_info:
                        _atomic_write(out_dir / "speaker_info.json", json.dumps(speaker_info, indent=2))
                    _atomic_write(out_dir / "speaker_map.json", json.dumps(speaker_map, indent=2))
                    m["speaker_map"] = speaker_map
                    m["speaker_info"] = speaker_info

            elif step == "summarize":
                transcript_path = out_dir / "transcript.json"
                data = json.loads(transcript_path.read_text())
                segments = data.get("segments", [])
                transcript_text = build_transcript_text(segments)
                duration = m.get("duration", 0)

                m["status"] = MeetingStatus.summarizing
                _update_progress(m, 72, "Re-summarizing...")
                _save_index()

                summary = await step_summarize(transcript_text, duration)
                if "title" in summary and summary["title"] != "Meeting":
                    m["title"] = summary["title"]

                _atomic_write(out_dir / "summary.json", json.dumps(summary, indent=2))
                summary_md = build_summary_markdown(summary, m)
                _atomic_write(out_dir / "summary.md", summary_md)
                m["summary"] = summary

            elif step == "tagging":
                transcript_path = out_dir / "transcript.json"
                data = json.loads(transcript_path.read_text())
                segments = data.get("segments", [])
                transcript_text = build_transcript_text(segments)

                m["status"] = MeetingStatus.tagging
                _update_progress(m, 84, "Re-tagging...")
                _save_index()

                tags = await step_auto_tag(transcript_text)
                m["tags"] = tags
                _atomic_write(out_dir / "tags.json", json.dumps(tags, indent=2))

            m["status"] = MeetingStatus.complete
            _update_progress(m, 100, "Complete")
            logger.info(f"[{meeting_id}] Reprocessing step '{step}' complete")

            # Re-compute link suggestions after reprocessing (non-fatal)
            try:
                _auto_compute_link_suggestions(meeting_id)
            except Exception as e2:
                logger.warning(f"[{meeting_id}] Auto-linking after reprocess failed (non-fatal): {e2}")

        except Exception as e:
            m["status"] = MeetingStatus.complete  # Restore to complete even on failure
            m["reprocess_error"] = f"{step}: {str(e)}"
            _save_index()
            logger.error(f"[{meeting_id}] Reprocessing step '{step}' failed: {e}")

    # Run in background
    asyncio.create_task(_run_reprocess())
    return {"detail": f"Reprocessing '{step}' for meeting {meeting_id}", "status": "processing"}


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------


class ChatSettingsRequest(BaseModel):
    endpoint: Optional[str] = None
    custom_url: Optional[str] = None
    custom_api_key: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_context_chunks: Optional[int] = None


class SettingsRequest(BaseModel):
    prompts: Optional[dict[str, str]] = None
    ollama_model: Optional[str] = None
    temperature: Optional[float] = None
    chat: Optional[ChatSettingsRequest] = None


@app.get("/api/settings")
async def get_settings():
    """Return current settings (prompts + model + temperature + chat) with defaults filled in."""
    settings = load_settings()
    # Also return the defaults so the UI can offer per-prompt reset
    return {
        "settings": settings,
        "defaults": json.loads(json.dumps(DEFAULT_SETTINGS)),
    }


@app.put("/api/settings")
async def update_settings(body: SettingsRequest):
    """Update settings and persist to disk."""
    settings = load_settings()

    if body.prompts is not None:
        for key in DEFAULT_PROMPTS:
            if key in body.prompts:
                settings["prompts"][key] = body.prompts[key]

    if body.ollama_model is not None:
        settings["ollama_model"] = body.ollama_model

    if body.temperature is not None:
        settings["temperature"] = max(0.0, min(1.0, body.temperature))

    if body.chat is not None:
        chat = settings.setdefault("chat", json.loads(json.dumps(DEFAULT_SETTINGS["chat"])))
        if body.chat.endpoint is not None and body.chat.endpoint in ("ollama", "openwebui", "custom"):
            chat["endpoint"] = body.chat.endpoint
        if body.chat.custom_url is not None:
            chat["custom_url"] = body.chat.custom_url
        if body.chat.custom_api_key is not None:
            chat["custom_api_key"] = body.chat.custom_api_key
        if body.chat.model is not None:
            chat["model"] = body.chat.model
        if body.chat.system_prompt is not None:
            chat["system_prompt"] = body.chat.system_prompt
        if body.chat.temperature is not None:
            chat["temperature"] = max(0.0, min(1.0, body.chat.temperature))
        if body.chat.max_context_chunks is not None:
            chat["max_context_chunks"] = max(1, min(50, body.chat.max_context_chunks))

    save_settings(settings)
    return {"detail": "Settings updated", "settings": settings}


@app.post("/api/settings/reset")
async def reset_settings():
    """Reset all settings to defaults."""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    save_settings(settings)
    return {"detail": "Settings reset to defaults", "settings": settings}


# ---------------------------------------------------------------------------
# Notes API Pydantic models
# ---------------------------------------------------------------------------

class NoteCreate(BaseModel):
    title: str
    folder: str = ""
    type: str = "note"
    body: str = ""


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[list] = None


class NoteLinkMeeting(BaseModel):
    meeting_id: str
    add: bool = True


class TaskToggle(BaseModel):
    note_id: str
    line: int
    done: bool
    expected_text: Optional[str] = None


class NoteRename(BaseModel):
    title: Optional[str] = None
    folder: Optional[str] = None


class PushActionItems(BaseModel):
    meeting_id: str


# ---------------------------------------------------------------------------
# Notes API (Markdown files on disk; see notes_store.py)
# ---------------------------------------------------------------------------

def _run_bg(fn, *args) -> None:
    """Fire-and-forget a blocking function in the default threadpool so it never
    blocks the event loop or the HTTP response. (BackgroundTasks can't be used:
    the BaseHTTPMiddleware in this app couples them to the response, so the client
    would wait for the ~10s cold embed.) Falls back to inline if no loop is running."""
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, lambda: fn(*args))
    except RuntimeError:
        fn(*args)


def _index_note_safe(rec: dict) -> None:
    """Best-effort, non-blocking: index a note's vectors; never let Qdrant/embedder
    errors (or latency) break or delay note CRUD."""
    if not rec:
        return

    def work():
        try:
            notes_vectors.index_note(get_qdrant(), get_embedder(), rec,
                                     collection=notes_vectors.NOTES_COLLECTION, dim=EMBEDDING_DIM)
        except Exception as e:
            logger.warning(f"Note vector index failed for {rec.get('id')} (non-fatal): {e}")
    _run_bg(work)


def _deindex_note_safe(note_id: str) -> None:
    def work():
        try:
            notes_vectors.delete_note_vectors(get_qdrant(), note_id,
                                              collection=notes_vectors.NOTES_COLLECTION)
        except Exception as e:
            logger.warning(f"Note vector delete failed for {note_id} (non-fatal): {e}")
    _run_bg(work)


def _enqueue_tag(note_id: str) -> None:
    """Enqueue a note for background tagging (coalesced by note_id)."""
    if note_id and note_id not in _tag_pending:
        _tag_pending.add(note_id)
        _tag_queue.put_nowait(note_id)


async def _tag_worker():
    """Background worker: consume note_ids from queue and tag them when pipeline is idle."""
    while True:
        note_id = await _tag_queue.get()
        try:
            while _pipeline_busy():
                await asyncio.sleep(TAG_IDLE_POLL)
            await _run_tag_job(note_id)
        except Exception as e:
            logger.warning(f"tag worker error (non-fatal): {e}")
        finally:
            _tag_pending.discard(note_id)
            _tag_queue.task_done()


@app.get("/api/notes")
async def api_list_notes(folder: Optional[str] = None, tag: Optional[str] = None,
                         type: Optional[str] = None, q: Optional[str] = None):
    return {"notes": notes_store.list_notes(
        notes_store.NOTES_DIR, folder=folder, tag=tag, type=type, q=q)}


@app.post("/api/notes")
async def api_create_note(payload: NoteCreate):
    try:
        rec = notes_store.create_note(
            notes_store.NOTES_DIR, payload.title, folder=payload.folder,
            type=payload.type, body=payload.body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _index_note_safe(rec)
    _enqueue_tag(rec.get("id"))
    return rec


@app.get("/api/notes/folders")
async def api_list_folders():
    return {"folders": notes_store.list_folders(notes_store.NOTES_DIR)}


@app.post("/api/notes/rescan")
async def api_rescan_notes():
    idx = notes_store.get_index(notes_store.NOTES_DIR, force=True)
    return {"count": len(idx)}


@app.get("/api/notes/search")
async def api_search_notes(q: str, limit: int = 10):
    # The query embed is a blocking HTTP call; run it off the event loop.
    def _do():
        return notes_vectors.search_notes(
            get_qdrant(), get_embedder(), q,
            collection=notes_vectors.NOTES_COLLECTION, dim=EMBEDDING_DIM, limit=limit)
    try:
        results = await asyncio.get_event_loop().run_in_executor(None, _do)
    except Exception as e:
        logger.warning(f"Note search failed (non-fatal): {e}")
        results = []
    return {"results": results}


@app.get("/api/notes/{note_id}")
async def api_get_note(note_id: str):
    rec = notes_store.read_note(notes_store.NOTES_DIR, note_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return rec


@app.put("/api/notes/{note_id}")
async def api_update_note(note_id: str, payload: NoteUpdate):
    rec = notes_store.update_note(
        notes_store.NOTES_DIR, note_id,
        title=payload.title, body=payload.body, tags=payload.tags)
    if rec is None:
        raise HTTPException(status_code=404, detail="Note not found")
    _index_note_safe(rec)
    if payload.body is not None:
        _enqueue_tag(note_id)
    return rec


@app.delete("/api/notes/{note_id}")
async def api_delete_note(note_id: str):
    if not notes_store.delete_note(notes_store.NOTES_DIR, note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    _deindex_note_safe(note_id)
    return {"deleted": True}


@app.post("/api/notes/{note_id}/retag")
async def api_retag_note(note_id: str):
    """Enqueue a note for background auto-tagging."""
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    _enqueue_tag(note_id)
    return {"queued": True}


@app.post("/api/notes/{note_id}/link-meeting")
async def api_link_meeting(note_id: str, payload: NoteLinkMeeting):
    rec = notes_store.link_meeting(
        notes_store.NOTES_DIR, note_id, payload.meeting_id, add=payload.add)
    if rec is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return rec


@app.get("/api/notes/{note_id}/links")
async def api_note_links(note_id: str):
    rec = notes_store.read_note(notes_store.NOTES_DIR, note_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Note not found")
    outgoing = notes_store.resolve_wikilinks(
        notes_store.NOTES_DIR, notes_store.extract_wikilinks(rec.get("body", "")))
    return {"outgoing": outgoing, "backlinks": notes_store.backlinks(notes_store.NOTES_DIR, note_id)}


@app.get("/api/notes/{note_id}/related")
async def api_note_related(note_id: str, limit: int = 5):
    note = notes_store.read_note(notes_store.NOTES_DIR, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    try:
        query = (note.get("title", "") + "\n" + (note.get("body", "") or ""))[:4000]
        hits = vector.search_meeting_vectors(get_qdrant(), get_embedder(), query, limit=limit * 3)
        seen, out = set(), []
        for h in hits:
            mid = h.get("meeting_id")
            if not mid or mid in seen:
                continue
            if h.get("score") is not None and h["score"] < RELATED_MIN_SCORE:
                continue
            seen.add(mid); out.append({"meeting_id": h.get("meeting_id"), "title": h.get("title", ""), "date": h.get("date", ""), "score": h.get("score")})
            if len(out) >= limit:
                break
        return {"related": out}
    except Exception as e:
        logger.warning(f"note related failed (non-fatal): {e}")
        return {"related": []}


def _collect_note_tasks() -> list:
    out = []
    for rec in notes_store.list_notes(notes_store.NOTES_DIR):
        full = notes_store.read_note(notes_store.NOTES_DIR, rec["id"])
        if full:
            out.extend(tasks_store.parse_tasks_from_body(full["body"], rec["id"], rec["title"]))
    return out


def _collect_meeting_tasks() -> list:
    out = []
    for mid, m in meetings.items():
        if m.get("status") != MeetingStatus.complete:
            continue
        out_dir = m.get("output_dir")
        if not out_dir:
            continue
        sp = Path(out_dir) / "summary.json"
        if not sp.exists():
            continue
        try:
            summary = json.loads(sp.read_text())
        except Exception:
            continue
        for item in summary.get("action_items", []):
            out.append(tasks_store.meeting_action_item_to_task(item, mid, m.get("title", mid)))
    return out


@app.get("/api/tasks")
async def api_list_tasks(status: Optional[str] = None, owner: Optional[str] = None,
                         source: Optional[str] = None, due: Optional[str] = None):
    tasks = _collect_note_tasks() + _collect_meeting_tasks()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tasks = tasks_store.filter_tasks(tasks, status=status, owner=owner, source=source, due=due, today=today)
    return {"tasks": tasks_store.sort_tasks(tasks)}


@app.post("/api/tasks/toggle")
async def api_toggle_task(payload: TaskToggle):
    rec = notes_store.read_note(notes_store.NOTES_DIR, payload.note_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Note not found")
    new_body, ok = tasks_store.toggle_line(rec["body"], payload.line, payload.done,
                                           expected_text=payload.expected_text)
    if not ok:
        raise HTTPException(status_code=409, detail="Task line changed or not a checkbox; refresh")
    notes_store.update_note(notes_store.NOTES_DIR, payload.note_id, body=new_body)
    return {"ok": True}


@app.post("/api/notes/{note_id}/rename")
async def api_rename_note(note_id: str, payload: NoteRename):
    try:
        rec = notes_store.rename_note(notes_store.NOTES_DIR, note_id,
                                      title=payload.title, folder=payload.folder)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if rec is None:
        raise HTTPException(status_code=404, detail="Note not found")
    _index_note_safe(rec)
    return rec


@app.post("/api/notes/{note_id}/push-action-items")
async def api_push_action_items(note_id: str, payload: PushActionItems):
    m = meetings.get(payload.meeting_id)
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    out_dir = m.get("output_dir")
    items = []
    if out_dir:
        sp = Path(out_dir) / "summary.json"
        if sp.exists():
            try:
                items = json.loads(sp.read_text()).get("action_items", [])
            except Exception:
                items = []
    if not items:
        raise HTTPException(status_code=400, detail="No action items to push")
    block = "\n".join(tasks_store.format_action_item_as_checkbox(it) for it in items)
    rec = notes_store.append_to_body(notes_store.NOTES_DIR, note_id, block)
    if rec is None:
        raise HTTPException(status_code=404, detail="Note not found")
    _index_note_safe(rec)
    return rec


@app.post("/api/notes/{note_id}/attachments")
async def api_add_attachment(note_id: str, file: UploadFile = File(...)):
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    data = await file.read()
    if len(data) > ATTACH_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Attachment too large")
    fname = notes_store.save_attachment(notes_store.NOTES_DIR, file.filename or "file", data)
    ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
    is_image = ext in _IMAGE_EXTS
    embed = f"![[{fname}]]" if is_image else f"[{fname}](attachments/{fname})"
    return {"filename": fname, "url": f"/api/notes/attachments/{fname}", "is_image": is_image, "embed": embed}


@app.get("/api/notes/attachments/{filename}")
async def api_get_attachment(filename: str):
    p = notes_store.attachment_path(notes_store.NOTES_DIR, filename)
    if p is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(p, media_type=_ATTACH_MEDIA.get(p.suffix.lower(), "application/octet-stream"), filename=p.name)


# ---------------------------------------------------------------------------
# AI Chat
# ---------------------------------------------------------------------------


class ChatContext(BaseModel):
    scope: str = "meeting"  # meeting | linked | category | keyword | speaker | custom | global
    meeting_id: Optional[str] = None
    meeting_ids: Optional[list[str]] = None  # for "custom" scope with explicit meeting IDs
    category: Optional[str] = None
    keyword: Optional[str] = None
    speaker: Optional[str] = None


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    context: ChatContext
    history: list[ChatMessage] = []


def _get_linked_cluster(meeting_id: str) -> list[str]:
    """Get all meeting IDs in the same linked cluster using union-find."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for mid, m in meetings.items():
        for linked_id in m.get("links", {}).get("manual", []):
            if linked_id in meetings:
                union(mid, linked_id)

    target_root = find(meeting_id)
    return [mid for mid in meetings if find(mid) == target_root]


def _resolve_chat_meeting_ids(ctx: ChatContext) -> list[str]:
    """Map a chat scope to a list of meeting IDs for Qdrant filtering."""
    if ctx.scope == "meeting" and ctx.meeting_id:
        return [ctx.meeting_id]

    if ctx.scope == "linked" and ctx.meeting_id:
        return _get_linked_cluster(ctx.meeting_id)

    if ctx.scope == "category" and ctx.category:
        return [
            mid for mid, m in meetings.items()
            if m.get("status") == MeetingStatus.complete
            and m.get("tags", {}).get("category", "") == ctx.category
        ]

    if ctx.scope == "keyword" and ctx.keyword:
        kw = ctx.keyword.lower()
        return [
            mid for mid, m in meetings.items()
            if m.get("status") == MeetingStatus.complete
            and kw in [k.lower() for k in m.get("tags", {}).get("keywords", [])]
        ]

    if ctx.scope == "speaker" and ctx.speaker:
        sp = ctx.speaker.lower()
        result = []
        for mid, m in meetings.items():
            if m.get("status") != MeetingStatus.complete:
                continue
            speaker_names = set()
            for info in m.get("speaker_info", {}).values():
                if isinstance(info, dict) and info.get("name"):
                    speaker_names.add(info["name"].lower())
            for name in m.get("speaker_map", {}).values():
                speaker_names.add(name.lower())
            if sp in speaker_names:
                result.append(mid)
        return result

    if ctx.scope == "custom" and ctx.meeting_ids:
        return [
            mid for mid in ctx.meeting_ids
            if mid in meetings and meetings[mid].get("status") == MeetingStatus.complete
        ]

    if ctx.scope == "global":
        return [
            mid for mid, m in meetings.items()
            if m.get("status") == MeetingStatus.complete
        ]

    # Fallback: single meeting
    if ctx.meeting_id:
        return [ctx.meeting_id]
    return []


async def _stream_chat_response(messages: list[dict], settings: dict):
    """Async generator that streams chat tokens as SSE events."""
    chat_cfg = settings.get("chat", DEFAULT_SETTINGS["chat"])
    endpoint = chat_cfg.get("endpoint", "ollama")
    model = chat_cfg.get("model", "") or settings.get("ollama_model", OLLAMA_MODEL)
    temperature = chat_cfg.get("temperature", 0.5)

    if endpoint == "ollama":
        url = f"{OLLAMA_URL}/api/chat"
        # Size num_ctx to the prompt: this is the one LLM call not routed through
        # _build_generate_body, so without an explicit num_ctx it inherits Ollama's
        # small default (~4096) and silently truncates the RAG chunks + history off
        # the front, degrading grounding with no error.
        chat_chars = "".join(m.get("content", "") for m in messages)
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": False,  # qwen3.x: don't stream a reasoning block before the answer
            "options": {
                "temperature": temperature,
                "num_ctx": _ctx_for_text(chat_chars, num_predict=1024),
            },
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                            content = chunk.get("message", {}).get("content", "")
                            if content:
                                yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    elif endpoint == "openwebui":
        url = f"{OPENWEBUI_URL}/api/chat/completions"
        headers = {"Content-Type": "application/json"}
        if OPENWEBUI_API_KEY:
            headers["Authorization"] = f"Bearer {OPENWEBUI_API_KEY}"
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            line = line[6:]
                        try:
                            chunk = json.loads(line)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    elif endpoint == "custom":
        url = chat_cfg.get("custom_url", "")
        api_key = chat_cfg.get("custom_api_key", "")
        if not url:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Custom endpoint URL not configured'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            line = line[6:]
                        try:
                            chunk = json.loads(line)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


@app.post("/meetings/chat")
async def chat_with_meetings(body: ChatRequest):
    """Chat about meeting content using RAG via Qdrant. Returns SSE stream."""
    settings = load_settings()
    chat_cfg = settings.get("chat", DEFAULT_SETTINGS["chat"])

    # Resolve which meetings to search
    meeting_ids = _resolve_chat_meeting_ids(body.context)
    if not meeting_ids:
        async def empty_stream():
            yield f"data: {json.dumps({'type': 'context', 'chunks_used': 0, 'meetings_searched': 0})}\n\n"
            yield f"data: {json.dumps({'type': 'error', 'content': 'No meetings found for the specified scope.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return StreamingResponse(empty_stream(), media_type="text/event-stream")

    # RAG: search Qdrant for relevant chunks
    max_chunks = chat_cfg.get("max_context_chunks", 15)
    context_chunks = []
    meetings_with_hits = set()
    try:
        embedder = get_embedder()
        qdrant = get_qdrant()
        query_vec = embedder.encode(body.message).tolist()

        must_conditions = []
        if len(meeting_ids) == 1:
            must_conditions.append(
                FieldCondition(key="meeting_id", match=MatchValue(value=meeting_ids[0]))
            )
        # For multi-meeting scopes, we filter client-side after search
        # (Qdrant doesn't support OR on same field easily)

        query_filter = Filter(must=must_conditions) if must_conditions else None
        search_limit = max_chunks * 3 if len(meeting_ids) > 1 else max_chunks
        meeting_id_set = set(meeting_ids)

        results = qdrant.search(
            collection_name=COLLECTION_NAME,
            query_vector=query_vec,
            query_filter=query_filter,
            limit=search_limit,
        )

        for hit in results:
            hit_mid = hit.payload.get("meeting_id", "")
            if len(meeting_ids) > 1 and hit_mid not in meeting_id_set:
                continue
            meetings_with_hits.add(hit_mid)
            title = hit.payload.get("title", "Meeting")
            date = hit.payload.get("date", "")
            chunk_type = hit.payload.get("chunk_type", "")
            text = hit.payload.get("text", "")
            context_chunks.append(f"[{title} ({date})] [{chunk_type}] {text}")
            if len(context_chunks) >= max_chunks:
                break
    except Exception as e:
        logger.warning(f"Chat Qdrant search failed: {e}")

    # Build context string
    context_text = "\n\n".join(context_chunks) if context_chunks else "No relevant context found."

    # Build messages for LLM
    system_prompt = chat_cfg.get("system_prompt", DEFAULT_CHAT_SYSTEM_PROMPT)
    llm_messages = [
        {"role": "system", "content": f"{system_prompt}\n\n--- Meeting Context ---\n{context_text}"},
    ]
    for msg in body.history[-20:]:  # Cap history
        llm_messages.append({"role": msg.role, "content": msg.content})
    llm_messages.append({"role": "user", "content": body.message})

    # Stream response
    async def chat_stream():
        # Send context info first
        yield f"data: {json.dumps({'type': 'context', 'chunks_used': len(context_chunks), 'meetings_searched': len(meetings_with_hits)})}\n\n"
        async for event in _stream_chat_response(llm_messages, settings):
            yield event

    return StreamingResponse(chat_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Static files (must be after all route definitions)
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup():
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    _load_index()
    # Pre-load sentence-transformer in background so first job doesn't pay the 37s load cost
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, get_embedder)
    # Backfill link suggestions for complete meetings that have none
    backfilled = 0
    for mid, m in meetings.items():
        if m.get("status") == "complete":
            links = m.get("links", {})
            if not links.get("suggestions"):
                _auto_compute_link_suggestions(mid)
                backfilled += 1
    if backfilled:
        logger.info(f"Backfilled link suggestions for {backfilled} meetings")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
