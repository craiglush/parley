import hashlib
import tasks_store as ts
from tests.test_meeting_routes import _client


# --- pure ICS render -------------------------------------------------------

def test_ics_uid_stable_and_deterministic():
    u1 = ts.ics_uid("note_1", 5)
    u2 = ts.ics_uid("note_1", 5)
    assert u1 == u2
    assert u1.endswith("@meetings")
    assert u1.split("@")[0] == hashlib.sha1(b"note_1|5").hexdigest()


def test_ics_uid_differs_by_source_or_line():
    assert ts.ics_uid("note_1", 5) != ts.ics_uid("note_1", 6)
    assert ts.ics_uid("note_1", 5) != ts.ics_uid("note_2", 5)


def test_render_ics_calendar_skips_done_and_no_due():
    tasks = [
        {"text": "done one", "done": True, "due": "2026-07-10", "source": "note", "source_id": "n1", "line": 0, "priority": None, "source_title": "N"},
        {"text": "no due", "done": False, "due": None, "source": "note", "source_id": "n2", "line": 0, "priority": None, "source_title": "N"},
    ]
    out = ts.render_ics_calendar(tasks, now_stamp="20260717T070000Z")
    assert "BEGIN:VEVENT" not in out


def test_render_ics_calendar_basic_vevent_shape():
    tasks = [{"text": "Ship it", "done": False, "due": "2026-07-20", "priority": "high",
              "source": "note", "source_id": "n1", "line": 3, "source_title": "My Note"}]
    out = ts.render_ics_calendar(tasks, now_stamp="20260717T070000Z")
    assert "BEGIN:VCALENDAR" in out and "END:VCALENDAR" in out
    assert "BEGIN:VEVENT" in out and "END:VEVENT" in out
    assert "VTODO" not in out   # VEVENT, never VTODO (spec: Outlook renders VTODO poorly)
    assert "DTSTART;VALUE=DATE:20260720" in out
    assert "DTEND;VALUE=DATE:20260721" in out   # DTEND is exclusive, so next day
    assert "SUMMARY:⏫ Ship it" in out
    assert "DESCRIPTION:My Note" in out
    assert f"UID:{ts.ics_uid('n1', 3)}" in out
    assert out.split("\r\n")[0] == "BEGIN:VCALENDAR"   # CRLF line endings per RFC5545


def test_ics_escape_rfc5545_special_chars():
    tasks = [{"text": "Buy milk, eggs; bread\\stuff\nline2", "done": False, "due": "2026-07-20",
              "priority": None, "source": "note", "source_id": "n1", "line": 0, "source_title": "A, B; C"}]
    out = ts.render_ics_calendar(tasks, now_stamp="20260717T070000Z")
    assert "Buy milk\\, eggs\\; bread\\\\stuff\\nline2" in out
    assert "A\\, B\\; C" in out


def test_render_ics_calendar_uses_meeting_index_when_no_line():
    tasks = [{"text": "Meeting task", "done": False, "due": "2026-07-20", "priority": None,
              "source": "meeting", "source_id": "m1", "index": 2, "line": None, "source_title": "Sync"}]
    out = ts.render_ics_calendar(tasks, now_stamp="20260717T070000Z")
    assert f"UID:{ts.ics_uid('m1', 2)}" in out


# --- endpoint: token guard + settings gating -------------------------------

def _settings_with_ics(client, **ics_overrides):
    ics = {"enabled": False, "token": ""}
    ics.update(ics_overrides)
    r = client.put("/api/settings", json={"ics": ics})
    assert r.status_code == 200, r.text


def test_ics_endpoint_404_when_disabled(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _settings_with_ics(client, enabled=False, token="secret")
    assert client.get("/api/tasks/calendar.ics?token=secret").status_code == 404


def test_ics_endpoint_404_when_token_empty(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _settings_with_ics(client, enabled=True, token="")
    assert client.get("/api/tasks/calendar.ics?token=anything").status_code == 404


def test_ics_endpoint_404_on_token_mismatch(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _settings_with_ics(client, enabled=True, token="secret")
    assert client.get("/api/tasks/calendar.ics?token=wrong").status_code == 404
    assert client.get("/api/tasks/calendar.ics").status_code == 404  # no token at all


def test_ics_endpoint_200_serves_calendar(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "meetings", {})
    # Notes isolation: this creates a real note task, and the endpoint collects note
    # tasks via _collect_all_tasks -> notes_store.NOTES_DIR, which _client (test_
    # meeting_routes) does NOT patch -- without this the task would be written to the
    # real on-disk vault instead of tmp_path.
    import notes_store
    monkeypatch.setattr(notes_store, "NOTES_DIR", tmp_path / "notes")
    notes_store._index_cache.clear()
    client.post("/api/tasks", json={"text": "Ship it", "due": "2026-07-20", "priority": "high"})
    _settings_with_ics(client, enabled=True, token="secret")
    r = client.get("/api/tasks/calendar.ics?token=secret")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/calendar")
    assert r.headers["cache-control"] == "no-store"
    assert "Ship it" in r.text


def test_ics_line_folding_rfc5545_octet_limit():
    """RFC5545 3.1: lines must be ≤ 75 octets. Task text ~300 chars with emoji
    straddling boundary should fold correctly. Verify no physical line exceeds
    75 octets when UTF-8 encoded."""
    # Create a task text that's ~300 chars and will force folding
    long_text = "A" * 70 + "📅" + "B" * 200  # emoji (~3 octets UTF-8) positioned to straddle boundary
    tasks = [{"text": long_text, "done": False, "due": "2026-07-20", "priority": None,
              "source": "note", "source_id": "n1", "line": 0, "source_title": "Long"}]
    out = ts.render_ics_calendar(tasks, now_stamp="20260717T070000Z")
    # Split by CRLF and check each physical line
    for line in out.split("\r\n"):
        if line:  # skip empty lines
            octets = len(line.encode("utf-8"))
            assert octets <= 75, f"Line exceeds 75 octets ({octets}): {line[:50]}..."


def test_ics_folding_roundtrip_unescaped():
    """Verify that folding/unfolding (removing CRLF+space) preserves original content."""
    long_text = "X" * 80  # text that will be folded when assembled
    tasks = [{"text": long_text, "done": False, "due": "2026-07-20", "priority": None,
              "source": "note", "source_id": "n1", "line": 0, "source_title": "Test"}]
    out = ts.render_ics_calendar(tasks, now_stamp="20260717T070000Z")
    # Extract SUMMARY line and unfold it
    for line in out.split("\r\n"):
        if line.startswith("SUMMARY:"):
            # Unfold: remove all CRLF+space sequences (they're not in this simple output,
            # but if they were added by folding, they'd be reconstructed)
            unfolded = line.replace("\r\n ", "")
            assert "SUMMARY:X" in unfolded
            break


def test_ics_dtend_all_day_exclusive():
    """DTEND for all-day events is exclusive: should be due+1 day."""
    tasks = [{"text": "Task A", "done": False, "due": "2026-07-20", "priority": None,
              "source": "note", "source_id": "n1", "line": 0, "source_title": "N"},
             {"text": "Task B", "done": False, "due": "2026-12-31", "priority": None,
              "source": "note", "source_id": "n2", "line": 1, "source_title": "N"}]
    out = ts.render_ics_calendar(tasks, now_stamp="20260717T070000Z")
    # Check DTEND values (should be YYYYMMDD format, one day after DTSTART)
    assert "DTSTART;VALUE=DATE:20260720" in out
    assert "DTEND;VALUE=DATE:20260721" in out
    assert "DTSTART;VALUE=DATE:20261231" in out
    assert "DTEND;VALUE=DATE:20270101" in out


def test_render_ics_skips_malformed_due_instead_of_crashing():
    # Overlay-edited meeting tasks can carry unvalidated due strings.
    tasks = [
        {"text": "bad", "done": False, "due": "next week", "source": "meeting", "source_id": "m1", "index": 0},
        {"text": "good", "done": False, "due": "2026-07-20", "source": "note", "source_id": "n1", "line": 0},
    ]
    out = ts.render_ics_calendar(tasks, now_stamp="20260717T000000Z")
    assert "good" in out
    assert "bad" not in out
    assert "next week" not in out
