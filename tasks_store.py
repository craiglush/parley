"""Tasks — derived from GFM checkboxes in notes (canonical) plus meeting action items.

A task is a checkbox line (- [ ] / - [x]) inside a note's markdown body, optionally
carrying Obsidian-Tasks-style inline metadata: due (📅 YYYY-MM-DD), priority
(⏫ high / 🔼 medium / 🔽 low), and owner (@name). Meeting action items (from each
meeting's summary.json) are surfaced read-only and can be pushed into a note.
"""
import hashlib
import re
from datetime import date, timedelta

# groups: 1=indent, 2=bullet char, 3=check char, 4=rest-of-line
_CHECKBOX_RE = re.compile(r"^(\s*)([-*+])\s+\[([ xX/])\]\s+(.*)$")
_DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_OWNER_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_-]+)")
_PRIORITY_EMOJI = {"⏫": "high", "🔼": "medium", "🔽": "low"}
_PRIORITY_TO_EMOJI = {"high": "⏫", "medium": "🔼", "low": "🔽"}
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

# Third checkbox state: '[/]' = doing (Obsidian-Tasks community convention).
# `_MARK_TO_STATE` drives parsing; `_STATE_TO_MARK` drives writing. Kept as the
# single source of truth so parse/write always agree on the three-way mapping.
_MARK_TO_STATE = {" ": "open", "x": "done", "X": "done", "/": "doing"}
_STATE_TO_MARK = {"open": " ", "doing": "/", "done": "x"}


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
        state = _MARK_TO_STATE.get(m.group(3), "open")
        text, due, priority, owner = parse_inline_metadata(m.group(4))
        tasks.append({
            "text": text, "done": state == "done", "state": state, "source": "note",
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
        "done": False, "state": "open", "source": "meeting",
        "source_id": meeting_id, "source_title": meeting_title,
        "line": None, "due": deadline, "priority": prio,
        "owner": who or None,
    }


def _date_plus_days(date_str: str, days: int) -> str:
    y, m, d = (int(x) for x in date_str.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def filter_tasks(tasks, *, status=None, owner=None, source=None, due=None, today=None) -> list:
    """Filter tasks. status: open|doing|done. due: overdue|today|week (requires today=YYYY-MM-DD)."""
    out = []
    for t in tasks:
        if status == "open" and t["done"]:
            continue
        if status == "done" and not t["done"]:
            continue
        if status == "doing" and (t.get("state") or ("done" if t.get("done") else "open")) != "doing":
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
    """Open before done; within open, doing before not-doing; then due (none last),
    then priority (high first), then text."""
    def key(t):
        state = t.get("state") or ("done" if t.get("done") else "open")
        return (
            bool(t.get("done")),
            0 if state == "doing" else 1,
            t.get("due") or "9999-99-99",
            _PRIORITY_RANK.get(t.get("priority"), 3),
            (t.get("text") or "").lower(),
        )
    return sorted(tasks, key=key)


def build_digest_snapshot(tasks, today: str) -> dict:
    """Bucket OPEN tasks into the four digest lanes (doing / overdue / today / week) +
    their counts, for the daily digest email. Pure; no I/O. A 'doing' task is pinned
    to the doing lane regardless of due date (mirrors the board's laneForTask); 'later'
    and 'done' tasks are omitted (v1 digest scope, per design -- no per-task timestamps
    exist to show "done yesterday"). `today` = YYYY-MM-DD."""
    lanes = {"doing": [], "overdue": [], "today": [], "week": []}
    for t in tasks:
        if t.get("done"):
            continue
        state = t.get("state") or "open"
        if state == "doing":
            lanes["doing"].append(t)
            continue
        due = t.get("due")
        if not due:
            continue
        if due < today:
            lanes["overdue"].append(t)
        elif due == today:
            lanes["today"].append(t)
        elif due <= _date_plus_days(today, 6):
            lanes["week"].append(t)
    return {"counts": {k: len(v) for k, v in lanes.items()}, "lanes": lanes}


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


def set_state_line(body: str, line_index: int, state: str, expected_text: str = None):
    """Set the checkbox at line_index to `state` ('open'|'doing'|'done'). Returns
    (new_body, ok) -- same contract as toggle_line. Refuses (body, False) if state is
    invalid, the line is not a checkbox, index is out of range, or expected_text
    doesn't match."""
    if state not in _STATE_TO_MARK:
        return body, False
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
    lines[line_index] = f"{m.group(1)}{m.group(2)} [{_STATE_TO_MARK[state]}] {m.group(4)}"
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


def format_task_line(text: str, owner=None, due=None, priority=None, done: bool = False,
                     state: str = None) -> str:
    """Render a GFM checkbox task line from explicit fields (text + optional metadata).
    `state` ('open'|'doing'|'done') takes precedence over the legacy `done` bool when
    given, so every existing done=True/False call site keeps working unchanged."""
    mark = _STATE_TO_MARK.get(state) if state in _STATE_TO_MARK else ("x" if done else " ")
    return f"- [{mark}] " + _task_remainder(text, owner, due, priority)


def update_line(body: str, line_index: int, expected_text, text: str,
                owner=None, due=None, priority=None):
    """Rewrite the checkbox at line_index with new text + metadata, preserving its
    current checkbox mark (open/doing/done), indent and bullet char verbatim -- an
    unrelated text/due/priority edit must never silently clear a 'doing' state.
    Returns (new_body, ok); refuses (body, False) if the line is not a checkbox, the
    index is out of range, or expected_text doesn't match."""
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
    mark = m.group(3)
    remainder = _task_remainder(text, owner, due, priority)
    lines[line_index] = f"{m.group(1)}{m.group(2)} [{mark}] {remainder}"
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
    an edit; an `edited` entry fully replaces text + metadata (empty -> cleared).

    `state` ('open'/'doing'/'done') is derived from the overlay entry: an explicit
    entry["state"] wins when present (both the toggle and the state endpoints always
    write "state" alongside "done", keeping them in lockstep -- see api_meeting_task_
    toggle and the new api_meeting_task_state); a legacy entry that only carries
    "done" (pre-dating this feature) falls back to deriving state from it."""
    if not entry:
        return task
    if entry.get("deleted"):
        return None
    t = dict(task)
    if "done" in entry:
        t["done"] = bool(entry["done"])
    if entry.get("state") in ("open", "doing", "done"):
        t["state"] = entry["state"]
    elif "done" in entry:
        t["state"] = "done" if t["done"] else "open"
    if entry.get("edited"):
        t["text"] = entry.get("text", t.get("text", ""))
        t["owner"] = entry.get("owner") or None
        t["due"] = entry.get("due") or None
        t["priority"] = entry.get("priority") or None
    return t


# --- ICS calendar feed --------------------------------------------------------

def ics_uid(source_id: str, line_or_index) -> str:
    """Stable per-task UID: sha1(source_id|line-or-index)@meetings. Deterministic
    across regenerations so a calendar client updates the same event in place rather
    than duplicating it on every feed refresh."""
    raw = f"{source_id}|{line_or_index}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest() + "@meetings"


def _ics_escape(text) -> str:
    """Escape a TEXT value per RFC5545 3.3.11: backslash first (or the escapes below
    would themselves get re-escaped), then comma/semicolon, then newlines -> literal
    \\n."""
    s = "" if text is None else str(text)
    s = s.replace("\\", "\\\\")
    s = s.replace(",", "\\,").replace(";", "\\;")
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return s


def _ics_fold(line: str) -> str:
    """RFC5545 3.1: fold content lines longer than 75 OCTETS (not chars) with
    CRLF + single space. Folds on UTF-8 byte boundaries so multi-byte chars
    (emoji in task text) are never split mid-sequence. Each physical line
    (between CRLFs) must be ≤ 75 octets: first line up to 75, continuations
    are 1 space + up to 74 octets of content."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts = []
    while raw:
        # First line: up to 75 octets; continuation lines: 74 (+ 1 space in join)
        cut = min(75 if not parts else 74, len(raw))
        # Back up if we're in the middle of a UTF-8 multi-byte sequence
        while cut > 1 and (raw[cut:cut+1] and (raw[cut] & 0xC0) == 0x80):
            cut -= 1
        parts.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
    return ("\r\n ").join(parts)


def render_ics_calendar(tasks: list, *, now_stamp: str) -> str:
    """Render OPEN, due-dated tasks as all-day VEVENTs (RFC5545). VEVENT, never
    VTODO -- Outlook renders VTODO poorly. `now_stamp` is a pre-formatted UTC
    'YYYYMMDDTHHMMSSZ' DTSTAMP, supplied by the caller so this stays pure/
    deterministic for tests. CRLF line endings per the RFC. Long lines (SUMMARY,
    DESCRIPTION) are folded per RFC5545 3.1 (75-octet limit with CRLF+space)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Meeting Service//Tasks//EN",
              "CALSCALE:GREGORIAN"]
    for t in tasks:
        due = t.get("due")
        if t.get("done") or not due:
            continue
        # Overlay-edited meeting tasks can carry an unvalidated due ("next week");
        # a malformed date must skip this event, not 500 the whole feed.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            continue
        prio_emoji = _PRIORITY_TO_EMOJI.get(t.get("priority") or "", "")
        summary = f"{prio_emoji} {t.get('text', '')}".strip()
        line_or_index = t.get("line") if t.get("source") == "note" else t.get("index")
        uid = ics_uid(t.get("source_id") or "", line_or_index)
        # DTEND is exclusive: for an all-day event on due date, DTEND = due + 1
        dtend = _date_plus_days(due, 1)
        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART;VALUE=DATE:{due.replace('-', '')}",
            f"DTEND;VALUE=DATE:{dtend.replace('-', '')}",
            _ics_fold(f"SUMMARY:{_ics_escape(summary)}"),
            _ics_fold(f"DESCRIPTION:{_ics_escape(t.get('source_title') or '')}"),
            "END:VEVENT",
        ]
        lines.extend(event_lines)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
