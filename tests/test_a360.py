"""a360 seam: bearer-guarded pull API + push hook (spec
docs/superpowers/specs/2026-07-15-company-tag-a360-design.md).

Guard tests monkeypatch app.A360_API_TOKEN (the guard reads the module global
at call time). This is the FIRST in-app auth in this service — port 8191 is
LAN-reachable with no Authelia — so the secure-default and non-ASCII cases are
load-bearing, not paranoia."""

import asyncio
import json
import threading

import pytest

from tests.test_meeting_routes import _client, _seed_complete

ENDPOINTS = ["/api/companies", "/api/companies/meetings?name=Acme"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------- bearer guard

@pytest.mark.parametrize("path", ENDPOINTS)
def test_guard_unset_token_is_401_even_with_header(tmp_path, monkeypatch, path):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "A360_API_TOKEN", "")      # secure default: disabled
    r = client.get(path, headers=_bearer("anything"))
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("headers", [
    {},                                                  # missing header
    {"Authorization": "Basic abc"},                      # wrong scheme
    _bearer("wrong-token"),                              # wrong token
    # non-ASCII token (bytes: httpx rejects non-ASCII str header values).
    # Regression guard: str compare_digest would raise TypeError -> 500.
    {"Authorization": "Bearer sécret".encode("utf-8")},
])
def test_guard_rejects_bad_credentials_with_401_not_500(tmp_path, monkeypatch, headers):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "A360_API_TOKEN", "secret")
    assert client.get("/api/companies", headers=headers).status_code == 401


def test_guard_accepts_correct_token_and_is_scoped(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "A360_API_TOKEN", "secret")
    assert client.get("/api/companies", headers=_bearer("secret")).status_code == 200
    # Existing surface stays Authelia-at-proxy only — no token, still 200.
    assert client.get("/meetings").status_code == 200
    assert client.get("/people").status_code == 200


# --------------------------------------------------------------- GET /api/companies

def test_companies_counts_dates_and_case_collapse(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "A360_API_TOKEN", "secret")
    _seed_complete(app, tmp_path, mid="m1", date="2026-07-01", company="ACME")
    _seed_complete(app, tmp_path, mid="m2", date="2026-07-10", company="Acme")
    _seed_complete(app, tmp_path, mid="m3", date="2026-07-05", company="Initech")
    _seed_complete(app, tmp_path, mid="m4")               # unconfirmed: never listed
    rows = client.get("/api/companies", headers=_bearer("secret")).json()
    assert rows == [
        {"company": "ACME", "meeting_count": 2, "last_meeting_date": "2026-07-10"},
        {"company": "Initech", "meeting_count": 1, "last_meeting_date": "2026-07-05"},
    ]


# ------------------------------------------------- GET /api/companies/meetings

def test_company_meetings_shape_and_filters(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "A360_API_TOKEN", "secret")
    rec, out = _seed_complete(app, tmp_path, mid="m1", date="2026-07-10", company="Acme",
                              speaker_info={
                                  "SPEAKER_00": {"name": "Sarah", "company": "Acme", "title": "PM"},
                                  "SPEAKER_01": {"name": "SPEAKER_01", "company": "", "title": ""},
                                  "SPEAKER_02": {"name": "", "company": "X", "title": ""},
                              })
    (out / "summary.json").write_text(json.dumps(
        {"summary": "Quarterly sync.", "action_items": [{"task": "Send deck", "who": "Sarah"}]}))
    # confirmed for Acme but NOT complete -> excluded (no summary to pull)
    app.meetings["m2"] = {"id": "m2", "date": "2026-07-11", "title": "Errored",
                          "status": app.MeetingStatus.error, "company": "Acme"}
    # complete but missing summary.json -> nulled fields, still listed
    _seed_complete(app, tmp_path, mid="m3", date="2026-07-12", company="Acme",
                   title="No summary yet")

    items = client.get("/api/companies/meetings", params={"name": "acme"},
                       headers=_bearer("secret")).json()
    assert [m["id"] for m in items] == ["m3", "m1"]        # date desc
    assert items[1] == {"id": "m1", "date": "2026-07-10", "title": "Weekly Sync",
                        "duration_formatted": "00:10:00", "company": "Acme",
                        "summary": "Quarterly sync.",
                        "action_items": [{"task": "Send deck", "who": "Sarah"}],
                        "attendees": [{"name": "Sarah", "company": "Acme", "title": "PM"}]}
    assert items[0]["summary"] is None and items[0]["action_items"] == []
    # unknown company -> [] (typo vs no-confirmed-meetings is indistinguishable)
    assert client.get("/api/companies/meetings", params={"name": "Nobody"},
                      headers=_bearer("secret")).json() == []
    # missing name -> FastAPI's standard 422
    assert client.get("/api/companies/meetings", headers=_bearer("secret")).status_code == 422


def test_company_meetings_slash_names_round_trip(tmp_path, monkeypatch):
    # Company names can contain '/' ("TBC Bank / JSC") — the reason `name` is a
    # query param, not a path segment. Must be BOTH enumerable and fetchable.
    client, app, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "A360_API_TOKEN", "secret")
    _seed_complete(app, tmp_path, mid="m1", company="TBC Bank / JSC")
    rows = client.get("/api/companies", headers=_bearer("secret")).json()
    assert rows[0]["company"] == "TBC Bank / JSC"
    items = client.get("/api/companies/meetings", params={"name": "TBC Bank / JSC"},
                       headers=_bearer("secret")).json()
    assert [m["id"] for m in items] == ["m1"]


# --------------------------------------------------------------- push hook

def _configure_push(monkeypatch, a360,
                    url="http://host.docker.internal:8012/api/ingest/meetings"):
    monkeypatch.setattr(a360, "A360_URL", url)
    monkeypatch.setattr(a360, "A360_TOKEN", "push-tok")
    return url


def test_hook_is_noop_when_unconfigured(monkeypatch):
    from integrations import a360
    calls = []
    monkeypatch.setattr(a360, "A360_URL", "")
    monkeypatch.setattr(a360, "A360_TOKEN", "")
    monkeypatch.setattr(a360.httpx, "post", lambda *a, **k: calls.append(1))
    a360.post_meeting_completed({"id": "m1", "summary": {}})
    assert calls == []


def test_hook_posts_meeting_v1_payload(monkeypatch):
    from integrations import a360
    calls = []

    def record(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        class R:
            status_code = 200
            def raise_for_status(self):
                pass
        return R()

    url = _configure_push(monkeypatch, a360)
    monkeypatch.setattr(a360.httpx, "post", record)
    a360.post_meeting_completed({
        "id": "m1", "date": "2026-07-10", "title": "Sync",
        "duration_formatted": "45:00", "company": None, "company_suggestion": "Acme",
        "summary": {"summary": "text", "action_items": [{"task": "Do"}]},
        "speaker_info": {"SPEAKER_00": {"name": "Sarah", "company": "Acme", "title": "PM"},
                         "SPEAKER_01": {"name": "SPEAKER_01", "company": "", "title": ""}},
        "transcript_text": "never pushed",
    })
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == url
    assert call["headers"] == {"Authorization": "Bearer push-tok"}
    assert call["timeout"] == 10.0
    p = call["json"]
    assert p["schema"] == "meeting.v1"
    assert p["company"] is None and p["company_suggestion"] == "Acme"
    assert p["summary"] == "text"
    assert p["action_items"] == [{"task": "Do"}]
    assert p["attendees"] == [{"name": "Sarah", "company": "Acme", "title": "PM"}]
    assert "speaker_info" not in p and "transcript_text" not in p   # whitelist, not passthrough


def test_completion_payload_gates_suggestion():
    # The dict process_meeting hands the hook: suggestion ONLY when no
    # confirmed company (same gate as the status endpoint) — never both.
    # Matters for retry/trim/adopt re-completions of a confirmed meeting.
    import app
    confirmed = {"id": "m1", "company": "Initech", "_task": object(),
                 "speaker_info": {"S0": {"name": "Sarah", "company": "Acme", "title": ""}}}
    p = app._a360_completion_payload(confirmed)
    assert p["company"] == "Initech" and p["company_suggestion"] is None
    assert "_task" not in p
    unconfirmed = {"id": "m2",
                   "speaker_info": {"S0": {"name": "Sarah", "company": "Acme", "title": ""}}}
    p2 = app._a360_completion_payload(unconfirmed)
    assert p2.get("company") is None and p2["company_suggestion"] == "Acme"


def test_hook_swallows_transport_errors(monkeypatch):
    from integrations import a360
    _configure_push(monkeypatch, a360)

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(a360.httpx, "post", boom)
    a360.post_meeting_completed({"id": "m1", "summary": {}})   # must not raise


def test_payload_build_inside_closure_swallows_raise(tmp_path, monkeypatch):
    """Payload raise inside _run_bg closure never flips meeting to error.

    Regression: eager `_a360_completion_payload(meeting)` on event loop could
    raise and escape to process_meeting's outer except, flipping completed→error.
    With closure pattern, payload building happens inside executor thread where
    exceptions never bubble to the pipeline (exception stored in discarded Future).

    Uses asyncio.run (no set_event_loop — a prior version leaked a global loop
    across the suite) and a threading.Event set by the monkeypatched payload
    builder itself, awaited off the loop via asyncio.to_thread, so the
    post-run assertions deterministically observe the executor thread's work
    instead of racing it.
    """
    import app
    from integrations import a360

    client, app_obj, _ = _client(tmp_path, monkeypatch)
    _configure_push(monkeypatch, a360)

    # Seed a complete meeting
    rec, out = _seed_complete(app_obj, tmp_path, mid="m1")
    # Verify initial status is complete
    assert app_obj.meetings["m1"]["status"] == app_obj.MeetingStatus.complete

    # Monkeypatch payload builder to raise (simulating malformed data)
    call_count = [0]
    done = threading.Event()

    def boom_payload(meeting):
        call_count[0] += 1
        done.set()   # signal completion before raising, so the wait below is deterministic
        raise ValueError("Malformed speaker_info")

    monkeypatch.setattr(app, "_a360_completion_payload", boom_payload)

    async def run():
        # Mirrors the production _a360_push closure pattern (app.py's
        # process_meeting), deliberately WITHOUT a try/except: the guarantee
        # under test is that _run_bg's executor never lets the closure's
        # exception propagate to the caller/event loop on its own.
        def _a360_push(m=rec):
            a360.post_meeting_completed(app._a360_completion_payload(m))

        app._run_bg(_a360_push)
        # If exception propagated synchronously, the test would fail here
        # (unreachable — proves _run_bg returned before the closure raised).

        # Drain barrier: block off the event loop until the executor thread
        # has actually run the closure, without relying on executor ordering.
        await asyncio.to_thread(done.wait, 5)

    asyncio.run(run())

    assert done.is_set()  # closure actually ran (not a false-positive race)
    # Meeting status must remain complete (not flipped to error)
    assert app_obj.meetings["m1"]["status"] == app_obj.MeetingStatus.complete
    assert call_count[0] == 1  # payload builder was called once
