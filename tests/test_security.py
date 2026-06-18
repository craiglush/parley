import asyncio
import pytest
from fastapi import HTTPException
import app


def test_validate_artifact_id_accepts_clean():
    app._validate_artifact_id("ins_20260617_abc123", kind="insight_id")  # no raise


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "..", "x.json", "", "a" * 200])
def test_validate_artifact_id_rejects_bad(bad):
    with pytest.raises(HTTPException) as exc:
        app._validate_artifact_id(bad, kind="insight_id")
    assert exc.value.status_code == 400


def test_cors_origins_wildcard():
    assert app._cors_origins("*") == ["*"]


def test_cors_origins_list():
    assert app._cors_origins("https://a.me, https://b.me ,") == ["https://a.me", "https://b.me"]


def test_save_index_makes_backup(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({"old": {"id": "old"}}))
    monkeypatch.setattr(app, "meetings", {"new": {"id": "new", "title": "t", "date": "2026-06-17"}})

    app._save_index()

    bak = tmp_path / "index.json.bak"
    assert bak.exists()
    assert json.loads(bak.read_text()) == {"old": {"id": "old"}}
    assert "new" in json.loads(idx.read_text())


def test_concurrent_delete_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "meetings", {"m1": {"id": "m1", "title": "t", "date": "2026-06-17"}})
    # Avoid Qdrant/network in this unit test.
    monkeypatch.setattr(app, "get_qdrant", lambda: (_ for _ in ()).throw(RuntimeError("no qdrant")))

    async def run():
        results = await asyncio.gather(
            app.delete_meeting("m1"), app.delete_meeting("m1"),
            return_exceptions=True,
        )
        return results

    results = asyncio.run(run())
    ok = [r for r in results if isinstance(r, dict)]
    not_found = [r for r in results if isinstance(r, HTTPException) and r.status_code == 404]
    assert len(ok) == 1 and len(not_found) == 1  # exactly one wins, the other 404s, no KeyError


def test_check_embedding_dim_logs_mismatch(caplog):
    import logging
    with caplog.at_level(logging.ERROR):
        app._check_embedding_dim(actual=4096, expected=1024)
    assert any("embedding" in r.message.lower() for r in caplog.records)


def test_check_embedding_dim_ok_is_quiet(caplog):
    import logging
    with caplog.at_level(logging.ERROR):
        app._check_embedding_dim(actual=1024, expected=1024)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
