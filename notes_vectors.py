"""Vector indexing + semantic search for notes (separate Qdrant `notes` collection).

All functions take the qdrant client and embedder as parameters (dependency
injection) so they can be unit-tested with in-memory fakes and never import app.
"""
import re
import uuid

from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue,
)

NOTES_COLLECTION = "notes"


def chunk_note(text: str, max_chars: int = 500) -> list:
    """Split note text into ~max_chars chunks at sentence/newline boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    chunks, cur = [], ""
    for s in parts:
        s = s.strip()
        if not s:
            continue
        if cur and len(cur) + len(s) + 1 > max_chars:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip() if cur else s
    if cur:
        chunks.append(cur)
    return chunks


def ensure_collection(qdrant, collection: str, dim: int) -> None:
    names = [c.name for c in qdrant.get_collections().collections]
    if collection not in names:
        qdrant.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )


def delete_note_vectors(qdrant, note_id: str, *, collection: str) -> None:
    qdrant.delete(
        collection_name=collection,
        points_selector=Filter(must=[FieldCondition(key="note_id", match=MatchValue(value=note_id))]),
    )


def index_note(qdrant, embedder, rec: dict, *, collection: str, dim: int, extra_text: str = "") -> int:
    """(Re)index a note's body (+ optional attachment text) into the notes
    collection. Returns chunk count."""
    ensure_collection(qdrant, collection, dim)
    delete_note_vectors(qdrant, rec["id"], collection=collection)
    corpus = (rec.get("body", "") or "")
    if extra_text:
        corpus = (corpus + "\n\n" + extra_text).strip()
    chunks = chunk_note(corpus)
    if not chunks:
        return 0
    vectors = embedder.encode(chunks)
    points = []
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        points.append(PointStruct(
            id=str(uuid.uuid4()),
            vector=[float(x) for x in vec],
            payload={
                "note_id": rec["id"],
                "title": rec.get("title", ""),
                "folder": rec.get("folder", ""),
                "tags": rec.get("tags") or [],
                "linked_meetings": rec.get("linked_meetings") or [],
                "chunk_index": i,
                "text": chunk,
            },
        ))
    qdrant.upsert(collection_name=collection, points=points)
    return len(points)


def search_notes(qdrant, embedder, query: str, *, collection: str, dim: int, limit: int = 10) -> list:
    """Semantic search over note chunks. Returns [{note_id, title, folder, text, score}]."""
    ensure_collection(qdrant, collection, dim)
    qvec = [float(x) for x in embedder.encode(query)]
    hits = qdrant.search(collection_name=collection, query_vector=qvec, limit=limit)
    out = []
    for h in hits:
        p = h.payload or {}
        out.append({
            "note_id": p.get("note_id"),
            "title": p.get("title", ""),
            "folder": p.get("folder", ""),
            "text": p.get("text", ""),
            "score": getattr(h, "score", None),
        })
    return out
