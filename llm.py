"""LLM helper layer — pure, self-contained Ollama prompt/response utilities.

Extracted from app.py (Phase 4 modularization). Behavior-preserving: app.py
re-imports these names so `app._strip_think` etc. still resolve. Covered by
tests/test_llm_helpers.py.
"""

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path

import httpx

logger = logging.getLogger("meeting-service")

# num_ctx sizing tiers (tokens). Bounds long-transcript prompts so Ollama does
# not silently truncate. Capped at 16k while sharing a single 16GB GPU: qwen3.5:9b
# is already ~14.4GB resident, and a 32k KV cache on top risks OOM / CPU-offload
# under contention. Restore the 32768 tier once the 2nd GPU (RTX 3060 Ti) lands —
# long meetings are already sharded by _hierarchical_summarize, so >16k input is rare.
_CTX_TIERS = (8192, 16384)
_CTX_MAX = 16384


def _ctx_for_text(text: str, num_predict: int = 2048) -> int:
    """Pick a bounded num_ctx that fits the prompt + expected output.

    Rough estimate: ~4 chars/token for the prompt, plus the reserved output
    tokens, plus headroom for the prompt template. Capped at 32k.
    """
    est_tokens = (len(text) // 4) + num_predict + 1024
    for tier in _CTX_TIERS:
        if est_tokens <= tier:
            return tier
    return _CTX_MAX


def _build_generate_body(
    model: str,
    prompt: str,
    *,
    temperature: float,
    num_predict: int,
    schema: dict | None = None,
    images: list | None = None,
) -> dict:
    """Build an Ollama /api/generate body with a bounded num_ctx and optional
    JSON-Schema structured output (`format`).

    `think: False` disables the separate reasoning channel of thinking models
    (e.g. qwen3.x): without it they can spend the whole num_predict budget
    reasoning and return an empty `response`, especially under a `format`
    constraint. Harmless for non-thinking models (the field is ignored)."""
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
            "num_ctx": _ctx_for_text(prompt, num_predict),
        },
    }
    if schema is not None:
        body["format"] = schema
    if images is not None:
        body["images"] = images
    return body


def _strip_think(raw: str) -> str:
    """Remove <think>...</think> reasoning blocks emitted by thinking models."""
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


def _parse_json_object(raw: str, context: str = "") -> dict:
    """Extract a JSON object from an LLM response, handling <think> blocks and
    markdown fences. Logs a warning instead of silently returning {}."""
    cleaned = _strip_think(raw)
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    logger.warning(
        "JSON object parse failed%s; raw[:200]=%r",
        f" for {context}" if context else "", cleaned[:200],
    )
    return {}


def _parse_json_array(raw: str, context: str = "") -> list:
    """Extract a JSON array from an LLM response, handling <think> blocks and
    markdown fences. Logs a warning instead of silently returning []."""
    cleaned = _strip_think(raw)
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
    logger.warning(
        "JSON array parse failed%s; raw[:200]=%r",
        f" for {context}" if context else "", cleaned[:200],
    )
    return []


# ---------------------------------------------------------------------------
# Local vision model (Ollama /api/generate with images). GPU-serialized (sem=1)
# and preflight-gated so absence degrades gracefully instead of erroring.
# ---------------------------------------------------------------------------

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl:8b")

_vision_sem = None            # lazily created asyncio.Semaphore(1)
_vision_present = None        # tri-state preflight cache: None unknown / True / False


class VisionUnavailable(RuntimeError):
    """Raised when the configured VISION_MODEL is not present on the Ollama host."""


def _get_vision_sem() -> "asyncio.Semaphore":
    global _vision_sem
    if _vision_sem is None:
        _vision_sem = asyncio.Semaphore(1)  # GPU: one vision call at a time
    return _vision_sem


async def _ollama_generate(body: dict) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=body)
        resp.raise_for_status()
        return resp.json()


async def _vision_available(force: bool = False) -> bool:
    """True iff VISION_MODEL (or its base name) is in `ollama list`.

    Cached on successful preflight (True if model present, False if absent).
    Transient errors (timeout, DNS, Ollama restart) return False for this call
    but are NOT cached, so the next call re-checks the connection.
    """
    global _vision_present
    if _vision_present is not None and not force:
        return _vision_present
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
        base = VISION_MODEL.split(":")[0]
        _vision_present = any(n == VISION_MODEL or n.split(":")[0] == base for n in names)
    except Exception as e:
        logger.warning(f"vision preflight failed (assuming unavailable): {e}")
        return False  # Transient error: return False but don't cache
    return _vision_present


async def describe_image(path: str, *, prompt: str) -> str:
    """Describe/transcribe an image via the local vision model. Concurrency 1.
    Raises VisionUnavailable if the model is not present (graceful-degrade seam)."""
    if not await _vision_available():
        raise VisionUnavailable(f"vision model {VISION_MODEL!r} not present on {OLLAMA_URL}")

    def _read_b64() -> str:
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")
    # File read can happen before acquiring the semaphore — offload the blocking
    # read+encode so a large image doesn't stall the event loop.
    b64 = await asyncio.get_event_loop().run_in_executor(None, _read_b64)
    body = _build_generate_body(VISION_MODEL, prompt, temperature=0.2,
                                num_predict=1024, images=[b64])
    async with _get_vision_sem():
        data = await _ollama_generate(body)
    return _strip_think(data.get("response", "")).strip()
