"""LLM helper layer — pure, self-contained Ollama prompt/response utilities.

Extracted from app.py (Phase 4 modularization). Behavior-preserving: app.py
re-imports these names so `app._strip_think` etc. still resolve. Covered by
tests/test_llm_helpers.py.
"""

import json
import logging
import re

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
