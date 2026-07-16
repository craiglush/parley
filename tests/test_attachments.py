import io
import notes_store as ns
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path)
    ns._index_cache.clear()
    import app
    monkeypatch.setattr(app, "get_qdrant", lambda: None)
    monkeypatch.setattr(app, "get_embedder", lambda: None)
    return TestClient(app.app)


def test_upload_and_serve_image(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]
    r = client.post(f"/api/notes/{nid}/attachments",
                    files={"file": ("pic.png", io.BytesIO(b"\x89PNG x"), "image/png")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["is_image"] is True
    assert data["embed"].startswith("![[") and data["filename"] in data["embed"]
    got = client.get(data["url"])
    assert got.status_code == 200 and got.content == b"\x89PNG x"


def test_upload_unknown_note_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/notes/n_missing/attachments",
                    files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")})
    assert r.status_code == 404


def test_serve_traversal_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/notes/attachments/missing.png").status_code == 404
    # Encoded path-traversal payload must not escape attachments/.
    assert client.get("/api/notes/attachments/..%2F..%2Fsecret.md").status_code == 404


def test_upload_too_large_413(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "ATTACH_MAX_BYTES", 8)  # tiny cap so the test is fast
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]
    r = client.post(f"/api/notes/{nid}/attachments",
                    files={"file": ("big.bin", io.BytesIO(b"x" * 16), "application/octet-stream")})
    assert r.status_code == 413


def test_upload_non_image_embed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]
    r = client.post(f"/api/notes/{nid}/attachments",
                    files={"file": ("notes.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["is_image"] is False
    assert data["embed"] == f"[{data['filename']}](attachments/{data['filename']})"


def test_save_and_resolve_attachment(tmp_path):
    name = ns.save_attachment(tmp_path, "My Diagram.PNG", b"\x89PNG data")
    assert name.endswith(".png") and name != "My Diagram.PNG"   # sanitized + unique
    p = ns.attachment_path(tmp_path, name)
    assert p is not None and p.read_bytes() == b"\x89PNG data"
    assert p.parent.name == "attachments"


def test_unique_on_collision(tmp_path):
    a = ns.save_attachment(tmp_path, "x.txt", b"1")
    b = ns.save_attachment(tmp_path, "x.txt", b"2")
    assert a != b


def test_attachment_path_rejects_traversal(tmp_path):
    assert ns.attachment_path(tmp_path, "../secret.md") is None
    assert ns.attachment_path(tmp_path, "a/b.png") is None
    assert ns.attachment_path(tmp_path, "missing.png") is None


def test_attachment_path_rejects_absolute(tmp_path):
    # An absolute path must not escape attachments/ even when the target exists.
    secret = tmp_path / "secret.md"
    secret.write_bytes(b"top secret")
    assert ns.attachment_path(tmp_path, str(secret)) is None


def test_list_attachments(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]
    up = client.post(f"/api/notes/{nid}/attachments",
                     files={"file": ("plan.txt", io.BytesIO(b"the quarterly plan"), "text/plain")}).json()
    fname = up["filename"]
    # embed the reference into the note body so it is "referenced"
    client.put(f"/api/notes/{nid}", json={"body": f"see [{fname}](attachments/{fname})"})
    r = client.get(f"/api/notes/{nid}/attachments")
    assert r.status_code == 200, r.text
    items = r.json()["attachments"]
    assert len(items) == 1
    it = items[0]
    assert it["filename"] == fname and it["is_image"] is False and it["size"] == 18
    assert it["extraction_status"] in ("done", "none")


def test_list_attachments_missing_note_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/notes/n_missing/attachments").status_code == 404


def test_delete_attachment_removes_bytes_and_extracted_only(tmp_path, monkeypatch):
    import extract
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]
    up = client.post(f"/api/notes/{nid}/attachments",
                     files={"file": ("plan.txt", io.BytesIO(b"content here"), "text/plain")}).json()
    fname = up["filename"]
    attach_dir = ns.attachments_dir(tmp_path)

    # seed extraction sidecar directly
    extract.write_extraction(attach_dir, fname, {"text": "x", "method": "text", "chars": 1, "status": "done"})

    # seed a per-note .analysis sidecar that must SURVIVE the single-attachment delete
    analysis = attach_dir / ".analysis" / f"{nid}.json"
    analysis.parent.mkdir(parents=True, exist_ok=True)
    analysis.write_text('{"summary": "keep me"}', encoding="utf-8")

    assert ns.attachment_path(tmp_path, fname) is not None
    assert extract.read_extraction(attach_dir, fname) is not None

    r = client.delete(f"/api/notes/{nid}/attachments/{fname}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert ns.attachment_path(tmp_path, fname) is None                       # bytes gone
    assert extract.read_extraction(attach_dir, fname) is None                # .extracted gone
    assert analysis.exists()                                                 # .analysis kept


def test_delete_attachment_missing_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]
    assert client.delete(f"/api/notes/{nid}/attachments/missing.png").status_code == 404
    assert client.delete("/api/notes/n_missing/attachments/x.png").status_code == 404


def test_upload_txt_extracts_inline(tmp_path, monkeypatch):
    import extract
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]
    r = client.post(f"/api/notes/{nid}/attachments",
                    files={"file": ("plan.txt", io.BytesIO(b"budget review notes"), "text/plain")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "done" and data["extracted"] is True
    sc = extract.read_extraction(ns.attachments_dir(tmp_path), data["filename"])
    assert sc["text"] == "budget review notes" and sc["method"] == "text"


def test_upload_image_is_pending_and_enqueued(tmp_path, monkeypatch):
    import app
    enq = []
    monkeypatch.setattr(app, "_enqueue_extract", lambda nid, fn: enq.append((nid, fn)))
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]
    r = client.post(f"/api/notes/{nid}/attachments",
                    files={"file": ("pic.png", io.BytesIO(b"\x89PNG x"), "image/png")})
    data = r.json()
    assert data["status"] == "pending" and data["extracted"] is False
    assert data["is_image"] is True                 # existing behavior preserved
    assert enq == [(nid, data["filename"])]


def test_upload_survives_sidecar_write_failure(tmp_path, monkeypatch):
    import extract
    client = _client(tmp_path, monkeypatch)
    nid = client.post("/api/notes", json={"title": "N", "body": ""}).json()["id"]

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(extract, "write_extraction", _boom)
    r = client.post(f"/api/notes/{nid}/attachments",
                    files={"file": ("x.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed" and body["extracted"] is False
    # bytes were still stored
    assert client.get(body["url"]).status_code == 200
