import importlib
from fastapi.testclient import TestClient
import notes_vectors


class _FakeEmbedder:
    def encode(self, texts):
        if isinstance(texts, str):
            return [0.1, 0.2, 0.3, 0.4]
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _FakeQdrant:
    def __init__(self):
        self.points = {}
    def get_collections(self):
        c = type("C", (), {})(); c.collections = [type("X", (), {"name": n})() for n in self.points]
        return c
    def create_collection(self, collection_name, vectors_config):
        self.points.setdefault(collection_name, [])
    def upsert(self, collection_name, points):
        self.points.setdefault(collection_name, []).extend(points)
    def delete(self, collection_name, points_selector):
        tgt = points_selector.must[0].match.value
        self.points[collection_name] = [p for p in self.points.get(collection_name, []) if (p.payload or {}).get("note_id") != tgt]
    def search(self, collection_name, query_vector, limit=10, query_filter=None):
        out = []
        for p in self.points.get(collection_name, [])[:limit]:
            h = type("H", (), {})(); h.payload = p.payload; h.score = 0.9
            out.append(h)
        return out


def _client(tmp_path, monkeypatch):
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path)
    notes_store._index_cache.clear()
    import app
    fake_q = _FakeQdrant()
    fake_e = _FakeEmbedder()
    monkeypatch.setattr(app, "get_qdrant", lambda: fake_q)
    monkeypatch.setattr(app, "get_embedder", lambda: fake_e)
    return TestClient(app.app)


def test_notes_crud_via_api(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    # create
    r = client.post("/api/notes", json={"title": "API Note", "folder": "Inbox", "type": "note", "body": "hi"})
    assert r.status_code == 200, r.text
    nid = r.json()["id"]

    # list
    r = client.get("/api/notes")
    assert any(n["id"] == nid for n in r.json()["notes"])

    # get
    r = client.get(f"/api/notes/{nid}")
    assert r.json()["body"].strip() == "hi"

    # update
    r = client.put(f"/api/notes/{nid}", json={"body": "updated", "tags": ["x"]})
    assert r.json()["tags"] == ["x"]

    # folders
    assert "Inbox" in client.get("/api/notes/folders").json()["folders"]

    # link meeting
    r = client.post(f"/api/notes/{nid}/link-meeting", json={"meeting_id": "20260617_x", "add": True})
    assert r.json()["linked_meetings"] == ["20260617_x"]

    # delete
    assert client.delete(f"/api/notes/{nid}").json()["deleted"] is True
    assert client.get(f"/api/notes/{nid}").status_code == 404


def test_get_missing_note_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/notes/n_missing").status_code == 404


def test_notes_search_and_links(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    hub = client.post("/api/notes", json={"title": "Hub", "body": "the central hub topic"}).json()
    client.post("/api/notes", json={"title": "Spoke", "body": "see [[Hub]] for more"})

    # search hits the indexed note
    res = client.get("/api/notes/search?q=hub").json()["results"]
    assert any(r["note_id"] == hub["id"] for r in res)

    # backlinks: Spoke links to Hub
    links = client.get(f"/api/notes/{hub['id']}/links").json()
    assert any(b["title"] == "Spoke" for b in links["backlinks"])
