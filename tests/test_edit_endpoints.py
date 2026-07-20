"""Tests for the edit-everything feature: PATCH title, PUT summary field,
PUT segment text, and the _reindex_meeting_safe delete->re-store cycle.

Harness mirrors tests/test_meeting_routes.py (bare TestClient so startup events
never fire; MEETINGS_DIR / meetings / get_qdrant / get_embedder monkeypatched)
with additions required to observe re-indexing without live services
(design spec, Testing section):
  * app._run_bg is patched to run inline (still used by unrelated
    fire-and-forget paths in app.py, e.g. the a360 push);
  * vector.get_qdrant / vector.get_embedder are patched to the SAME fakes,
    because store_in_qdrant resolves those names in the vector module's own
    namespace (vector.py:126-129) — app-level patches alone would let the
    upsert half hit real httpx/Qdrant.
  * reindex jobs are now coalesced onto app._reindex_queue and consumed by
    app._reindex_worker (a real background asyncio task started at app
    startup, which never fires in this bare-TestClient harness). Tests that
    need the effect of a reindex call the `_drain_reindex(app)` helper below,
    which synchronously runs every currently-queued job the same way the
    worker eventually would (minus its debounce sleep).
"""

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient


class _FakeEmbedder:
    # store_in_qdrant calls .encode(texts, batch_size=32).tolist(); search paths
    # call .encode(q).tolist() — accept both, return numpy arrays.
    def encode(self, texts, batch_size=32):
        if isinstance(texts, str):
            return np.asarray([0.1, 0.2, 0.3, 0.4], dtype="float32")
        return np.asarray([[0.1, 0.2, 0.3, 0.4] for _ in texts], dtype="float32")


class _FakeQdrant:
    def __init__(self):
        self.points = {}    # collection -> [PointStruct-or-dict]
        self.deleted = []   # (collection_name, points_selector) per delete call

    def get_collections(self):
        c = type("C", (), {})()
        c.collections = [type("X", (), {"name": n})() for n in self.points]
        return c

    def create_collection(self, collection_name, vectors_config):
        self.points.setdefault(collection_name, [])

    def upsert(self, collection_name, points):
        self.points.setdefault(collection_name, []).extend(points)

    def delete(self, collection_name, points_selector=None):
        # Record the filter (assertions target it) AND emulate the
        # delete-by-meeting_id so double-reindex tests can assert no duplicates.
        self.deleted.append((collection_name, points_selector))
        try:
            mid = points_selector.must[0].match.value
        except Exception:
            return None
        kept = []
        for p in self.points.get(collection_name, []):
            payload = p.payload if hasattr(p, "payload") else p
            if payload.get("meeting_id") != mid:
                kept.append(p)
        self.points[collection_name] = kept
        return None

    def search(self, collection_name, query_vector, limit=10, query_filter=None):
        return []


def _payloads(fake_q, collection="meetings"):
    """Payload dicts for every point currently in the fake collection."""
    return [p.payload if hasattr(p, "payload") else p
            for p in fake_q.points.get(collection, [])]


def _client(tmp_path, monkeypatch):
    import app
    import vector
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(app, "meetings", {})            # fresh, auto-reverts
    fake_q = _FakeQdrant()
    monkeypatch.setattr(app, "get_qdrant", lambda: fake_q)
    monkeypatch.setattr(app, "get_embedder", lambda: _FakeEmbedder())
    # store_in_qdrant resolves these in vector's namespace — patch there too.
    monkeypatch.setattr(vector, "get_qdrant", lambda: fake_q)
    monkeypatch.setattr(vector, "get_embedder", lambda: _FakeEmbedder())
    # Run fire-and-forget non-reindex background work inline so its effects
    # are deterministic (reindex itself now goes through the coalescing
    # queue — see _drain_reindex).
    monkeypatch.setattr(app, "_run_bg", lambda fn, *a: fn(*a))
    # Fresh reindex queue/pending-set per test: these are real module globals
    # (not monkeypatched, since _reindex_worker needs the live objects), so
    # clear any leftovers from a previous test.
    app._reindex_pending.clear()
    while not app._reindex_queue.empty():
        app._reindex_queue.get_nowait()
    return TestClient(app.app), app, fake_q


def _drain_reindex(app):
    """Synchronously run every currently-queued reindex job, mirroring what
    app._reindex_worker would eventually do minus its debounce sleep. Safe to
    call even when the queue is empty."""
    while not app._reindex_queue.empty():
        mid = app._reindex_queue.get_nowait()
        app._reindex_pending.discard(mid)
        app._reindex_now(mid)


SEGMENTS = [
    {"start": 0.0, "end": 2.0, "speaker": "Alex",
     "text": "We agreed to ship the beta on Friday."},
    {"start": 2.0, "end": 4.0, "speaker": "Dana",
     "text": "I will draft the release notes."},
]

SUMMARY = {
    "title": "Weekly Sync",
    "summary": "Alex and Dana planned the beta release.",
    "topics": [{"topic": "Beta", "summary": "Ship plan", "outcome": "agreed"}],
    "action_items": [
        {"task": "Ship the beta", "who": "Alex", "deadline": "Friday", "priority": "high"},
        {"task": "Draft release notes", "who": "Dana", "deadline": "", "priority": "medium"},
    ],
    "decisions": [{"decision": "Ship Friday", "context": "Everyone agreed"}],
    "open_questions": [{"question": "Who signs off QA?", "asked_by": "Dana", "answered": False}],
    "concerns": [{"concern": "QA is underwater", "raised_by": "Alex",
                  "resolved": False, "notes": "hire?"}],
    "figures": [],
    "sentiment": {},
}


def _seed_editable(app, tmp_path, mid="m1", title="Weekly Sync"):
    """A complete meeting with real transcript.json / summary.json on disk
    (the same shape the pipeline's storage step writes)."""
    out = tmp_path / f"2026-06-17_{mid}"
    out.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": mid, "date": "2026-06-17", "title": title,
        "status": app.MeetingStatus.complete,
        "created_at": "2026-06-17T10:00:00+00:00",
        "duration_formatted": "00:10:00", "duration": 600,
        "progress_percent": 100, "progress_detail": "Complete",
        "transcript_cleaned": True, "step_timings": {"transcription": 1.0},
        "tags": {"category": "standup", "keywords": ["roadmap"],
                 "entities": {"people": ["Alex"], "companies": [], "projects": [],
                              "technologies": [], "dates": []}},
        "output_dir": str(out),
        "links": {"manual": [], "suggestions": []},
        "summary": json.loads(json.dumps(SUMMARY)),
    }
    app.meetings[mid] = rec
    transcript = {"meeting_id": mid, "date": "2026-06-17", "duration": 600,
                  "language": "en", "segments": json.loads(json.dumps(SEGMENTS)),
                  "cleaned": True}
    (out / "transcript.json").write_text(json.dumps(transcript, indent=2))
    (out / "summary.json").write_text(json.dumps(SUMMARY, indent=2))
    return rec, out


# ------------------------------------------------------------- _reindex_meeting_safe

def test_reindex_deletes_by_meeting_filter_then_restores(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    _seed_editable(app, tmp_path, mid="m1")
    app._reindex_meeting_safe("m1")
    _drain_reindex(app)
    # Exactly one delete call, targeting exactly this meeting's points.
    assert len(fake_q.deleted) == 1
    coll, selector = fake_q.deleted[0]
    assert coll == "meetings"
    assert selector.must[0].key == "meeting_id"
    assert selector.must[0].match.value == "m1"
    # Points re-stored from the on-disk files.
    payloads = _payloads(fake_q)
    assert payloads, "expected points to be re-upserted"
    assert all(p["meeting_id"] == "m1" for p in payloads)
    texts = " ".join(p["text"] for p in payloads)
    assert "release notes" in texts        # transcript chunk
    assert "Ship the beta" in texts        # action item


def test_reindex_twice_does_not_duplicate_points(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    _seed_editable(app, tmp_path, mid="m1")
    app._reindex_meeting_safe("m1")
    _drain_reindex(app)
    once = len(fake_q.points.get("meetings", []))
    assert once > 0
    app._reindex_meeting_safe("m1")
    _drain_reindex(app)
    assert len(fake_q.points.get("meetings", [])) == once


def test_reindex_skips_meeting_deleted_while_queued(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    _seed_editable(app, tmp_path, mid="m1")
    app._reindex_meeting_safe("m1")   # enqueue, but don't run the job yet
    del app.meetings["m1"]            # meeting deleted before the job runs
    _drain_reindex(app)               # now run the stale job
    # The worker must bail inside the lock: no delete, no resurrected points.
    assert fake_q.deleted == []
    assert fake_q.points.get("meetings", []) == []
    # Unknown id is a silent no-op too (captures nothing new).
    app._reindex_meeting_safe("never-existed")
    _drain_reindex(app)
    assert fake_q.deleted == []


def test_reindex_failure_is_nonfatal(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    _seed_editable(app, tmp_path, mid="m1")

    def _boom(**kwargs):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(fake_q, "delete", _boom)
    app._reindex_meeting_safe("m1")
    _drain_reindex(app)   # must not raise (warning logged)


def test_reindex_burst_coalesces_to_single_run(tmp_path, monkeypatch):
    """3 rapid edits to the same meeting must coalesce into ONE queued job
    (and therefore one reindex run), not three."""
    client, app, fake_q = _client(tmp_path, monkeypatch)
    _seed_editable(app, tmp_path, mid="m1")
    app._reindex_meeting_safe("m1")
    app._reindex_meeting_safe("m1")
    app._reindex_meeting_safe("m1")
    assert app._reindex_queue.qsize() == 1
    assert "m1" in app._reindex_pending
    _drain_reindex(app)
    assert len(fake_q.deleted) == 1, "burst of 3 edits must coalesce to one reindex run"


def test_delete_meeting_endpoint_still_works_with_lock(tmp_path, monkeypatch):
    # Regression guard for the delete_meeting change: taking _lock_for(mid)
    # around vector-delete + record-removal must not break or deadlock DELETE.
    client, app, fake_q = _client(tmp_path, monkeypatch)
    _seed_editable(app, tmp_path, mid="m1")
    r = client.delete("/meetings/m1")
    assert r.status_code == 200, r.text
    assert "m1" not in app.meetings
    assert fake_q.deleted and fake_q.deleted[0][1].must[0].match.value == "m1"


# ------------------------------------------------------------- PATCH title

def test_patch_title_404_409_400(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    assert client.patch("/meetings/nope", json={"title": "X"}).status_code == 404
    app.meetings["q1"] = {"id": "q1", "date": "2026-06-17", "title": "Q",
                          "status": app.MeetingStatus.queued,
                          "output_dir": str(tmp_path / "q")}
    assert client.patch("/meetings/q1", json={"title": "X"}).status_code == 409
    _seed_editable(app, tmp_path, mid="m1")
    assert client.patch("/meetings/m1", json={"title": "   "}).status_code == 400


def test_patch_title_success_rewrites_record_files_and_vectors(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    r = client.patch("/meetings/m1", json={"title": "Beta Launch Sync"})
    assert r.status_code == 200, r.text
    assert r.json() == {"detail": "Title updated", "title": "Beta Launch Sync"}
    # record + flag + persisted index.json
    assert app.meetings["m1"]["title"] == "Beta Launch Sync"
    assert app.meetings["m1"]["title_edited"] is True
    idx = json.loads((tmp_path / "index.json").read_text())
    assert idx["m1"]["title"] == "Beta Launch Sync"
    assert idx["m1"]["title_edited"] is True
    # summary.json's own title key mirrored (summary.md H1 sources from it)
    assert json.loads((out / "summary.json").read_text())["title"] == "Beta Launch Sync"
    assert (out / "summary.md").read_text().splitlines()[0] == "# Beta Launch Sync"
    assert (out / "transcript.md").read_text().splitlines()[0] == "# Transcript: Beta Launch Sync"
    # reindexed: delete targeted this meeting; new payloads carry the new title
    _drain_reindex(app)
    assert fake_q.deleted and fake_q.deleted[0][1].must[0].match.value == "m1"
    payloads = _payloads(fake_q)
    assert payloads and all(p["title"] == "Beta Launch Sync" for p in payloads)


def test_reprocess_summarize_preserves_manually_edited_title(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    assert client.patch("/meetings/m1", json={"title": "My Title"}).status_code == 200

    done = {}

    async def _fake_summarize(transcript_text, duration, progress_callback=None, context=""):
        done["ran"] = True
        return {**json.loads(json.dumps(SUMMARY)), "title": "LLM Title"}

    monkeypatch.setattr(app, "step_summarize", _fake_summarize)
    monkeypatch.setattr(app, "_gather_meeting_context", lambda mid: "")
    r = client.post("/meetings/m1/reprocess", json={"step": "summarize"})
    assert r.status_code == 200, r.text
    # The step runs via asyncio.create_task; each TestClient GET pumps the loop
    # (same idiom as tests/test_meeting_context.py::test_reprocess_summarize_passes_linked_context).
    for _ in range(50):
        s = client.get("/meetings/m1/status").json()
        if done.get("ran") and s["status"] == "complete":
            break
    assert app.meetings["m1"]["title"] == "My Title"
    # gate also mirrors the manual title back into the regenerated summary.json,
    # so summary.md's H1 and the exported JSON stay consistent with the record
    assert json.loads((out / "summary.json").read_text())["title"] == "My Title"


# ------------------------------------------------------------- PUT summary field

def test_put_summary_field_validation(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    # unknown meeting -> 404
    assert client.put("/meetings/nope/summary/summary", json={"value": "x"}).status_code == 404
    # field outside the approved five -> 400 (topics/figures/sentiment are not editable)
    assert client.put("/meetings/m1/summary/topics", json={"value": []}).status_code == 400
    # wrong types -> 400
    assert client.put("/meetings/m1/summary/summary", json={"value": ["not a str"]}).status_code == 400
    assert client.put("/meetings/m1/summary/summary", json={"value": "   "}).status_code == 400
    assert client.put("/meetings/m1/summary/decisions", json={"value": "not a list"}).status_code == 400
    assert client.put("/meetings/m1/summary/decisions", json={"value": ["not a dict"]}).status_code == 400
    # action_items length change -> 400 (task_overlay.json is keyed by index)
    assert client.put("/meetings/m1/summary/action_items",
                      json={"value": [{"task": "only one"}]}).status_code == 400
    # summary.json missing on disk -> 404
    (out / "summary.json").unlink()
    assert client.put("/meetings/m1/summary/summary", json={"value": "x"}).status_code == 404


@pytest.mark.parametrize("field,value,expect_text,old_text", [
    ("summary", "Rewritten overview of the beta plan.",
     "Rewritten overview", "planned the beta release"),
    ("action_items",
     [{"task": "Ship the beta", "who": "Alex", "deadline": "Friday", "priority": "high"},
      {"task": "Publish the changelog", "who": "Dana", "deadline": "", "priority": "medium"}],
     "Publish the changelog", "Draft release notes"),
    ("decisions", [{"decision": "Delay to Monday", "context": "QA needs the weekend"}],
     "Delay to Monday", "Ship Friday"),
    ("concerns", [{"concern": "Scope creep", "raised_by": "Dana", "resolved": False, "notes": ""}],
     "Scope creep", "QA is underwater"),
    ("open_questions", [{"question": "Which CDN do we use?", "asked_by": "Alex", "answered": False}],
     "Which CDN do we use?", "Who signs off QA?"),
])
def test_put_summary_field_success(tmp_path, monkeypatch, field, value, expect_text, old_text):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    r = client.put(f"/meetings/m1/summary/{field}", json={"value": value})
    assert r.status_code == 200, r.text
    updated = r.json()
    expected_value = value.strip() if isinstance(value, str) else value
    assert updated[field] == expected_value
    # summary.json + summary.md rewritten; in-memory mirror updated
    assert json.loads((out / "summary.json").read_text())[field] == expected_value
    assert expect_text in (out / "summary.md").read_text()
    assert app.meetings["m1"]["summary"][field] == expected_value
    # reindexed payloads contain the edited text and no longer the replaced text
    _drain_reindex(app)
    assert fake_q.deleted, "expected a vector delete->re-store"
    texts = " ".join(p["text"] for p in _payloads(fake_q))
    assert expect_text in texts
    assert old_text not in texts


def test_put_summary_canonicalizes_legacy_aliases(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    legacy = {"title": "Weekly Sync",
              "executive_summary": "old style summary",
              "questions_raised": [{"question": "legacy?", "answered": False}],
              "action_items": []}
    (out / "summary.json").write_text(json.dumps(legacy))
    # summary: canonical key written, executive_summary alias removed
    r = client.put("/meetings/m1/summary/summary", json={"value": "new canonical text"})
    assert r.status_code == 200, r.text
    on_disk = json.loads((out / "summary.json").read_text())
    assert on_disk["summary"] == "new canonical text"
    assert "executive_summary" not in on_disk
    # GET serves the edit (readers fall back summary-or-legacy)
    assert client.get("/meetings/m1/summary").json()["summary"] == "new canonical text"
    # open_questions: same canonicalization for questions_raised
    r = client.put("/meetings/m1/summary/open_questions",
                   json={"value": [{"question": "new?", "answered": True}]})
    assert r.status_code == 200, r.text
    on_disk = json.loads((out / "summary.json").read_text())
    assert on_disk["open_questions"] == [{"question": "new?", "answered": True}]
    assert "questions_raised" not in on_disk


def test_put_action_items_clears_stale_overlay_text(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    overlay = {"0": {"text": "todo-side override", "edited": True, "done": True},
               "1": {"text": "keep me", "edited": True}}
    (out / "task_overlay.json").write_text(json.dumps(overlay))
    new_items = [
        {"task": "Ship the beta v2", "who": "Alex", "deadline": "Friday", "priority": "high"},   # 0: text changed
        {"task": "Draft release notes", "who": "Dana", "deadline": "", "priority": "medium"},     # 1: text unchanged
    ]
    r = client.put("/meetings/m1/summary/action_items", json={"value": new_items})
    assert r.status_code == 200, r.text
    ov = json.loads((out / "task_overlay.json").read_text())
    # changed index: summary-side edit wins — text/edited cleared, done preserved
    assert "text" not in ov["0"] and "edited" not in ov["0"]
    assert ov["0"]["done"] is True
    # unchanged index: the To-Do-side override still overlay-wins
    assert ov["1"]["text"] == "keep me" and ov["1"]["edited"] is True


# ------------------------------------------------------------- PUT segment text

def test_put_segment_404_400_and_stale_409(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    assert client.put("/meetings/nope/segments/0", json={"text": "x"}).status_code == 404
    assert client.put("/meetings/m1/segments/99", json={"text": "x"}).status_code == 404
    assert client.put("/meetings/m1/segments/0", json={"text": "   "}).status_code == 400
    # stale expected_text -> 409 and transcript.json byte-identical (never writes)
    before = (out / "transcript.json").read_bytes()
    r = client.put("/meetings/m1/segments/0",
                   json={"text": "new", "expected_text": "not what is there"})
    assert r.status_code == 409, r.text
    assert "refresh" in r.json()["detail"].lower()
    assert (out / "transcript.json").read_bytes() == before
    # untouched meeting reports transcript_edited False in /status
    assert client.get("/meetings/m1/status").json()["transcript_edited"] is False


def test_put_segment_success_rewrites_files_and_reindexes(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    r = client.put("/meetings/m1/segments/0", json={
        "text": "We agreed to ship the beta next Tuesday.",
        "expected_text": "We agreed to ship the beta on Friday.",
    })
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "We agreed to ship the beta next Tuesday."
    # transcript.json / .srt / .md all carry the new text
    disk = json.loads((out / "transcript.json").read_text())
    assert disk["segments"][0]["text"] == "We agreed to ship the beta next Tuesday."
    assert "next Tuesday" in (out / "transcript.srt").read_text()
    assert "next Tuesday" in (out / "transcript.md").read_text()
    # in-memory transcript_text refreshed; edited flag set + visible in /status
    assert "next Tuesday" in app.meetings["m1"]["transcript_text"]
    assert app.meetings["m1"]["transcript_edited"] is True
    assert client.get("/meetings/m1/status").json()["transcript_edited"] is True
    # raw_transcript.json (pre-cleanup source) deliberately untouched
    assert not (out / "raw_transcript.json").exists()
    # reindexed with the edited text only
    _drain_reindex(app)
    assert fake_q.deleted, "expected a vector delete->re-store"
    texts = " ".join(p["text"] for p in _payloads(fake_q))
    assert "next Tuesday" in texts and "on Friday" not in texts


def test_reprocess_summarize_clears_transcript_edited(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    assert client.put("/meetings/m1/segments/0",
                      json={"text": "edited text"}).status_code == 200
    assert app.meetings["m1"]["transcript_edited"] is True

    done = {}

    async def _fake_summarize(transcript_text, duration, progress_callback=None, context=""):
        done["ran"] = True
        return json.loads(json.dumps(SUMMARY))

    monkeypatch.setattr(app, "step_summarize", _fake_summarize)
    monkeypatch.setattr(app, "_gather_meeting_context", lambda mid: "")
    assert client.post("/meetings/m1/reprocess", json={"step": "summarize"}).status_code == 200
    for _ in range(50):
        s = client.get("/meetings/m1/status").json()
        if done.get("ran") and s["status"] == "complete":
            break
    assert app.meetings["m1"].get("transcript_edited") is False
    assert client.get("/meetings/m1/status").json()["transcript_edited"] is False


# ------------------------------------------------------------- reindex hooks

@pytest.mark.parametrize("method,path,body", [
    ("put", "/meetings/m1/speakers",
     {"speaker_map": {"SPEAKER_00": "Alex"}}),
    ("post", "/meetings/m1/speakers/merge",
     {"speakers": ["Alex", "Dana"], "target": "Alex"}),
    ("post", "/meetings/m1/speakers/reassign",
     {"segment_indices": [0], "new_speaker": "Dana"}),
])
def test_speaker_edits_reindex(tmp_path, monkeypatch, method, path, body):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")
    r = getattr(client, method)(path, json=body)
    assert r.status_code == 200, r.text
    _drain_reindex(app)
    assert fake_q.deleted, "speaker edits must delete->re-store vectors"
    assert fake_q.deleted[0][1].must[0].match.value == "m1"
    payloads = _payloads(fake_q)
    assert payloads and all(p["meeting_id"] == "m1" for p in payloads)


def test_reprocess_step_reindexes_on_success(tmp_path, monkeypatch):
    client, app, fake_q = _client(tmp_path, monkeypatch)
    rec, out = _seed_editable(app, tmp_path, mid="m1")

    done = {}

    async def _fake_summarize(transcript_text, duration, progress_callback=None, context=""):
        done["ran"] = True
        return {**json.loads(json.dumps(SUMMARY)), "summary": "Reprocessed summary text."}

    monkeypatch.setattr(app, "step_summarize", _fake_summarize)
    monkeypatch.setattr(app, "_gather_meeting_context", lambda mid: "")
    assert client.post("/meetings/m1/reprocess", json={"step": "summarize"}).status_code == 200
    for _ in range(50):
        s = client.get("/meetings/m1/status").json()
        if done.get("ran") and s["status"] == "complete":
            break
    _drain_reindex(app)
    assert fake_q.deleted, "reprocess success path must delete->re-store vectors"
    texts = " ".join(p["text"] for p in _payloads(fake_q))
    assert "Reprocessed summary text." in texts   # vectors reflect the NEW summary.json
