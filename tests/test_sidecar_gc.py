"""Tests for _sidecar_gc: orphan-sidecar GC for notes/.analysis, .enhance_state.json,
and attachments/.extracted, plus the eager prune on note delete (api_delete_note).

Liveness must be read from disk (notes_store.get_index(force=True) walks the real
.md files; attachment liveness is the attachment file's own existence) — these
tests seed a real vault under tmp_path via notes_store.NOTES_DIR so the GC sees
genuine disk state, not the in-memory index.
"""
import json

import app
import notes_store
import extract


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path)
    notes_store._index_cache.clear()
    return notes_store


def test_sidecar_gc_removes_orphans_keeps_live(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)

    live = notes_store.create_note(tmp_path, "Live Note", body="keep me")
    live_id = live["id"]
    gone_id = "note-does-not-exist"

    attach_dir = notes_store.attachments_dir(tmp_path)

    # (B) .analysis sidecars: one for the live note, one orphaned
    analysis_dir = attach_dir / ".analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / f"{live_id}.json").write_text(json.dumps({"summary": "live"}))
    (analysis_dir / f"{gone_id}.json").write_text(json.dumps({"summary": "orphan"}))

    # (C) .enhance_state.json: one live key, one gone key
    (tmp_path / ".enhance_state.json").write_text(json.dumps({
        live_id: {"tag_sig": "abc"},
        gone_id: {"tag_sig": "def"},
    }))

    # (A) .extracted sidecars: one for a present attachment, one for an absent one
    present_fname = notes_store.save_attachment(tmp_path, "present.txt", b"hi")
    absent_fname = "absent-123abc.txt"
    extracted_dir = attach_dir / extract.EXTRACTED_DIRNAME
    extracted_dir.mkdir(parents=True, exist_ok=True)
    (extracted_dir / f"{present_fname}.json").write_text(json.dumps({"text": "present"}))
    (extracted_dir / f"{absent_fname}.json").write_text(json.dumps({"text": "absent"}))

    app._sidecar_gc()

    # (B) orphan removed, live kept
    assert not (analysis_dir / f"{gone_id}.json").exists()
    assert (analysis_dir / f"{live_id}.json").exists()
    assert json.loads((analysis_dir / f"{live_id}.json").read_text())["summary"] == "live"

    # (C) pruned to live keys only
    state = json.loads((tmp_path / ".enhance_state.json").read_text())
    assert state == {live_id: {"tag_sig": "abc"}}

    # (A) present-attachment sidecar kept, absent-attachment sidecar removed
    assert (extracted_dir / f"{present_fname}.json").exists()
    assert not (extracted_dir / f"{absent_fname}.json").exists()


def test_sidecar_gc_is_nonfatal_on_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(notes_store, "get_index", _boom)
    app._sidecar_gc()   # must not raise


def test_sidecar_gc_no_enhance_state_file_is_noop(tmp_path, monkeypatch):
    # No .enhance_state.json / .analysis / .extracted on disk at all — must not error.
    _seed(tmp_path, monkeypatch)
    notes_store.create_note(tmp_path, "Only Note", body="x")
    app._sidecar_gc()   # must not raise
    assert not (tmp_path / ".enhance_state.json").exists()


def test_eager_prune_on_note_delete(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "get_qdrant", lambda: None)
    monkeypatch.setattr(app, "get_embedder", lambda: None)
    # Run the fire-and-forget prune/deindex work inline so it's deterministic.
    monkeypatch.setattr(app, "_run_bg", lambda fn, *a: fn(*a))
    client = TestClient(app.app)

    nid = client.post("/api/notes", json={"title": "N", "body": "hi"}).json()["id"]
    analysis_dir = notes_store.attachments_dir(tmp_path) / ".analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / f"{nid}.json").write_text(json.dumps({"summary": "S"}))
    (tmp_path / ".enhance_state.json").write_text(json.dumps({nid: {"tag_sig": "x"}}))

    r = client.delete(f"/api/notes/{nid}")
    assert r.status_code == 200, r.text

    # eager cleanup happened immediately (not waiting for the 24h sweep)
    assert not (analysis_dir / f"{nid}.json").exists()
    state = json.loads((tmp_path / ".enhance_state.json").read_text())
    assert nid not in state
