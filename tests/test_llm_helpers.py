import app


def test_ctx_small_text_uses_floor():
    assert app._ctx_for_text("hello world", num_predict=512) == 8192


def test_ctx_scales_with_length():
    # ~10k tokens of transcript -> needs the 16k tier
    text = "word " * 10000  # ~50k chars -> ~12.5k tokens
    assert app._ctx_for_text(text, num_predict=2048) == 16384


def test_ctx_capped_at_16k():
    text = "word " * 100000  # far exceeds 16k tokens
    assert app._ctx_for_text(text, num_predict=2048) == 16384


def test_build_body_sets_num_ctx_and_options():
    body = app._build_generate_body("m", "hi", temperature=0.3, num_predict=2048)
    assert body["model"] == "m"
    assert body["stream"] is False
    assert body["think"] is False  # disable thinking-model reasoning channel
    assert body["options"]["temperature"] == 0.3
    assert body["options"]["num_predict"] == 2048
    assert body["options"]["num_ctx"] in (8192, 16384)
    assert "format" not in body


def test_build_body_includes_schema_format():
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    body = app._build_generate_body("m", "hi", temperature=0.0, num_predict=512, schema=schema)
    assert body["format"] == schema


def test_strip_think_removes_reasoning():
    raw = "<think>let me reason\nmore</think>\n{\"title\": \"x\"}"
    assert app._strip_think(raw).strip() == '{"title": "x"}'


def test_parse_object_handles_think_and_fences():
    raw = "<think>reasoning</think>\n```json\n{\"title\": \"Sync\"}\n```"
    assert app._parse_json_object(raw) == {"title": "Sync"}


def test_parse_object_logs_on_garbage(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        assert app._parse_json_object("not json at all", context="pass A") == {}
    assert any("pass A" in r.message for r in caplog.records)


def test_parse_array_handles_think():
    raw = "<think>x</think>[{\"task\": \"a\"}]"
    assert app._parse_json_array(raw) == [{"task": "a"}]


def test_build_body_omits_images_by_default():
    body = app._build_generate_body("m", "hi", temperature=0.3, num_predict=512)
    assert "images" not in body


def test_build_body_includes_images_when_passed():
    b64 = "aGVsbG8="  # "hello"
    body = app._build_generate_body("m", "hi", temperature=0.3, num_predict=512, images=[b64])
    assert body["images"] == [b64]
    # text portion identical to the no-images body (regression guard)
    plain = app._build_generate_body("m", "hi", temperature=0.3, num_predict=512)
    assert {k: v for k, v in body.items() if k != "images"} == plain
