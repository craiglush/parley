"""
Meeting Service - Orchestrator for audio transcription, summarization, and search.

Pipeline: Upload audio -> Parakeet+pyannote transcription -> LLM transcript cleanup -> Ollama summarization -> Qdrant storage -> file output
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
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
import extract
import tasks_store
import emailer
from integrations import a360
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
# STT + audio helpers live in stt.py; re-bound so process_meeting's bare
# calls (preprocess_audio, split_audio, step_transcribe, merge_chunk_segments) resolve.
from stt import (
    preprocess_audio,
    probe_duration,
    split_audio,
    trim_audio,
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

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:14b")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
OPENWEBUI_URL = os.getenv("OPENWEBUI_URL", "http://open-webui:8080")
OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY", "")
A360_API_TOKEN = os.getenv("A360_API_TOKEN", "")  # bearer for the a360 pull API; "" = disabled (secure default)
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
        "You are a professional transcription editor. You are given raw speech-to-text\n"
        "segments from a meeting, one per line, each prefixed with its index like [0], [1], [2].\n"
        "\n"
        "Correct transcription errors WITHOUT changing meaning:\n"
        "- Fix misheard words, homophones, run-together words, punctuation, capitalization, and grammar.\n"
        "- Correct proper nouns, people's names, company and product names, and technical\n"
        "  terms/acronyms using context from the surrounding conversation.\n"
        "- Keep the speaker's own wording, tone, and intent. Do NOT paraphrase, translate,\n"
        "  summarize, censor, add, remove, merge, or split segments.\n"
        "- Leave filler as-is unless it is clearly a recognition error.\n"
        "\n"
        "Output rules (critical):\n"
        "- Return EXACTLY one line per input segment, in the same order.\n"
        "- Keep each segment's original [index] prefix.\n"
        "- Use the format: [0] corrected text"
    ),
    "speaker_id": (
        "You are analyzing a meeting transcript to identify who each speaker is.\n"
        "\n"
        "The transcript uses generic diarization labels: {speaker_list}\n"
        "\n"
        "Work only from evidence in the transcript. Look for clues such as:\n"
        "- Self-introductions: \"Hi, I'm Alex\" or \"This is Sarah from Acme Corp\".\n"
        "- People being addressed by name: \"Alex, what do you think?\" (the person who\n"
        "  answers, or the one being addressed, is likely Alex).\n"
        "- Role or title mentions: \"As the project manager...\", \"Speaking as CTO...\".\n"
        "- Company or team mentions: \"We at Acme Corp...\", \"On behalf of TechStart...\".\n"
        "- Email addresses, signatures, or handles: \"I'll send it from alex@example.com\".\n"
        "\n"
        "Do NOT guess. Only assign a name when the transcript gives real evidence for it.\n"
        "Prefer a full name when available; capture role/title and company when stated.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Respond ONLY with valid JSON (no markdown fences, no commentary). Include an entry\n"
        "for each speaker you can identify by at least a name; omit speakers you cannot identify.\n"
        "\n"
        "{json_example}"
    ),
    "analysis_pass_a": (
        "You are a meeting analyst. Read the transcript and produce a title, a concise\n"
        "overview, and the main topics discussed. Base everything strictly on what is in the\n"
        "transcript — do not invent details.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Guidance:\n"
        "- title: a short, specific, descriptive meeting title (not a generic label).\n"
        "- summary: 2-3 sentences capturing the purpose and the most important outcomes.\n"
        "- topics: the distinct subjects actually discussed; for each, a brief summary of the\n"
        "  discussion and the outcome or next step (use \"\" for outcome if none was reached).\n"
        "\n"
        "Respond ONLY with valid JSON, no other text:\n"
        "{\n"
        '  "title": "Short descriptive meeting title",\n'
        '  "summary": "2-3 sentence overview of the meeting",\n'
        '  "topics": [\n'
        '    {"topic": "Topic name", "summary": "Brief summary of discussion", "outcome": "What was concluded or the next step"}\n'
        "  ]\n"
        "}"
    ),
    "analysis_pass_b": (
        "You are a meeting analyst. Extract every action item, task, or commitment that was\n"
        "made in the meeting. Include only real commitments to do something — not general\n"
        "discussion, opinions, or ideas that were rejected.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Guidance:\n"
        "- task: what needs to be done, phrased as a clear action.\n"
        "- who: the person responsible; use \"UNKNOWN\" if the transcript does not say.\n"
        "- deadline: any due date or timeframe mentioned; use \"\" if none.\n"
        "- priority: \"high\", \"medium\", or \"low\" based on urgency and emphasis.\n"
        "If there are no action items, return an empty array [].\n"
        "\n"
        "Respond ONLY with a valid JSON array, no other text:\n"
        "[\n"
        '  {"task": "What needs to be done", "who": "Person responsible or UNKNOWN", "deadline": "Deadline if mentioned or empty", "priority": "high/medium/low"}\n'
        "]"
    ),
    "analysis_pass_c": (
        "You are a meeting analyst. Extract the decisions that were actually made and the\n"
        "questions that were raised but left unanswered. Ground every item in the transcript.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Guidance:\n"
        "- decisions: concrete conclusions the group committed to; include brief context for why.\n"
        "- open_questions: questions that were asked but not resolved; set \"answered\" to false\n"
        "  if left open, true if it was later answered in the meeting. Use \"UNKNOWN\" when the\n"
        "  asker is unclear.\n"
        "Return empty arrays if there were no decisions or no open questions.\n"
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
        "You are a meeting analyst. Identify concerns, risks, objections, blockers, or\n"
        "hesitations raised by participants. Capture the substance, not passing remarks.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Guidance:\n"
        "- concern: what the concern, risk, or objection is.\n"
        "- raised_by: who raised it; use \"UNKNOWN\" if unclear.\n"
        "- resolved: true if it was addressed or resolved during the meeting, otherwise false.\n"
        "- notes: any resolution, mitigation, or follow-up discussed; use \"\" if none.\n"
        "If no concerns were raised, return an empty array [].\n"
        "\n"
        "Respond ONLY with a valid JSON array, no other text:\n"
        "[\n"
        '  {"concern": "Description of the concern or risk", "raised_by": "Who raised it or UNKNOWN", "resolved": false, "notes": "Any resolution or follow-up discussed"}\n'
        "]"
    ),
    "analysis_pass_e": (
        "You are a meeting analyst. Extract specific quantitative facts mentioned in the\n"
        "meeting: numbers, dates, costs, budgets, metrics, percentages, quantities, and\n"
        "deadlines. Only include figures that actually appear in the transcript.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Guidance:\n"
        "- figure: the number, date, amount, or metric exactly as stated.\n"
        "- context: what it refers to or why it matters.\n"
        "- said_by: who mentioned it; use \"UNKNOWN\" if unclear.\n"
        "If no specific figures were mentioned, return an empty array [].\n"
        "\n"
        "Respond ONLY with a valid JSON array, no other text:\n"
        "[\n"
        '  {"figure": "The number, date, or metric", "context": "What it refers to", "said_by": "Who mentioned it or UNKNOWN"}\n'
        "]"
    ),
    "analysis_pass_f": (
        "You are a meeting analyst. Assess the overall sentiment and emotional tone of the\n"
        "meeting based on how participants spoke and reacted.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Guidance:\n"
        "- overall: one of \"positive\", \"neutral\", \"negative\", or \"mixed\".\n"
        "- notable_moments: specific moments that stood out emotionally (agreement, tension,\n"
        "  frustration, humour, excitement), each with a short tone label. Use an empty array\n"
        "  if nothing notable stood out.\n"
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
        "You are a meeting analyst. Read the transcript and produce categorization tags and\n"
        "the key entities mentioned. Only include entities that actually appear in the transcript.\n"
        "\n"
        "TRANSCRIPT:\n"
        "{transcript}\n"
        "\n"
        "Guidance:\n"
        "- category: choose the single best-fitting type of meeting from the list below.\n"
        "- keywords: 3-5 short, specific tags describing what the meeting was about.\n"
        "- entities: real names grouped by type; use empty arrays for types with no matches.\n"
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
        "This is one portion (segment {chunk_index} of {chunk_total}) of a long meeting\n"
        "transcript. Summarize what happens in THIS portion as 3-5 concise bullet points,\n"
        "capturing key discussion, decisions, and action items. Base it only on the text\n"
        "below; do not speculate about other portions.\n"
        "\n"
        "{chunk}"
    ),
    "note_analysis": (
        "You are a knowledge assistant. Read the note below — it may include the note\n"
        "body plus text extracted from its attachments — and produce a structured\n"
        "analysis. Base everything strictly on the provided text; do not invent details.\n"
        "\n"
        "NOTE:\n"
        "{corpus}\n"
        "\n"
        "Guidance:\n"
        "- summary: 2-4 sentences capturing what this note is about and why it matters.\n"
        "- key_points: the most important facts or ideas, each a short standalone bullet.\n"
        "- action_items: concrete tasks or follow-ups implied by the note; [] if none.\n"
        "- insights: non-obvious observations, connections, or risks worth noting; [] if none.\n"
        "\n"
        "Respond ONLY with valid JSON, no other text:\n"
        "{\n"
        '  "summary": "2-4 sentence overview",\n'
        '  "key_points": ["short bullet", "short bullet"],\n'
        '  "action_items": ["actionable task"],\n'
        '  "insights": ["non-obvious observation"]\n'
        "}"
    ),
    "note_cleanup": (
        "You are tidying up a short piece of dictated (voice-to-text) note content.\n"
        "Fix punctuation and capitalization, remove filler words and false starts, and\n"
        "reflow the text into clean paragraphs or bullet points where that helps\n"
        "readability. Tighten phrasing, but PRESERVE every fact and the original\n"
        "meaning exactly -- never invent, add, or drop content.\n"
        "\n"
        "RAW TEXT:\n"
        "{text}\n"
        "\n"
        "Respond ONLY with valid JSON, no other text:\n"
        "{\n"
        '  "text": "the polished version"\n'
        "}"
    ),
}

# Appended to the cleanup preamble AT PROMPT-BUILD TIME when the remove_filler
# setting is ON. Never written into a saved template: user prompt templates are
# served verbatim (load_settings), so this composes with customized preambles
# and vanishes entirely when the toggle is OFF. The final rule exists because
# _parse_cleanup_response discards empty lines and then fails the whole batch on
# a count mismatch — the LLM must never delete a line, only the pre-pass can.
FILLER_DIRECTIVE = (
    "\n"
    "Filler removal (active — this overrides the earlier instruction to leave filler as-is):\n"
    "- Remove remaining disfluencies: um, uh, er, ah, hmm, stutters, false starts,\n"
    "  and immediate word repetitions (\"we we should\" -> \"we should\").\n"
    "- Remove semantically empty filler phrases — \"you know\", \"like\", \"sort of\",\n"
    "  \"kind of\", \"I mean\", \"basically\" — ONLY where they carry no meaning.\n"
    "  Keep them when meaningful (\"I like this plan\", \"sort of a hybrid approach\").\n"
    "- Never paraphrase, shorten, or tighten real content; keep the speaker's wording.\n"
    "- Never return an empty line: if a line is nothing but filler, return it unchanged."
)

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
    "note_analysis": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "key_points": _STR_ARRAY,
            "action_items": _STR_ARRAY,
            "insights": _STR_ARRAY,
        },
        "required": ["summary"],
    },
    "note_cleanup": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    },
    "digest_briefing": {
        "type": "object",
        "properties": {
            "briefing": {"type": "string"},
        },
        "required": ["briefing"],
    },
    "task_parse": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "due": {"type": "string"},
            "priority": {"type": "string"},
            "owner": {"type": "string"},
        },
        "required": ["text"],
    },
    "task_triage": {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "priority": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "priority", "reason"],
                },
            },
            "focus": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["suggestions"],
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
    "stt_backend": os.getenv("MEETING_STT_BACKEND", "parakeet"),
    "diarize": True,
    "remove_filler": True,
    "chat": {
        "endpoint": "ollama",  # "ollama", "openwebui", or "custom"
        "custom_url": "",
        "custom_api_key": "",
        "model": "",  # empty = use main ollama_model
        "system_prompt": DEFAULT_CHAT_SYSTEM_PROMPT,
        "temperature": 0.5,
        "max_context_chunks": 15,
    },
    "smtp": {
        "enabled": False,
        "host": os.getenv("SMTP_HOST", ""),
        "port": int(os.getenv("SMTP_PORT", "587") or "587"),
        "secure": os.getenv("SMTP_SECURE", "").lower() == "true",  # True = implicit TLS (465)
        "username": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from_email": os.getenv("EMAIL_FROM", ""),
        "from_name": os.getenv("EMAIL_FROM_NAME", "Meeting Service"),
        "reply_to": os.getenv("EMAIL_REPLY_TO", ""),
        "recipients": os.getenv("EMAIL_RECIPIENTS", ""),  # comma-separated
    },
    "digest": {
        "enabled": False,
        "time": "07:00",
        "timezone": "Europe/London",
        "recipients": "",  # blank -> falls back to EMAIL_RECIPIENTS env / smtp.recipients at send time
    },
    "ics": {
        "enabled": False,
        "token": "",
    },
}


def load_settings() -> dict:
    """Load settings from disk, filling in any missing keys from defaults."""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            # Merge prompts (keep saved values, fill missing with defaults).
            # A blank saved value falls back to the built-in default so the analysis
            # pipeline (and the settings UI) never run on an empty prompt.
            if "prompts" in saved and isinstance(saved["prompts"], dict):
                for key in DEFAULT_PROMPTS:
                    val = saved["prompts"].get(key)
                    if isinstance(val, str) and val.strip():
                        settings["prompts"][key] = val
            if "ollama_model" in saved:
                settings["ollama_model"] = saved["ollama_model"]
            if "temperature" in saved:
                settings["temperature"] = saved["temperature"]
            if saved.get("stt_backend") in ("parakeet",):
                settings["stt_backend"] = saved["stt_backend"]
            if isinstance(saved.get("diarize"), bool):
                settings["diarize"] = saved["diarize"]
            if isinstance(saved.get("remove_filler"), bool):
                settings["remove_filler"] = saved["remove_filler"]
            # Merge chat settings
            if "chat" in saved and isinstance(saved["chat"], dict):
                for key in DEFAULT_SETTINGS["chat"]:
                    if key not in saved["chat"]:
                        continue
                    val = saved["chat"][key]
                    # A blank system prompt falls back to the built-in default;
                    # other chat fields may legitimately be empty.
                    if key == "system_prompt" and isinstance(val, str) and not val.strip():
                        continue
                    settings["chat"][key] = val
            # Merge SMTP settings
            if "smtp" in saved and isinstance(saved["smtp"], dict):
                for key in DEFAULT_SETTINGS["smtp"]:
                    if key in saved["smtp"]:
                        settings["smtp"][key] = saved["smtp"][key]
            # Merge digest settings
            if "digest" in saved and isinstance(saved["digest"], dict):
                for key in DEFAULT_SETTINGS["digest"]:
                    if key in saved["digest"]:
                        settings["digest"][key] = saved["digest"][key]
            # Merge ICS settings
            if "ics" in saved and isinstance(saved["ics"], dict):
                for key in DEFAULT_SETTINGS["ics"]:
                    if key in saved["ics"]:
                        settings["ics"][key] = saved["ics"][key]
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")
    return settings


def save_settings(settings: dict):
    """Persist settings to disk."""
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(SETTINGS_PATH, json.dumps(settings, indent=2))


def _digest_zoneinfo(tz_str: str | None) -> ZoneInfo:
    """Resolve an IANA timezone name to a ZoneInfo, falling back to UTC on an
    invalid/unknown name (never raises -- the digest must still fire on a typo'd
    timezone, just in UTC instead of the intended zone)."""
    try:
        return ZoneInfo(tz_str or DEFAULT_SETTINGS["digest"]["timezone"])
    except Exception:
        return ZoneInfo("UTC")


def _parse_ollama_tags(data: dict) -> list[dict]:
    """Shape Ollama /api/tags output into the UI model list, sorted by name."""
    out = []
    for m in data.get("models", []):
        det = m.get("details", {}) or {}
        out.append({
            "name": m.get("name", ""),
            "size": m.get("size", 0),
            "parameter_size": det.get("parameter_size", ""),
            "quantization": det.get("quantization_level", ""),
        })
    out.sort(key=lambda x: x["name"])
    return out


async def _list_ollama_models() -> list[dict]:
    """Fetch installed Ollama models; empty list on any failure (UI degrades to free text)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            return _parse_ollama_tags(resp.json())
    except Exception as e:
        logger.warning(f"Failed to list Ollama models: {e}")
        return []


# Chunking / splitting
MAX_AUDIO_CHUNK_SECONDS = 1800  # 30 min chunks for very long audio
OVERLAP_SECONDS = 30

# Upload validation
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(500 * 1024 * 1024)))  # 500MB
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".mkv", ".ogg", ".webm", ".flac", ".aac"}

# Streaming capture (server shadow backup) — see
# docs/superpowers/specs/2026-07-07-streaming-capture-design.md. Each in-progress
# recording streams its chunks here so a copy survives even if the recording
# device dies before the user uploads. Staging: _captures/{sid}/chunks/{seq}.part.
# (The root is derived from MEETINGS_DIR at call time via _captures_root() so it
# tracks a monkeypatched MEETINGS_DIR in tests, like the upload/trim routes do.)
CAPTURE_MAX_CHUNK_BYTES = 2 * 1024 * 1024          # generous — 1s of opus is a few KB
CAPTURE_MAX_AGE_SECONDS = 14 * 24 * 60 * 60        # mirrors the client CAP_MAX_AGE_MS sweep
_SID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

# Attachment handling
ATTACH_MAX_BYTES = 50 * 1024 * 1024

# Dictation upload cap: generous headroom over uncompressed 16-bit stereo 48kHz
# audio for the 120s duration cap (~23MB), well below ATTACH_MAX_BYTES.
DICTATE_MAX_BYTES = 25 * 1024 * 1024
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_ATTACH_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
                 ".pdf": "application/pdf"}
# Vision prompt used when resolving a deferred image/scanned-PDF attachment for analysis.
VISION_EXTRACT_PROMPT = os.getenv(
    "VISION_EXTRACT_PROMPT",
    "Transcribe every piece of text visible in this image, verbatim and in reading "
    "order. If the image contains no text, describe it in one or two sentences.")
# Note-analysis corpus cap (chars). Sized so body+attachment text stays inside the
# 16k num_ctx tier alongside a 3072-token output budget. This TRUNCATES the corpus;
# _ctx_for_text (llm.py:23) only sizes num_ctx, it does not cut.
ANALYSIS_CORPUS_MAX = int(os.getenv("ANALYSIS_CORPUS_MAX", "40000"))

ATTACH_TEXT_MAX = 20000  # per-attachment extracted-text cap fed to search/tags/analysis
VISION_PROMPT = ("Describe this image in detail and transcribe any visible text "
                 "verbatim. Be thorough and factual; do not speculate.")

# Meeting-analysis context (Phase C): char cap on the linked-note + attachment
# text prepended to the transcript before the analysis passes. Named constant so
# the prompt stays bounded; _ctx_for_text sizes num_ctx separately.
MEETING_CONTEXT_MAX = int(os.getenv("MEETING_CONTEXT_MAX", str(20000)))

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


class CacheControlMiddleware(BaseHTTPMiddleware):
    """Mark the app shell (HTML, sw.js, manifest, /static/*) `no-cache` so updates land
    immediately. The static files carry ETag/Last-Modified, so `no-cache` means
    "revalidate before use" -> a conditional request returns 304 when unchanged (cheap)
    and fresh bytes after a deploy. Without this, .js/.css get edge-cached (Cloudflare)
    or held by the service worker and a stale UI is served after every deploy — including
    a stale sw.js, which blocks the new worker from ever installing. Data downloads
    (audio/transcripts/attachments) are left untouched."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        p = request.url.path
        if p in ("/", "/sw.js", "/manifest.json") or p.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.add_middleware(IFrameMiddleware)
app.add_middleware(CacheControlMiddleware)
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
    _an_t = asyncio.create_task(_analysis_worker()); _bg_tasks.add(_an_t); _an_t.add_done_callback(_bg_tasks.discard)
    _cln_t = asyncio.create_task(_cleanup_worker()); _bg_tasks.add(_cln_t); _cln_t.add_done_callback(_bg_tasks.discard)
    _gc_t = asyncio.create_task(_capture_gc_worker()); _bg_tasks.add(_gc_t); _gc_t.add_done_callback(_bg_tasks.discard)
    _sgc_t = asyncio.create_task(_sidecar_gc_worker()); _bg_tasks.add(_sgc_t); _sgc_t.add_done_callback(_bg_tasks.discard)
    _ext_t = asyncio.create_task(_extract_worker()); _bg_tasks.add(_ext_t); _ext_t.add_done_callback(_bg_tasks.discard)
    _rescan_t = asyncio.create_task(_rescan_pending_extractions()); _bg_tasks.add(_rescan_t); _rescan_t.add_done_callback(_bg_tasks.discard)
    _digest_t = asyncio.create_task(_digest_worker()); _bg_tasks.add(_digest_t); _digest_t.add_done_callback(_bg_tasks.discard)
    _reindex_t = asyncio.create_task(_reindex_worker()); _bg_tasks.add(_reindex_t); _reindex_t.add_done_callback(_bg_tasks.discard)


# In-memory meeting registry (survives container restarts via on-disk JSON index)
meetings: dict[str, dict] = {}
meetings_lock = asyncio.Lock()

# Background tag worker
TAG_IDLE_POLL = 30.0
_tag_queue: "asyncio.Queue[str]" = asyncio.Queue()
_tag_pending: set = set()
_bg_tasks: set = set()

# Background note-analysis worker (same enqueue -> idle-worker -> LLM shape as tags)
ANALYSIS_IDLE_POLL = 30.0
_analysis_queue: "asyncio.Queue[str]" = asyncio.Queue()
_analysis_pending: set = set()
_analysis_status: dict[str, str] = {}  # note_id -> queued|running|done|error

# Background note-cleanup worker (silent LLM polish of freshly-dictated text; same
# enqueue -> idle-worker -> LLM shape as tags/analysis, but results are ephemeral --
# no disk sidecar, consumed by a short client poll within tens of seconds)
CLEANUP_IDLE_POLL = 30.0
_cleanup_queue: "asyncio.Queue[str]" = asyncio.Queue()
_cleanup_pending: set = set()
_cleanup_status: dict[str, str] = {}   # note_id -> queued|running|done|error
_cleanup_text: dict[str, str] = {}     # note_id -> pending raw text to clean
_cleanup_result: dict[str, str] = {}   # note_id -> cleaned text (once done)

# Background attachment-extraction worker (deferred STT/vision — GPU work)
EXTRACT_IDLE_POLL = 30.0
_extract_queue: "asyncio.Queue[tuple[str, str]]" = asyncio.Queue()
_extract_pending: set = set()


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


def _norm_company(s: str) -> str:
    """Canonical display form for a company string: trim + collapse internal
    whitespace. Comparison keys are _norm_company(s).casefold()."""
    return " ".join((s or "").split())


def suggest_company(meeting: dict) -> Optional[str]:
    """Deterministic, pure, cheap (two small dict scans) company suggestion.

    1. Attendee majority wins when speaker-company data exists: skip
       speaker_info entries whose name is empty or a SPEAKER_* placeholder
       (the list_people filter), count non-empty companies by casefolded
       comparison key; the strictly highest count wins.
    2. Tie-break (deterministic): a tied key equal to the comparison key of
       tags.entities.companies[0] wins; otherwise the casefold-alphabetically
       smallest tied key. Display form = first-seen normalized original
       casing (dict insertion order — stable per meeting).
    3. Fallback: normalized tags.entities.companies[0] when non-empty.
    4. Otherwise None.

    NEVER persisted — computed lazily wherever a meeting without a confirmed
    company is read. That is also the migration story for legacy meetings:
    m.get("company") is falsy, the suggestion is computed on open, and
    index.json is never rewritten in bulk."""
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for entry in (meeting.get("speaker_info") or {}).values():
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name or name.upper().startswith("SPEAKER_"):
            continue
        company = _norm_company(entry.get("company") or "")
        if not company:
            continue
        key = company.casefold()
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, company)

    entities = (meeting.get("tags") or {}).get("entities") or {}
    companies = entities.get("companies") or []
    first_entity = _norm_company(str(companies[0])) if companies else ""

    if counts:
        top = max(counts.values())
        tied = sorted(k for k, c in counts.items() if c == top)
        if len(tied) == 1:
            return display[tied[0]]
        if first_entity and first_entity.casefold() in tied:
            return display[first_entity.casefold()]
        return display[tied[0]]

    return first_entity or None


def _a360_completion_payload(meeting: dict) -> dict:
    """The dict process_meeting hands to a360.post_meeting_completed: every
    meeting key except the unserializable _task handle, stamped with
    company_suggestion under the SAME gate as the status endpoint — a payload
    never pairs a confirmed company with a competing suggestion. This matters
    for retry/trim/adopt re-completions of an already-confirmed meeting:
    those push company set, company_suggestion null."""
    return {**{k: v for k, v in meeting.items() if k != "_task"},
            "company_suggestion": (suggest_company(meeting)
                                   if not meeting.get("company") else None)}


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
            data = json.loads(index_path.read_text(encoding="utf-8"))
            for mid, m in data.items():
                meetings[mid] = m
                # Load tags from disk if not in index
                if not m.get("tags") and m.get("output_dir"):
                    tags_path = Path(m["output_dir"]) / "tags.json"
                    if tags_path.exists():
                        try:
                            m["tags"] = json.loads(tags_path.read_text(encoding="utf-8"))
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
# Vector re-indexing (edit-everything: every text edit re-embeds the meeting)
# ---------------------------------------------------------------------------

_reindex_locks: dict[str, threading.Lock] = {}
_reindex_locks_guard = threading.Lock()


def _lock_for(meeting_id: str) -> threading.Lock:
    """Per-meeting lock serializing Qdrant delete->upsert pairs (reindex jobs
    and delete_meeting) so overlapping jobs can't interleave and leave
    duplicate points, and a queued reindex can't resurrect a deleted meeting."""
    with _reindex_locks_guard:
        lock = _reindex_locks.get(meeting_id)
        if lock is None:
            lock = threading.Lock()
            _reindex_locks[meeting_id] = lock
        return lock


def _reindex_now(meeting_id: str) -> None:
    """Delete this meeting's Qdrant points and re-store them from the on-disk
    transcript.json / summary.json (files are the source of truth for all
    text edits). Blocking/sync — run off-loop by the caller. Failures are
    non-fatal — the files stay correct and the next edit/reprocess reindexes
    everything again (full delete -> re-store, so it self-heals).

    Re-fetches the meeting record fresh at call time (not at enqueue time),
    since a coalesced job can sit in the queue for a while before running.

    NOTE: calls the app-namespace get_qdrant() / store_in_qdrant on purpose
    (never vector.-qualified) so test monkeypatches on app.* intercept them.
    """
    try:
        with _lock_for(meeting_id):
            m = meetings.get(meeting_id)
            if not m:
                return  # deleted before/while queued — never resurrect points
            out_dir = Path(m.get("output_dir") or "")
            get_qdrant().delete(
                collection_name=COLLECTION_NAME,
                points_selector=Filter(
                    must=[FieldCondition(key="meeting_id",
                                         match=MatchValue(value=meeting_id))]
                ),
            )
            segments = []
            tp = out_dir / "transcript.json"
            if tp.exists():
                segments = json.loads(tp.read_text(encoding="utf-8")).get("segments", [])
            summary = {}
            sp = out_dir / "summary.json"
            if sp.exists():
                summary = json.loads(sp.read_text(encoding="utf-8"))
            store_in_qdrant(meeting_id, m, segments, summary)
    except Exception as e:
        logger.warning(f"[{meeting_id}] Vector reindex failed (non-fatal): {e}")


# Background reindex worker: coalesces rapid successive edits to the same
# meeting into a single reindex job instead of firing one executor job (and
# one embed pass) per edit. Mirrors the _tag_worker pattern.
REINDEX_DEBOUNCE = 2.0
_reindex_queue: "asyncio.Queue[str]" = asyncio.Queue()
_reindex_pending: set = set()


def _enqueue_reindex(meeting_id: str) -> None:
    """Enqueue a meeting for background reindex (coalesced by meeting_id)."""
    if meeting_id and meeting_id not in _reindex_pending:
        _reindex_pending.add(meeting_id)
        _reindex_queue.put_nowait(meeting_id)


async def _reindex_worker():
    """Background worker: consume meeting_ids from the queue, debounce briefly
    to let a burst of edits coalesce, then reindex once. `mid` is discarded
    from `_reindex_pending` immediately after get() (NOT in a finally after the
    reindex runs) so an edit made DURING the reindex re-enqueues and is not
    lost — reindex freshness matters more here than in the tag worker."""
    while True:
        mid = await _reindex_queue.get()
        _reindex_pending.discard(mid)
        try:
            await asyncio.sleep(REINDEX_DEBOUNCE)
            await asyncio.to_thread(_reindex_now, mid)
        except Exception as e:
            logger.warning(f"reindex worker error (non-fatal): {e}")
        finally:
            _reindex_queue.task_done()


def _reindex_meeting_safe(meeting_id: str) -> None:
    """Back-compat alias: existing callers fire-and-forget a reindex by name;
    this now enqueues onto the coalescing queue instead of spawning its own
    executor job."""
    _enqueue_reindex(meeting_id)


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


def _read_extracted_text(filename: str) -> str:
    """Return an attachment's extracted text from its Phase A `.extracted` sidecar
    (`attachments/.extracted/<filename>.json`, shape
    {text, method, chars, extracted_at, status}). Only a `done` extraction yields
    text; a missing / pending / empty / failed / unreadable sidecar yields "".
    Never raises."""
    try:
        base = (notes_store.attachments_dir(notes_store.NOTES_DIR) / ".extracted").resolve()
        p = (base / f"{filename}.json").resolve()
        if p.parent != base:
            return ""  # traversal attempt or nested path — never read outside .extracted
        if not p.exists():
            return ""
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict) or data.get("status") != "done":
        return ""
    return data.get("text") or ""


def _gather_meeting_context(meeting_id: str) -> str:
    """Concatenate the body + extracted attachment text of every note the user
    EXPLICITLY linked to this meeting via its `linked_meetings` frontmatter
    (deterministic and user-controlled — not fuzzy related-notes). Hard-capped at
    MEETING_CONTEXT_MAX characters. Returns "" when nothing is linked, which keeps
    the analysis prompt byte-for-byte identical to today. Never raises — meeting
    analysis must not break because of note/attachment reads."""
    try:
        index = notes_store.get_index(notes_store.NOTES_DIR)
    except Exception as e:
        logger.warning(f"[{meeting_id}] context gather: notes index read failed (non-fatal): {e}")
        return ""
    parts: list[str] = []
    for rec in index.values():
        if meeting_id not in (rec.get("linked_meetings") or []):
            continue
        try:
            note = notes_store.read_note(notes_store.NOTES_DIR, rec["id"])
        except Exception:
            note = None
        if not note:
            continue
        parts.append(f"## Linked note: {note.get('title') or 'Untitled'}\n\n{note.get('body') or ''}")
        try:
            filenames = notes_store.note_attachments(notes_store.NOTES_DIR, rec["id"])
        except Exception:
            filenames = []
        for fname in filenames:
            text = _read_extracted_text(fname)
            if text:
                parts.append(f"### Attachment: {fname}\n\n{text}")
    if not parts:
        return ""
    return "\n\n".join(parts)[:MEETING_CONTEXT_MAX]


async def step_summarize(transcript_text: str, duration: float, progress_callback=None, context: str = "") -> dict:
    """Use Ollama to generate a structured meeting summary via 6 focused passes."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)

    # For long meetings (>90 min), do hierarchical summarization
    if duration > 5400:
        return await _hierarchical_summarize(transcript_text, duration, model, temperature, progress_callback, context=context)

    return await _run_analysis_passes(transcript_text, model, temperature, progress_callback, context=context)


async def _run_analysis_passes(transcript_text: str, model: str, temperature: float, progress_callback=None, context: str = "") -> dict:
    """Run 6 sequential analysis passes against the transcript, each with a focused prompt."""
    settings = load_settings()
    prompts = settings.get("prompts", {})

    # Phase C: prepend context from notes the user linked to this meeting. Done
    # ONCE here, before the pass loop, so all six prompts share it. An empty or
    # None context leaves transcript_text untouched, keeping every prompt
    # byte-for-byte identical to the pre-context behaviour (regression-guarded).
    if context:
        transcript_text = (
            "[Context from notes linked to this meeting — background only; "
            "base all analysis on the transcript that follows]\n\n"
            f"{context}\n\n"
            "[Transcript]\n\n"
            f"{transcript_text}"
        )

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
            return json.loads(p.read_text(encoding="utf-8"))
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


async def auto_tag_note(title: str, body: str, *, attachment_text: str = "") -> dict:
    """Tag a note the way meetings are tagged (analysis_pass_g). Non-fatal -> defaults.
    Body comes first; attachment text is appended, then the whole blob is capped."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)
    template = settings["prompts"].get("analysis_pass_g", DEFAULT_PROMPTS.get("analysis_pass_g", ""))
    blob = f"{title}\n\n{body}"
    if attachment_text:
        blob = f"{blob}\n\n{attachment_text}"
    text = blob[:16000]
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


async def analyze_note_text(text: str) -> dict:
    """Run one structured 'note_analysis' pass over a note+attachments corpus.
    Non-fatal: any failure or unparseable/empty response yields empty defaults."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)
    template = settings["prompts"].get("note_analysis", DEFAULT_PROMPTS.get("note_analysis", ""))
    prompt = template.replace("{corpus}", text)
    default = {"summary": "", "key_points": [], "action_items": [], "insights": []}
    try:
        body = _build_generate_body(
            model, prompt, temperature=temperature, num_predict=3072,
            schema=ANALYSIS_SCHEMAS.get("note_analysis"))  # num_ctx sized by _ctx_for_text
        resp = await _retry_ollama_call(
            "POST", f"{OLLAMA_URL}/api/generate",
            json_body=body, timeout_seconds=300.0, max_retries=2)
        parsed = _parse_json_object(resp.json().get("response", ""), context="note-analysis")
        if not parsed:
            return default
        return {
            "summary": str(parsed.get("summary", "")),
            "key_points": [str(x) for x in (parsed.get("key_points") or []) if x],
            "action_items": [str(x) for x in (parsed.get("action_items") or []) if x],
            "insights": [str(x) for x in (parsed.get("insights") or []) if x],
        }
    except Exception as e:
        logger.warning(f"note analysis failed (non-fatal): {e}")
        return default


async def cleanup_note_text(text: str) -> str:
    """Silently polish a short dictated-text fragment: fix punctuation/filler, reflow
    into clean paragraphs/bullets, tighten phrasing -- preserve meaning and every fact.
    Non-fatal: any failure or unparseable/empty response returns the original text
    unchanged, since cleanup must never blank out what the user said."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)
    template = settings["prompts"].get("note_cleanup", DEFAULT_PROMPTS.get("note_cleanup", ""))
    prompt = template.replace("{text}", text)
    try:
        body = _build_generate_body(
            model, prompt, temperature=temperature, num_predict=2048,
            schema=ANALYSIS_SCHEMAS.get("note_cleanup"))
        resp = await _retry_ollama_call(
            "POST", f"{OLLAMA_URL}/api/generate",
            json_body=body, timeout_seconds=120.0, max_retries=2)
        parsed = _parse_json_object(resp.json().get("response", ""), context="note-cleanup")
        cleaned = str(parsed.get("text", "")).strip() if parsed else ""
        return cleaned if cleaned else text
    except Exception as e:
        logger.warning(f"note cleanup failed (non-fatal): {e}")
        return text


def _build_analysis_corpus(note: dict, extractions: list[dict]) -> str:
    """Assemble the analysis corpus: title header + body + each attachment's
    extracted text, blank-line separated, hard-capped at ANALYSIS_CORPUS_MAX."""
    parts = []
    title = (note.get("title") or "").strip()
    if title:
        parts.append(f"# {title}")
    body = note.get("body") or ""
    if body.strip():
        parts.append(body)
    for ex in extractions or []:
        text = (ex.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)[:ANALYSIS_CORPUS_MAX]


async def _resolve_note_extractions(note_id: str) -> list[dict]:
    """Ensure every referenced attachment has a resolved .extracted sidecar, running
    deferred vision NOW (explicit user action -> allowed to use the GPU). A terminal
    sidecar is reused; a pending 'vision' method is resolved via llm.describe_image;
    other pending methods (e.g. 'stt') are left to Phase A's idle extraction worker
    and contribute no text this pass. Non-fatal per attachment: a failure yields a
    'failed' Extraction and never aborts the analysis."""
    results = []
    loop = asyncio.get_event_loop()
    for fname in notes_store.note_attachments(notes_store.NOTES_DIR, note_id):
        p = notes_store.attachment_path(notes_store.NOTES_DIR, fname)
        if p is None:
            continue
        side_path = extract.extracted_sidecar_path(
            notes_store.attachments_dir(notes_store.NOTES_DIR), fname)
        ex = None
        if side_path.exists():
            try:
                raw = await loop.run_in_executor(None, lambda: side_path.read_text(encoding="utf-8"))
                ex = json.loads(raw)
            except Exception:
                ex = None
        if ex and ex.get("status") in ("done", "empty", "failed"):
            results.append(ex)
            continue
        try:
            ex = await loop.run_in_executor(None, extract.extract_text, str(p), fname)
            if ex.get("status") == "pending" and ex.get("method") == "vision":
                text = await llm.describe_image(str(p), prompt=VISION_EXTRACT_PROMPT)
                ex = {"text": text or "", "method": "vision",
                      "chars": len(text or ""), "status": "done"}
        except llm.VisionUnavailable as e:
            # Model not present right now (e.g. still loading / GPU busy elsewhere) —
            # keep this retryable instead of burying it as a terminal failure.
            logger.warning(f"vision model unavailable for {fname}; leaving pending for retry: {e}")
            ex = {"text": "", "method": "vision", "chars": 0, "status": "pending"}
        except Exception as e:
            logger.warning(f"attachment extraction failed for {fname} (non-fatal): {e}")
            ex = {"text": "", "method": (ex or {}).get("method", ""), "chars": 0, "status": "failed"}
        ex.setdefault("status", "empty")
        ex["extracted_at"] = notes_store.now_iso()
        ex["note_id"] = note_id
        try:
            def _write():
                side_path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(side_path, json.dumps(ex, indent=2))
            await loop.run_in_executor(None, _write)
        except Exception as e:
            logger.warning(f"extracted sidecar write failed for {fname} (non-fatal): {e}")
        results.append(ex)
    return results


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
    attach_text = _note_attachment_text(note)
    tags = await auto_tag_note(note.get("title", ""), note.get("body", ""),
                               attachment_text=attach_text)
    kws = list(tags.get("keywords", []))
    if tags.get("category") and tags["category"] != "other":
        kws.append(tags["category"])
    notes_store.apply_auto_tags(notes_store.NOTES_DIR, note_id, tags.get("category", ""), kws)
    state.setdefault(note_id, {})["tag_sig"] = sig
    _save_enhance_state(state)
    return True


async def _hierarchical_summarize(transcript: str, duration: float, model: str = None, temperature: float = None, progress_callback=None, context: str = "") -> dict:
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
    return await _run_analysis_passes(prefix + combined, model, temperature, progress_callback, context=context)


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


# --- Deterministic filler pre-pass (Layer 1 of the remove_filler toggle) ---
# Standalone disfluency tokens plus natural elongations: um/umm, uh/uhh,
# er/err/erm, ah/ahh, hmm/hmmm, mm/mmm. The [\w'-] guards make matches
# word-boundary safe AND hyphen/apostrophe safe: "umbrella", "summer",
# "ahead", "ermine" are untouched, and the affirmations "uh-huh" / "mm-hmm"
# are deliberately preserved (they carry meaning: agreement). Accepted
# tradeoff (spec): the rare verb "err" is stripped.
_FILLER_RE = re.compile(r"(?<![\w'-])(?:um+|uh+|er+m?|ah+|hm+|mm+)(?![\w'-])",
                        re.IGNORECASE)


def strip_fillers(text: str) -> str:
    """Strip standalone filler tokens from one segment text. Pure.

    Repair order (spec): remove tokens -> collapse comma runs -> drop space
    before punctuation -> strip leading whitespace/commas/periods/semicolons ->
    collapse whitespace runs -> recapitalize if a leading filler was removed.
    """
    out = _FILLER_RE.sub("", text)
    if out == text:
        return text
    removed_at_start = _FILLER_RE.match(text.lstrip()) is not None
    out = re.sub(r"(?:,\s*)+,", ",", out)        # ", , " -> ", "   ",," -> ","
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)   # "word ," -> "word,"
    out = re.sub(r"^[\s,.;]+", "", out)          # ", so we..." -> "so we..."
    out = re.sub(r"\s{2,}", " ", out).strip()
    if removed_at_start:
        for i, ch in enumerate(out):
            if ch.isalpha():
                out = out[:i] + ch.upper() + out[i + 1:]
                break
    return out


def apply_filler_prepass(segments: list[dict]) -> tuple[list[dict], int]:
    """Regex pre-pass over cleanup input segments. Pure; input list untouched.

    Returns (new_segments, changed) where changed counts modified PLUS dropped
    segments. A segment whose stripped text has no alphanumeric character was
    pure filler (e.g. a lone "Um.") and is DROPPED — timestamps and speaker
    label go with it, exactly as if STT had never emitted it. This is the only
    mechanism that can remove such a segment: _parse_cleanup_response requires
    exactly one non-empty line per input segment, so the LLM cannot delete one.
    Safety guard: if EVERY segment would drop (pathological input), return the
    input unchanged with count 0 — never produce an empty transcript.
    """
    out: list[dict] = []
    changed = 0
    for seg in segments:
        text = seg.get("text") or ""
        new_text = strip_fillers(text)
        if not re.search(r"[A-Za-z0-9]", new_text):
            changed += 1  # pure filler: drop
            continue
        if new_text != text:
            changed += 1
        out.append({**seg, "text": new_text})
    if not out:
        logger.warning("Filler pre-pass would drop every segment; leaving transcript unchanged")
        return segments, 0
    return out, changed


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
    preamble = system_preamble.replace("{meeting_context}", meeting_context or "")
    if settings.get("remove_filler", True):
        # Build-time directive — never persisted into the saved template.
        preamble += FILLER_DIRECTIVE
    parts.append(preamble)
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


def _segment_texts_differ(before: list[dict], after: list[dict]) -> bool:
    """True when cleanup changed anything — text edits OR dropped segments.
    zip() alone truncates to the shorter list, so a drop-only filler pre-pass
    run would otherwise report zero changes and skip the transcript rewrite."""
    return len(before) != len(after) or any(
        a["text"] != b["text"] for a, b in zip(before, after))


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

    if settings.get("remove_filler", True):
        # Layer 1: deterministic strip before any LLM batch. May DROP
        # pure-filler segments, so the returned list can be SHORTER than the
        # input (callers compare length-aware; see _segment_texts_differ).
        segments, _prepass_changes = apply_filler_prepass(segments)

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


def reconcile_speaker_tags(
    markers: list[dict],
    segments: list[dict],
    roster: list[dict] | None = None,
    lag: float = 2.0,
) -> dict:
    """Map human 'who is talking' markers to diarized SPEAKER_XX clusters.

    markers: [{"t": recorded_seconds, "name": str}] (name only; never a label).
    segments: diarized [{"start","end","speaker"}].
    roster: optional [{"name","company","title"}] used only to enrich display_name.
    Human-tagged clusters get confidence "manual"; untagged clusters are left for
    the LLM step. Returns {"speaker_map", "speaker_info"} in the standard shape.
    """
    if not markers or not segments:
        return {"speaker_map": {}, "speaker_info": {}}

    roster_lookup = {}
    for r in (roster or []):
        nm = (r.get("name") or "").strip()
        if nm:
            roster_lookup[nm.lower()] = {
                "company": (r.get("company") or "").strip(),
                "title": (r.get("title") or "").strip(),
            }

    # votes[label][name] = total overlap seconds credited to that (cluster, name).
    votes: dict[str, dict[str, float]] = {}
    for m in markers:
        name = (m.get("name") or "").strip()
        if not name or name.lower() in ("unknown", "none", "null"):
            continue
        try:
            t = float(m.get("t"))
        except (TypeError, ValueError):
            continue
        w_start, w_end = t - lag, t + lag
        # Credit each cluster by how much of [w_start, w_end] its segments cover.
        per_label: dict[str, float] = {}
        for seg in segments:
            label = seg.get("speaker", "UNKNOWN")
            if label == "UNKNOWN":
                continue
            ov = min(w_end, seg.get("end", 0.0)) - max(w_start, seg.get("start", 0.0))
            if ov > 0:
                per_label[label] = per_label.get(label, 0.0) + ov
        best_label = None
        best_overlap = 0.0
        for label, ov in per_label.items():
            if ov > best_overlap:
                best_overlap, best_label = ov, label
        if best_label is None:
            continue  # marker in silence / outside all segments -> dropped
        votes.setdefault(best_label, {})
        votes[best_label][name] = votes[best_label].get(name, 0.0) + best_overlap

    speaker_map: dict[str, str] = {}
    speaker_info: dict[str, dict] = {}
    for label, name_votes in votes.items():
        # Majority by total overlap weight.
        max_weight = max(name_votes.values())
        winners = [n for n in name_votes if name_votes[n] == max_weight]
        if len(winners) > 1:
            # Genuine tie — cannot confidently pick. Leave the cluster unresolved
            # so the LLM step / post-meeting editing handles it (spec: never guess silently).
            continue
        name = winners[0]
        extra = roster_lookup.get(name.lower(), {})
        title = extra.get("title", "")
        company = extra.get("company", "")
        parts = [p for p in (title, company) if p]
        display_name = f"{name} ({', '.join(parts)})" if parts else name
        speaker_map[label] = name
        speaker_info[label] = {
            "name": name,
            "title": title,
            "company": company,
            "display_name": display_name,
            "confidence": "manual",
            "auto_detected": False,
        }

    return {"speaker_map": speaker_map, "speaker_info": speaker_info}


def merge_speaker_identifications(tag_ident: dict, llm_ident: dict) -> dict:
    """Combine human-tag identification with the LLM's. Human tags win per label."""
    speaker_map = {**llm_ident.get("speaker_map", {}), **tag_ident.get("speaker_map", {})}
    speaker_info = {**llm_ident.get("speaker_info", {}), **tag_ident.get("speaker_info", {})}
    return {"speaker_map": speaker_map, "speaker_info": speaker_info}


async def step_identify_speakers(transcript_text: str, segments: list[dict],
                                 expected_participants: str = "") -> dict:
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
        "{expected_participants}", expected_participants or ""
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
        data = json.loads(transcript_path.read_text(encoding="utf-8"))
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

        _stt_cfg = load_settings()
        _stt_backend = _stt_cfg.get("stt_backend")          # None -> stt env default
        _stt_diarize = _stt_cfg.get("diarize", True)

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
                backend=_stt_backend,
                diarize=_stt_diarize,
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

        # Layer 1: deterministic filler pre-pass (raw_segments were deep-copied
        # above, so raw_transcript.json keeps the unstripped STT output).
        if cleanup_settings.get("remove_filler", True):
            segments, prepass_changes = apply_filler_prepass(segments)
            if prepass_changes > 0:
                # Bookkeeping BEFORE the LLM try-block: its catch-all swallows a
                # total LLM failure, and stripped `segments` must never pair with
                # an unstripped transcript_text, a stale segment_count, or
                # "cleaned": false (speaker ID / summary / Qdrant read these).
                transcript_text = build_transcript_text(segments)
                meeting["segment_count"] = len(segments)
                meeting["transcript_cleaned"] = True
                logger.info(f"[{meeting_id}] Filler pre-pass: {prepass_changes} segments modified or dropped")

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

        # Human live-tag reconciliation (empty if the meeting wasn't tagged live).
        markers = meeting.get("speaker_tags") or []
        roster = meeting.get("speaker_roster") or []
        tag_ident = reconcile_speaker_tags(markers, segments, roster)

        # Diarized labels present in this transcript.
        all_labels = sorted({s.get("speaker", "UNKNOWN") for s in segments} - {"UNKNOWN"})
        resolved = set(tag_ident["speaker_map"])
        # Skip the LLM call entirely if every cluster was tagged live.
        if all_labels and resolved >= set(all_labels):
            llm_ident = {"speaker_map": {}, "speaker_info": {}}
            logger.info(f"[{meeting_id}] All {len(all_labels)} speakers resolved from live tags; skipping LLM ID")
        else:
            expected = ", ".join(sorted({(r.get("name") or "").strip() for r in roster} - {""}))
            llm_ident = await step_identify_speakers(transcript_text, segments, expected)

        identification = merge_speaker_identifications(tag_ident, llm_ident)
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
        context = await asyncio.get_event_loop().run_in_executor(
            None, _gather_meeting_context, meeting_id)
        summary = await step_summarize(transcript_text, duration, progress_callback=_summarize_progress, context=context)
        if meeting.get("title_edited"):
            # Manual rename wins over the LLM title; keep summary.json's title
            # key (written below) consistent with the preserved record title.
            summary["title"] = meeting.get("title", summary.get("title"))
        elif "title" in summary and summary["title"] != "Meeting":
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

        # Write raw transcript JSON (original Parakeet+pyannote output)
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

        # Email the summary if SMTP is enabled (non-fatal)
        try:
            await _maybe_send_summary_email(meeting_id, meeting, summary)
        except Exception as e:
            logger.warning(f"[{meeting_id}] Summary email failed (non-fatal): {e}")

        # Push to a360 if configured (fire-and-forget via _run_bg; guarded
        # no-op otherwise — never blocks or fails the pipeline). retry/trim/
        # adopt re-completions inherit this because they all funnel back
        # through process_meeting; single-step reprocess does not fire it.
        def _a360_push(m=meeting):
            try:
                a360.post_meeting_completed(_a360_completion_payload(m))
            except Exception as e:
                logger.warning(f"a360 push failed (non-fatal) [{m.get('id')}]: {e}")
        _run_bg(_a360_push)

    except Exception as e:
        meeting["status"] = MeetingStatus.error
        meeting["error"] = str(e)
        meeting["step_timings"] = step_timings
        logger.error(f"[{meeting_id}] Processing failed: {e}")
        _save_index()
        raise


async def _maybe_send_summary_email(meeting_id: str, meeting: dict, summary: dict):
    """Email the meeting summary if SMTP is enabled and recipients are configured (best-effort)."""
    smtp = load_settings().get("smtp", {})
    if not smtp.get("enabled"):
        return
    recipients = emailer.parse_recipients(smtp.get("recipients"))
    if not smtp.get("host") or not recipients:
        logger.info(f"[{meeting_id}] Summary email skipped: SMTP host or recipients not configured")
        return

    text_body = build_summary_markdown(summary, meeting)
    subject, html_body, text_body = emailer.render_summary_email(
        meeting, summary, meeting.get("speaker_info", {}), text_body=text_body
    )
    await asyncio.get_event_loop().run_in_executor(
        None, emailer.send_email, smtp, recipients, subject, html_body, text_body
    )
    logger.info(f"[{meeting_id}] Summary email sent to {', '.join(recipients)}")


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


async def _link_note_to_meeting(note_id: Optional[str], meeting_id: str) -> None:
    """Best-effort: add meeting_id to a vault note's linked_meetings frontmatter.

    Called by upload_meeting / capture_adopt right after the meeting insert and
    on their sid-dedup early-return paths (repairing a link whose first attempt
    predated the note's flush). Idempotent — link_meeting is a membership add,
    so retries/races can never double-link.

    Runs ON THE THREADPOOL, not inline: link_meeting -> find_path -> get_index
    can trigger a synchronous full index rebuild (seconds over the bind mount),
    and upload_meeting is the hot path offline-queue flushes and mobile
    reconnects hit — an inline call could stall the event loop and block every
    concurrent request (api_link_meeting stays inline: small, user-initiated).

    Never raises — the upload/adopt must never fail on linking. No format
    validation beyond a 64-char length cap: the id is only ever an index key
    (find_path never joins it into a filesystem path), so malformed and temp
    (n_local_*) ids simply miss the lookup — the ignored-malformed-sid rule.
    """
    if not note_id or len(note_id) > 64:
        return
    try:
        rec = await asyncio.to_thread(
            notes_store.link_meeting, notes_store.NOTES_DIR, note_id, meeting_id, True)
        if rec is None:
            logger.warning(f"[{meeting_id}] note link skipped — unknown note_id {note_id!r}")
    except Exception as e:
        logger.warning(f"[{meeting_id}] note link failed (non-fatal): {e}")


@app.post("/meetings/upload")
async def upload_meeting(
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    min_speakers: Optional[int] = Form(default=None),
    max_speakers: Optional[int] = Form(default=None),
    meeting_context: Optional[str] = Form(default=None),
    speaker_tags: Optional[str] = Form(default=None),
    speaker_roster: Optional[str] = Form(default=None),
    sid: Optional[str] = Form(default=None),
    note_id: Optional[str] = Form(default=None),
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

    # Idempotency: if this capture session was already turned into a meeting —
    # a second tab flushing the offline queue, or a retry after a lost 202 —
    # return that meeting instead of creating a duplicate (a duplicate would
    # spawn a second GPU transcription job). The scan below and the insert
    # further down run with no `await` between them, so two concurrent uploads
    # of the same sid can't both slip past this check. A malformed sid is
    # ignored (not used for dedup, not stored) rather than rejected.
    valid_sid = sid if _valid_sid(sid) else None
    if valid_sid:
        for existing_id, m in meetings.items():
            if m.get("source_sid") == valid_sid:
                logger.info(f"[{existing_id}] Duplicate upload for capture {valid_sid} — returning existing meeting")
                # Dedup-path link repair: the first attempt may have uploaded
                # before the note flushed (no note_id then); this retry carries
                # the real id. Idempotent — link_meeting checks membership.
                await _link_note_to_meeting(note_id, existing_id)
                return JSONResponse(
                    content={"meeting_id": existing_id, "status": "queued"},
                    status_code=202,
                )

    meeting_id = str(uuid.uuid4())[:8]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Save uploaded file to temp location
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = str(MEETINGS_DIR / f"_upload_{meeting_id}{suffix}")
    with open(tmp_path, "wb") as f:
        f.write(content)

    def _parse_json_list(raw):
        if not raw:
            return []
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    parsed_tags = _parse_json_list(speaker_tags)
    parsed_roster = _parse_json_list(speaker_roster)

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
        "speaker_tags": parsed_tags,
        "speaker_roster": parsed_roster,
        "source_sid": valid_sid,        # for upload idempotency (None when absent/invalid)
        "progress_percent": 0,
        "progress_detail": "Queued",
    }
    meetings[meeting_id] = meeting
    _save_index()

    logger.info(f"[{meeting_id}] Meeting uploaded: {file.filename} ({len(content) / (1024*1024):.1f} MB)")

    # Link the in-meeting note BEFORE processing is scheduled, so the link
    # exists before Phase C's _gather_meeting_context could ever look
    # (belt-and-braces on top of transcription taking minutes anyway).
    await _link_note_to_meeting(note_id, meeting_id)

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


@app.get("/people")
async def list_people():
    """Distinct speakers seen across meetings, for live-roster autocomplete.

    Most-recent meeting wins for each person's company/title.
    """
    # Sort meetings oldest->newest so later writes overwrite earlier ones.
    ordered = sorted(meetings.values(), key=lambda m: (m.get("date") or "", m.get("created_at") or ""))
    people: dict[str, dict] = {}
    for m in ordered:
        info = m.get("speaker_info") or {}
        for entry in info.values():
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            if not name or name.upper().startswith("SPEAKER_"):
                continue
            people[name.lower()] = {
                "name": name,
                "company": (entry.get("company") or "").strip(),
                "title": (entry.get("title") or "").strip(),
            }
    return sorted(people.values(), key=lambda p: p["name"].lower())


def _compact_meeting_summary(mid: str, m: dict) -> dict:
    """Return a compact summary dict for grouped view items."""
    return {
        "id": mid,
        "date": m.get("date"),
        "title": m.get("title"),
        "status": m.get("status"),
        "duration_formatted": m.get("duration_formatted"),
        "tags": m.get("tags", {}),
        "company": m.get("company"),
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
    company: Optional[str] = Query(default=None),
):
    """List all meetings with their status, with optional filtering."""
    company_key = _norm_company(company).casefold() if company else None
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

        # Company filter: case-insensitive normalized EXACT match against the
        # CONFIRMED company only — suggestion-only meetings never match.
        if company_key is not None:
            mine = m.get("company")
            if not mine or _norm_company(mine).casefold() != company_key:
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
            "company": m.get("company"),
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
        "transcript_edited": m.get("transcript_edited", False),
        # Company tag: confirmed value (persisted) or None; the suggestion is
        # computed lazily and ONLY when no confirmed company exists (a response
        # never pairs both) — this is also the legacy-meeting migration path.
        # NOTE: a sibling feature may have added `transcript_edited` here — insert
        # after whatever the final entry is; do not replace the dict.
        "company": m.get("company"),
        "company_suggestion": (None if m.get("company") else suggest_company(m)),
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
        return json.loads(transcript_path.read_text(encoding="utf-8"))

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
        return json.loads(summary_path.read_text(encoding="utf-8"))

    raise HTTPException(status_code=404, detail="Summary file not found")


class MeetingPatch(BaseModel):
    title: str


@app.patch("/meetings/{meeting_id}")
async def patch_meeting(meeting_id: str, body: MeetingPatch):
    """Rename a completed meeting. Sets title_edited so LLM re-summarization
    never clobbers the manual title; mirrors the title into summary.json (the
    source of summary.md's H1) BEFORE regenerating the markdown exports; then
    re-embeds — every Qdrant payload stamps the title. The on-disk folder name
    is a creation-time slug and is deliberately NOT renamed (output_dir is the
    stable pointer everything holds)."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")

    m["title"] = title
    m["title_edited"] = True

    out_dir = Path(m.get("output_dir", ""))

    # 1) Rewrite summary.json's own title key FIRST — build_summary_markdown
    # takes its H1 from summary.get("title"), not from the meeting record.
    summary_path = out_dir / "summary.json"
    summary_data = None
    if summary_path.exists():
        try:
            summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
            summary_data["title"] = title
            _atomic_write(summary_path, json.dumps(summary_data, indent=2))
            if isinstance(m.get("summary"), dict):
                m["summary"]["title"] = title
        except Exception as e:
            logger.warning(f"[{meeting_id}] summary.json title rewrite failed (non-fatal): {e}")
            summary_data = None

    # 2) Regenerate the derived markdown files that embed the title.
    transcript_path = out_dir / "transcript.json"
    if transcript_path.exists():
        try:
            segments = json.loads(transcript_path.read_text(encoding="utf-8")).get("segments", [])
            _atomic_write(out_dir / "transcript.md", build_transcript_markdown(segments, m))
        except Exception as e:
            logger.warning(f"[{meeting_id}] transcript.md regeneration failed (non-fatal): {e}")
    if summary_data is not None:
        _atomic_write(out_dir / "summary.md", build_summary_markdown(summary_data, m))

    _save_index()
    _reindex_meeting_safe(meeting_id)
    return {"detail": "Title updated", "title": title}


# The five approved editable summary fields (spec: topics/figures/sentiment/tags
# are out of scope). Two have legacy aliases from the old summary schema; the
# write path canonicalizes so every `x or legacy` reader picks up the edit.
ALLOWED_SUMMARY_FIELDS = {"summary", "action_items", "decisions", "concerns", "open_questions"}
_SUMMARY_LEGACY_ALIASES = {"summary": "executive_summary", "open_questions": "questions_raised"}


class SummaryFieldPut(BaseModel):
    value: Any


@app.put("/meetings/{meeting_id}/summary/{field}")
async def update_summary_field(meeting_id: str, field: str, body: SummaryFieldPut):
    """Replace one summary field wholesale (edit-in-place UI granularity).
    Single-user app: the client sends back the array it received with one
    item's text changed. NOTE: keep this handler's read-modify-write synchronous
    (no await between the file read and _atomic_write) — that invariant is what
    makes summary.json writers safe without an expected_* guard."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")
    if field not in ALLOWED_SUMMARY_FIELDS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown field. Allowed: {sorted(ALLOWED_SUMMARY_FIELDS)}")

    value = body.value
    if field == "summary":
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=400, detail="summary must be a non-empty string")
        value = value.strip()
    else:
        if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
            raise HTTPException(status_code=400, detail=f"{field} must be a list of objects")

    out_dir = Path(m.get("output_dir", ""))
    summary_path = out_dir / "summary.json"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="Summary file not found")
    summary_data = json.loads(summary_path.read_text(encoding="utf-8"))

    if field == "action_items":
        current = summary_data.get("action_items", [])
        if len(value) != len(current):
            raise HTTPException(
                status_code=400,
                detail=f"action_items must keep the same length ({len(current)}); "
                       "add/remove is not supported (task_overlay.json is keyed by index)")
        # Summary-side edit wins over a stale To-Do-side text override: clear the
        # overlay's text/edited keys for each index whose task text changed,
        # preserving done/deleted state (spec, Overlay precedence).
        def _task_text(item):
            return item.get("task") or item.get("description", "")
        changed = [i for i, (old, new) in enumerate(zip(current, value))
                   if _task_text(old) != _task_text(new)]
        if changed:
            overlay = _load_meeting_overlay(meeting_id)
            dirty = False
            for i in changed:
                entry = overlay.get(str(i))
                if entry and ("text" in entry or "edited" in entry):
                    entry.pop("text", None)
                    entry.pop("edited", None)
                    dirty = True
            if dirty:
                _save_meeting_overlay(meeting_id, overlay)

    summary_data[field] = value
    legacy = _SUMMARY_LEGACY_ALIASES.get(field)
    if legacy:
        summary_data.pop(legacy, None)

    _atomic_write(summary_path, json.dumps(summary_data, indent=2))
    _atomic_write(out_dir / "summary.md", build_summary_markdown(summary_data, m))
    m["summary"] = summary_data          # related-notes reads the in-memory copy
    _save_index()
    _reindex_meeting_safe(meeting_id)
    return summary_data


class SegmentEdit(BaseModel):
    text: str
    expected_text: Optional[str] = None  # optimistic guard, mirrors TaskToggle


@app.put("/meetings/{meeting_id}/segments/{index}")
async def update_segment(meeting_id: str, index: int, body: SegmentEdit):
    """Edit one transcript segment's text in place. expected_text (when sent)
    must match the current on-disk text or the edit 409s and writes nothing —
    the same stale-guard contract as task-line edits. raw_transcript.json is
    deliberately untouched (pre-cleanup source; reprocess 'cleanup' re-derives
    from it and is an accepted destructive re-run)."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]
    if m.get("status") != MeetingStatus.complete:
        raise HTTPException(status_code=409, detail=f"Meeting not ready (status: {m.get('status')})")

    new_text = (body.text or "").strip()
    if not new_text:
        raise HTTPException(status_code=400, detail="Segment text cannot be empty")

    out_dir = Path(m.get("output_dir", ""))
    transcript_path = out_dir / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=404, detail="Transcript file not found")
    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    segments = transcript_data.get("segments", [])
    if index < 0 or index >= len(segments):
        raise HTTPException(status_code=404, detail="Segment not found")
    if body.expected_text is not None and body.expected_text != segments[index].get("text"):
        raise HTTPException(status_code=409, detail="Segment changed; refresh")

    segments[index]["text"] = new_text
    _atomic_write(transcript_path, json.dumps(transcript_data, indent=2))
    _atomic_write(out_dir / "transcript.srt", _generate_srt(segments))
    _atomic_write(out_dir / "transcript.md", build_transcript_markdown(segments, m))
    m["transcript_text"] = build_transcript_text(segments)
    m["transcript_edited"] = True
    _save_index()
    _reindex_meeting_safe(meeting_id)
    return segments[index]


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
            data = json.loads(transcript_path.read_text(encoding="utf-8"))
            segments = data.get("segments", [])
            if segments:
                md = build_transcript_markdown(segments, m)
                _atomic_write(file_path, md)
        elif filename == "summary.md":
            summary_path = out_dir / "summary.json"
            if summary_path.exists():
                summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
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

        # Serialize with any queued/running reindex job for this meeting so the
        # job can't re-upsert points after this delete (permanent orphans that
        # /meetings/search would keep surfacing — see _reindex_meeting_safe).
        # Runs off the event loop (asyncio.to_thread) because threading.Lock is
        # blocking: holding it synchronously here would freeze the whole server
        # for as long as a queued reindex job (real Qdrant delete + re-embed)
        # takes to release it.
        def _locked_delete():
            with _lock_for(meeting_id):
                # Re-check membership now that we hold the per-meeting lock --
                # mirrors _reindex_meeting_safe's "deleted while queued" guard
                # above, so a second/losing caller (however it got here) can
                # never KeyError on an already-removed meeting.
                if meeting_id not in meetings:
                    return False

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

                meetings.pop(meeting_id, None)
                _save_index()
                return True

        deleted = await asyncio.to_thread(_locked_delete)

    if not deleted:
        raise HTTPException(status_code=404, detail="Meeting not found")
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
            tags = json.loads(tags_path.read_text(encoding="utf-8"))
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
                current_tags = json.loads(tags_path.read_text(encoding="utf-8"))
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


class CompanyUpdateRequest(BaseModel):
    company: Optional[str] = None   # null or "" clears the tag


@app.patch("/meetings/{meeting_id}/company")
async def update_company(meeting_id: str, body: CompanyUpdateRequest):
    """Set or clear a meeting's confirmed company tag. User metadata, not
    pipeline output — allowed for ANY meeting status. index.json is the source
    of truth (via _save_index); the summary.json mirror is best-effort, a
    mirror failure logs a warning and never fails the request (house
    non-fatal pattern, same as update_tags' tags.json write)."""
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]
    value = _norm_company(body.company or "")
    if value:
        m["company"] = value
    else:
        m.pop("company", None)
    _save_index()

    out_dir = Path(m.get("output_dir", ""))
    summary_path = out_dir / "summary.json"
    if m.get("output_dir") and summary_path.exists():
        try:
            # FRESH read on purpose (read-modify-write) — never a cached JSON
            # read: a stale summary here would clobber newer fields on rewrite.
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            if value:
                data["company"] = value
            else:
                data.pop("company", None)
            _atomic_write(summary_path, json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"[{meeting_id}] company mirror to summary.json failed (non-fatal): {e}")

    return {"detail": "Company updated", "company": value or None}


# ---------------------------------------------------------------------------
# a360 (Sales Intel) pull API — bearer-guarded; the only in-app auth
# ---------------------------------------------------------------------------


def require_a360_token(authorization: str = Header(default="")) -> None:
    """Bearer guard for the two a360 pull routes ONLY — a per-route dependency,
    NOT middleware (the BaseHTTPMiddleware stack has known coupling quirks —
    see _run_bg's docstring — and a middleware would either break the browser
    app, which sends no token, or need a path allowlist). This is the first
    in-app auth in this service: host port 8191 is LAN-reachable with no
    Authelia in front.

    Reads the module global at call time so tests can monkeypatch it.
    Secure default: env unset -> 401 (feature off until a token is minted).
    compare_digest on BYTES, not str: constant-time AND no TypeError->500 on a
    non-ASCII token from the unauthenticated LAN port. No token echoes in
    logs; no 403s."""
    if not A360_API_TOKEN:
        raise HTTPException(status_code=401, detail="a360 API disabled",
                            headers={"WWW-Authenticate": "Bearer"})
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.encode("utf-8"), A360_API_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid token",
                            headers={"WWW-Authenticate": "Bearer"})


@app.get("/api/companies", dependencies=[Depends(require_a360_token)])
async def a360_list_companies():
    """a360 pull API: distinct CONFIRMED companies across all meetings.
    Suggestion-only meetings never appear (unconfirmed = not a company row).
    Grouped by casefolded comparison key — display form is the first-seen
    original casing (meetings-dict insertion order) — sorted by meeting_count
    desc then name. In-memory scan of `meetings`; no disk IO."""
    agg: dict[str, dict] = {}
    for m in meetings.values():
        raw = m.get("company")
        if not raw:
            continue
        norm = _norm_company(raw)
        row = agg.setdefault(norm.casefold(),
                             {"company": norm, "meeting_count": 0, "last_meeting_date": ""})
        row["meeting_count"] += 1
        row["last_meeting_date"] = max(row["last_meeting_date"], m.get("date") or "")
    return sorted(agg.values(), key=lambda r: (-r["meeting_count"], r["company"].casefold()))


@app.get("/api/companies/meetings", dependencies=[Depends(require_a360_token)])
async def a360_company_meetings(name: str = Query(...)):
    """a360 pull API: complete meetings whose CONFIRMED company matches `name`.

    `name` is a REQUIRED QUERY PARAM, never a path segment: company names are
    free-form strings from LLM extraction and user-typed rosters and can
    contain '/' ("TBC Bank / JSC") — Starlette path params match one segment
    and do not route %2F, so a path route would let /api/companies enumerate
    rows this endpoint could not fetch, silently breaking the pull-API-as-
    reconciliation guarantee. Missing name -> FastAPI's standard 422.
    Unknown company -> [] (not 404: a typo and "no confirmed meetings yet" are
    indistinguishable, and both are recoverable). Missing/corrupt summary.json
    -> summary null / action_items [] rather than dropping the meeting.
    Sorted date desc."""
    want = _norm_company(name).casefold()
    out = []
    for mid, m in meetings.items():
        mine = m.get("company")
        if not want or not mine or _norm_company(mine).casefold() != want:
            continue
        if m.get("status") != MeetingStatus.complete:
            continue    # nothing to pull yet — no summary exists
        # Read-only summary access via the (mtime_ns, size)-keyed JSON cache
        # (same source meeting_summary serves; the PATCH mirror rewrite changes
        # the mtime, so a fresh confirm is picked up immediately).
        data = _read_json_cached(Path(m.get("output_dir", "")) / "summary.json")
        attendees = []
        for entry in (m.get("speaker_info") or {}).values():
            if not isinstance(entry, dict):
                continue
            pname = (entry.get("name") or "").strip()
            if not pname or pname.upper().startswith("SPEAKER_"):
                continue    # the list_people placeholder filter
            attendees.append({"name": pname,
                              "company": (entry.get("company") or "").strip(),
                              "title": (entry.get("title") or "").strip()})
        out.append({
            "id": mid,
            "date": m.get("date"),
            "title": m.get("title"),
            "duration_formatted": m.get("duration_formatted"),
            "company": mine,
            "summary": data.get("summary") if isinstance(data, dict) else None,
            "action_items": (data.get("action_items") or []) if isinstance(data, dict) else [],
            "attendees": attendees,
        })
    out.sort(key=lambda r: r["date"] or "", reverse=True)
    return out


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
            return json.loads(notes_path.read_text(encoding="utf-8"))
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
                    entry = json.loads(f.read_text(encoding="utf-8"))
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
        data = json.loads(legacy.read_text(encoding="utf-8"))
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
            entry = json.loads(f.read_text(encoding="utf-8"))
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
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
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
        return json.loads(path.read_text(encoding="utf-8"))

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
        speaker_info = json.loads(speaker_info_path.read_text(encoding="utf-8"))
        return {"speaker_info": speaker_info}

    speaker_map_path = out_dir / "speaker_map.json"
    if speaker_map_path.exists():
        speaker_map = json.loads(speaker_map_path.read_text(encoding="utf-8"))
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
            existing_map = json.loads(existing_map_path.read_text(encoding="utf-8"))
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
        transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
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
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
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
            speaker_info = json.loads(speaker_info_path.read_text(encoding="utf-8"))
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

    # Re-embed: Qdrant payloads stamp speaker names into text + the speaker
    # field, so renames must delete->re-store or RAG answers with old names.
    _reindex_meeting_safe(meeting_id)

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
        transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
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
        speaker_map = json.loads(speaker_map_path.read_text(encoding="utf-8"))
        for label, name in list(speaker_map.items()):
            if name in sources:
                speaker_map[label] = target
        _atomic_write(speaker_map_path, json.dumps(speaker_map, indent=2))
        m["speaker_map"] = speaker_map

    # Update speaker_info.json — remove merged speakers
    speaker_info_path = out_dir / "speaker_info.json"
    if speaker_info_path.exists():
        try:
            speaker_info = json.loads(speaker_info_path.read_text(encoding="utf-8"))
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
        summary_text = summary_path.read_text(encoding="utf-8")
        for src in sources:
            summary_text = summary_text.replace(src, target)
        _atomic_write(summary_path, summary_text)
        try:
            summary_data = json.loads(summary_text)
            _atomic_write(out_dir / "summary.md", build_summary_markdown(summary_data, m))
        except Exception:
            pass

    _save_index()
    _reindex_meeting_safe(meeting_id)
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

    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
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
    _reindex_meeting_safe(meeting_id)
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


class TrimRequest(BaseModel):
    start: float
    end: float
    title: Optional[str] = None


@app.post("/meetings/{meeting_id}/trim")
async def trim_meeting(meeting_id: str, body: TrimRequest):
    """Cut a time span out of a meeting's audio and process it as a NEW meeting.

    The source meeting is left untouched; the trimmed copy goes through the
    normal upload -> process pipeline (transcription, summary, tags).
    """
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    m = meetings[meeting_id]

    out_dir = m.get("output_dir")
    audio_files = list(Path(out_dir).glob("audio.*")) if out_dir else []
    if not audio_files:
        raise HTTPException(status_code=404, detail="No stored audio for this meeting")
    src = audio_files[0]

    start = max(0.0, float(body.start))
    end = float(body.end)
    duration = await asyncio.to_thread(probe_duration, str(src))
    if duration > 0:
        end = min(end, duration)
        if start >= duration:
            raise HTTPException(status_code=400, detail="Start is beyond the end of the audio")
    if end - start < 1.0:
        raise HTTPException(status_code=400, detail="Trimmed span must be at least 1 second")

    new_id = str(uuid.uuid4())[:8]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = src.suffix.lower()
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = str(MEETINGS_DIR / f"_upload_{new_id}{suffix}")
    try:
        await asyncio.to_thread(trim_audio, str(src), tmp_path, start, end)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")[-400:] if e.stderr else ""
        logger.error(f"[{meeting_id}] Trim failed: {stderr}")
        raise HTTPException(status_code=500, detail="ffmpeg failed to trim the audio")

    title = (body.title or "").strip() or f"{m.get('title', 'Meeting')} (trimmed)"
    meeting = {
        "id": new_id,
        "date": date_str,
        "title": title,
        "status": MeetingStatus.queued,
        "original_path": tmp_path,
        "original_filename": f"{src.stem}_trim{suffix}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "min_speakers": m.get("min_speakers"),
        "max_speakers": m.get("max_speakers"),
        "meeting_context": m.get("meeting_context"),
        "trimmed_from": {"meeting_id": meeting_id, "start": start, "end": end},
        "progress_percent": 0,
        "progress_detail": "Queued",
    }
    meetings[new_id] = meeting
    _save_index()

    logger.info(f"[{new_id}] Trimmed from [{meeting_id}]: {start:.1f}s-{end:.1f}s of {src.name}")

    task = asyncio.create_task(process_meeting(new_id))
    meeting["_task"] = task

    return JSONResponse(
        content={"meeting_id": new_id, "status": "queued", "title": title},
        status_code=202,
    )


# ---------------------------------------------------------------------------
# Streaming capture (server shadow backup)
# ---------------------------------------------------------------------------


def _valid_sid(sid: str) -> bool:
    return bool(_SID_RE.match(sid or ""))


def _captures_root() -> Path:
    return MEETINGS_DIR / "_captures"


def _capture_dir(sid: str) -> Path:
    return _captures_root() / sid


def _read_capture_meta(sid: str) -> Optional[dict]:
    mp = _capture_dir(sid) / "meta.json"
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_capture_meta(sid: str, meta: dict) -> None:
    d = _capture_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _gc_captures() -> int:
    """Prune capture staging dirs older than CAPTURE_MAX_AGE_SECONDS (or unreadable)."""
    if not _captures_root().exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - CAPTURE_MAX_AGE_SECONDS
    pruned = 0
    for d in _captures_root().iterdir():
        if not d.is_dir():
            continue
        meta = _read_capture_meta(d.name)
        updated = (meta or {}).get("updated_at", 0)
        if not meta or updated < cutoff:
            logger.info(f"GC: pruning stale capture {d.name} (updated_at={updated})")
            shutil.rmtree(d, ignore_errors=True)
            pruned += 1
    return pruned


async def _capture_gc_worker():
    """Sweep stale captures on startup and once a day thereafter."""
    while True:
        try:
            await asyncio.to_thread(_gc_captures)
        except Exception as e:
            logger.warning(f"Capture GC failed (non-fatal): {e}")
        await asyncio.sleep(24 * 60 * 60)


def _sidecar_gc() -> None:
    """Sweep sidecars orphaned when their parent note (or attachment) was
    deleted without the sidecar being cleaned up: (A) attachment
    `.extracted/<stored>.json` sidecars, (B) `.analysis/<note_id>.json`, and
    (C) per-note entries in `.enhance_state.json`. Meeting sidecars are out of
    scope here — delete_meeting rmtree's the whole output_dir already.

    Liveness is always read from DISK, never the in-memory index: notes
    liveness is a fresh (force=True) notes_store.get_index() scan of the
    actual .md files under NOTES_DIR (which excludes .trash/attachments —
    see notes_store._walk_notes), and attachment liveness is the attachment
    file's own existence under attachments/. This avoids deleting a live
    sidecar during an index-staleness window.

    Sync/blocking (file IO over the bind mount) — callers offload it. Never
    raises: a GC error is logged and swallowed so it can't take the app down.
    """
    removed = {"analysis": 0, "enhance_state": 0, "extracted": 0}
    try:
        live_note_ids = set(notes_store.get_index(notes_store.NOTES_DIR, force=True).keys())
        attach_dir = notes_store.attachments_dir(notes_store.NOTES_DIR)

        # (B) .analysis/<note_id>.json — orphaned when the note is gone
        analysis_dir = attach_dir / ".analysis"
        if analysis_dir.is_dir():
            for f in analysis_dir.glob("*.json"):
                if f.stem not in live_note_ids:
                    try:
                        f.unlink()
                        removed["analysis"] += 1
                    except OSError:
                        pass

        # (C) .enhance_state.json — prune keys for notes no longer on disk
        state_path = _enhance_state_path()
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = None
            if isinstance(state, dict):
                pruned = {k: v for k, v in state.items() if k in live_note_ids}
                if len(pruned) != len(state):
                    removed["enhance_state"] = len(state) - len(pruned)
                    _atomic_write(state_path, json.dumps(pruned, indent=2))

        # (A) .extracted/<stored>.json — orphaned when the attachment file itself is gone
        extracted_dir = attach_dir / extract.EXTRACTED_DIRNAME
        if extracted_dir.is_dir():
            for f in extracted_dir.glob("*.json"):
                stored_name = f.name[:-len(".json")] if f.name.endswith(".json") else f.name
                if not (attach_dir / stored_name).exists():
                    try:
                        f.unlink()
                        removed["extracted"] += 1
                    except OSError:
                        pass
    except Exception as e:
        logger.warning(f"Sidecar GC failed (non-fatal): {e}")
        return
    logger.info(
        f"Sidecar GC: removed {removed['analysis']} orphaned .analysis, "
        f"{removed['enhance_state']} orphaned .enhance_state entries, "
        f"{removed['extracted']} orphaned .extracted sidecars"
    )


async def _sidecar_gc_worker():
    """Sweep orphaned note/attachment sidecars on startup and once a day thereafter."""
    while True:
        try:
            await asyncio.to_thread(_sidecar_gc)
        except Exception as e:
            logger.warning(f"Sidecar GC worker failed (non-fatal): {e}")
        await asyncio.sleep(24 * 60 * 60)


class CaptureStartRequest(BaseModel):
    sid: str
    mimeType: Optional[str] = "audio/webm"
    source: Optional[str] = None
    startedAt: Optional[int] = None


@app.post("/captures")
async def capture_start(body: CaptureStartRequest):
    """Announce a new streaming capture. Idempotent: re-announcing keeps existing chunks."""
    sid = body.sid
    if not _valid_sid(sid):
        raise HTTPException(status_code=400, detail="Invalid capture id")
    if (_capture_dir(sid) / "meta.json").exists():
        return JSONResponse(content={"sid": sid, "status": "exists"}, status_code=200)
    (_capture_dir(sid) / "chunks").mkdir(parents=True, exist_ok=True)
    _write_capture_meta(sid, {
        "sid": sid,
        "mimeType": body.mimeType or "audio/webm",
        "source": body.source,
        "startedAt": body.startedAt,
        "stopped": False,
        "bytes": 0,
        "last_seq": -1,
        "chunk_count": 0,
        "updated_at": datetime.now(timezone.utc).timestamp(),
    })
    return JSONResponse(content={"sid": sid, "status": "created"}, status_code=201)


@app.post("/captures/{sid}/chunks/{seq}")
async def capture_chunk(sid: str, seq: int, request: Request):
    """Persist one recorded chunk (raw blob body). Idempotent per seq; order-independent."""
    if not _valid_sid(sid):
        raise HTTPException(status_code=400, detail="Invalid capture id")
    if seq < 0:
        raise HTTPException(status_code=400, detail="Invalid seq")
    meta = _read_capture_meta(sid)
    if meta is None:
        raise HTTPException(status_code=404, detail="Unknown capture")
    data = await request.body()
    if len(data) > CAPTURE_MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail="Chunk too large")
    chunk_path = _capture_dir(sid) / "chunks" / f"{seq:06d}.part"
    is_new = not chunk_path.exists()
    prev = 0 if is_new else chunk_path.stat().st_size
    if meta.get("bytes", 0) - prev + len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Capture exceeds maximum size")
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_path.write_bytes(data)
    meta["bytes"] = max(0, meta.get("bytes", 0) - prev + len(data))
    if is_new:
        meta["chunk_count"] = meta.get("chunk_count", 0) + 1
    meta["last_seq"] = max(meta.get("last_seq", -1), seq)
    meta["updated_at"] = datetime.now(timezone.utc).timestamp()
    _write_capture_meta(sid, meta)
    return {"ok": True, "seq": seq, "bytes": meta["bytes"]}


class CaptureStopRequest(BaseModel):
    durationLabel: Optional[str] = None
    fileName: Optional[str] = None


@app.post("/captures/{sid}/stop")
async def capture_stop(sid: str, body: CaptureStopRequest):
    if not _valid_sid(sid):
        raise HTTPException(status_code=400, detail="Invalid capture id")
    meta = _read_capture_meta(sid)
    if meta is None:
        raise HTTPException(status_code=404, detail="Unknown capture")
    meta["stopped"] = True
    if body.durationLabel:
        meta["durationLabel"] = body.durationLabel
    if body.fileName:
        meta["fileName"] = body.fileName
    meta["updated_at"] = datetime.now(timezone.utc).timestamp()
    _write_capture_meta(sid, meta)
    return {"ok": True}


class CaptureTagsRequest(BaseModel):
    markers: list[dict] = []
    roster: list[dict] = []
    title: Optional[str] = None
    context: Optional[str] = None
    note_id: Optional[str] = None


@app.post("/captures/{sid}/tags")
async def capture_tags(sid: str, body: CaptureTagsRequest):
    """Persist live speaker tags/roster/title/context onto a capture's meta.

    Idempotent: the client re-sends the full current set, so we overwrite.
    """
    if not _valid_sid(sid):
        raise HTTPException(status_code=400, detail="Invalid capture id")
    meta = _read_capture_meta(sid)
    if meta is None:
        raise HTTPException(status_code=404, detail="Unknown capture")
    meta["speaker_tags"] = body.markers or []
    meta["speaker_roster"] = body.roster or []
    if body.title is not None:
        meta["title"] = body.title
    if body.context is not None:
        meta["context"] = body.context
    if body.note_id is not None:
        # Only-when-present: frequent tag-only re-posts from liveTags._flush
        # must never wipe a previously mirrored in-meeting note id. This is
        # what makes dead-device adopt linking possible — a server-only
        # capture has no local meta left, so this copy is the only survivor.
        meta["note_id"] = body.note_id
    meta["updated_at"] = datetime.now(timezone.utc).timestamp()
    _write_capture_meta(sid, meta)
    return {"ok": True, "tag_count": len(meta["speaker_tags"])}


@app.get("/captures")
async def capture_list():
    """List pending captures with recoverable data (newest first)."""
    out = []
    if _captures_root().exists():
        for d in _captures_root().iterdir():
            if not d.is_dir():
                continue
            meta = _read_capture_meta(d.name)
            if not meta or meta.get("chunk_count", 0) <= 0:
                continue
            out.append({
                "sid": meta["sid"],
                "startedAt": meta.get("startedAt"),
                "stopped": meta.get("stopped", False),
                "bytes": meta.get("bytes", 0),
                "chunk_count": meta.get("chunk_count", 0),
                "mimeType": meta.get("mimeType", "audio/webm"),
                "durationLabel": meta.get("durationLabel"),
                "fileName": meta.get("fileName"),
            })
    out.sort(key=lambda c: c.get("startedAt") or 0, reverse=True)
    return out


class CaptureAdoptRequest(BaseModel):
    title: Optional[str] = None
    note_id: Optional[str] = None


@app.post("/captures/{sid}/adopt")
async def capture_adopt(sid: str, body: CaptureAdoptRequest):
    """Assemble a streamed capture's chunks into a new meeting and process it."""
    if not _valid_sid(sid):
        raise HTTPException(status_code=400, detail="Invalid capture id")
    meta = _read_capture_meta(sid)
    if meta is None:
        raise HTTPException(status_code=404, detail="Unknown capture")
    chunk_dir = _capture_dir(sid) / "chunks"
    # Zero-padded names sort in seq order — same assembly as the client's Blob(chunks).
    parts = sorted(chunk_dir.glob("*.part")) if chunk_dir.exists() else []
    if not parts:
        raise HTTPException(status_code=404, detail="Capture has no data")

    # Effective note id: explicit body value, else the server capture meta's
    # mirrored copy (posted by the live tags mirror while recording) — mirrors
    # the existing title fallback below. The recovery UI keeps posting {}.
    eff_note_id = body.note_id or meta.get("note_id")

    # Idempotency across recovery paths: if the local blob already uploaded this
    # capture (a meeting carries source_sid == sid), don't create a second meeting —
    # return the existing one and drop the now-redundant staging dir.
    for existing_id, m in meetings.items():
        if m.get("source_sid") == sid:
            shutil.rmtree(_capture_dir(sid), ignore_errors=True)
            logger.info(f"[{existing_id}] Adopt for already-uploaded capture {sid} — returning existing meeting")
            await _link_note_to_meeting(eff_note_id, existing_id)
            return JSONResponse(
                content={"meeting_id": existing_id, "title": m.get("title"), "status": "queued"},
                status_code=202,
            )

    mime = meta.get("mimeType", "audio/webm")
    ext = ".ogg" if "ogg" in mime else ".m4a" if "mp4" in mime else ".webm"
    new_id = str(uuid.uuid4())[:8]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = str(MEETINGS_DIR / f"_upload_{new_id}{ext}")

    def _assemble():
        with open(tmp_path, "wb") as out:
            for p in parts:
                out.write(p.read_bytes())

    await asyncio.to_thread(_assemble)

    title = (body.title or "").strip() or (meta.get("title") or "").strip() or f"Recovered recording ({date_str})"
    meeting = {
        "id": new_id,
        "date": date_str,
        "title": title,
        "status": MeetingStatus.queued,
        "original_path": tmp_path,
        "original_filename": meta.get("fileName") or f"capture_{sid}{ext}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "recovered_from_capture": sid,
        "source_sid": sid,              # upload idempotency: a later blob-flush upload dedups against this

        "speaker_tags": meta.get("speaker_tags") or [],
        "speaker_roster": meta.get("speaker_roster") or [],
        "meeting_context": meta.get("context") or None,
        "progress_percent": 0,
        "progress_detail": "Queued",
    }
    meetings[new_id] = meeting
    _save_index()

    logger.info(f"[{new_id}] Adopted streamed capture {sid} ({len(parts)} chunks, {meta.get('bytes', 0)} bytes)")

    await _link_note_to_meeting(eff_note_id, new_id)

    task = asyncio.create_task(process_meeting(new_id))
    meeting["_task"] = task

    # Bytes are safely copied into _upload_* now — drop the staging dir.
    shutil.rmtree(_capture_dir(sid), ignore_errors=True)

    return JSONResponse(
        content={"meeting_id": new_id, "status": "queued", "title": title},
        status_code=202,
    )


@app.delete("/captures/{sid}")
async def capture_delete(sid: str):
    if not _valid_sid(sid):
        raise HTTPException(status_code=400, detail="Invalid capture id")
    shutil.rmtree(_capture_dir(sid), ignore_errors=True)
    return {"ok": True}


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
                # Re-clean from the raw (pre-cleanup) transcript.
                raw_path = out_dir / "raw_transcript.json"
                transcript_path = out_dir / "transcript.json"
                if not raw_path.exists():
                    # Legacy meeting: transcript.json is the ONLY copy. Snapshot it
                    # to raw_transcript.json BEFORE any destructive re-cleanup so a
                    # toggle-OFF Re-cleanup can always restore the unstripped text.
                    # If this write raises, the outer except aborts the re-cleanup —
                    # never strip the only remaining copy of the transcript.
                    _atomic_write(raw_path, transcript_path.read_text(encoding="utf-8"))

                data = json.loads(raw_path.read_text(encoding="utf-8"))
                segments = data.get("segments", [])

                m["status"] = MeetingStatus.cleaning_transcript
                _update_progress(m, 42, "Re-cleaning transcript...")
                _save_index()

                cleaned = await step_cleanup_transcript(segments, m.get("meeting_context"))

                # Length-aware: the filler pre-pass can DROP segments (see
                # _segment_texts_differ) — a drop-only run must still rewrite.
                if _segment_texts_differ(segments, cleaned):
                    transcript_text = build_transcript_text(cleaned)
                    transcript_data = {**data, "segments": cleaned, "cleaned": True}
                    _atomic_write(transcript_path, json.dumps(transcript_data, indent=2))
                    _atomic_write(out_dir / "transcript.srt", _generate_srt(cleaned))
                    _atomic_write(out_dir / "transcript.md", build_transcript_markdown(cleaned, m))
                    m["transcript_cleaned"] = True
                    # dropped pure-filler segments change the count (mirrors the
                    # live-path bookkeeping)
                    m["segment_count"] = len(cleaned)

            elif step == "identify_speakers":
                transcript_path = out_dir / "transcript.json"
                raw_transcript_path = out_dir / "raw_transcript.json"

                # Prefer raw_transcript.json which has original SPEAKER_XX labels
                if raw_transcript_path.exists():
                    raw_data = json.loads(raw_transcript_path.read_text(encoding="utf-8"))
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
                            old_map = json.loads(old_map_path.read_text(encoding="utf-8"))
                            reverse_map = {v: k for k, v in old_map.items()}
                            for seg in source_segments:
                                sp = seg.get("speaker", "")
                                if sp in reverse_map:
                                    seg["speaker"] = reverse_map[sp]
                else:
                    # No raw_transcript.json at all, fall back to transcript.json with reverse-map
                    data = json.loads(transcript_path.read_text(encoding="utf-8"))
                    source_segments = data.get("segments", [])
                    old_map_path = out_dir / "speaker_map.json"
                    if old_map_path.exists():
                        old_map = json.loads(old_map_path.read_text(encoding="utf-8"))
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
                    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
                    display_segments = transcript_data.get("segments", [])

                    # Build reverse map from old speaker_map to undo previous names
                    old_map_path = out_dir / "speaker_map.json"
                    if old_map_path.exists():
                        old_map = json.loads(old_map_path.read_text(encoding="utf-8"))
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
                data = json.loads(transcript_path.read_text(encoding="utf-8"))
                segments = data.get("segments", [])
                transcript_text = build_transcript_text(segments)
                duration = m.get("duration", 0)

                m["status"] = MeetingStatus.summarizing
                _update_progress(m, 72, "Re-summarizing...")
                _save_index()

                context = await asyncio.get_event_loop().run_in_executor(
                    None, _gather_meeting_context, meeting_id)
                summary = await step_summarize(transcript_text, duration, context=context)
                if m.get("title_edited"):
                    # Manual rename wins over the LLM title; keep summary.json's
                    # title key consistent with the preserved record title.
                    summary["title"] = m.get("title", summary.get("title"))
                elif "title" in summary and summary["title"] != "Meeting":
                    m["title"] = summary["title"]

                _atomic_write(out_dir / "summary.json", json.dumps(summary, indent=2))
                summary_md = build_summary_markdown(summary, m)
                _atomic_write(out_dir / "summary.md", summary_md)
                m["summary"] = summary
                m["transcript_edited"] = False  # summary is fresh w.r.t. the edited transcript

            elif step == "tagging":
                transcript_path = out_dir / "transcript.json"
                data = json.loads(transcript_path.read_text(encoding="utf-8"))
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

            # Re-index vectors from the rewritten files so chat RAG / search /
            # insights see the reprocessed text (closes the pre-existing
            # staleness gap for all four steps; failures are non-fatal inside
            # _reindex_meeting_safe itself).
            _reindex_meeting_safe(meeting_id)

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


class SmtpSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    host: Optional[str] = None
    port: Optional[int] = None
    secure: Optional[bool] = None
    username: Optional[str] = None
    password: Optional[str] = None
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    reply_to: Optional[str] = None
    recipients: Optional[str] = None


class DigestSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    time: Optional[str] = None
    timezone: Optional[str] = None
    recipients: Optional[str] = None


class IcsSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    token: Optional[str] = None


class SettingsRequest(BaseModel):
    prompts: Optional[dict[str, str]] = None
    ollama_model: Optional[str] = None
    temperature: Optional[float] = None
    chat: Optional[ChatSettingsRequest] = None
    smtp: Optional[SmtpSettingsRequest] = None
    digest: Optional[DigestSettingsRequest] = None
    ics: Optional[IcsSettingsRequest] = None
    stt_backend: Optional[str] = None
    diarize: Optional[bool] = None
    remove_filler: Optional[bool] = None


@app.get("/api/models")
async def api_models():
    """List installed Ollama models for the settings model picker."""
    return {"models": await _list_ollama_models()}


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

    if body.smtp is not None:
        smtp = settings.setdefault("smtp", json.loads(json.dumps(DEFAULT_SETTINGS["smtp"])))
        if body.smtp.enabled is not None:
            smtp["enabled"] = bool(body.smtp.enabled)
        if body.smtp.host is not None:
            smtp["host"] = body.smtp.host.strip()
        if body.smtp.port is not None:
            smtp["port"] = max(1, min(65535, int(body.smtp.port)))
        if body.smtp.secure is not None:
            smtp["secure"] = bool(body.smtp.secure)
        if body.smtp.username is not None:
            smtp["username"] = body.smtp.username.strip()
        if body.smtp.password is not None:
            smtp["password"] = body.smtp.password
        if body.smtp.from_email is not None:
            smtp["from_email"] = body.smtp.from_email.strip()
        if body.smtp.from_name is not None:
            smtp["from_name"] = body.smtp.from_name.strip()
        if body.smtp.reply_to is not None:
            smtp["reply_to"] = body.smtp.reply_to.strip()
        if body.smtp.recipients is not None:
            smtp["recipients"] = body.smtp.recipients.strip()

    if body.digest is not None:
        digest = settings.setdefault("digest", json.loads(json.dumps(DEFAULT_SETTINGS["digest"])))
        if body.digest.enabled is not None:
            digest["enabled"] = bool(body.digest.enabled)
        if body.digest.time is not None:
            t = body.digest.time.strip()
            if re.match(r"^\d{2}:\d{2}$", t):
                digest["time"] = t
        if body.digest.timezone is not None:
            tz = body.digest.timezone.strip()
            if tz:
                digest["timezone"] = tz
        if body.digest.recipients is not None:
            digest["recipients"] = body.digest.recipients.strip()

    if body.ics is not None:
        ics = settings.setdefault("ics", json.loads(json.dumps(DEFAULT_SETTINGS["ics"])))
        if body.ics.enabled is not None:
            ics["enabled"] = bool(body.ics.enabled)
        if body.ics.token is not None:
            ics["token"] = body.ics.token.strip()

    if body.stt_backend is not None and body.stt_backend in ("parakeet",):
        settings["stt_backend"] = body.stt_backend

    if body.diarize is not None:
        settings["diarize"] = bool(body.diarize)

    if body.remove_filler is not None:
        settings["remove_filler"] = bool(body.remove_filler)

    save_settings(settings)
    return {"detail": "Settings updated", "settings": settings}


@app.post("/api/settings/reset")
async def reset_settings():
    """Reset all settings to defaults."""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    save_settings(settings)
    return {"detail": "Settings reset to defaults", "settings": settings}


@app.post("/api/settings/test-email")
async def test_email():
    """Send a test email using the currently-saved SMTP config, to the saved recipients."""
    smtp = load_settings().get("smtp", {})
    recipients = emailer.parse_recipients(smtp.get("recipients"))
    if not smtp.get("host"):
        raise HTTPException(status_code=400, detail="SMTP host is not configured")
    if not recipients:
        raise HTTPException(status_code=400, detail="No recipients configured")

    subject = "Meeting Service — test email"
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:15px;color:#1a1a1a;">'
        "<p>This is a test email from your Meeting Service.</p>"
        "<p>If you received it, SMTP is configured correctly and meeting summaries "
        "will be delivered here automatically.</p></div>"
    )
    text_body = (
        "This is a test email from your Meeting Service.\n\n"
        "If you received it, SMTP is configured correctly and meeting summaries "
        "will be delivered here automatically."
    )
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, emailer.send_email, smtp, recipients, subject, html_body, text_body
        )
    except Exception as e:
        logger.warning(f"Test email failed: {e}")
        raise HTTPException(status_code=400, detail=f"Send failed: {e}")
    return {"detail": f"Test email sent to {', '.join(recipients)}"}


async def _generate_digest_briefing(snapshot: dict) -> str:
    """One-shot AI 'what matters today' briefing (2-3 sentences) from the digest task
    snapshot. Non-fatal: any failure or unparseable/empty response yields "" so the
    digest still sends without a briefing (email must never be blocked by the GPU)."""
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)
    counts = snapshot.get("counts", {})
    lanes = snapshot.get("lanes", {})

    def _fmt(lane_key):
        return "; ".join((t.get("text") or "") for t in lanes.get(lane_key, [])[:15])

    prompt = (
        "You are a concise daily-planning assistant. Write a 2-3 sentence briefing "
        "of what matters today, based on this task snapshot. Be specific (mention "
        "task names), not generic. Do not use bullet points.\n\n"
        f"Overdue ({counts.get('overdue', 0)}): {_fmt('overdue')}\n"
        f"Due today ({counts.get('today', 0)}): {_fmt('today')}\n"
        f"In progress / Doing ({counts.get('doing', 0)}): {_fmt('doing')}\n"
        f"This week ({counts.get('week', 0)}): {_fmt('week')}\n\n"
        'Respond ONLY with valid JSON: {"briefing": "2-3 sentence summary"}'
    )
    try:
        body = _build_generate_body(model, prompt, temperature=temperature, num_predict=512,
            schema=ANALYSIS_SCHEMAS.get("digest_briefing"))
        resp = await _retry_ollama_call("POST", f"{OLLAMA_URL}/api/generate",
            json_body=body, timeout_seconds=60.0, max_retries=2)
        parsed = _parse_json_object(resp.json().get("response", ""), context="digest-briefing")
        return str(parsed.get("briefing") or "").strip()
    except Exception as e:
        logger.warning(f"digest briefing failed (non-fatal): {e}")
        return ""


async def _build_digest_email() -> tuple[str, str, str]:
    """Build (subject, html, text) for the daily task digest: collect tasks off the
    event loop, bucket via tasks_store.build_digest_snapshot in the digest timezone,
    fold in a non-fatal AI briefing, and render via emailer.render_digest_email."""
    settings = load_settings()
    digest = settings.get("digest", DEFAULT_SETTINGS["digest"])
    tz = _digest_zoneinfo(digest.get("timezone"))
    now_local = datetime.now(tz)

    tasks = await _collect_all_tasks()
    today = now_local.strftime("%Y-%m-%d")
    snapshot = tasks_store.build_digest_snapshot(tasks, today)
    briefing = await _generate_digest_briefing(snapshot)
    data = {
        "weekday": now_local.strftime("%A"),
        "date": today,
        "counts": snapshot["counts"],
        "lanes": snapshot["lanes"],
        "briefing": briefing,
    }
    public_url = os.getenv("MEETING_PUBLIC_URL", "https://meetings.example.com")
    return emailer.render_digest_email(data, public_url)


@app.post("/api/digest/test")
async def test_digest():
    """Build and send the daily digest immediately, to the configured recipients.
    Mirrors POST /api/settings/test-email's shape and failure modes."""
    settings = load_settings()
    smtp = settings.get("smtp", {})
    digest = settings.get("digest", DEFAULT_SETTINGS["digest"])
    recipients = emailer.parse_recipients(
        digest.get("recipients") or smtp.get("recipients") or os.getenv("EMAIL_RECIPIENTS", ""))
    if not smtp.get("host"):
        raise HTTPException(status_code=400, detail="SMTP host is not configured")
    if not recipients:
        raise HTTPException(status_code=400, detail="No digest recipients configured")

    subject, html_body, text_body = await _build_digest_email()
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, emailer.send_email, smtp, recipients, subject, html_body, text_body
        )
    except Exception as e:
        logger.warning(f"Digest send failed: {e}")
        raise HTTPException(status_code=400, detail=f"Send failed: {e}")
    return {"detail": f"Digest sent to {', '.join(recipients)}", "subject": subject, "recipients": recipients}


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
    expected_body_hash: Optional[str] = None


class NoteLinkMeeting(BaseModel):
    meeting_id: str
    add: bool = True


class TaskToggle(BaseModel):
    note_id: str
    line: int
    done: bool
    expected_text: Optional[str] = None


class TaskCreate(BaseModel):
    text: str
    note_id: Optional[str] = None  # None -> the "Tasks" inbox note (created if absent)
    owner: Optional[str] = None
    due: Optional[str] = None
    priority: Optional[str] = None


class TaskUpdate(BaseModel):
    note_id: str
    line: int
    text: str
    expected_text: Optional[str] = None
    owner: Optional[str] = None
    due: Optional[str] = None
    priority: Optional[str] = None


class TaskDelete(BaseModel):
    note_id: str
    line: int
    expected_text: Optional[str] = None


class TaskState(BaseModel):
    note_id: str
    line: int
    state: str
    expected_text: Optional[str] = None


class MeetingTaskToggle(BaseModel):
    index: int
    done: bool


class MeetingTaskEdit(BaseModel):
    index: int
    text: str
    owner: Optional[str] = None
    due: Optional[str] = None
    priority: Optional[str] = None


class MeetingTaskDismiss(BaseModel):
    index: int


class MeetingTaskState(BaseModel):
    index: int
    state: str


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


def _note_attachment_text(rec: dict, cap: int = None) -> str:
    """Concatenate the `done` extracted text of a note's referenced attachments,
    capped at ATTACH_TEXT_MAX. Best-effort — never raises."""
    if cap is None:
        cap = ATTACH_TEXT_MAX
    try:
        filenames = notes_store.note_attachments(notes_store.NOTES_DIR, rec.get("id", ""))
        attach_dir = notes_store.attachments_dir(notes_store.NOTES_DIR)
        parts = []
        for fname in filenames:
            sc = extract.read_extraction(attach_dir, fname)
            if sc and sc.get("status") == "done" and sc.get("text"):
                parts.append(sc["text"])
        return ("\n\n".join(parts))[:cap]
    except Exception as e:
        logger.warning(f"attachment-text gather failed for {rec.get('id')} (non-fatal): {e}")
        return ""


def _index_note_safe(rec: dict) -> None:
    """Best-effort, non-blocking: index a note's vectors (+ attachment text); never
    let Qdrant/embedder errors (or latency) break or delay note CRUD."""
    if not rec:
        return

    def work():
        try:
            extra = _note_attachment_text(rec)
            notes_vectors.index_note(get_qdrant(), get_embedder(), rec,
                                     collection=notes_vectors.NOTES_COLLECTION,
                                     dim=EMBEDDING_DIM, extra_text=extra)
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


def _prune_note_sidecars_eager(note_id: str) -> None:
    """Best-effort, offloaded cleanup of this note's .analysis sidecar and
    .enhance_state.json entry right after deletion, so a single note delete
    doesn't have to wait for the next 24h _sidecar_gc_worker sweep. Attachment
    .extracted sidecars are left to the sweep (their liveness is keyed off the
    attachment file, not the note)."""
    def work():
        try:
            p = _analysis_path(note_id)
            if p.exists():
                p.unlink()
        except OSError as e:
            logger.warning(f"Analysis sidecar prune failed for {note_id} (non-fatal): {e}")
        try:
            state = _enhance_state()
            if note_id in state:
                del state[note_id]
                _save_enhance_state(state)
        except Exception as e:
            logger.warning(f"Enhance-state prune failed for {note_id} (non-fatal): {e}")
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


def _analysis_path(note_id: str) -> Path:
    return notes_store.attachments_dir(notes_store.NOTES_DIR) / ".analysis" / f"{note_id}.json"


def _write_analysis(note_id: str, result: dict) -> None:
    d = notes_store.attachments_dir(notes_store.NOTES_DIR) / ".analysis"
    d.mkdir(parents=True, exist_ok=True)
    payload = {**result, "analyzed_at": notes_store.now_iso()}
    _atomic_write(d / f"{note_id}.json", json.dumps(payload, indent=2))


async def _run_analysis_job(note_id: str) -> dict:
    """Resolve deferred extraction, build the corpus, run one analysis pass, and
    persist the result sidecar. Returns the analysis dict (empty if the note is gone)."""
    note = notes_store.read_note(notes_store.NOTES_DIR, note_id)
    if not note:
        return {}
    extractions = await _resolve_note_extractions(note_id)
    corpus = _build_analysis_corpus(note, extractions)
    result = await analyze_note_text(corpus)
    _write_analysis(note_id, result)
    return result


def _enqueue_analysis(note_id: str) -> None:
    """Enqueue a note for background analysis (coalesced by note_id)."""
    if note_id and note_id not in _analysis_pending:
        _analysis_pending.add(note_id)
        _analysis_status[note_id] = "queued"
        _analysis_queue.put_nowait(note_id)


async def _analysis_worker():
    """Background worker: analyze queued notes once the meeting pipeline is idle."""
    while True:
        note_id = await _analysis_queue.get()
        try:
            while _pipeline_busy():
                await asyncio.sleep(ANALYSIS_IDLE_POLL)
            _analysis_status[note_id] = "running"
            await _run_analysis_job(note_id)
            _analysis_status[note_id] = "done"
        except Exception as e:
            logger.warning(f"analysis worker error (non-fatal): {e}")
            _analysis_status[note_id] = "error"
        finally:
            _analysis_pending.discard(note_id)
            _analysis_queue.task_done()


async def _run_cleanup_job(note_id: str) -> str:
    """Run one cleanup pass over the note's pending dictated text. Returns the cleaned
    text and stores it for polling."""
    text = _cleanup_text.get(note_id, "")
    cleaned = await cleanup_note_text(text)
    _cleanup_result[note_id] = cleaned
    return cleaned


def _enqueue_cleanup(note_id: str, text: str) -> None:
    """Enqueue (or extend) a pending note-cleanup job. The pending text is always
    overwritten with the latest span -- this is how a fast follow-up dictation batch
    extends an already-queued job instead of queuing a second one. In the intended
    client flow only one cleanup-span request is ever in flight per note at a time, so
    this mainly guards against a stray duplicate request."""
    _cleanup_text[note_id] = text
    if note_id not in _cleanup_pending:
        _cleanup_pending.add(note_id)
        _cleanup_status[note_id] = "queued"
        _cleanup_queue.put_nowait(note_id)


async def _cleanup_worker():
    """Background worker: polish queued dictated text once the meeting pipeline is idle."""
    while True:
        note_id = await _cleanup_queue.get()
        try:
            while _pipeline_busy():
                await asyncio.sleep(CLEANUP_IDLE_POLL)
            _cleanup_status[note_id] = "running"
            await _run_cleanup_job(note_id)
            _cleanup_status[note_id] = "done"
        except Exception as e:
            logger.warning(f"cleanup worker error (non-fatal): {e}")
            _cleanup_status[note_id] = "error"
        finally:
            _cleanup_pending.discard(note_id)
            _cleanup_queue.task_done()


def _enqueue_extract(note_id: str, filename: str) -> None:
    """Enqueue a deferred (STT/vision) extraction, coalesced by (note_id, filename)."""
    key = f"{note_id}\x00{filename}"
    if key not in _extract_pending:
        _extract_pending.add(key)
        _extract_queue.put_nowait((note_id, filename))


async def _extract_worker():
    """Resolve deferred attachment extractions when no meeting is mid-pipeline."""
    while True:
        note_id, filename = await _extract_queue.get()
        key = f"{note_id}\x00{filename}"
        try:
            while _pipeline_busy():
                await asyncio.sleep(EXTRACT_IDLE_POLL)
            await _run_extract_job(note_id, filename)
        except Exception as e:
            logger.warning(f"extract worker error (non-fatal): {e}")
        finally:
            _extract_pending.discard(key)
            _extract_queue.task_done()


def _join_segments(result: dict) -> str:
    return "\n".join(s.get("text", "") for s in result.get("segments", []) if s.get("text"))


async def _transcribe_plain(path: str) -> str:
    """Transcribe an audio/video file (no diarization) into plain lines. Callers that
    need a length cap (e.g. interactive dictation) should check stt.preprocess_audio's
    return themselves rather than adding one here -- attachment transcription has no
    such cap."""
    loop = asyncio.get_event_loop()
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "audio.wav")
        await loop.run_in_executor(None, stt.preprocess_audio, path, wav)
        result = await stt.step_transcribe(wav, None, None, diarize=False)
    return _join_segments(result)


async def _extract_stt(path: str) -> str:
    """Transcribe an audio/video attachment (no diarization) into plain lines."""
    return await _transcribe_plain(path)


DICTATE_MAX_SECONDS = 120.0


@app.post("/api/dictate")
async def api_dictate(audio: UploadFile = File(...)):
    """Transcribe a short dictation clip (no diarization, no note/meeting association).
    Stateless: audio in, text out. Not gated behind _pipeline_busy() -- this is an
    interactive, user-waited call, unlike the silent background cleanup/analysis passes."""
    data = await audio.read()
    if len(data) > DICTATE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Recording too large")
    loop = asyncio.get_event_loop()
    with tempfile.TemporaryDirectory() as td:
        suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
        input_path = os.path.join(td, f"input{suffix}")
        with open(input_path, "wb") as f:
            f.write(data)
        wav = os.path.join(td, "audio.wav")
        try:
            duration = await loop.run_in_executor(None, stt.preprocess_audio, input_path, wav)
        except Exception as e:
            logger.warning(f"dictate preprocess failed: {e}")
            raise HTTPException(status_code=400, detail="Could not process audio")
        if duration > DICTATE_MAX_SECONDS:
            raise HTTPException(status_code=400, detail="Recording too long")
        result = await stt.step_transcribe(wav, None, None, diarize=False)
    return {"text": _join_segments(result)}


async def _extract_scanned_pdf(path: str) -> str:
    loop = asyncio.get_event_loop()
    pngs = await loop.run_in_executor(None, extract.render_pdf_page_pngs, path)
    texts = []
    with tempfile.TemporaryDirectory() as td:
        for i, png in enumerate(pngs):
            fp = os.path.join(td, f"page{i}.png")
            with open(fp, "wb") as fh:
                fh.write(png)
            texts.append(await llm.describe_image(fp, prompt=VISION_PROMPT))
    return "\n\n".join(t for t in texts if t.strip())


async def _extract_vision(path: str, filename: str) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext == ".pdf":
        return await _extract_scanned_pdf(path)
    return await llm.describe_image(path, prompt=VISION_PROMPT)


async def _run_extract_job(note_id: str, filename: str) -> bool:
    """Resolve one deferred extraction; write the sidecar; re-feed the owning note.
    Never raises — failure -> status='failed'. Returns True iff status='done'."""
    attach_dir = notes_store.attachments_dir(notes_store.NOTES_DIR)
    p = notes_store.attachment_path(notes_store.NOTES_DIR, filename)
    if p is None:
        return False
    sidecar = extract.read_extraction(attach_dir, filename) or {}
    # A terminal sidecar means someone else (inline Analyze, or an earlier pass)
    # already resolved this — skip the duplicate GPU pass / prompt-mismatch overwrite.
    if sidecar.get("status") in ("done", "empty", "failed"):
        return sidecar.get("status") == "done"
    method = sidecar.get("method", "")
    if method not in ("stt", "vision"):
        return False
    try:
        text = await (_extract_stt(str(p)) if method == "stt"
                      else _extract_vision(str(p), filename))
        text = (text or "")[:ATTACH_TEXT_MAX]
        result = {"text": text, "method": method, "chars": len(text),
                  "status": "done" if text.strip() else "empty"}
    except llm.VisionUnavailable as e:
        # Model not present right now — keep retryable instead of terminal 'failed'.
        logger.warning(f"vision model unavailable for {filename}; leaving pending for retry: {e}")
        result = {"text": "", "method": "vision", "chars": 0, "status": "pending"}
    except Exception as e:
        logger.warning(f"deferred extraction failed for {filename} (non-fatal): {e}")
        result = {"text": "", "method": method, "chars": 0, "status": "failed"}
    result["note_id"] = note_id
    extract.write_extraction(attach_dir, filename, result)
    # Feed the new text into search + tags now that the attachment resolved.
    if result["status"] == "done":
        rec = notes_store.read_note(notes_store.NOTES_DIR, note_id)
        if rec:
            _index_note_safe(rec)
            _enqueue_tag(note_id)
    return result["status"] == "done"


async def _rescan_pending_extractions() -> None:
    """Startup rescan: the deferred-extraction queue lives only in memory, so a
    restart strands any attachment whose sidecar is still 'pending' (queued but
    never resolved). Walk the .extracted sidecars and re-enqueue anything pending
    that carries a note_id. Non-fatal — a missing/unreadable vault is a no-op."""
    def _scan() -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        edir = notes_store.attachments_dir(notes_store.NOTES_DIR) / extract.EXTRACTED_DIRNAME
        if not edir.is_dir():
            return pairs
        for sc in edir.glob("*.json"):
            try:
                data = json.loads(sc.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            note_id = data.get("note_id")
            if data.get("status") == "pending" and note_id:
                pairs.append((note_id, sc.stem))   # "<stored_filename>.json" -> stem strips ".json"
        return pairs
    try:
        pairs = await asyncio.get_event_loop().run_in_executor(None, _scan)
        for note_id, filename in pairs:
            _enqueue_extract(note_id, filename)
    except Exception as e:
        logger.warning(f"pending extraction rescan failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Daily digest scheduler
# ---------------------------------------------------------------------------

DIGEST_POLL_SECONDS = 300   # sleep-slice cap so settings edits (time/tz/enabled) apply without a restart
DIGEST_STATE_FILENAME = "digest_state.json"


def _digest_state_path() -> Path:
    return MEETINGS_DIR / DIGEST_STATE_FILENAME


def _load_digest_state() -> dict:
    p = _digest_state_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_digest_state(state: dict) -> None:
    _atomic_write(_digest_state_path(), json.dumps(state, indent=2))


def _next_digest_fire(now_utc: datetime, time_str: str, tz_str: str) -> datetime:
    """Pure: the next aware-UTC datetime at which `time_str` ('HH:MM') next occurs in
    `tz_str`, strictly after now_utc. DST-safe: computed in local wall-clock time via
    zoneinfo, then converted back to UTC, so the returned UTC instant shifts by
    exactly the DST offset either side of a transition. A malformed time_str -- or
    one that matches the settings-save regex (^\\d{2}:\\d{2}$) but is out of range,
    e.g. "25:99" -- falls back to 07:00 rather than raising."""
    try:
        hh, mm = (int(x) for x in time_str.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError("digest time out of range")
    except Exception:
        hh, mm = 7, 0
    tz = _digest_zoneinfo(tz_str)
    now_local = now_utc.astimezone(tz)
    candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    # Equality (not just "<") rolls forward: at the exact fire instant the worker is
    # already inside the send block (this function isn't called again mid-fire), so
    # the *next* call -- the post-send recompute -- must land on tomorrow, not
    # immediately re-fire the same instant.
    if candidate <= now_local:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc)


async def _send_digest_with_retry(smtp: dict, recipients: list, subject: str, html_body: str,
                                   text_body: str, attempts: int = 3, gap: int = 300) -> None:
    """Send the digest email via the (sync) emailer, retrying in-place on failure.
    A single SMTP blip shouldn't cost the whole day's digest -- the next scheduled
    fire is tomorrow -- so this tries up to `attempts` times total, sleeping `gap`
    seconds between attempts (never after the last one). Each failed attempt is
    logged individually. If every attempt fails, re-raises the final attempt's
    exception so the caller's existing except-block (log + fall through to
    tomorrow) handles it unchanged."""
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, emailer.send_email, smtp, recipients, subject, html_body, text_body)
            return
        except Exception as e:
            logger.warning(f"Digest send attempt {attempt}/{attempts} failed: {e}")
            if attempt == attempts:
                raise
            await asyncio.sleep(gap)


async def _attempt_digest_send(smtp: dict, recipients: list, state: dict, today_local: str) -> None:
    """The worker's fire-block body, factored out so it's testable without driving
    the worker's `while True` loop: build the email, retry the SMTP send up to 3x
    (~10 min window) via `_send_digest_with_retry`, and on success persist
    `last_sent_date` into `state` (mutated in place) as today. On total failure
    (all retries exhausted), log the give-up warning and leave `state` unwritten --
    the next scheduled fire is tomorrow. Same control flow as before this was
    extracted, just named."""
    try:
        subject, html_body, text_body = await _build_digest_email()
        # Bounded in-place retry (3 attempts / ~10 min window) so one transient
        # SMTP blip doesn't lose the whole day -- next fire is tomorrow. Only the
        # final failure falls through to the warning below.
        await _send_digest_with_retry(smtp, recipients, subject, html_body, text_body)
        state["last_sent_date"] = today_local
        _save_digest_state(state)
        logger.info(f"Digest sent to {', '.join(recipients)}")
    except Exception as e:
        logger.warning(f"Digest send failed after all retries (giving up until tomorrow): {e}")


def _digest_should_fire(digest: dict, smtp: dict, state: dict, today_local: str) -> bool:
    """Pure: given already-loaded digest/smtp settings, the persisted
    digest_state.json dict, and today's date in the digest timezone, should the
    digest fire right now? True only when the digest is enabled, SMTP is enabled,
    AND it hasn't already been sent today -- the last_sent_date comparison is the
    ONLY thing guarding against a double-send (e.g. a restart landing inside the
    fire minute, or the worker somehow re-entering this branch before its post-fire
    sleep completes), so this stays the single source of truth for that guard."""
    return bool(digest.get("enabled")) and bool(smtp.get("enabled")) and state.get("last_sent_date") != today_local


async def _digest_worker():
    """Daily digest scheduler: polls in <=300s slices toward the next configured
    fire time (re-reading settings each slice, so time/tz/enabled edits apply
    without a restart) WHILE more than one slice remains; once within one slice of
    the fire time, sleeps the exact remainder and falls through into the send block
    below (no `continue` there) -- a naive 'sleep-then-continue' on every pass,
    including the final slice, would recompute fire_at AFTER it has already rolled
    to tomorrow and never reach the send block at all, which is exactly the bug this
    structure avoids. Sends once via the executor (emailer stays sync) when
    _digest_should_fire says so, and persists the last-sent date so a restart inside
    the fire minute can't double-send AND a restart later the same day doesn't
    re-send."""
    while True:
        try:
            settings = load_settings()
            digest = settings.get("digest", DEFAULT_SETTINGS["digest"])
            now_utc = datetime.now(timezone.utc)
            fire_at = _next_digest_fire(now_utc, digest.get("time", "07:00"), digest.get("timezone", "Europe/London"))
            remaining = (fire_at - now_utc).total_seconds()
            if remaining > DIGEST_POLL_SECONDS:
                await asyncio.sleep(DIGEST_POLL_SECONDS)
                continue
            # Within one poll slice of the fire time -- sleep the exact remainder,
            # then fall through into the send block below (deliberately no `continue`).
            await asyncio.sleep(max(remaining, 0))
            # Fire minute reached -- re-read settings fresh in case they changed
            # during the final sleep slice.
            settings = load_settings()
            digest = settings.get("digest", DEFAULT_SETTINGS["digest"])
            smtp = settings.get("smtp", {})
            tz = _digest_zoneinfo(digest.get("timezone"))
            today_local = datetime.now(timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
            state = _load_digest_state()
            if _digest_should_fire(digest, smtp, state, today_local):
                recipients = emailer.parse_recipients(
                    digest.get("recipients") or smtp.get("recipients") or os.getenv("EMAIL_RECIPIENTS", ""))
                if smtp.get("host") and recipients:
                    await _attempt_digest_send(smtp, recipients, state, today_local)
                else:
                    logger.info("Digest fire skipped: SMTP host or recipients not configured")
            await asyncio.sleep(61)   # past the fire minute so this branch doesn't re-trigger immediately
        except Exception as e:
            logger.warning(f"Digest worker error (non-fatal): {e}")
            await asyncio.sleep(DIGEST_POLL_SECONDS)


@app.get("/api/notes")
async def api_list_notes(folder: Optional[str] = None, tag: Optional[str] = None,
                         type: Optional[str] = None, q: Optional[str] = None):
    # Scanning the vault is blocking file IO; run it off the event loop so a
    # large vault can't freeze the whole service.
    def _do():
        return notes_store.list_notes(
            notes_store.NOTES_DIR, folder=folder, tag=tag, type=type, q=q)
    notes = await asyncio.get_event_loop().run_in_executor(None, _do)
    return {"notes": notes}


@app.get("/api/notes/export")
async def api_export_notes(request: Request):
    """Full mirror payload for offline sync: every note record WITH its body and
    content_hash (the .trash/ and attachments/ subtrees are excluded by the
    index). Body-only — never inlines attachment extracted text.

    Three phases: (1) refresh the index and compute the vault ETag (quoted sha1
    of the walk signature — changes iff any note file changed); (2) compare
    If-None-Match; (3) assemble bodies from the cache ONLY on mismatch. The
    offline mirror polls this every 60s per tab; an unchanged vault now costs a
    304 + at most one stat-sweep instead of ~12MB of body re-reads. The 304
    repeats the ETag header (Caddy sits in front; the header must survive).
    Offloaded like api_list_notes."""
    loop = asyncio.get_event_loop()
    sig = await loop.run_in_executor(None, notes_store.index_signature, notes_store.NOTES_DIR)
    etag = f'"{sig}"'
    inm = request.headers.get("if-none-match")   # Starlette header lookup is case-insensitive
    if inm is not None and inm.strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})
    notes = await loop.run_in_executor(
        None, lambda: list(notes_store.get_bodies(notes_store.NOTES_DIR).values()))
    return JSONResponse(content={"notes": notes}, headers={"ETag": etag})


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
    # Walking the tree is blocking file IO; offload it off the event loop.
    folders = await asyncio.get_event_loop().run_in_executor(
        None, notes_store.list_folders, notes_store.NOTES_DIR)
    return {"folders": folders}


@app.post("/api/notes/rescan")
async def api_rescan_notes():
    # Full re-index reads every note file; offload so it can't freeze the app.
    idx = await asyncio.get_event_loop().run_in_executor(
        None, lambda: notes_store.get_index(notes_store.NOTES_DIR, force=True))
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
    try:
        rec = notes_store.update_note(
            notes_store.NOTES_DIR, note_id,
            title=payload.title, body=payload.body, tags=payload.tags,
            expected_body_hash=payload.expected_body_hash)
    except notes_store.NoteConflict as e:
        # Body/title changed elsewhere (another device or Obsidian): reject the
        # push and hand the client our current record so it can make a conflict copy.
        return JSONResponse(status_code=409, content=e.current)
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
    _prune_note_sidecars_eager(note_id)
    return {"deleted": True}


@app.post("/api/notes/{note_id}/retag")
async def api_retag_note(note_id: str):
    """Enqueue a note for background auto-tagging."""
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    _enqueue_tag(note_id)
    return {"queued": True}


@app.post("/api/notes/{note_id}/analyze")
async def api_analyze_note(note_id: str):
    """Enqueue a note for background AI analysis (resolves deferred extraction,
    then runs one structured pass). Returns immediately; poll GET .../analysis."""
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    _enqueue_analysis(note_id)
    return {"queued": True, "status": _analysis_status.get(note_id, "queued")}


@app.get("/api/notes/{note_id}/analysis")
async def api_get_note_analysis(note_id: str):
    """Report analysis status + the last persisted result (if any)."""
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    result = None
    p = _analysis_path(note_id)
    if p.exists():
        try:
            result = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            result = None
    status = _analysis_status.get(note_id) or ("done" if result else "idle")
    if status == "done" and result is None:
        # In-memory says done but the sidecar is missing/corrupt — never
        # present "done" without content; the UI renders result on "done".
        status = "error"
    return {"status": status, "result": result}


class NoteCleanupSpanRequest(BaseModel):
    text: str


@app.post("/api/notes/{note_id}/cleanup-span")
async def api_cleanup_span(note_id: str, payload: NoteCleanupSpanRequest):
    """Enqueue (or extend) a background cleanup pass over a freshly-dictated text span.
    Returns immediately; poll GET .../cleanup-span."""
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    _enqueue_cleanup(note_id, payload.text)
    return {"queued": True, "status": _cleanup_status.get(note_id, "queued")}


@app.get("/api/notes/{note_id}/cleanup-span")
async def api_get_cleanup_span(note_id: str):
    """Report cleanup status + the cleaned text (if ready) for this note's pending span."""
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    status = _cleanup_status.get(note_id, "idle")
    cleaned = _cleanup_result.get(note_id)
    result = {"text": cleaned} if (status == "done" and cleaned is not None) else None
    if status == "done" and result is None:
        status = "error"
    return {"status": status, "result": result}


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
    # Bulk path: ONE get_bodies() (cache-served when warm) instead of a disk
    # read per note per call. list_notes still drives the iteration so task
    # collection order stays exactly as before (newest-updated note first).
    out = []
    bodies = notes_store.get_bodies(notes_store.NOTES_DIR)
    for rec in notes_store.list_notes(notes_store.NOTES_DIR):
        full = bodies.get(rec["id"])
        if full:
            out.extend(tasks_store.parse_tasks_from_body(full.get("body", ""), rec["id"], rec["title"]))
    return out


def _meeting_overlay_path(meeting_id: str) -> Optional[Path]:
    """Path to a meeting's task-overlay sidecar (in-place complete/edit state for its
    AI-derived action items). None if the meeting/output_dir is unknown."""
    m = meetings.get(meeting_id)
    out_dir = m.get("output_dir") if m else None
    return (Path(out_dir) / "task_overlay.json") if out_dir else None


def _load_meeting_overlay(meeting_id: str) -> dict:
    """FRESH read on purpose (meeting-side mirror of the notes RMW rule): the
    overlay toggle/edit/dismiss endpoints do read-modify-write through here —
    a stale cached overlay would clobber a newer one. Read-only consumers
    (_collect_meeting_tasks) go through _read_json_cached instead."""
    p = _meeting_overlay_path(meeting_id)
    if p and p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_meeting_overlay(meeting_id: str, overlay: dict) -> bool:
    p = _meeting_overlay_path(meeting_id)
    if not p:
        return False
    _atomic_write(p, json.dumps(overlay, indent=2))
    return True


def _meeting_action_item_count(meeting_id: str) -> int:
    m = meetings.get(meeting_id)
    out_dir = m.get("output_dir") if m else None
    if not out_dir:
        return 0
    sp = Path(out_dir) / "summary.json"
    if not sp.exists():
        return 0
    try:
        return len(json.loads(sp.read_text(encoding="utf-8")).get("action_items", []))
    except Exception:
        return 0


_json_file_cache: dict = {}  # str(path) -> {"key": (mtime_ns, size), "data": parsed-json}


def _read_json_cached(path: Path):
    """(mtime_ns, size)-keyed JSON read cache for the small per-meeting sidecars
    (summary.json / task_overlay.json) that /api/tasks touches per meeting per
    call over the slow bind mount. Returns the parsed JSON, or None when the
    file is missing/unreadable/unparseable (parse failures are NOT cached, so a
    half-written file heals on the next call). Returned objects are shared
    cache state — callers must treat them as READ-ONLY. One entry per sidecar
    path; bounded by the number of meetings."""
    key_path = str(path)
    try:
        st = path.stat()
    except OSError:
        _json_file_cache.pop(key_path, None)
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _json_file_cache.get(key_path)
    if hit is not None and hit["key"] == key:
        return hit["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    _json_file_cache[key_path] = {"key": key, "data": data}
    return data


def _collect_meeting_tasks() -> list:
    """Meeting action items as tasks. summary.json / task_overlay.json come through
    the (mtime_ns, size)-keyed JSON cache — strictly read-only here; the overlay
    WRITE endpoints keep fresh reads via _load_meeting_overlay."""
    out = []
    for mid, m in meetings.items():
        if m.get("status") != MeetingStatus.complete:
            continue
        out_dir = m.get("output_dir")
        if not out_dir:
            continue
        summary = _read_json_cached(Path(out_dir) / "summary.json")
        if not isinstance(summary, dict):
            continue
        op = _meeting_overlay_path(mid)
        raw_overlay = _read_json_cached(op) if op else None
        overlay = raw_overlay if isinstance(raw_overlay, dict) else {}
        for i, item in enumerate(summary.get("action_items", [])):
            task = tasks_store.meeting_action_item_to_task(item, mid, m.get("title", mid))
            task["index"] = i  # stable per-meeting id (summary.json is immutable post-processing)
            task = tasks_store.apply_meeting_overlay(task, overlay.get(str(i)))
            if task is not None:
                out.append(task)
    return out


async def _collect_all_task_dicts() -> list:
    """Collect every note + meeting task, off the event loop (blocking file IO over
    the slow bind-mount). Shared by every endpoint that needs the full task list --
    POST /api/digest/test (via _build_digest_email), GET /api/tasks/calendar.ics,
    POST /api/tasks/ai/triage, and GET /api/tasks -- so the collection + executor-hop
    pattern lives in exactly one place."""
    def _collect():
        return _collect_note_tasks() + _collect_meeting_tasks()
    return await asyncio.get_event_loop().run_in_executor(None, _collect)


async def _collect_all_tasks() -> list:
    """Back-compat alias of _collect_all_task_dicts (kept so existing call sites --
    POST /api/digest/test, GET /api/tasks/calendar.ics, POST /api/tasks/ai/triage --
    don't need to change)."""
    return await _collect_all_task_dicts()


@app.get("/api/tasks")
async def api_list_tasks(status: Optional[str] = None, owner: Optional[str] = None,
                         source: Optional[str] = None, due: Optional[str] = None):
    # Collection is blocking file IO over the slow bind-mount on cold/changed
    # paths; run it off the event loop like its api_list_notes /
    # api_export_notes siblings so a cold vault can't stall every request.
    tasks = await _collect_all_task_dicts()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tasks = tasks_store.filter_tasks(tasks, status=status, owner=owner, source=source, due=due, today=today)
    return {"tasks": tasks_store.sort_tasks(tasks)}


@app.post("/api/tasks/toggle")
async def api_toggle_task(payload: TaskToggle):
    # Vault read-modify-write is blocking file IO on the bind mount; run the whole
    # unit off the event loop so it stays atomic w.r.t. the executor thread, then
    # translate the result to 404/409 back on the loop.
    def _do():
        rec = notes_store.read_note(notes_store.NOTES_DIR, payload.note_id)
        if rec is None:
            return "not_found"
        new_body, ok = tasks_store.toggle_line(rec["body"], payload.line, payload.done,
                                               expected_text=payload.expected_text)
        if not ok:
            return "conflict"
        notes_store.update_note(notes_store.NOTES_DIR, payload.note_id, body=new_body)
        return "ok"
    status = await asyncio.get_event_loop().run_in_executor(None, _do)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Note not found")
    if status == "conflict":
        raise HTTPException(status_code=409, detail="Task line changed or not a checkbox; refresh")
    return {"ok": True}


@app.post("/api/tasks/state")
async def api_set_task_state(payload: TaskState):
    if payload.state not in ("open", "doing", "done"):
        raise HTTPException(status_code=400, detail="Invalid state")

    def _do():
        rec = notes_store.read_note(notes_store.NOTES_DIR, payload.note_id)
        if rec is None:
            return "not_found"
        new_body, ok = tasks_store.set_state_line(rec["body"], payload.line, payload.state,
                                                  expected_text=payload.expected_text)
        if not ok:
            return "conflict"
        notes_store.update_note(notes_store.NOTES_DIR, payload.note_id, body=new_body)
        return "ok"
    status = await asyncio.get_event_loop().run_in_executor(None, _do)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Note not found")
    if status == "conflict":
        raise HTTPException(status_code=409, detail="Task line changed or not a checkbox; refresh")
    return {"ok": True}


TASKS_INBOX_TITLE = "Tasks"


def _find_or_create_tasks_inbox() -> str:
    """Return the id of the root 'Tasks' inbox note, creating it if it doesn't exist."""
    for rec in notes_store.list_notes(notes_store.NOTES_DIR, folder=""):
        if (rec.get("title") or "").strip().lower() == TASKS_INBOX_TITLE.lower():
            return rec["id"]
    rec = notes_store.create_note(notes_store.NOTES_DIR, title=TASKS_INBOX_TITLE,
                                  folder="", type="note", body="")
    _index_note_safe(rec)
    return rec["id"]


@app.post("/api/tasks")
async def api_create_task(payload: TaskCreate):
    if not (payload.text or "").strip():
        raise HTTPException(status_code=400, detail="Task text is required")

    def _do():
        note_id = payload.note_id or _find_or_create_tasks_inbox()
        if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
            return note_id, None
        line = tasks_store.format_task_line(payload.text, owner=payload.owner,
                                            due=payload.due, priority=payload.priority)
        rec = notes_store.append_task_line(notes_store.NOTES_DIR, note_id, line)
        return note_id, rec
    note_id, rec = await asyncio.get_event_loop().run_in_executor(None, _do)
    if rec is None:
        raise HTTPException(status_code=404, detail="Note not found")
    _index_note_safe(rec)
    return {"ok": True, "note_id": note_id}


# --- AI task capture + triage (non-fatal, never auto-applied) -----------------

def _task_ref(t: dict) -> dict:
    """The stable {source, source_id, line|index} identity used to route an AI
    triage suggestion (or a kanban drag) back to the right mutation endpoint --
    exactly ONE of line/index is present, mirroring how every task dict already
    carries line (notes) XOR index (meetings)."""
    ref = {"source": t.get("source"), "source_id": t.get("source_id")}
    if t.get("source") == "note":
        ref["line"] = t.get("line")
    else:
        ref["index"] = t.get("index")
    return ref


class TaskParseRequest(BaseModel):
    text: str


@app.post("/api/tasks/ai/parse")
async def api_tasks_ai_parse(payload: TaskParseRequest):
    """Natural-language task capture: 'chase John about the contract next Friday,
    high priority' -> {text, due, priority, owner}. Non-fatal -> the original text
    with blank fields on any failure. Never writes anything -- the UI fills an
    editable preview row the user must confirm."""
    text = (payload.text or "").strip()
    default = {"text": text, "due": "", "priority": "", "owner": ""}
    if not text:
        return default
    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)
    tz = _digest_zoneinfo((settings.get("digest") or {}).get("timezone"))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    prompt = (
        f"Today's date is {today}. Parse the following into a task. Resolve relative "
        "dates (e.g. \"next Friday\", \"tomorrow\") against today's date into an ISO "
        "YYYY-MM-DD date; leave due \"\" if no date is implied. priority is one of "
        "high/medium/low, or \"\" if not implied. owner is a first name if one is "
        "mentioned, else \"\".\n\n"
        f'TEXT: "{text}"\n\n'
        "Respond ONLY with valid JSON: "
        '{"text":"clean task text with no date/priority/owner words","due":"YYYY-MM-DD or empty",'
        '"priority":"high|medium|low or empty","owner":"name or empty"}'
    )
    try:
        body = _build_generate_body(model, prompt, temperature=temperature, num_predict=512,
            schema=ANALYSIS_SCHEMAS.get("task_parse"))
        resp = await _retry_ollama_call("POST", f"{OLLAMA_URL}/api/generate",
            json_body=body, timeout_seconds=60.0, max_retries=2)
        parsed = _parse_json_object(resp.json().get("response", ""), context="task-parse")
        if not parsed:
            return default
        due = str(parsed.get("due") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
            due = ""
        prio = str(parsed.get("priority") or "").strip().lower()
        if prio not in ("high", "medium", "low"):
            prio = ""
        return {
            "text": str(parsed.get("text") or text).strip() or text,
            "due": due, "priority": prio,
            "owner": str(parsed.get("owner") or "").strip(),
        }
    except Exception as e:
        logger.warning(f"task parse failed (non-fatal): {e}")
        return default


@app.post("/api/tasks/ai/triage")
async def api_tasks_ai_triage():
    """AI prioritisation: sends the OPEN task list (capped at 80) to the LLM by INDEX
    (not full identity -- the model never needs source_id/line), then maps returned
    indices back to task refs server-side. Never auto-applies -- the UI renders
    Apply/Dismiss per suggestion."""
    tasks = await _collect_all_tasks()
    open_tasks = tasks_store.filter_tasks(tasks, status="open")
    open_tasks = tasks_store.sort_tasks(open_tasks)[:80]
    default = {"suggestions": [], "focus": []}
    if not open_tasks:
        return default

    settings = load_settings()
    model = settings.get("ollama_model", OLLAMA_MODEL)
    temperature = settings.get("temperature", 0.3)
    lines = []
    for i, t in enumerate(open_tasks):
        bits = [f"{i}: {t.get('text', '')}"]
        if t.get("due"):
            bits.append(f"due {t['due']}")
        if t.get("priority"):
            bits.append(f"priority {t['priority']}")
        if t.get("owner"):
            bits.append(f"owner {t['owner']}")
        if t.get("source_title"):
            bits.append(f"from {t['source_title']}")
        lines.append(" | ".join(bits))
    prompt = (
        "You are a task-prioritization assistant. Below is a numbered list of open "
        "tasks (index: text | due | priority | owner | source). Suggest a priority "
        "(high/medium/low) for tasks whose priority should change, with a one-sentence "
        "reason, and pick up to 3 indices as today's focus.\n\n"
        + "\n".join(lines) +
        "\n\nRespond ONLY with valid JSON: "
        '{"suggestions":[{"index":0,"priority":"high","reason":"..."}],"focus":[0]}'
    )
    try:
        body = _build_generate_body(model, prompt, temperature=temperature, num_predict=1536,
            schema=ANALYSIS_SCHEMAS.get("task_triage"))
        resp = await _retry_ollama_call("POST", f"{OLLAMA_URL}/api/generate",
            json_body=body, timeout_seconds=180.0, max_retries=2)
        parsed = _parse_json_object(resp.json().get("response", ""), context="task-triage")
        if not parsed:
            return default
        n = len(open_tasks)
        suggestions = []
        for s in parsed.get("suggestions") or []:
            try:
                idx = int(s.get("index"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= n:
                continue
            prio = str(s.get("priority") or "").strip().lower()
            if prio not in ("high", "medium", "low"):
                continue
            suggestions.append({"ref": _task_ref(open_tasks[idx]), "priority": prio,
                                "reason": str(s.get("reason") or "").strip()})
        focus_refs = []
        for idx in parsed.get("focus") or []:
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n:
                focus_refs.append(_task_ref(open_tasks[idx]))
        focus = focus_refs[:3]
        return {"suggestions": suggestions, "focus": focus}
    except Exception as e:
        logger.warning(f"task triage failed (non-fatal): {e}")
        return default


@app.patch("/api/tasks")
async def api_update_task(payload: TaskUpdate):
    if not (payload.text or "").strip():
        raise HTTPException(status_code=400, detail="Task text is required")

    def _do():
        rec = notes_store.read_note(notes_store.NOTES_DIR, payload.note_id)
        if rec is None:
            return "not_found"
        new_body, ok = tasks_store.update_line(rec["body"], payload.line, payload.expected_text,
                                               payload.text, owner=payload.owner,
                                               due=payload.due, priority=payload.priority)
        if not ok:
            return "conflict"
        notes_store.update_note(notes_store.NOTES_DIR, payload.note_id, body=new_body)
        return "ok"
    status = await asyncio.get_event_loop().run_in_executor(None, _do)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Note not found")
    if status == "conflict":
        raise HTTPException(status_code=409, detail="Task line changed or not a checkbox; refresh")
    return {"ok": True}


@app.delete("/api/tasks")
async def api_delete_task(payload: TaskDelete):
    def _do():
        rec = notes_store.read_note(notes_store.NOTES_DIR, payload.note_id)
        if rec is None:
            return "not_found"
        new_body, ok = tasks_store.delete_line(rec["body"], payload.line,
                                               expected_text=payload.expected_text)
        if not ok:
            return "conflict"
        notes_store.update_note(notes_store.NOTES_DIR, payload.note_id, body=new_body)
        return "ok"
    status = await asyncio.get_event_loop().run_in_executor(None, _do)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Note not found")
    if status == "conflict":
        raise HTTPException(status_code=409, detail="Task line changed or not a checkbox; refresh")
    return {"ok": True}


@app.get("/api/tasks/calendar.ics")
async def api_tasks_calendar_ics(token: str = ""):
    """Token-gated ICS feed of open, due-dated tasks. Always 404 on any failure
    mode (feature disabled, empty configured token, mismatched token) -- never a
    distinguishing 401/403, which would leak that the route exists/means something."""
    settings = load_settings()
    ics = settings.get("ics", DEFAULT_SETTINGS["ics"])
    configured = (ics.get("token") or "").strip()
    if not ics.get("enabled") or not configured or not secrets.compare_digest(
            (token or "").encode("utf-8"), configured.encode("utf-8")):
        raise HTTPException(status_code=404)

    tasks = await _collect_all_tasks()
    tasks = tasks_store.filter_tasks(tasks, status="open")
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = tasks_store.render_ics_calendar(tasks, now_stamp=now_stamp)
    return Response(content=body, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


# --- Meeting tasks: in-place complete/edit/dismiss via a per-meeting overlay ---
# Meeting action items are an AI-derived projection (no note to edit), so their
# mutable state lives in task_overlay.json beside the meeting, keyed by the
# action item's index in summary.json.

async def _require_meeting_index(meeting_id: str, index: int) -> None:
    if meeting_id not in meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")
    # summary.json read (_meeting_action_item_count) is blocking file IO on the
    # bind mount; offload it. The 404 raises stay on-loop (never raise inside
    # the executor thread).
    count = await asyncio.get_event_loop().run_in_executor(
        None, _meeting_action_item_count, meeting_id
    )
    if index < 0 or index >= count:
        raise HTTPException(status_code=404, detail="Task not found")


@app.post("/api/meetings/{meeting_id}/tasks/toggle")
async def api_meeting_task_toggle(meeting_id: str, payload: MeetingTaskToggle):
    await _require_meeting_index(meeting_id, payload.index)

    # Overlay load+mutate+save is blocking file IO on the bind mount; run the
    # whole unit off the event loop.
    def _do():
        ov = _load_meeting_overlay(meeting_id)
        entry = ov.get(str(payload.index)) or {}
        entry["done"] = bool(payload.done)
        # Toggle always lands cleanly on done/open, never doing -- mirrors the note-side
        # toggle_line, which always rewrites the checkbox mark to 'x' or ' ' regardless
        # of what it was before. Keeps entry["state"] from going stale after a toggle.
        entry["state"] = "done" if payload.done else "open"
        ov[str(payload.index)] = entry
        return _save_meeting_overlay(meeting_id, ov)
    saved = await asyncio.get_event_loop().run_in_executor(None, _do)
    if not saved:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/tasks/state")
async def api_meeting_task_state(meeting_id: str, payload: MeetingTaskState):
    if payload.state not in ("open", "doing", "done"):
        raise HTTPException(status_code=400, detail="Invalid state")
    await _require_meeting_index(meeting_id, payload.index)

    def _do():
        ov = _load_meeting_overlay(meeting_id)
        entry = ov.get(str(payload.index)) or {}
        entry["state"] = payload.state
        entry["done"] = (payload.state == "done")   # keep the legacy done flag in lockstep
        ov[str(payload.index)] = entry
        return _save_meeting_overlay(meeting_id, ov)
    saved = await asyncio.get_event_loop().run_in_executor(None, _do)
    if not saved:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"ok": True}


@app.patch("/api/meetings/{meeting_id}/tasks")
async def api_meeting_task_edit(meeting_id: str, payload: MeetingTaskEdit):
    if not (payload.text or "").strip():
        raise HTTPException(status_code=400, detail="Task text is required")
    await _require_meeting_index(meeting_id, payload.index)

    def _do():
        ov = _load_meeting_overlay(meeting_id)
        entry = ov.get(str(payload.index)) or {}
        entry["edited"] = True
        entry["text"] = payload.text.strip()
        entry["owner"] = (payload.owner or "").strip() or None
        entry["due"] = (payload.due or "").strip() or None
        entry["priority"] = (payload.priority or "").strip().lower() or None
        ov[str(payload.index)] = entry
        return _save_meeting_overlay(meeting_id, ov)
    saved = await asyncio.get_event_loop().run_in_executor(None, _do)
    if not saved:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"ok": True}


@app.delete("/api/meetings/{meeting_id}/tasks")
async def api_meeting_task_dismiss(meeting_id: str, payload: MeetingTaskDismiss):
    await _require_meeting_index(meeting_id, payload.index)

    def _do():
        ov = _load_meeting_overlay(meeting_id)
        entry = ov.get(str(payload.index)) or {}
        entry["deleted"] = True
        ov[str(payload.index)] = entry
        return _save_meeting_overlay(meeting_id, ov)
    saved = await asyncio.get_event_loop().run_in_executor(None, _do)
    if not saved:
        raise HTTPException(status_code=404, detail="Meeting not found")
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
                items = json.loads(sp.read_text(encoding="utf-8")).get("action_items", [])
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

    # Instant extraction runs in the executor (a 50 MiB docx parse must not block
    # the handler); deferred formats only get a `pending` sidecar + enqueue.
    attach_dir = notes_store.attachments_dir(notes_store.NOTES_DIR)

    def _extract_and_write():
        try:
            result = extract.extract_text(str(attach_dir / fname), fname)
            if result.get("text"):
                result["text"] = result["text"][:ATTACH_TEXT_MAX]
                result["chars"] = len(result["text"])
            result["note_id"] = note_id   # so a restart-stranded 'pending' sidecar can be rescanned
            extract.write_extraction(attach_dir, fname, result)
            return result
        except Exception as e:
            logger.warning(f"attachment extraction/sidecar failed for {fname} (non-fatal): {e}")
            return {"text": "", "method": "", "chars": 0, "status": "failed"}

    result = await asyncio.get_event_loop().run_in_executor(None, _extract_and_write)
    if result["status"] == "pending":
        _enqueue_extract(note_id, fname)
    return {"filename": fname, "url": f"/api/notes/attachments/{fname}",
            "is_image": is_image, "embed": embed,
            "extracted": result["status"] == "done", "status": result["status"]}


@app.get("/api/notes/attachments/{filename}")
async def api_get_attachment(filename: str):
    p = notes_store.attachment_path(notes_store.NOTES_DIR, filename)
    if p is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(p, media_type=_ATTACH_MEDIA.get(p.suffix.lower(), "application/octet-stream"), filename=p.name)


@app.get("/api/notes/{note_id}/attachments")
async def api_list_attachments(note_id: str):
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")

    def _do():
        attach_dir = notes_store.attachments_dir(notes_store.NOTES_DIR)
        out = []
        for fname in notes_store.note_attachments(notes_store.NOTES_DIR, note_id):
            p = notes_store.attachment_path(notes_store.NOTES_DIR, fname)
            if p is None:
                continue
            ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
            sc = extract.read_extraction(attach_dir, fname)
            out.append({
                "filename": fname,
                "is_image": ext in _IMAGE_EXTS,
                "size": p.stat().st_size,
                "extraction_status": (sc or {}).get("status", "none"),
            })
        return out

    items = await asyncio.get_event_loop().run_in_executor(None, _do)
    return {"attachments": items}


@app.delete("/api/notes/{note_id}/attachments/{filename}")
async def api_delete_attachment(note_id: str, filename: str):
    if notes_store.read_note(notes_store.NOTES_DIR, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    p = notes_store.attachment_path(notes_store.NOTES_DIR, filename)
    if p is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    attach_dir = notes_store.attachments_dir(notes_store.NOTES_DIR)
    sidecar = extract.extracted_sidecar_path(attach_dir, filename)
    try:
        p.unlink()
    except OSError:
        pass
    # Remove ONLY this attachment's per-attachment .extracted sidecar. The note-level
    # .analysis/<note_id>.json is left as (possibly stale) — re-run Analyze to refresh.
    try:
        sidecar.unlink()
    except OSError:
        pass
    return {"deleted": True}


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
