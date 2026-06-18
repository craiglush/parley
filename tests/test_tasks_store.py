import tasks_store


def test_parse_inline_metadata_full():
    text, due, prio, owner = tasks_store.parse_inline_metadata(
        "Ship the report @alex 📅 2026-06-20 ⏫")
    assert text == "Ship the report"
    assert due == "2026-06-20"
    assert prio == "high"
    assert owner == "alex"


def test_parse_inline_metadata_none():
    text, due, prio, owner = tasks_store.parse_inline_metadata("just a plain task")
    assert text == "just a plain task"
    assert due is None and prio is None and owner is None


def test_priority_levels():
    assert tasks_store.parse_inline_metadata("a 🔼")[2] == "medium"
    assert tasks_store.parse_inline_metadata("b 🔽")[2] == "low"


def test_parse_tasks_from_body():
    body = "# Notes\n- [ ] open one @amy 📅 2026-07-01\nsome text\n- [x] done two ⏫\n* [ ] bullet star"
    tasks = tasks_store.parse_tasks_from_body(body, "n_1", "My Note")
    assert len(tasks) == 3
    t0 = tasks[0]
    assert t0["text"] == "open one" and t0["done"] is False
    assert t0["source"] == "note" and t0["source_id"] == "n_1" and t0["source_title"] == "My Note"
    assert t0["line"] == 1 and t0["owner"] == "amy" and t0["due"] == "2026-07-01"
    assert tasks[1]["done"] is True and tasks[1]["priority"] == "high" and tasks[1]["line"] == 3
    assert tasks[2]["text"] == "bullet star" and tasks[2]["line"] == 4


def test_parse_tasks_empty():
    assert tasks_store.parse_tasks_from_body("no tasks here", "n_1", "x") == []


def test_meeting_action_item_to_task():
    t = tasks_store.meeting_action_item_to_task(
        {"task": "Email the deck", "who": "Sam", "deadline": "2026-06-20", "priority": "High"},
        "20260618_sync", "Sync")
    assert t["text"] == "Email the deck" and t["done"] is False
    assert t["source"] == "meeting" and t["source_id"] == "20260618_sync"
    assert t["owner"] == "Sam" and t["due"] == "2026-06-20" and t["priority"] == "high"
    assert t["line"] is None
    # junk deadline/priority -> None
    t2 = tasks_store.meeting_action_item_to_task({"task": "x", "deadline": "soon", "priority": "??"}, "m", "M")
    assert t2["due"] is None and t2["priority"] is None


def test_filter_tasks():
    tasks = [
        {"text": "a", "done": False, "source": "note", "owner": "amy", "due": "2026-06-10", "priority": "high"},
        {"text": "b", "done": True, "source": "note", "owner": "bob", "due": None, "priority": None},
        {"text": "c", "done": False, "source": "meeting", "owner": "amy", "due": "2026-06-18", "priority": "low"},
    ]
    assert len(tasks_store.filter_tasks(tasks, status="open")) == 2
    assert len(tasks_store.filter_tasks(tasks, status="done")) == 1
    assert len(tasks_store.filter_tasks(tasks, owner="amy")) == 2
    assert len(tasks_store.filter_tasks(tasks, source="meeting")) == 1
    assert len(tasks_store.filter_tasks(tasks, due="overdue", today="2026-06-18")) == 1  # only a (2026-06-10)
    assert len(tasks_store.filter_tasks(tasks, due="today", today="2026-06-18")) == 1   # only c


def test_sort_tasks():
    tasks = [
        {"text": "done", "done": True, "due": None, "priority": None},
        {"text": "later", "done": False, "due": "2026-07-01", "priority": "low"},
        {"text": "soon-hi", "done": False, "due": "2026-06-19", "priority": "high"},
    ]
    out = tasks_store.sort_tasks(tasks)
    assert [t["text"] for t in out] == ["soon-hi", "later", "done"]


def test_toggle_line():
    body = "intro\n- [ ] do it @amy 📅 2026-07-01\noutro"
    new, ok = tasks_store.toggle_line(body, 1, True)
    assert ok and new.split("\n")[1] == "- [x] do it @amy 📅 2026-07-01"
    back, ok2 = tasks_store.toggle_line(new, 1, False)
    assert ok2 and back.split("\n")[1].startswith("- [ ]")


def test_toggle_line_guards():
    body = "- [ ] task one\nplain line"
    assert tasks_store.toggle_line(body, 1, True)[1] is False   # not a checkbox
    assert tasks_store.toggle_line(body, 9, True)[1] is False   # out of range
    assert tasks_store.toggle_line(body, 0, True, expected_text="different")[1] is False  # mismatch
    assert tasks_store.toggle_line(body, 0, True, expected_text="task one")[1] is True


def test_format_action_item_as_checkbox():
    s = tasks_store.format_action_item_as_checkbox(
        {"task": "Send notes", "who": "Amy Lee", "deadline": "2026-06-20", "priority": "high"})
    assert s.startswith("- [ ] Send notes")
    assert "@Amy-Lee" in s and "📅 2026-06-20" in s and "⏫" in s
    # UNKNOWN owner omitted
    s2 = tasks_store.format_action_item_as_checkbox({"task": "x", "who": "UNKNOWN"})
    assert "@" not in s2
