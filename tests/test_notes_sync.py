import hashlib

import notes_store as ns
from fastapi.testclient import TestClient


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
        pass
    def search(self, collection_name, query_vector, limit=10, query_filter=None):
        return []


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path)
    ns._index_cache.clear()
    import app
    monkeypatch.setattr(app, "get_qdrant", lambda: _FakeQdrant())
    monkeypatch.setattr(app, "get_embedder", lambda: _FakeEmbedder())
    return TestClient(app.app)


def test_content_hash_is_sha1_of_title_nul_body():
    assert ns.content_hash("Hello", "World") == hashlib.sha1(b"Hello\x00World").hexdigest()


def test_content_hash_treats_none_as_empty():
    assert ns.content_hash(None, None) == hashlib.sha1(b"\x00").hexdigest()


def test_read_note_includes_content_hash(tmp_path):
    rec = ns.create_note(tmp_path, "Hello", body="World")
    full = ns.read_note(tmp_path, rec["id"])
    assert full["content_hash"] == ns.content_hash("Hello", "World")


def test_apply_auto_tags_leaves_content_hash_unchanged(tmp_path):
    rec = ns.create_note(tmp_path, "N", body="the body text")
    before = ns.read_note(tmp_path, rec["id"])["content_hash"]
    ns.apply_auto_tags(tmp_path, rec["id"], "planning", ["roadmap", "okrs"])
    after = ns.read_note(tmp_path, rec["id"])["content_hash"]
    assert before == after


def test_content_hash_stable_across_write_read_roundtrip(tmp_path):
    # Round-trip regression guard: parse_frontmatter's whitespace quirks must
    # not change the hash between create and re-read (else every pull looks dirty).
    for body in ("World", "  padded  ", "\n\nleading blanks", "multi\n\npara\n"):
        rec = ns.create_note(tmp_path, "RT", body=body)
        got = ns.read_note(tmp_path, rec["id"])
        assert got["content_hash"] == rec["content_hash"], repr(body)


def test_put_with_matching_hash_applies(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "T", "body": "orig"}).json()["id"]
    h = client.get(f"/api/notes/{nid}").json()["content_hash"]
    r = client.put(f"/api/notes/{nid}", json={"body": "edited", "expected_body_hash": h})
    assert r.status_code == 200, r.text
    assert r.json()["body"].strip() == "edited"


def test_put_with_stale_hash_409_and_no_write(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "T", "body": "orig"}).json()["id"]
    r = client.put(f"/api/notes/{nid}", json={"body": "edited", "expected_body_hash": "deadbeef"})
    assert r.status_code == 409, r.text
    assert r.json()["id"] == nid
    assert r.json()["body"].strip() == "orig"                       # server returns its current record
    assert client.get(f"/api/notes/{nid}").json()["body"].strip() == "orig"   # file unchanged


def test_put_without_hash_is_unchanged_behavior(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "T", "body": "orig"}).json()["id"]
    r = client.put(f"/api/notes/{nid}", json={"body": "edited"})
    assert r.status_code == 200 and r.json()["body"].strip() == "edited"


def test_tag_bump_does_not_block_a_pending_body_push(tmp_path, monkeypatch):
    # Regression: a tag-only apply_auto_tags bump must NOT invalidate a body push
    # keyed on the pre-bump content hash (the false-conflict fix).
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "T", "body": "orig"}).json()["id"]
    pre = client.get(f"/api/notes/{nid}").json()["content_hash"]
    ns.apply_auto_tags(tmp_path, nid, "planning", ["roadmap"])      # server re-tags in the background
    r = client.put(f"/api/notes/{nid}", json={"body": "edited", "expected_body_hash": pre})
    assert r.status_code == 200, r.text
    assert r.json()["body"].strip() == "edited"
    assert "roadmap" in r.json()["tags"]                           # tags omitted from push -> server tags survive


def test_export_returns_bodies_and_hashes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    a = client.post("/api/notes", json={"title": "A", "body": "alpha"}).json()
    client.post("/api/notes", json={"title": "B", "body": "beta"})
    data = client.get("/api/notes/export").json()["notes"]
    assert len(data) == 2
    ra = next(n for n in data if n["id"] == a["id"])
    assert ra["body"].strip() == "alpha"
    assert ra["content_hash"] == client.get(f"/api/notes/{a['id']}").json()["content_hash"]


def test_export_excludes_attachments_subtree(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/notes", json={"title": "Real", "body": "x"})
    # A stray .md dropped under attachments/ must never appear in the export.
    att = tmp_path / "attachments"
    att.mkdir(parents=True, exist_ok=True)
    (att / "note-ish.md").write_text("---\nid: n_att\ntitle: Sneaky\n---\n\nbody\n")
    ns._index_cache.clear()
    titles = [n["title"] for n in client.get("/api/notes/export").json()["notes"]]
    assert "Real" in titles and "Sneaky" not in titles
