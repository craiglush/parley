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
