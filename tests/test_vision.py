import asyncio
import llm


def test_describe_image_returns_response(tmp_path, monkeypatch):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")
    captured = {}

    async def fake_generate(body):
        captured["body"] = body
        return {"response": "<think>look</think>A red bar chart."}

    async def fake_available(force=False):
        return True

    monkeypatch.setattr(llm, "_ollama_generate", fake_generate)
    monkeypatch.setattr(llm, "_vision_available", fake_available)
    out = asyncio.run(llm.describe_image(str(img), prompt="Describe."))
    assert out == "A red bar chart."          # <think> stripped
    assert captured["body"]["model"] == llm.VISION_MODEL
    assert captured["body"]["images"] and isinstance(captured["body"]["images"][0], str)


def test_describe_image_raises_when_model_absent(tmp_path, monkeypatch):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")

    async def fake_available(force=False):
        return False

    monkeypatch.setattr(llm, "_vision_available", fake_available)
    try:
        asyncio.run(llm.describe_image(str(img), prompt="Describe."))
        assert False, "expected VisionUnavailable"
    except llm.VisionUnavailable:
        pass


def test_vision_available_checks_tags(monkeypatch):
    import llm as _llm
    _llm._vision_present = None  # reset the cache

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "qwen3-vl:8b"}, {"name": "qwen3:14b"}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    monkeypatch.setattr(_llm.httpx, "AsyncClient", _Client)
    assert asyncio.run(_llm._vision_available(force=True)) is True


def test_vision_transient_error_not_cached(monkeypatch):
    import llm

    calls = {"n": 0}

    class _FlakyClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connection refused")

            class _R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"models": [{"name": llm.VISION_MODEL}]}

            return _R()

    monkeypatch.setattr(llm, "_vision_present", None)
    monkeypatch.setattr(llm.httpx, "AsyncClient", _FlakyClient)

    assert asyncio.run(llm._vision_available()) is False   # transient error -> False this call
    assert llm._vision_present is None                      # ...but NOT cached
    assert asyncio.run(llm._vision_available()) is True     # next call re-checks and succeeds
    assert llm._vision_present is True
