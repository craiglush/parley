import asyncio
import notes_store as ns


def _seed_note_with_attachment(tmp_path, fname, body_ref, pending_method):
    import extract
    attach = ns.attachments_dir(tmp_path)
    (attach / fname).write_bytes(b"fake bytes")
    rec = ns.create_note(tmp_path, "N", body=body_ref)
    extract.write_extraction(attach, fname, {"text": "", "method": pending_method,
                                             "chars": 0, "status": "pending"})
    return rec


def test_run_extract_job_stt(tmp_path, monkeypatch):
    import app, extract, stt
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "meetings", {})  # not busy
    rec = _seed_note_with_attachment(tmp_path, "rec-ab12.m4a", "![[rec-ab12.m4a]]", "stt")

    monkeypatch.setattr(stt, "preprocess_audio", lambda inp, out: 1.0)

    async def fake_transcribe(path, mn, mx, *, backend=None, diarize=True):
        return {"language": "en", "duration": 2.0,
                "segments": [{"text": "hello"}, {"text": "world"}]}
    monkeypatch.setattr(stt, "step_transcribe", fake_transcribe)

    indexed, tagged = [], []
    monkeypatch.setattr(app, "_index_note_safe", lambda r: indexed.append(r["id"]))
    monkeypatch.setattr(app, "_enqueue_tag", lambda nid: tagged.append(nid))

    ok = asyncio.run(app._run_extract_job(rec["id"], "rec-ab12.m4a"))
    assert ok is True
    sc = extract.read_extraction(ns.attachments_dir(tmp_path), "rec-ab12.m4a")
    assert sc["status"] == "done" and sc["text"] == "hello\nworld" and sc["method"] == "stt"
    assert indexed == [rec["id"]] and tagged == [rec["id"]]


def test_run_extract_job_vision_raster(tmp_path, monkeypatch):
    import app, extract, llm
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "meetings", {})
    rec = _seed_note_with_attachment(tmp_path, "pic-cd34.png", "![[pic-cd34.png]]", "vision")

    async def fake_describe(path, *, prompt):
        return "A bar chart of revenue."
    monkeypatch.setattr(llm, "describe_image", fake_describe)
    monkeypatch.setattr(app, "_index_note_safe", lambda r: None)
    monkeypatch.setattr(app, "_enqueue_tag", lambda nid: None)

    ok = asyncio.run(app._run_extract_job(rec["id"], "pic-cd34.png"))
    assert ok is True
    sc = extract.read_extraction(ns.attachments_dir(tmp_path), "pic-cd34.png")
    assert sc["status"] == "done" and "bar chart" in sc["text"] and sc["method"] == "vision"


def test_run_extract_job_failure_marks_failed(tmp_path, monkeypatch):
    # Generic (non-VisionUnavailable) failures still terminate as 'failed' — only
    # VisionUnavailable gets the retryable 'pending' treatment (see the test below).
    import app, extract, llm
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "meetings", {})
    rec = _seed_note_with_attachment(tmp_path, "pic-ee55.png", "![[pic-ee55.png]]", "vision")

    async def boom(path, *, prompt):
        raise RuntimeError("decode error")
    monkeypatch.setattr(llm, "describe_image", boom)
    monkeypatch.setattr(app, "_index_note_safe", lambda r: None)
    monkeypatch.setattr(app, "_enqueue_tag", lambda nid: None)

    ok = asyncio.run(app._run_extract_job(rec["id"], "pic-ee55.png"))
    assert ok is False
    sc = extract.read_extraction(ns.attachments_dir(tmp_path), "pic-ee55.png")
    assert sc["status"] == "failed"


def test_run_extract_job_vision_unavailable_stays_pending(tmp_path, monkeypatch):
    # F3: VisionUnavailable must not be buried as a terminal 'failed' — the model
    # may just be cold/busy, so the sidecar should stay 'pending' for a later retry
    # (a subsequent Analyze click or worker pass), not require a fresh upload.
    import app, extract, llm
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "meetings", {})
    rec = _seed_note_with_attachment(tmp_path, "pic-gg77.png", "![[pic-gg77.png]]", "vision")

    async def unavailable(path, *, prompt):
        raise llm.VisionUnavailable("model not present")
    monkeypatch.setattr(llm, "describe_image", unavailable)
    monkeypatch.setattr(app, "_index_note_safe", lambda r: None)
    monkeypatch.setattr(app, "_enqueue_tag", lambda nid: None)

    ok = asyncio.run(app._run_extract_job(rec["id"], "pic-gg77.png"))
    assert ok is False
    sc = extract.read_extraction(ns.attachments_dir(tmp_path), "pic-gg77.png")
    assert sc["status"] == "pending"
    assert sc["method"] == "vision"
    assert sc["note_id"] == rec["id"]   # F4: pending sidecars carry note_id for the restart rescan


def test_run_extract_job_skips_terminal_sidecar(tmp_path, monkeypatch):
    # F7: a sidecar already resolved (e.g. by an inline Analyze click) must not be
    # re-processed by a later worker pass — that would waste a GPU pass and could
    # overwrite it with a different prompt's result.
    import app, extract
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "meetings", {})
    rec = _seed_note_with_attachment(tmp_path, "pic-ff66.png", "![[pic-ff66.png]]", "vision")
    attach = ns.attachments_dir(tmp_path)
    extract.write_extraction(attach, "pic-ff66.png",
                              {"text": "already resolved", "method": "vision", "chars": 17, "status": "done"})

    called = []
    async def spy(path, filename):
        called.append(filename)
        return "should not run"
    monkeypatch.setattr(app, "_extract_vision", spy)
    monkeypatch.setattr(app, "_index_note_safe", lambda r: None)
    monkeypatch.setattr(app, "_enqueue_tag", lambda nid: None)

    ok = asyncio.run(app._run_extract_job(rec["id"], "pic-ff66.png"))
    assert ok is True
    assert called == []
    sc = extract.read_extraction(attach, "pic-ff66.png")
    assert sc["text"] == "already resolved"   # not overwritten


def test_rescan_pending_extractions_requeues(tmp_path, monkeypatch):
    # F4: sidecars 'pending' with a known note_id must be re-enqueued on startup
    # (the in-memory extract queue is lost across a restart otherwise).
    import app, extract
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    attach = ns.attachments_dir(tmp_path)
    (attach / "rec-ab12.m4a").write_bytes(b"fake bytes")
    (attach / "pic-cd34.png").write_bytes(b"fake bytes")
    (attach / "old-ee55.txt").write_bytes(b"fake bytes")
    rec = ns.create_note(tmp_path, "N", body="![[rec-ab12.m4a]] ![[pic-cd34.png]] ![[old-ee55.txt]]")

    extract.write_extraction(attach, "rec-ab12.m4a", {"text": "", "method": "stt", "chars": 0,
                                                        "status": "pending", "note_id": rec["id"]})
    # pending but no note_id (pre-fix sidecar) -> must NOT be requeued
    extract.write_extraction(attach, "pic-cd34.png", {"text": "", "method": "vision", "chars": 0,
                                                        "status": "pending"})
    # terminal -> must NOT be requeued
    extract.write_extraction(attach, "old-ee55.txt", {"text": "hi", "method": "text", "chars": 2,
                                                        "status": "done", "note_id": rec["id"]})

    recorded = []
    monkeypatch.setattr(app, "_enqueue_extract", lambda nid, fn: recorded.append((nid, fn)))

    asyncio.run(app._rescan_pending_extractions())
    assert recorded == [(rec["id"], "rec-ab12.m4a")]


def test_rescan_pending_extractions_missing_dir_is_noop(tmp_path, monkeypatch):
    # Non-fatal: a vault with no .extracted directory at all must not raise.
    import app
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    recorded = []
    monkeypatch.setattr(app, "_enqueue_extract", lambda nid, fn: recorded.append((nid, fn)))
    asyncio.run(app._rescan_pending_extractions())
    assert recorded == []


def test_enqueue_extract_coalesces(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "_extract_pending", set())
    # fresh queue for the test
    monkeypatch.setattr(app, "_extract_queue", asyncio.Queue())
    app._enqueue_extract("n_1", "a.png")
    app._enqueue_extract("n_1", "a.png")  # duplicate -> coalesced
    app._enqueue_extract("n_1", "b.png")
    assert app._extract_queue.qsize() == 2


def test_note_attachment_text_joins_done_sidecars(tmp_path, monkeypatch):
    import app, extract
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    monkeypatch.setattr(app, "ATTACH_TEXT_MAX", 40)
    attach = ns.attachments_dir(tmp_path)
    (attach / "a-1.txt").write_bytes(b"x")
    (attach / "b-2.txt").write_bytes(b"x")
    extract.write_extraction(attach, "a-1.txt", {"text": "alpha content", "method": "text",
                                                 "chars": 13, "status": "done"})
    extract.write_extraction(attach, "b-2.txt", {"text": "", "method": "vision",
                                                 "chars": 0, "status": "pending"})
    rec = ns.create_note(tmp_path, "N", body="![[a-1.txt]] and ![[b-2.txt]]")
    full = ns.read_note(tmp_path, rec["id"])
    text = app._note_attachment_text(full)
    assert "alpha content" in text        # done sidecar included
    assert len(text) <= 40                 # capped by ATTACH_TEXT_MAX
    # pending/failed/empty sidecars contribute nothing
    assert "vision" not in text


def test_index_note_safe_passes_attachment_text(tmp_path, monkeypatch):
    import app, extract, notes_vectors
    monkeypatch.setattr(ns, "NOTES_DIR", tmp_path); ns._index_cache.clear()
    attach = ns.attachments_dir(tmp_path)
    (attach / "c-3.txt").write_bytes(b"x")
    extract.write_extraction(attach, "c-3.txt", {"text": "quarterly figures", "method": "text",
                                                 "chars": 17, "status": "done"})
    rec = ns.create_note(tmp_path, "N", body="see ![[c-3.txt]]")
    full = ns.read_note(tmp_path, rec["id"])
    captured = {}
    monkeypatch.setattr(notes_vectors, "index_note",
                        lambda q, e, r, *, collection, dim, extra_text="": captured.setdefault("extra", extra_text) or 1)
    monkeypatch.setattr(app, "get_qdrant", lambda: object())
    monkeypatch.setattr(app, "get_embedder", lambda: object())
    # no running loop -> _run_bg runs work() inline
    app._index_note_safe(full)
    assert "quarterly figures" in captured["extra"]
