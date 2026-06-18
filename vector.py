"""Qdrant vector store + Ollama embeddings for meeting search.

Extracted from app.py (Phase 4 §8.4). Depends only on config (read from env;
none of these are monkeypatched by tests) + stdlib/httpx/numpy/qdrant — NOT on
app.py's `meetings` global — so app.py re-imports these with no import cycle.

app.py re-binds get_embedder / get_qdrant into its own namespace, so its route
handlers' bare `get_qdrant()` / `get_embedder()` calls resolve app's binding and
the existing test monkeypatches (monkeypatch.setattr(app, "get_qdrant", ...))
keep working. _check_embedding_dim is covered by tests/test_security.py.
NOTE: _get_search_context stays in app.py (it reads the `meetings` global).
"""

import logging
import os
import uuid
from typing import Optional

import httpx
import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

logger = logging.getLogger("meeting-service")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = "meetings"
EMBEDDING_DIM = 1024  # qwen3-embedding:0.6b

# Lazy-loaded singletons
_embedder: "Optional[_OllamaEmbedder]" = None
_qdrant: Optional[QdrantClient] = None


class _OllamaEmbedder:
    """Drop-in replacement for SentenceTransformer.encode, backed by Ollama's
    embedding API. Unifies meeting embeddings on the shared qwen3-embedding model
    (GPU-served), matching OpenWebUI/Clearview, and avoids in-process torch/ST.
    Returns numpy arrays so existing callers can call .tolist() unchanged."""

    def __init__(self, base_url: str, model: str):
        self._url = base_url.rstrip("/") + "/api/embed"
        self._model = model

    def encode(self, texts, batch_size: int = 32):
        single = isinstance(texts, str)
        inputs = [texts] if single else list(texts)
        vecs = []
        with httpx.Client(timeout=300.0) as client:
            for i in range(0, len(inputs), batch_size):
                batch = inputs[i:i + batch_size]
                resp = client.post(self._url, json={"model": self._model, "input": batch})
                resp.raise_for_status()
                vecs.extend(resp.json()["embeddings"])
        arr = np.asarray(vecs, dtype="float32")
        return arr[0] if single else arr


def get_embedder() -> "_OllamaEmbedder":
    global _embedder
    if _embedder is None:
        _embedder = _OllamaEmbedder(OLLAMA_URL, EMBEDDING_MODEL)
    return _embedder


def _check_embedding_dim(actual: int, expected: int) -> None:
    """Log loudly if the embedding model's dim doesn't match the Qdrant collection.
    A silent mismatch corrupts all vector search."""
    if actual != expected:
        logger.error(
            "Embedding dim mismatch: model returned %d but EMBEDDING_DIM=%d "
            "(collection '%s'). Update EMBEDDING_DIM and recreate the collection.",
            actual, expected, COLLECTION_NAME,
        )


def get_qdrant() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL)
        # Ensure collection exists
        collections = [c.name for c in _qdrant.get_collections().collections]
        if COLLECTION_NAME not in collections:
            _qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )
    return _qdrant


def _smart_chunk_segments(segments: list[dict], target_size: int = 500) -> list[list[dict]]:
    """Group segments into chunks of approximately target_size chars, splitting at sentence boundaries."""
    if not segments:
        return []

    chunks = []
    current_chunk = []
    current_len = 0

    for seg in segments:
        text = seg["text"]
        current_chunk.append(seg)
        current_len += len(text)

        if current_len >= target_size:
            # Check if this segment ends at a sentence boundary
            if text.rstrip().endswith((".", "!", "?")):
                chunks.append(current_chunk)
                current_chunk = []
                current_len = 0
            # Otherwise, keep accumulating until we find a boundary (within 2x target)
            elif current_len >= target_size * 2:
                # Force split if we've gone too long without a boundary
                chunks.append(current_chunk)
                current_chunk = []
                current_len = 0

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def store_in_qdrant(meeting_id: str, meeting: dict, segments: list[dict], summary: dict):
    """Store transcript chunks and summary items as vectors in Qdrant."""
    embedder = get_embedder()
    qdrant = get_qdrant()
    date_str = meeting.get("date", "")
    title = meeting.get("title", "Meeting")
    tags = meeting.get("tags", {})
    tag_category = tags.get("category", "")
    tag_keywords = tags.get("keywords", [])

    # Collect all texts and payloads for batch embedding
    all_texts = []
    all_payloads = []

    # Smart-chunk transcript segments
    segment_chunks = _smart_chunk_segments(segments, target_size=500)

    for chunk_group in segment_chunks:
        chunk_text = " ".join(s["text"] for s in chunk_group)
        speaker = chunk_group[0].get("speaker", "UNKNOWN")
        start_ts = chunk_group[0]["start"]
        all_texts.append(chunk_text)
        all_payloads.append({
            "meeting_id": meeting_id,
            "date": date_str,
            "title": title,
            "chunk_type": "transcript",
            "speaker": speaker,
            "timestamp": start_ts,
            "text": chunk_text,
            "category": tag_category,
            "keywords": tag_keywords,
        })

    # Action items (support both new "task" field and legacy "description" field)
    for item in summary.get("action_items", []):
        task_text = item.get("task") or item.get("description", "")
        text = f"Action item: {task_text}"
        all_texts.append(text)
        all_payloads.append({
            "meeting_id": meeting_id,
            "date": date_str,
            "title": title,
            "chunk_type": "action_item",
            "assigned_to": item.get("who") or item.get("assigned_to", ""),
            "priority": item.get("priority", "medium"),
            "text": text,
            "category": tag_category,
            "keywords": tag_keywords,
        })

    # Decisions
    for dec in summary.get("decisions", []):
        text = f"Decision: {dec.get('decision', '')} - {dec.get('context', '')}"
        all_texts.append(text)
        all_payloads.append({
            "meeting_id": meeting_id,
            "date": date_str,
            "title": title,
            "chunk_type": "decision",
            "text": text,
            "category": tag_category,
            "keywords": tag_keywords,
        })

    # Open questions (support both new "open_questions" and legacy "questions_raised")
    for q in summary.get("open_questions", summary.get("questions_raised", [])):
        text = f"Question: {q.get('question', '')}"
        all_texts.append(text)
        all_payloads.append({
            "meeting_id": meeting_id,
            "date": date_str,
            "title": title,
            "chunk_type": "question",
            "text": text,
            "category": tag_category,
            "keywords": tag_keywords,
        })

    # Concerns & Risks (new from Pass D)
    for c in summary.get("concerns", []):
        text = f"Concern: {c.get('concern', '')}"
        all_texts.append(text)
        all_payloads.append({
            "meeting_id": meeting_id,
            "date": date_str,
            "title": title,
            "chunk_type": "concern",
            "text": text,
            "category": tag_category,
            "keywords": tag_keywords,
        })

    # Key Figures & Dates (new from Pass E)
    for fig in summary.get("figures", []):
        text = f"Figure: {fig.get('figure', '')} - {fig.get('context', '')}"
        all_texts.append(text)
        all_payloads.append({
            "meeting_id": meeting_id,
            "date": date_str,
            "title": title,
            "chunk_type": "figure",
            "text": text,
            "category": tag_category,
            "keywords": tag_keywords,
        })

    # Summary text (support both new "summary" and legacy "executive_summary")
    exec_summary = summary.get("summary") or summary.get("executive_summary", "")
    if exec_summary:
        all_texts.append(exec_summary)
        all_payloads.append({
            "meeting_id": meeting_id,
            "date": date_str,
            "title": title,
            "chunk_type": "summary",
            "text": exec_summary,
            "category": tag_category,
            "keywords": tag_keywords,
        })

    if not all_texts:
        return

    # Batch encode all texts at once
    vectors = embedder.encode(all_texts, batch_size=32).tolist()

    # Build points
    points = []
    for vec, payload in zip(vectors, all_payloads):
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload=payload,
            )
        )

    # Upsert in batches of 100
    for i in range(0, len(points), 100):
        qdrant.upsert(collection_name=COLLECTION_NAME, points=points[i : i + 100])


def search_meeting_vectors(qdrant, embedder, query: str, *, limit: int = 10) -> list[dict]:
    """Semantic search over the meetings collection. Returns [{meeting_id,title,date,score}]."""
    qvec = embedder.encode(query)
    qvec = qvec.tolist() if hasattr(qvec, "tolist") else list(qvec)
    hits = qdrant.search(collection_name=COLLECTION_NAME, query_vector=qvec, limit=limit)
    out = []
    for h in hits:
        p = h.payload or {}
        out.append({"meeting_id": p.get("meeting_id"), "title": p.get("title", ""),
                    "date": p.get("date", ""), "score": getattr(h, "score", None)})
    return out
