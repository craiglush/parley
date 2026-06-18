import json
import app


def test_schemas_exist_for_all_passes():
    for key in ("analysis_pass_a", "analysis_pass_b", "analysis_pass_c",
                "analysis_pass_d", "analysis_pass_e", "analysis_pass_f",
                "analysis_pass_g"):
        assert key in app.ANALYSIS_SCHEMAS, f"missing schema for {key}"


def test_schemas_are_json_serializable_objects_or_arrays():
    for key, schema in app.ANALYSIS_SCHEMAS.items():
        json.dumps(schema)  # must serialize
        assert schema.get("type") in ("object", "array"), key


def test_pass_a_schema_shape():
    a = app.ANALYSIS_SCHEMAS["analysis_pass_a"]
    assert a["type"] == "object"
    assert "title" in a["properties"]
    assert "summary" in a["properties"]
    assert "topics" in a["properties"]


def _item_props(schema_key):
    """Return the property names of an array schema's item objects."""
    items = app.ANALYSIS_SCHEMAS[schema_key]["items"]
    assert items["type"] == "object", schema_key
    return set(items["properties"].keys())


def _obj_array_props(schema_key, prop):
    """Return item property names for an object schema's array-of-objects property."""
    arr = app.ANALYSIS_SCHEMAS[schema_key]["properties"][prop]
    assert arr["type"] == "array", f"{schema_key}.{prop}"
    items = arr["items"]
    assert items["type"] == "object", f"{schema_key}.{prop}"
    return set(items["properties"].keys())


def test_schemas_match_prompt_object_shapes():
    # Pinned to the JSON shapes the prompt templates ask for (and that the
    # frontend / summary.json consume). Ollama ENFORCES `format`, so a string
    # array here would flatten the output to blanks. Guards that regression.
    assert _obj_array_props("analysis_pass_a", "topics") == {"topic", "summary", "outcome"}

    b = _item_props("analysis_pass_b")
    assert "task" in b and "who" in b  # prompt uses `who`, NOT `assigned_to`
    assert "assigned_to" not in b

    assert _obj_array_props("analysis_pass_c", "decisions") == {"decision", "context"}
    assert _obj_array_props("analysis_pass_c", "open_questions") == {"question", "asked_by", "answered"}

    assert _item_props("analysis_pass_d") == {"concern", "raised_by", "resolved", "notes"}
    assert _item_props("analysis_pass_e") == {"figure", "context", "said_by"}

    f = app.ANALYSIS_SCHEMAS["analysis_pass_f"]
    assert "overall" in f["properties"]
    assert _obj_array_props("analysis_pass_f", "notable_moments") == {"moment", "tone"}
