"""a360 (Sales Intel) push adapter — the loose-coupling seam.

Owns its own env config and NEVER imports app (no import cycle). a360 is
pre-alpha with no ingest endpoint yet, so A360_URL is the FULL webhook URL
(not a base URL + hardcoded path): when a360 grows an ingest route we point
an env var at it and change no code. No retry queue, no delivery guarantees —
the bearer-guarded pull API is the reconciliation path; a missed push costs
one warning line."""

import logging
import os

import httpx

logger = logging.getLogger("meeting-service.a360")

A360_URL = os.getenv("A360_URL", "")      # FULL webhook URL, e.g.
                                          # http://host.docker.internal:8012/api/ingest/meetings
A360_TOKEN = os.getenv("A360_TOKEN", "")


def enabled() -> bool:
    return bool(A360_URL and A360_TOKEN)


def _attendees(speaker_info: dict) -> list:
    """speaker_info -> attendee list, minus empty and SPEAKER_* placeholder
    names — the same filter app.list_people applies, duplicated here (three
    lines) so this module never imports app."""
    out = []
    for entry in (speaker_info or {}).values():
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name or name.upper().startswith("SPEAKER_"):
            continue
        out.append({"name": name,
                    "company": (entry.get("company") or "").strip(),
                    "title": (entry.get("title") or "").strip()})
    return out


def build_payload(meeting: dict) -> dict:
    """meeting.v1 push payload — schema defined by US and versioned so a360
    can evolve independently (a meeting.v2 can ship alongside). Whitelisted
    keys only, never the whole meeting dict. summary/action_items come from
    meeting["summary"], which process_meeting sets in memory at completion —
    this function reads no disk. company/company_suggestion arrive pre-gated
    by the caller (app._a360_completion_payload): never both non-null."""
    summary = meeting.get("summary") or {}
    return {
        "schema": "meeting.v1",
        "id": meeting.get("id"),
        "date": meeting.get("date"),
        "title": meeting.get("title"),
        "duration_formatted": meeting.get("duration_formatted"),
        "company": meeting.get("company"),
        "company_suggestion": meeting.get("company_suggestion"),
        "summary": summary.get("summary"),
        "action_items": summary.get("action_items") or [],
        "attendees": _attendees(meeting.get("speaker_info") or {}),
    }


def post_meeting_completed(meeting: dict) -> None:
    """Blocking; run via app._run_bg. Guarded no-op unless enabled().
    Never raises: single attempt, 10 s timeout, blanket except -> warning.
    A crashed/absent a360 costs one warning line per meeting, nothing else."""
    if not enabled():
        return
    try:
        resp = httpx.post(
            A360_URL,
            json=build_payload(meeting),
            headers={"Authorization": f"Bearer {A360_TOKEN}"},
            timeout=10.0,
        )
        resp.raise_for_status()   # a 4xx/5xx from a360 is a warning, not silence
    except Exception as e:
        logger.warning(f"a360 push failed for meeting {meeting.get('id')} (non-fatal): {e}")
