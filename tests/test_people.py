"""GET /people aggregates distinct speakers across meetings for autocomplete."""

from tests.test_meeting_routes import _client


def test_people_aggregates_distinct(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    app.meetings["m1"] = {
        "id": "m1", "date": "2026-07-01", "status": app.MeetingStatus.complete,
        "speaker_info": {"SPEAKER_00": {"name": "Alex", "company": "OldCo", "title": "Eng"}},
    }
    app.meetings["m2"] = {
        "id": "m2", "date": "2026-07-05", "status": app.MeetingStatus.complete,
        "speaker_info": {
            "SPEAKER_00": {"name": "Alex", "company": "Acme", "title": "CTO"},
            "SPEAKER_01": {"name": "Sarah", "company": "Acme", "title": "PM"},
        },
    }
    people = client.get("/people").json()
    names = {p["name"]: p for p in people}
    assert set(names) == {"Alex", "Sarah"}
    # Most recent meeting (m2, 2026-07-05) wins for Alex's company/title.
    assert names["Alex"]["company"] == "Acme"
    assert names["Alex"]["title"] == "CTO"


def test_people_empty(tmp_path, monkeypatch):
    client, app, _ = _client(tmp_path, monkeypatch)
    assert client.get("/people").json() == []
