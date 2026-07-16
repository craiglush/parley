"""Company tag: deterministic suggest_company + lazy status surface + PATCH/list
plumbing (spec: docs/superpowers/specs/2026-07-15-company-tag-a360-design.md).

House pattern: bare TestClient via tests.test_meeting_routes._client — startup
events never fire; MEETINGS_DIR / meetings / qdrant / embedder monkeypatched.
suggest_company itself is pure (no client needed)."""

import json
import pytest

from tests.test_meeting_routes import _client, _seed_complete


def _mk(speakers=None, companies=None):
    """Minimal meeting dict: speakers = [(name, company), ...] -> speaker_info;
    companies -> tags.entities.companies."""
    m = {"id": "m1", "date": "2026-07-10"}
    if speakers is not None:
        m["speaker_info"] = {
            f"SPEAKER_{i:02d}": {"name": n, "company": c, "title": ""}
            for i, (n, c) in enumerate(speakers)
        }
    if companies is not None:
        m["tags"] = {"category": "sales", "keywords": [],
                     "entities": {"people": [], "companies": companies,
                                  "projects": [], "technologies": [], "dates": []}}
    return m


# ------------------------------------------------------------- suggest_company

def test_entities_only_uses_first_company_normalized():
    import app
    # Whitespace collapse is part of normalization.
    assert app.suggest_company(_mk(companies=["  Acme   Corp  "])) == "Acme Corp"


def test_attendee_majority_beats_entities_first():
    import app
    m = _mk(speakers=[("Ann", "Initech"), ("Bob", "Initech"), ("Alex", "Acme")],
            companies=["Acme"])
    assert app.suggest_company(m) == "Initech"


def test_tie_prefers_entities_companies_first_casefolded():
    import app
    # 1-1 tie; entities.companies[0] casefolds to "initech" -> that side wins.
    # Display form = first-seen normalized casing from speaker_info ("initech").
    m = _mk(speakers=[("Ann", "Acme"), ("Bob", "initech")], companies=["INITECH"])
    assert app.suggest_company(m) == "initech"


def test_tie_without_entities_match_alphabetical():
    import app
    m = _mk(speakers=[("Ann", "Zeta"), ("Bob", "Acme")])
    assert app.suggest_company(m) == "Acme"     # casefold-alphabetically smallest


def test_placeholder_and_empty_entries_ignored():
    import app
    # SPEAKER_* placeholder names and empty names are skipped (list_people
    # filter); entries with an empty company contribute nothing.
    m = _mk(speakers=[("SPEAKER_03", "Ghost Co"), ("", "Ghost Co"), ("Alex", "")])
    assert app.suggest_company(m) is None


@pytest.mark.parametrize("meeting", [{}, _mk(companies=[]), _mk(speakers=[])])
def test_no_signal_returns_none(meeting):
    import app
    assert app.suggest_company(meeting) is None


def test_case_variant_companies_count_as_one_key():
    import app
    m = _mk(speakers=[("Ann", "ACME"), ("Bob", "acme"), ("Alex", "Initech")])
    # casefolded key counts 2 vs 1; display = first-seen casing.
    assert app.suggest_company(m) == "ACME"


# ------------------------------------------------------------- lazy status surface

def test_status_lazy_suggestion_for_legacy_meeting_no_disk_write(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1", speaker_info={
        "SPEAKER_00": {"name": "Sarah", "company": "Acme", "title": "PM"}})
    # Legacy meeting: NO "company" key anywhere. The sentinel index.json must
    # survive the read — lazy suggestion never bulk-migrates index.json.
    sentinel = '{"sentinel": true}'
    (tmp_path / "index.json").write_text(sentinel)
    s = client.get("/meetings/m1/status").json()
    assert s["company"] is None
    assert s["company_suggestion"] == "Acme"
    assert (tmp_path / "index.json").read_text() == sentinel


def test_status_confirmed_company_suppresses_suggestion(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1", company="Initech", speaker_info={
        "SPEAKER_00": {"name": "Sarah", "company": "Acme", "title": "PM"}})
    s = client.get("/meetings/m1/status").json()
    assert s["company"] == "Initech"
    assert s["company_suggestion"] is None      # never both (spec gating)


# ------------------------------------------------------------- PATCH /company

def test_patch_company_sets_normalized_and_mirrors(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(app, tmp_path, mid="m1")
    (out / "summary.json").write_text(json.dumps(
        {"title": "Weekly Sync", "summary": "text", "action_items": []}))
    r = client.patch("/meetings/m1/company", json={"company": "  acme   corp "})
    assert r.status_code == 200, r.text
    assert r.json() == {"detail": "Company updated", "company": "acme corp"}
    assert app.meetings["m1"]["company"] == "acme corp"
    # persisted to index.json (the source of truth)...
    idx = json.loads((tmp_path / "index.json").read_text())
    assert idx["m1"]["company"] == "acme corp"
    # ...and mirrored into summary.json
    assert json.loads((out / "summary.json").read_text())["company"] == "acme corp"


@pytest.mark.parametrize("cleared", [None, ""])
def test_patch_company_clears(tmp_path, monkeypatch, cleared):
    client, app, _ = _client(tmp_path, monkeypatch)
    rec, out = _seed_complete(app, tmp_path, mid="m1", company="Acme")
    (out / "summary.json").write_text(json.dumps({"summary": "text", "company": "Acme"}))
    r = client.patch("/meetings/m1/company", json={"company": cleared})
    assert r.status_code == 200
    assert r.json()["company"] is None
    assert "company" not in app.meetings["m1"]
    assert "company" not in json.loads((tmp_path / "index.json").read_text())["m1"]
    assert "company" not in json.loads((out / "summary.json").read_text())


def test_patch_company_unknown_meeting_404(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    r = client.patch("/meetings/nope/company", json={"company": "Acme"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Meeting not found"   # our handler, not a bare no-route 404


def test_patch_company_missing_summary_json_still_succeeds(tmp_path, monkeypatch):
    # Mirror is best-effort: no summary.json on disk must not fail the request.
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1")
    r = client.patch("/meetings/m1/company", json={"company": "Acme"})
    assert r.status_code == 200
    assert app.meetings["m1"]["company"] == "Acme"


# ------------------------------------------------------------- list filter + fields

def test_list_meetings_company_filter_and_field(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1", company="Acme Corp")
    # suggestion-only meeting: speaker data but NO confirmed company -> never matches
    _seed_complete(app, tmp_path, mid="m2", speaker_info={
        "SPEAKER_00": {"name": "Sarah", "company": "Acme Corp", "title": ""}})
    all_items = {m["id"]: m for m in client.get("/meetings").json()}
    assert all_items["m1"]["company"] == "Acme Corp"
    assert all_items["m2"]["company"] is None
    got = client.get("/meetings", params={"company": "acme   corp"}).json()
    assert [m["id"] for m in got] == ["m1"]     # case-insensitive normalized EXACT match
    assert client.get("/meetings", params={"company": "Acme"}).json() == []  # exact, not substring


def test_grouped_compact_summary_carries_company(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    _seed_complete(app, tmp_path, mid="m1", company="Acme")
    grouped = client.get("/meetings/grouped?group_by=week").json()
    items = [m for g in grouped["groups"] for m in g["meetings"]]
    m1 = next(m for m in items if m["id"] == "m1")
    assert m1["company"] == "Acme"
