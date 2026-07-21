import asyncio
from fastapi.testclient import TestClient


def test_parse_ollama_tags_shapes_and_sorts():
    import app
    data = {"models": [
        {"name": "qwen3:14b", "size": 9_300_000_000,
         "details": {"parameter_size": "14.8B", "quantization_level": "Q4_K_M"}},
        {"name": "aaa:1b", "size": 1_000_000_000, "details": {}},
    ]}
    out = app._parse_ollama_tags(data)
    assert [m["name"] for m in out] == ["aaa:1b", "qwen3:14b"]  # sorted
    assert out[1]["size"] == 9_300_000_000
    assert out[1]["parameter_size"] == "14.8B"
    assert out[1]["quantization"] == "Q4_K_M"
    assert out[0]["parameter_size"] == ""  # missing details tolerated


def test_api_models_route_uses_helper(monkeypatch):
    import app
    async def fake_list():
        return [{"name": "qwen3:14b", "size": 1, "parameter_size": "14.8B", "quantization": "Q4_K_M"}]
    monkeypatch.setattr(app, "_list_ollama_models", fake_list)
    client = TestClient(app.app)
    r = client.get("/api/models")
    assert r.status_code == 200
    assert r.json() == {"models": [
        {"name": "qwen3:14b", "size": 1, "parameter_size": "14.8B", "quantization": "Q4_K_M"}]}


def test_list_ollama_models_degrades_on_failure(monkeypatch):
    import app
    def explode(*a, **kw):
        raise Exception("unreachable")
    monkeypatch.setattr(app.httpx, "AsyncClient", explode)
    assert asyncio.run(app._list_ollama_models()) == []


# ---------------------------------------------------------------------------
# A2: STT + diarize settings persistence
# ---------------------------------------------------------------------------

def _settings_client(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "MEETINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")
    return TestClient(app.app), app


def test_stt_settings_roundtrip(tmp_path, monkeypatch):
    client, app = _settings_client(tmp_path, monkeypatch)
    # defaults present
    got = client.get("/api/settings").json()["settings"]
    assert "stt_backend" in got and "diarize" in got
    # update
    r = client.put("/api/settings", json={"stt_backend": "parakeet", "diarize": False})
    assert r.status_code == 200
    saved = r.json()["settings"]
    assert saved["stt_backend"] == "parakeet" and saved["diarize"] is False
    # persisted across a fresh load
    again = client.get("/api/settings").json()["settings"]
    assert again["stt_backend"] == "parakeet" and again["diarize"] is False


def test_stt_backend_rejects_unknown_value(tmp_path, monkeypatch):
    client, app = _settings_client(tmp_path, monkeypatch)
    client.put("/api/settings", json={"stt_backend": "bogus"})
    assert client.get("/api/settings").json()["settings"]["stt_backend"] in ("parakeet",)
