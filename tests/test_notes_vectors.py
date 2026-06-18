import notes_vectors


def test_collection_name():
    assert notes_vectors.NOTES_COLLECTION == "notes"


def test_chunk_note_empty():
    assert notes_vectors.chunk_note("") == []
    assert notes_vectors.chunk_note("   \n  ") == []


def test_chunk_note_splits_to_size():
    text = ". ".join([f"sentence number {i} here" for i in range(60)])
    chunks = notes_vectors.chunk_note(text, max_chars=200)
    assert len(chunks) > 1
    assert all(c.strip() for c in chunks)
    assert all(len(c) <= 260 for c in chunks)  # ~max_chars + one sentence slack


def test_chunk_note_short_is_one():
    assert notes_vectors.chunk_note("just a short note") == ["just a short note"]


class FakeEmbedder:
    def __init__(self, dim=4):
        self.dim = dim
    def encode(self, texts):
        if isinstance(texts, str):
            return [float(len(texts) % 7)] * self.dim
        return [[float((len(t) + i) % 7) for _ in range(self.dim)] for i, t in enumerate(texts)]


class FakeQdrant:
    def __init__(self):
        self.collections = {}   # name -> dim
        self.points = {}        # name -> list[PointStruct]
    def get_collections(self):
        class C: pass
        c = C(); c.collections = [type("X", (), {"name": n})() for n in self.collections]
        return c
    def create_collection(self, collection_name, vectors_config):
        self.collections[collection_name] = vectors_config.size
        self.points[collection_name] = []
    def upsert(self, collection_name, points):
        self.points.setdefault(collection_name, []).extend(points)
    def delete(self, collection_name, points_selector):
        # points_selector is a Filter on note_id; drop matching points
        keep = []
        target = points_selector.must[0].match.value
        for p in self.points.get(collection_name, []):
            if (p.payload or {}).get("note_id") != target:
                keep.append(p)
        self.points[collection_name] = keep
    def search(self, collection_name, query_vector, limit=10, query_filter=None):
        pts = self.points.get(collection_name, [])
        out = []
        for p in pts[:limit]:
            h = type("H", (), {})(); h.payload = p.payload; h.score = 1.0
            out.append(h)
        return out


def test_index_and_delete_note_vectors():
    q, e = FakeQdrant(), FakeEmbedder()
    rec = {"id": "n_1", "title": "T", "folder": "Inbox", "tags": ["x"],
           "linked_meetings": [], "body": "Sentence one. Sentence two. Sentence three."}
    n = notes_vectors.index_note(q, e, rec, collection="notes", dim=4)
    assert n >= 1
    assert "notes" in q.collections and q.collections["notes"] == 4
    assert all((p.payload["note_id"] == "n_1") for p in q.points["notes"])
    assert q.points["notes"][0].payload["title"] == "T"

    # re-index replaces (no duplicate growth beyond new chunk count)
    n2 = notes_vectors.index_note(q, e, rec, collection="notes", dim=4)
    assert len(q.points["notes"]) == n2

    notes_vectors.delete_note_vectors(q, "n_1", collection="notes")
    assert q.points["notes"] == []


def test_index_empty_body_noop():
    q, e = FakeQdrant(), FakeEmbedder()
    assert notes_vectors.index_note(q, e, {"id": "n_1", "body": ""}, collection="notes", dim=4) == 0


def test_search_notes():
    q, e = FakeQdrant(), FakeEmbedder()
    rec = {"id": "n_1", "title": "Recipe", "folder": "Food", "tags": [],
           "linked_meetings": [], "body": "Apple pie. Bake at 350. Serve warm."}
    notes_vectors.index_note(q, e, rec, collection="notes", dim=4)
    hits = notes_vectors.search_notes(q, e, "apple", collection="notes", dim=4, limit=5)
    assert len(hits) >= 1
    h = hits[0]
    assert h["note_id"] == "n_1" and h["title"] == "Recipe" and h["folder"] == "Food"
    assert "text" in h and "score" in h
