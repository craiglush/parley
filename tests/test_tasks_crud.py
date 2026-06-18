"""Unit tests for the task-line CRUD helpers in tasks_store."""
import tasks_store as ts


def test_format_task_line_plain():
    assert ts.format_task_line("do it") == "- [ ] do it"


def test_format_task_line_with_metadata():
    assert ts.format_task_line("ship", owner="alex", due="2026-07-01", priority="high") \
        == "- [ ] ship @alex 📅 2026-07-01 ⏫"


def test_format_task_line_done_mark():
    assert ts.format_task_line("x", done=True).startswith("- [x] ")


def test_format_task_line_owner_normalized():
    # leading @ stripped, spaces hyphenated so the @owner token re-parses
    assert "@two-words" in ts.format_task_line("t", owner="@two words")


def test_update_line_changes_text_and_meta_preserving_done():
    body = "- [x] old @bob 📅 2026-01-01 ⏫"
    new, ok = ts.update_line(body, 0, "old", "new", owner="amy", due="2026-02-02", priority="low")
    assert ok and new == "- [x] new @amy 📅 2026-02-02 🔽"


def test_update_line_clears_metadata_when_omitted():
    body = "- [ ] task @bob 📅 2026-01-01 ⏫"
    new, ok = ts.update_line(body, 0, "task", "task")
    assert ok and new == "- [ ] task"


def test_update_line_expected_mismatch_refuses():
    body = "- [ ] hello"
    assert ts.update_line(body, 0, "WRONG", "x") == (body, False)


def test_update_line_non_checkbox_refuses():
    assert ts.update_line("just a line", 0, None, "x") == ("just a line", False)


def test_update_line_out_of_range_refuses():
    assert ts.update_line("- [ ] a", 5, None, "x") == ("- [ ] a", False)


def test_delete_line_removes_only_that_line():
    body = "- [ ] a\n- [ ] b\n- [ ] c"
    new, ok = ts.delete_line(body, 1, "b")
    assert ok and new == "- [ ] a\n- [ ] c"


def test_delete_line_mismatch_refuses():
    body = "- [ ] a\n- [ ] b"
    assert ts.delete_line(body, 0, "WRONG") == (body, False)


def test_format_then_parse_roundtrip():
    body = ts.format_task_line("write report", owner="alex", due="2026-09-09", priority="medium")
    t = ts.parse_tasks_from_body(body, "n1", "Note")[0]
    assert (t["text"], t["owner"], t["due"], t["priority"]) == ("write report", "alex", "2026-09-09", "medium")
