import numpy as np
import vector
import notes_store as ns
from fastapi.testclient import TestClient


class _Emb:
    def encode(self, x): return np.asarray([0.1, 0.2, 0.3, 0.4], dtype="float32")


class _Q:
    def __init__(self, payloads): self._p = payloads
    def search(self, collection_name, query_vector, limit=10, query_filter=None):
        out = []
        for p in self._p[:limit]:
            h = type("H", (), {})(); h.payload = p; h.score = 0.8; out.append(h)
        return out


class _FakeEmbedder:
    def encode(self, x): return np.asarray([0.1, 0.2, 0.3, 0.4], dtype="float32")


class _FakeQdrant:
    def __init__(self): self.points = {}
    def get_collections(self):
        c = type("C", (), {})(); c.collections = [type("X", (), {"name": n})() for n in self.points]; return c
    def create_collection(self, collection_name, vectors_config): self.points.setdefault(collection_name, [])
    def upsert(self, collection_name, points): self.points.setdefault(collection_name, []).extend(points)
    def search(self, collection_name, query_vector, limit=10, query_filter=None):
        out = []
        for p in self.points.get(collection_name, [])[:limit]:
            h = type("H", (), {})(); h.payload = p; h.score = 0.9; out.append(h)
        return out


def _rc(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    import app
    fq = _FakeQdrant()
    monkeypatch.setattr(app, "get_qdrant", lambda: fq)
    monkeypatch.setattr(app, "get_embedder", lambda: _FakeEmbedder())
    return TestClient(app.app), app, fq


def test_search_meeting_vectors_shape():
    q = _Q([{"meeting_id": "m1", "title": "Sync", "date": "2026-06-17", "text": "x"}])
    res = vector.search_meeting_vectors(q, _Emb(), "roadmap", limit=5)
    assert res[0]["meeting_id"] == "m1" and res[0]["title"] == "Sync"
    assert res[0]["date"] == "2026-06-17" and res[0]["score"] == 0.8


def test_note_related_meetings(tmp_path, monkeypatch):
    client, app, fq = _rc(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": "roadmap planning"}).json()["id"]
    fq.points["meetings"] = [{"meeting_id": "m1", "title": "Sync", "date": "2026-06-17"}]
    r = client.get(f"/api/notes/{nid}/related")
    assert r.status_code == 200, r.text
    assert r.json()["related"][0]["meeting_id"] == "m1"


def test_meeting_related_notes(tmp_path, monkeypatch):
    client, app, fq = _rc(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "meetings", {"m1": {"id": "m1", "title": "Sync",
        "status": app.MeetingStatus.complete, "summary": {"summary": "roadmap"}}})
    fq.points["notes"] = [{"note_id": "n_1", "title": "Plan", "folder": "Work"}]
    r = client.get("/meetings/m1/related-notes")
    assert r.status_code == 200, r.text
    assert r.json()["related"][0]["note_id"] == "n_1"
