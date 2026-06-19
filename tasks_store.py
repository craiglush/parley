"""Tasks — derived from GFM checkboxes in notes (canonical) plus meeting action items.

A task is a checkbox line (- [ ] / - [x]) inside a note's markdown body, optionally
carrying Obsidian-Tasks-style inline metadata: due (📅 YYYY-MM-DD), priority
(⏫ high / 🔼 medium / 🔽 low), and owner (@name). Meeting action items (from each
meeting's summary.json) are surfaced read-only and can be pushed into a note.
"""
import re
from datetime import date, timedelta

# groups: 1=indent, 2=bullet char, 3=check char, 4=rest-of-line
_CHECKBOX_RE = re.compile(r"^(\s*)([-*+])\s+\[([ xX])\]\s+(.*)$")
_DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_OWNER_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_-]+)")
_PRIORITY_EMOJI = {"⏫": "high", "🔼": "medium", "🔽": "low"}
_PRIORITY_TO_EMOJI = {"high": "⏫", "medium": "🔼", "low": "🔽"}
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def parse_inline_metadata(text: str):
    """Extract (clean_text, due, priority, owner) from a task's text, stripping markers."""
    due = None
    m = _DUE_RE.search(text)
    if m:
        due = m.group(1)
        text = _DUE_RE.sub("", text)
    priority = None
    for emoji, level in _PRIORITY_EMOJI.items():
        if emoji in text:
            priority = level
            text = text.replace(emoji, "")
            break
    owner = None
    om = _OWNER_RE.search(text)
    if om:
        owner = om.group(1)
        text = _OWNER_RE.sub(" ", text, count=1)
    return re.sub(r"\s+", " ", text).strip(), due, priority, owner


def parse_tasks_from_body(body: str, source_id: str, source_title: str) -> list:
    """Return a task dict for every checkbox line in a note body, with its line index."""
    tasks = []
    for i, line in enumerate((body or "").split("\n")):
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        done = m.group(3).lower() == "x"
        text, due, priority, owner = parse_inline_metadata(m.group(4))
        tasks.append({
            "text": text, "done": done, "source": "note",
            "source_id": source_id, "source_title": source_title,
            "line": i, "due": due, "priority": priority, "owner": owner,
        })
    return tasks


def meeting_action_item_to_task(item: dict, meeting_id: str, meeting_title: str) -> dict:
    """Map a summary.json action_item ({task, who/assigned_to, deadline, priority}) to a task."""
    prio = (item.get("priority") or "").strip().lower()
    if prio not in _PRIORITY_RANK:
        prio = None
    deadline = (item.get("deadline") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", deadline):
        deadline = None
    who = (item.get("who") or item.get("assigned_to") or "").strip()
    return {
        "text": (item.get("task") or "").strip(),
        "done": False, "source": "meeting",
        "source_id": meeting_id, "source_title": meeting_title,
        "line": None, "due": deadline, "priority": prio,
        "owner": who or None,
    }


def _date_plus_days(date_str: str, days: int) -> str:
    y, m, d = (int(x) for x in date_str.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def filter_tasks(tasks, *, status=None, owner=None, source=None, due=None, today=None) -> list:
    """Filter tasks. status: open|done. due: overdue|today|week (requires today=YYYY-MM-DD)."""
    out = []
    for t in tasks:
        if status == "open" and t["done"]:
            continue
        if status == "done" and not t["done"]:
            continue
        if owner is not None and (t.get("owner") or "") != owner:
            continue
        if source is not None and t.get("source") != source:
            continue
        if due and today:
            d = t.get("due")
            if not d:
                continue
            if due == "overdue" and not (d < today):
                continue
            if due == "today" and d != today:
                continue
            if due == "week" and (d < today or d > _date_plus_days(today, 6)):
                continue
        out.append(t)
    return out


def sort_tasks(tasks) -> list:
    """Open before done, then due (none last), then priority (high first), then text."""
    def key(t):
        return (
            bool(t.get("done")),
            t.get("due") or "9999-99-99",
            _PRIORITY_RANK.get(t.get("priority"), 3),
            (t.get("text") or "").lower(),
        )
    return sorted(tasks, key=key)


def toggle_line(body: str, line_index: int, done: bool, expected_text: str = None):
    """Set the checkbox at line_index to `done`. Returns (new_body, ok). Refuses (body, False)
    if the line is not a checkbox, index is out of range, or expected_text doesn't match."""
    lines = (body or "").split("\n")
    if line_index < 0 or line_index >= len(lines):
        return body, False
    m = _CHECKBOX_RE.match(lines[line_index])
    if not m:
        return body, False
    if expected_text is not None:
        cur_text, _, _, _ = parse_inline_metadata(m.group(4))
        if cur_text != expected_text:
            return body, False
    mark = "x" if done else " "
    lines[line_index] = f"{m.group(1)}{m.group(2)} [{mark}] {m.group(4)}"
    return "\n".join(lines), True


def format_action_item_as_checkbox(item: dict) -> str:
    """Render a meeting action_item as a GFM checkbox line with inline metadata."""
    parts = ["- [ ]", (item.get("task") or "").strip()]
    who = (item.get("who") or item.get("assigned_to") or "").strip()
    if who and who.upper() != "UNKNOWN":
        parts.append("@" + re.sub(r"\s+", "-", who))
    deadline = (item.get("deadline") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", deadline):
        parts.append("📅 " + deadline)
    prio = (item.get("priority") or "").strip().lower()
    if prio in _PRIORITY_TO_EMOJI:
        parts.append(_PRIORITY_TO_EMOJI[prio])
    return " ".join(parts)


def _task_remainder(text: str, owner=None, due=None, priority=None) -> str:
    """Build the part after the checkbox: 'text @owner 📅 YYYY-MM-DD <prio-emoji>'."""
    parts = [(text or "").strip()]
    if owner:
        parts.append("@" + re.sub(r"\s+", "-", str(owner).strip().lstrip("@")))
    d = (due or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        parts.append("📅 " + d)
    prio = (priority or "").strip().lower()
    if prio in _PRIORITY_TO_EMOJI:
        parts.append(_PRIORITY_TO_EMOJI[prio])
    return " ".join(p for p in parts if p)


def format_task_line(text: str, owner=None, due=None, priority=None, done: bool = False) -> str:
    """Render a GFM checkbox task line from explicit fields (text + optional metadata)."""
    return f"- [{'x' if done else ' '}] " + _task_remainder(text, owner, due, priority)


def update_line(body: str, line_index: int, expected_text, text: str,
                owner=None, due=None, priority=None):
    """Rewrite the checkbox at line_index with new text + metadata, preserving its done
    state, indent and bullet char. Returns (new_body, ok); refuses (body, False) if the
    line is not a checkbox, the index is out of range, or expected_text doesn't match."""
    lines = (body or "").split("\n")
    if line_index < 0 or line_index >= len(lines):
        return body, False
    m = _CHECKBOX_RE.match(lines[line_index])
    if not m:
        return body, False
    if expected_text is not None:
        cur_text, _, _, _ = parse_inline_metadata(m.group(4))
        if cur_text != expected_text:
            return body, False
    done = m.group(3).lower() == "x"
    remainder = _task_remainder(text, owner, due, priority)
    lines[line_index] = f"{m.group(1)}{m.group(2)} [{'x' if done else ' '}] {remainder}"
    return "\n".join(lines), True


def delete_line(body: str, line_index: int, expected_text=None):
    """Remove the checkbox line at line_index. Returns (new_body, ok); refuses (body, False)
    if the line is not a checkbox, index is out of range, or expected_text doesn't match."""
    lines = (body or "").split("\n")
    if line_index < 0 or line_index >= len(lines):
        return body, False
    m = _CHECKBOX_RE.match(lines[line_index])
    if not m:
        return body, False
    if expected_text is not None:
        cur_text, _, _, _ = parse_inline_metadata(m.group(4))
        if cur_text != expected_text:
            return body, False
    del lines[line_index]
    return "\n".join(lines), True


def apply_meeting_overlay(task: dict, entry: dict | None) -> dict | None:
    """Apply a saved per-meeting overlay entry to a meeting task (in-place edit/complete
    state for AI-derived action items, which have no note to live in). Returns the
    updated task, or None if the entry dismisses (deletes) it. `done` is independent of
    an edit; an `edited` entry fully replaces text + metadata (empty -> cleared)."""
    if not entry:
        return task
    if entry.get("deleted"):
        return None
    t = dict(task)
    if "done" in entry:
        t["done"] = bool(entry["done"])
    if entry.get("edited"):
        t["text"] = entry.get("text", t.get("text", ""))
        t["owner"] = entry.get("owner") or None
        t["due"] = entry.get("due") or None
        t["priority"] = entry.get("priority") or None
    return t
