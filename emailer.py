"""Email rendering + SMTP delivery for meeting summaries.

Stateless helpers (like storage.py): they operate purely on their arguments and
have no dependency on app.py's monkeypatched globals, so app.py can import them
with no import cycle.

`render_summary_email` turns a processed meeting + its summary into an
(subject, html_body, text_body) triple; `send_email` delivers a
multipart/alternative message over SMTP using only the stdlib.

The SMTP semantics mirror the reference implementation in
Cyber_Portal_Revived (nodemailer): port 465 / secure => implicit TLS, otherwise
STARTTLS on 587; auth when a username is set; a `From: "Name" <addr>` header and
optional Reply-To; a plain-text part plus an HTML part.
"""

import html
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

def parse_recipients(value) -> list[str]:
    """Split a comma/semicolon/newline-separated recipient string into addresses."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = re.split(r"[,;\n]+", str(value))
    return [p.strip() for p in parts if p and p.strip()]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _section(title: str, inner_html: str) -> str:
    """Wrap a rendered section body under a heading; returns '' if empty."""
    if not inner_html:
        return ""
    return (
        f'<h2 style="margin:24px 0 8px;font-size:16px;color:#041E42;'
        f'border-bottom:1px solid #e2e8f0;padding-bottom:4px;">{_esc(title)}</h2>'
        f"{inner_html}"
    )


def _participants_html(speaker_info: dict) -> str:
    if not speaker_info:
        return ""
    rows = []
    for info in speaker_info.values():
        if not isinstance(info, dict):
            continue
        name = info.get("display_name") or info.get("name")
        if not name:
            continue
        bits = [f"<strong>{_esc(name)}</strong>"]
        title = info.get("title")
        company = info.get("company")
        detail = ", ".join(x for x in (title, company) if x)
        if detail:
            bits.append(f'<span style="color:#555;"> — {_esc(detail)}</span>')
        rows.append(f'<li style="margin:2px 0;">{"".join(bits)}</li>')
    if not rows:
        return ""
    return f'<ul style="margin:8px 0;padding-left:20px;">{"".join(rows)}</ul>'


def _action_items_html(actions: list) -> str:
    if not actions:
        return ""
    head = (
        '<tr style="background:#f1f5f9;text-align:left;">'
        '<th style="padding:6px 10px;border:1px solid #e2e8f0;">Task</th>'
        '<th style="padding:6px 10px;border:1px solid #e2e8f0;">Owner</th>'
        '<th style="padding:6px 10px;border:1px solid #e2e8f0;">Deadline</th>'
        '<th style="padding:6px 10px;border:1px solid #e2e8f0;">Priority</th></tr>'
    )
    rows = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        task = a.get("task") or a.get("description", "")
        who = a.get("who") or a.get("assigned_to") or "Unassigned"
        deadline = a.get("deadline") or "—"
        priority = a.get("priority", "medium")
        rows.append(
            "<tr>"
            f'<td style="padding:6px 10px;border:1px solid #e2e8f0;">{_esc(task)}</td>'
            f'<td style="padding:6px 10px;border:1px solid #e2e8f0;">{_esc(who)}</td>'
            f'<td style="padding:6px 10px;border:1px solid #e2e8f0;">{_esc(deadline)}</td>'
            f'<td style="padding:6px 10px;border:1px solid #e2e8f0;">{_esc(priority)}</td>'
            "</tr>"
        )
    if not rows:
        return ""
    return (
        '<table style="border-collapse:collapse;width:100%;font-size:14px;margin:8px 0;">'
        f"{head}{''.join(rows)}</table>"
    )


def _bullets_html(items: list, render) -> str:
    lis = []
    for item in items or []:
        text = render(item)
        if text:
            lis.append(f'<li style="margin:3px 0;">{text}</li>')
    if not lis:
        return ""
    return f'<ul style="margin:8px 0;padding-left:20px;">{"".join(lis)}</ul>'


def _html_to_text(markup: str) -> str:
    """Very small HTML -> text fallback (only used when no text_body is supplied)."""
    text = re.sub(r"<(br|/p|/h[1-6]|/tr|/li)[^>]*>", "\n", markup, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_summary_email(
    meeting: dict,
    summary: dict,
    speaker_info: dict | None = None,
    text_body: str | None = None,
    public_url: str | None = None,
) -> tuple[str, str, str]:
    """Render (subject, html_body, text_body) for a processed meeting.

    `text_body` is the plain-text alternative; callers pass the existing
    build_summary_markdown() output. If None, a stripped-HTML fallback is used.
    """
    summary = summary or {}
    meeting = meeting or {}
    speaker_info = speaker_info or {}

    title = summary.get("title") or meeting.get("title") or "Meeting Summary"
    date = meeting.get("date", "Unknown")
    duration = meeting.get("duration_formatted", "")

    subject = f"Meeting summary: {title}"
    if date and date != "Unknown":
        subject += f" ({date})"

    if public_url is None:
        public_url = os.getenv("MEETING_PUBLIC_URL", "http://localhost:8191")
    link = public_url.rstrip("/")

    meta_bits = [f"<strong>Date:</strong> {_esc(date)}"]
    if duration:
        meta_bits.append(f"<strong>Duration:</strong> {_esc(duration)}")

    body_parts = [
        '<div style="background:linear-gradient(135deg,#041E42 0%,#2C5697 100%);'
        'padding:20px 24px;border-radius:8px 8px 0 0;">'
        f'<div style="color:#fff;font-weight:700;font-size:20px;">{_esc(title)}</div>'
        f'<div style="color:#70E2CB;font-size:13px;margin-top:6px;">{" &nbsp;•&nbsp; ".join(meta_bits)}</div>'
        "</div>",
        '<div style="padding:8px 24px 24px;">',
    ]

    overview = summary.get("summary") or summary.get("executive_summary")
    if overview:
        body_parts.append(_section("Overview", f'<p style="margin:8px 0;">{_esc(overview)}</p>'))

    body_parts.append(_section("Participants", _participants_html(speaker_info)))
    body_parts.append(_section("Action Items", _action_items_html(summary.get("action_items", []))))

    body_parts.append(_section("Decisions", _bullets_html(
        summary.get("decisions", []),
        lambda d: f"<strong>{_esc(d.get('decision', ''))}</strong>"
                  + (f" — {_esc(d.get('context'))}" if d.get("context") else "") if isinstance(d, dict) else "",
    )))

    body_parts.append(_section("Open Questions", _bullets_html(
        summary.get("open_questions") or summary.get("questions_raised", []),
        lambda q: (
            f"{_esc(q.get('question', ''))}"
            + (f" <em>(asked by {_esc(q.get('asked_by'))})</em>" if q.get("asked_by") else "")
            + (" [Answered]" if q.get("answered") else " [Open]")
        ) if isinstance(q, dict) else _esc(q),
    )))

    body_parts.append(_section("Concerns & Risks", _bullets_html(
        summary.get("concerns", []),
        lambda c: (
            f"{_esc(c.get('concern', ''))}"
            + (f" <em>(raised by {_esc(c.get('raised_by'))})</em>" if c.get("raised_by") else "")
            + (" [Resolved]" if c.get("resolved") else " [Open]")
            + (f" — {_esc(c.get('notes'))}" if c.get("notes") else "")
        ) if isinstance(c, dict) else _esc(c),
    )))

    body_parts.append(_section("Key Figures & Dates", _bullets_html(
        summary.get("figures", []),
        lambda f: (
            f"<strong>{_esc(f.get('figure', ''))}</strong>"
            + (f": {_esc(f.get('context'))}" if f.get("context") else "")
            + (f" <em>(mentioned by {_esc(f.get('said_by'))})</em>" if f.get("said_by") else "")
        ) if isinstance(f, dict) else _esc(f),
    )))

    body_parts.append(
        '<p style="margin:24px 0 0;padding-top:12px;border-top:1px solid #e2e8f0;font-size:13px;color:#888;">'
        f'View the full meeting: <a href="{_esc(link)}" style="color:#2C5697;">{_esc(link)}</a><br>'
        "Sent automatically by the Meeting Service."
        "</p>"
    )
    body_parts.append("</div>")

    inner = "".join(p for p in body_parts if p)
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;'
        'color:#1a1a1a;font-size:15px;line-height:1.5;border:1px solid #e2e8f0;border-radius:8px;">'
        f"{inner}</div>"
    )

    if text_body is None:
        text_body = _html_to_text(html_body)

    return subject, html_body, text_body


def _digest_task_row_html(t: dict) -> str:
    bits = []
    due = t.get("due")
    if due:
        bits.append(f'<span style="color:#555;">📅 {_esc(due)}</span>')
    prio = t.get("priority")
    if prio:
        bits.append(f'<span style="color:#555;">{_esc(prio)}</span>')
    owner = t.get("owner")
    if owner:
        bits.append(f'<span style="color:#555;">@{_esc(owner)}</span>')
    src = t.get("source_title") or t.get("source") or ""
    if src:
        bits.append(f'<span style="color:#888;">— {_esc(src)}</span>')
    meta = "  ".join(bits)
    return (
        "<tr>"
        f'<td style="padding:4px 8px;border-bottom:1px solid #f1f5f9;">{_esc(t.get("text", ""))}</td>'
        f'<td style="padding:4px 8px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#666;">{meta}</td>'
        "</tr>"
    )


def _digest_lane_html(title: str, tasks: list) -> str:
    if not tasks:
        return ""
    rows = "".join(_digest_task_row_html(t) for t in tasks)
    table = f'<table style="border-collapse:collapse;width:100%;font-size:14px;margin:4px 0 12px;">{rows}</table>'
    return _section(f"{title} ({len(tasks)})", table)


def _digest_text(data: dict, link: str) -> str:
    """Build plain-text digest from data dict (no HTML conversion).

    Renders readable plain text with proper spacing, avoiding the mashed-cell
    problem of _html_to_text. Only used for digest emails.

    data: {"weekday": str, "date": str, "counts": {...}, "lanes": {...}, "briefing": str}
    link: public URL for the footer
    """
    data = data or {}
    counts = data.get("counts") or {}
    lanes = data.get("lanes") or {}
    weekday = data.get("weekday", "")
    date_str = data.get("date", "")
    briefing = (data.get("briefing") or "").strip()

    lines = []

    # Header
    lines.append(f"Tasks digest — {weekday} {date_str}")

    # Counts summary
    count_parts = [
        f"{counts.get('overdue', 0)} overdue",
        f"{counts.get('today', 0)} today",
        f"{counts.get('doing', 0)} doing",
        f"{counts.get('week', 0)} this week",
    ]
    lines.append(" · ".join(count_parts))
    lines.append("")  # blank line

    # Briefing
    if briefing:
        lines.append(briefing)
        lines.append("")  # blank line

    # Lanes
    for lane_key, lane_label in [
        ("overdue", "OVERDUE"),
        ("today", "TODAY"),
        ("doing", "DOING"),
        ("week", "THIS WEEK"),
    ]:
        tasks = lanes.get(lane_key) or []
        if not tasks:
            continue

        lines.append(f"{lane_label} ({len(tasks)})")
        for task in tasks:
            task_text = task.get("text", "")
            chips = []

            due = task.get("due")
            if due:
                chips.append(f"[due {due}]")

            priority = task.get("priority")
            if priority:
                chips.append(f"[{priority}]")

            owner = task.get("owner")
            if owner:
                chips.append(f"[@{owner}]")

            source = task.get("source_title") or task.get("source")

            # Build the line: "- text  [chips] (source)"
            line = f"- {task_text}"
            if chips or source:
                line += "  "
                if chips:
                    line += " ".join(chips)
                if source:
                    if chips:
                        line += " "
                    line += f"({source})"

            lines.append(line)

        lines.append("")  # blank line after lane

    # Footer
    lines.append(f"Open the task board: {link}")
    lines.append("Sent automatically by the Meeting Service.")

    return "\n".join(lines)


def render_digest_email(data: dict, public_url: str | None = None) -> tuple[str, str, str]:
    """Render (subject, html_body, text_body) for the daily task digest.

    `data`: {"weekday": str, "date": "YYYY-MM-DD", "counts": {"overdue","today",
    "doing","week"}, "lanes": {"overdue","today","doing","week": [task,...]},
    "briefing": str}. Each task dict carries text/due/priority/owner/source/
    source_title (the same shape /api/tasks returns).
    """
    data = data or {}
    counts = data.get("counts") or {}
    lanes = data.get("lanes") or {}
    weekday = data.get("weekday", "")
    date_str = data.get("date", "")

    subject = (
        f"Tasks digest — {weekday} {date_str}: "
        f"{counts.get('overdue', 0)} overdue, {counts.get('today', 0)} today"
    )

    if public_url is None:
        public_url = os.getenv("MEETING_PUBLIC_URL", "https://meetings.example.com")
    link = public_url.rstrip("/")

    count_bits = [
        f'<strong>{counts.get("overdue", 0)}</strong> overdue',
        f'<strong>{counts.get("today", 0)}</strong> today',
        f'<strong>{counts.get("doing", 0)}</strong> doing',
        f'<strong>{counts.get("week", 0)}</strong> this week',
    ]

    body_parts = [
        '<div style="background:linear-gradient(135deg,#041E42 0%,#2C5697 100%);'
        'padding:20px 24px;border-radius:8px 8px 0 0;">'
        f'<div style="color:#fff;font-weight:700;font-size:20px;">Tasks digest — {_esc(weekday)} {_esc(date_str)}</div>'
        f'<div style="color:#70E2CB;font-size:13px;margin-top:6px;">{" &nbsp;•&nbsp; ".join(count_bits)}</div>'
        "</div>",
        '<div style="padding:8px 24px 24px;">',
    ]

    briefing = (data.get("briefing") or "").strip()
    if briefing:
        body_parts.append(_section("Today", f'<p style="margin:8px 0;">{_esc(briefing)}</p>'))

    body_parts.append(_digest_lane_html("Overdue", lanes.get("overdue") or []))
    body_parts.append(_digest_lane_html("Today", lanes.get("today") or []))
    body_parts.append(_digest_lane_html("Doing", lanes.get("doing") or []))
    body_parts.append(_digest_lane_html("This Week", lanes.get("week") or []))

    body_parts.append(
        '<p style="margin:24px 0 0;padding-top:12px;border-top:1px solid #e2e8f0;font-size:13px;color:#888;">'
        f'Open the task board: <a href="{_esc(link)}" style="color:#2C5697;">{_esc(link)}</a><br>'
        "Sent automatically by the Meeting Service."
        "</p>"
    )
    body_parts.append("</div>")

    inner = "".join(p for p in body_parts if p)
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;'
        'color:#1a1a1a;font-size:15px;line-height:1.5;border:1px solid #e2e8f0;border-radius:8px;">'
        f"{inner}</div>"
    )
    text_body = _digest_text(data, link)
    return subject, html_body, text_body


# ---------------------------------------------------------------------------
# SMTP delivery
# ---------------------------------------------------------------------------

def _tls_context() -> ssl.SSLContext:
    """Verify certs by default; allow opt-out for self-signed servers."""
    ctx = ssl.create_default_context()
    if os.getenv("EMAIL_TLS_REJECT_UNAUTHORIZED", "").lower() == "false":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def send_email(
    smtp: dict,
    recipients,
    subject: str,
    html_body: str,
    text_body: str,
    timeout: float = 30.0,
) -> None:
    """Send a multipart/alternative email over SMTP. Raises on failure.

    `smtp` keys: host, port, secure, username, password, from_email, from_name, reply_to.
    Mirrors the reference: port 465 (or secure=True) => implicit TLS, else STARTTLS.
    """
    to_addrs = parse_recipients(recipients)
    if not to_addrs:
        raise ValueError("No recipients configured")

    host = (smtp.get("host") or "").strip()
    if not host:
        raise ValueError("SMTP host not configured")
    port = int(smtp.get("port") or 587)
    secure = bool(smtp.get("secure")) or port == 465
    username = (smtp.get("username") or "").strip()
    password = smtp.get("password") or ""
    from_email = (smtp.get("from_email") or username or "").strip()
    if not from_email:
        raise ValueError("From address not configured")
    from_name = smtp.get("from_name") or "Meeting Service"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = ", ".join(to_addrs)
    reply_to = (smtp.get("reply_to") or "").strip()
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text_body or "")
    msg.add_alternative(html_body or "", subtype="html")

    context = _tls_context()
    if secure:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=timeout) as server:
            if username:
                server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.starttls(context=context)
            if username:
                server.login(username, password)
            server.send_message(msg)
