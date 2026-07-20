"""Digest tests: pure snapshot collection (T5), rendering (T5), the AI briefing +
manual test endpoint (T6), and the scheduler + state-file dedup (T7)."""
import tasks_store as ts
import emailer


# --- tasks_store.build_digest_snapshot -----------------------------------------

def test_build_digest_snapshot_buckets():
    tasks = [
        {"text": "doing1", "done": False, "state": "doing", "due": None},
        {"text": "overdue1", "done": False, "state": "open", "due": "2026-07-10"},
        {"text": "today1", "done": False, "state": "open", "due": "2026-07-17"},
        {"text": "week1", "done": False, "state": "open", "due": "2026-07-20"},
        {"text": "later1", "done": False, "state": "open", "due": "2026-07-30"},
        {"text": "nodue", "done": False, "state": "open", "due": None},
        {"text": "done1", "done": True, "state": "done", "due": "2026-07-01"},
    ]
    snap = ts.build_digest_snapshot(tasks, "2026-07-17")
    assert snap["counts"] == {"doing": 1, "overdue": 1, "today": 1, "week": 1}
    assert [t["text"] for t in snap["lanes"]["doing"]] == ["doing1"]
    assert [t["text"] for t in snap["lanes"]["overdue"]] == ["overdue1"]
    assert [t["text"] for t in snap["lanes"]["today"]] == ["today1"]
    assert [t["text"] for t in snap["lanes"]["week"]] == ["week1"]


def test_build_digest_snapshot_ignores_done_and_no_state_key():
    tasks = [{"text": "legacy open", "done": False, "due": "2026-07-10"}]  # no "state" key
    snap = ts.build_digest_snapshot(tasks, "2026-07-17")
    assert snap["counts"]["overdue"] == 1   # falls back to "open" via .get("state") or "open"


# --- emailer.render_digest_email -----------------------------------------------

DATA = {
    "weekday": "Friday", "date": "2026-07-17",
    "counts": {"overdue": 1, "today": 2, "doing": 1, "week": 0},
    "lanes": {
        "overdue": [{"text": "Ship the report", "due": "2026-07-10", "priority": "high", "owner": "amy", "source_title": "Weekly Sync"}],
        "today": [{"text": "Call Bob", "due": "2026-07-17", "priority": None, "owner": None, "source_title": "Inbox"},
                  {"text": "Review PR", "due": "2026-07-17", "priority": "low", "owner": "sam", "source_title": "Notes"}],
        "doing": [{"text": "Draft proposal", "due": None, "priority": None, "owner": None, "source_title": "Notes"}],
        "week": [],
    },
    "briefing": "Focus on the overdue report first; two things are due today.",
}


def test_render_digest_email_subject_and_sections():
    subject, html, text = emailer.render_digest_email(DATA, public_url="https://meetings.example.com")
    assert subject == "Tasks digest — Friday 2026-07-17: 1 overdue, 2 today"
    assert "Ship the report" in html and "Call Bob" in html and "Review PR" in html and "Draft proposal" in html
    assert "Focus on the overdue report first" in html
    assert "meetings.example.com" in html
    # Text assertions: structure and content
    assert isinstance(text, str)
    assert "Tasks digest — Friday 2026-07-17" in text
    assert "1 overdue" in text and "2 today" in text
    assert "Ship the report" in text
    assert "Call Bob" in text and "Review PR" in text and "Draft proposal" in text
    assert "Focus on the overdue report first" in text
    assert "OVERDUE (1)" in text and "TODAY (2)" in text and "DOING (1)" in text
    assert "THIS WEEK" not in text  # empty lane should be omitted
    assert "meetings.example.com" in text
    # One task line per task: check for 4 "\n- " patterns (task bullet lines)
    assert text.count("\n- ") == 4
    # Metadata should be present in task lines
    assert "Ship the report  [due 2026-07-10]" in text
    assert "[high]" in text and "[@amy]" in text
    assert "[low]" in text and "[@sam]" in text


def test_render_digest_email_empty_lanes_omit_sections():
    data = dict(DATA, lanes={"overdue": [], "today": [], "doing": [], "week": []}, briefing="")
    subject, html, text = emailer.render_digest_email(data)
    assert "Ship the report" not in html
    assert "Focus on the overdue" not in html   # no briefing paragraph when empty
    # Text should also omit empty lanes and briefing
    assert text.count("\n- ") == 0  # no task lines
    assert "Focus on the overdue" not in text
    assert "OVERDUE" not in text and "TODAY" not in text and "DOING" not in text  # empty lanes omitted
    assert "Open the task board:" in text  # footer always present


def test_render_digest_email_escapes_html():
    data = dict(DATA)
    data["lanes"] = dict(DATA["lanes"], overdue=[{"text": "<script>alert(1)</script>", "due": "2026-07-10", "priority": None, "owner": None, "source_title": "X"}])
    _, html, _ = emailer.render_digest_email(data)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ==============================================================================
# T6: AI briefing + POST /api/digest/test
# ==============================================================================
import asyncio
from fastapi.testclient import TestClient
from tests.test_meeting_routes import _client as _routes_client


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def json(self):
        return self._payload


def test_generate_digest_briefing_happy_path(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")

    async def _fake_call(method, url, *, json_body, **kw):
        assert json_body["format"] == app.ANALYSIS_SCHEMAS["digest_briefing"]
        return _FakeResp({"response": '{"briefing": "Ship the report first."}'})
    monkeypatch.setattr(app, "_retry_ollama_call", _fake_call)

    snapshot = {"counts": {"overdue": 1, "today": 0, "doing": 0, "week": 0},
                "lanes": {"overdue": [{"text": "Ship the report"}], "today": [], "doing": [], "week": []}}
    out = asyncio.run(app._generate_digest_briefing(snapshot))
    assert out == "Ship the report first."


def test_generate_digest_briefing_non_fatal_on_failure(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")

    async def _boom(method, url, *, json_body, **kw):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(app, "_retry_ollama_call", _boom)

    out = asyncio.run(app._generate_digest_briefing({"counts": {}, "lanes": {}}))
    assert out == ""


def test_digest_test_endpoint_disabled_smtp_400(tmp_path, monkeypatch):
    client, app, _ = _routes_client(tmp_path, monkeypatch)
    r = client.post("/api/digest/test")
    assert r.status_code == 400   # no SMTP host configured -> clean 4xx, not a crash


def test_digest_test_endpoint_sends_and_returns_subject(tmp_path, monkeypatch):
    client, app, _ = _routes_client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "meetings", {})
    # Notes isolation: _build_digest_email() collects note tasks via _collect_all_tasks,
    # which reads notes_store.NOTES_DIR -- _routes_client does NOT patch this (it only
    # patches MEETINGS_DIR/SETTINGS_PATH/meetings/qdrant/embedder), so without this the
    # digest snapshot would be built from whatever is on the real NOTES_DIR on disk.
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()
    client.put("/api/settings", json={
        "smtp": {"enabled": True, "host": "smtp.example.com", "from_email": "a@x.com"},
        "digest": {"recipients": "alex@example.com"},
    })

    async def _fake_briefing(snapshot):
        return ""
    monkeypatch.setattr(app, "_generate_digest_briefing", _fake_briefing)

    sent = {}
    def _fake_send(smtp, recipients, subject, html_body, text_body):
        sent["recipients"] = recipients
        sent["subject"] = subject
    monkeypatch.setattr(app.emailer, "send_email", _fake_send)

    r = client.post("/api/digest/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipients"] == ["alex@example.com"]
    assert "Tasks digest —" in body["subject"]
    assert sent["recipients"] == ["alex@example.com"]


# ==============================================================================
# T7: _next_digest_fire (DST-safe) + _digest_should_fire + digest_state.json dedup
# ==============================================================================
from datetime import datetime, timezone
import app


def test_next_digest_fire_same_day_still_ahead():
    now = datetime(2026, 7, 17, 5, 0, 0, tzinfo=timezone.utc)
    fire = app._next_digest_fire(now, "07:00", "Europe/London")
    # 07:00 BST (UTC+1) on 2026-07-17 = 06:00 UTC, still ahead of 05:00 UTC
    assert fire.isoformat() == "2026-07-17T06:00:00+00:00"


def test_next_digest_fire_rolls_to_next_day_when_passed():
    now = datetime(2026, 7, 17, 8, 0, 0, tzinfo=timezone.utc)   # past 07:00 BST already
    fire = app._next_digest_fire(now, "07:00", "Europe/London")
    assert fire.isoformat() == "2026-07-18T06:00:00+00:00"


def test_next_digest_fire_dst_spring_forward():
    # Last fire was 2026-03-28 07:00 GMT; next fire crosses the 2026-03-29 DST jump.
    now = datetime(2026, 3, 28, 8, 0, 0, tzinfo=timezone.utc)
    fire = app._next_digest_fire(now, "07:00", "Europe/London")
    assert fire.isoformat() == "2026-03-29T06:00:00+00:00"   # 07:00 BST = 06:00 UTC


def test_next_digest_fire_dst_fall_back():
    # Crosses the 2026-10-25 DST fall-back.
    now = datetime(2026, 10, 24, 8, 0, 0, tzinfo=timezone.utc)
    fire = app._next_digest_fire(now, "07:00", "Europe/London")
    assert fire.isoformat() == "2026-10-25T07:00:00+00:00"   # 07:00 GMT = 07:00 UTC


def test_next_digest_fire_malformed_time_falls_back_to_0700():
    now = datetime(2026, 7, 17, 5, 0, 0, tzinfo=timezone.utc)
    fire = app._next_digest_fire(now, "not-a-time", "Europe/London")
    assert fire.isoformat() == "2026-07-17T06:00:00+00:00"


def test_next_digest_fire_out_of_range_time_falls_back_to_0700():
    # Passes the ^\d{2}:\d{2}$ save-endpoint regex but is not a valid time --
    # must not crash the worker; falls back to 07:00 like a malformed string.
    now = datetime(2026, 7, 17, 5, 0, 0, tzinfo=timezone.utc)
    fire = app._next_digest_fire(now, "25:99", "Europe/London")
    assert fire.isoformat() == "2026-07-17T06:00:00+00:00"


def test_next_digest_fire_exact_instant_rolls_to_tomorrow():
    # now == candidate exactly (07:00 BST on 2026-07-17 is 06:00 UTC) -- the "<="
    # (not just "<") must roll to tomorrow: at the exact fire instant the worker is
    # already inside the send block, so the post-send recompute must yield tomorrow,
    # not immediately re-fire the same instant.
    now = datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc)
    fire = app._next_digest_fire(now, "07:00", "Europe/London")
    assert fire.isoformat() == "2026-07-18T06:00:00+00:00"


def test_next_digest_fire_dst_spring_forward_gap():
    # America/New_York: clocks jump 02:00 -> 03:00 on 2026-03-08, so 02:30 that day
    # falls inside the spring-forward gap and doesn't exist as a wall-clock time.
    # now = 05:00 UTC = 00:00 EST (before the gap). Must not raise, and must resolve
    # via fold=0 (pre-transition/EST offset), which for a gap-time shifts the
    # resulting UTC instant forward by the gap size: 02:30 EST(-05:00) -> 07:30 UTC.
    now = datetime(2026, 3, 8, 5, 0, 0, tzinfo=timezone.utc)
    fire = app._next_digest_fire(now, "02:30", "America/New_York")
    assert fire.isoformat() == "2026-03-08T07:30:00+00:00"


# --- _digest_should_fire (the fire-decision the worker delegates to) -----------

def test_digest_should_fire_true_when_enabled_and_not_sent_today():
    digest = {"enabled": True}
    smtp = {"enabled": True}
    state = {"last_sent_date": "2026-07-16"}
    assert app._digest_should_fire(digest, smtp, state, "2026-07-17") is True


def test_digest_should_fire_false_when_digest_disabled():
    assert app._digest_should_fire({"enabled": False}, {"enabled": True}, {}, "2026-07-17") is False


def test_digest_should_fire_false_when_smtp_disabled():
    assert app._digest_should_fire({"enabled": True}, {"enabled": False}, {}, "2026-07-17") is False


def test_digest_should_fire_false_when_already_sent_today():
    digest = {"enabled": True}
    smtp = {"enabled": True}
    state = {"last_sent_date": "2026-07-17"}
    assert app._digest_should_fire(digest, smtp, state, "2026-07-17") is False


# --- digest_state.json dedup ---------------------------------------------------

def test_digest_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    assert app._load_digest_state() == {}
    app._save_digest_state({"last_sent_date": "2026-07-17"})
    assert app._load_digest_state() == {"last_sent_date": "2026-07-17"}
    assert (tmp_path / "digest_state.json").exists()


def test_digest_state_corrupt_file_yields_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    (tmp_path / "digest_state.json").write_text("not json")
    assert app._load_digest_state() == {}


# ==============================================================================
# Fix round 1 (review): bounded send-failure retry -- 3 attempts / ~10 min window
# ==============================================================================
import pytest


async def _fake_build_digest_email():
    return "Tasks digest", "<p>html</p>", "text body"


def test_send_digest_with_retry_succeeds_after_two_failures(monkeypatch):
    """First two attempts raise, third succeeds -- 3 send calls total, 2 sleeps
    (never a sleep after the final/successful attempt), no real waiting."""
    calls = []
    def _fake_send(smtp, recipients, subject, html_body, text_body):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("smtp blip")
    monkeypatch.setattr(app.emailer, "send_email", _fake_send)

    sleeps = []
    async def _fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(app.asyncio, "sleep", _fake_sleep)

    asyncio.run(app._send_digest_with_retry(
        {"host": "smtp.example.com"}, ["a@x.com"], "subj", "<p>x</p>", "x"))
    assert len(calls) == 3
    assert sleeps == [300, 300]


def test_send_digest_with_retry_raises_after_all_attempts_fail(monkeypatch):
    """All 3 attempts fail -- re-raises the final exception so the caller's
    existing except-block handles it; still exactly 3 send calls, 2 sleeps."""
    calls = []
    def _fake_send(smtp, recipients, subject, html_body, text_body):
        calls.append(1)
        raise RuntimeError("smtp down")
    monkeypatch.setattr(app.emailer, "send_email", _fake_send)

    sleeps = []
    async def _fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(app.asyncio, "sleep", _fake_sleep)

    with pytest.raises(RuntimeError, match="smtp down"):
        asyncio.run(app._send_digest_with_retry(
            {"host": "smtp.example.com"}, ["a@x.com"], "subj", "<p>x</p>", "x"))
    assert len(calls) == 3
    assert sleeps == [300, 300]


def test_attempt_digest_send_retries_then_writes_state(tmp_path, monkeypatch, caplog):
    """End-to-end through the worker's actual fire-block body (_attempt_digest_send):
    send fails twice then succeeds -> 3 send calls, state file written as today."""
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "_build_digest_email", _fake_build_digest_email)

    calls = []
    def _fake_send(smtp, recipients, subject, html_body, text_body):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("smtp blip")
    monkeypatch.setattr(app.emailer, "send_email", _fake_send)

    sleeps = []
    async def _fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(app.asyncio, "sleep", _fake_sleep)

    state = {}
    caplog.set_level("INFO")
    asyncio.run(app._attempt_digest_send({"host": "smtp.example.com"}, ["a@x.com"], state, "2026-07-17"))

    assert len(calls) == 3
    assert sleeps == [300, 300]
    assert state["last_sent_date"] == "2026-07-17"
    assert app._load_digest_state() == {"last_sent_date": "2026-07-17"}   # persisted to disk
    assert any("Digest sent to" in r.message for r in caplog.records)


def test_attempt_digest_send_all_attempts_fail_state_not_written(tmp_path, monkeypatch, caplog):
    """All 3 attempts fail -> state left unwritten (both in-memory dict and on
    disk) and the give-up warning is logged; next scheduled fire is tomorrow."""
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "_build_digest_email", _fake_build_digest_email)

    calls = []
    def _fake_send(smtp, recipients, subject, html_body, text_body):
        calls.append(1)
        raise RuntimeError("smtp down")
    monkeypatch.setattr(app.emailer, "send_email", _fake_send)

    sleeps = []
    async def _fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(app.asyncio, "sleep", _fake_sleep)

    state = {}
    caplog.set_level("WARNING")
    asyncio.run(app._attempt_digest_send({"host": "smtp.example.com"}, ["a@x.com"], state, "2026-07-17"))

    assert len(calls) == 3
    assert sleeps == [300, 300]
    assert "last_sent_date" not in state
    assert app._load_digest_state() == {}   # nothing persisted
    assert any("giving up until tomorrow" in r.message for r in caplog.records)
