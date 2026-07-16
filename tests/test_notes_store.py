import re
import notes_store


def test_module_constants_present():
    assert notes_store.TRASH_DIRNAME == ".trash"
    assert notes_store.INDEX_NAME == "notes_index.json"
    assert str(notes_store.NOTES_DIR)  # a Path


def test_now_iso_format():
    s = notes_store.now_iso()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", s)


def test_parse_frontmatter_basic():
    text = "---\nid: n_1\ntitle: Hello\ntags:\n  - a\n  - b\n---\n\n# Body\ntext"
    meta, body = notes_store.parse_frontmatter(text)
    assert meta["id"] == "n_1"
    assert meta["title"] == "Hello"
    assert meta["tags"] == ["a", "b"]
    assert body.strip() == "# Body\ntext"


def test_parse_frontmatter_absent():
    meta, body = notes_store.parse_frontmatter("just text, no frontmatter")
    assert meta == {}
    assert body == "just text, no frontmatter"


def test_serialize_roundtrip():
    meta = {"id": "n_1", "title": "T", "tags": ["x"], "linked_meetings": []}
    body = "line one\nline two"
    doc = notes_store.serialize_note(meta, body)
    assert doc.startswith("---\n")
    meta2, body2 = notes_store.parse_frontmatter(doc)
    assert meta2 == meta
    assert body2.strip() == body


import pytest


def test_slugify_and_id():
    assert notes_store.slugify("Hello, World!") == "hello-world"
    assert notes_store.slugify("") == "untitled"
    nid = notes_store.new_note_id()
    assert nid.startswith("n_") and len(nid) == 14


def test_safe_dir_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        notes_store.safe_dir(tmp_path, "../escape")
    ok = notes_store.safe_dir(tmp_path, "Journal/2026")
    assert str(tmp_path) in str(ok)


def test_create_note_writes_md(tmp_path):
    rec = notes_store.create_note(tmp_path, "My First Note", folder="Journal", type="journal", body="hello")
    assert rec["id"].startswith("n_")
    assert rec["title"] == "My First Note"
    assert rec["type"] == "journal"
    assert rec["folder"] == "Journal"
    assert rec["tags"] == [] and rec["linked_meetings"] == []
    # The .md exists on disk with matching frontmatter
    f = tmp_path / rec["path"]
    assert f.exists() and f.suffix == ".md"
    meta, body = notes_store.parse_frontmatter(f.read_text(encoding="utf-8"))
    assert meta["id"] == rec["id"]
    assert body.strip() == "hello"


def test_build_index_scans_md(tmp_path):
    a = notes_store.create_note(tmp_path, "Alpha", folder="", type="note")
    b = notes_store.create_note(tmp_path, "Beta", folder="Projects", type="note")
    idx = notes_store.build_index(tmp_path)
    assert set(idx.keys()) == {a["id"], b["id"]}
    assert idx[b["id"]]["folder"] == "Projects"
    assert (tmp_path / notes_store.INDEX_NAME).exists()


def test_index_excludes_trash(tmp_path):
    a = notes_store.create_note(tmp_path, "Keep")
    trash = tmp_path / notes_store.TRASH_DIRNAME
    trash.mkdir()
    (trash / "ghost.md").write_text("---\nid: n_ghost\ntitle: G\n---\n\nx", encoding="utf-8")
    idx = notes_store.build_index(tmp_path)
    assert a["id"] in idx and "n_ghost" not in idx


def test_get_index_caches_until_change(tmp_path):
    notes_store.create_note(tmp_path, "One")
    idx1 = notes_store.get_index(tmp_path, force=True)
    assert len(idx1) == 1
    notes_store.create_note(tmp_path, "Two")
    idx2 = notes_store.get_index(tmp_path)  # signature changed -> rebuild
    assert len(idx2) == 2


def test_find_path(tmp_path):
    a = notes_store.create_note(tmp_path, "Findme")
    p = notes_store.find_path(tmp_path, a["id"])
    assert p is not None and p.exists()
    assert notes_store.find_path(tmp_path, "n_missing") is None


def test_read_note(tmp_path):
    a = notes_store.create_note(tmp_path, "Readable", body="content here")
    got = notes_store.read_note(tmp_path, a["id"])
    assert got["id"] == a["id"]
    assert got["body"].strip() == "content here"
    assert notes_store.read_note(tmp_path, "n_nope") is None


def test_update_note_preserves_id_and_bumps_updated(tmp_path):
    a = notes_store.create_note(tmp_path, "Orig", body="v1")
    up = notes_store.update_note(tmp_path, a["id"], body="v2", tags=["t1"])
    assert up["id"] == a["id"]
    assert up["created"] == a["created"]
    assert up["tags"] == ["t1"]
    again = notes_store.read_note(tmp_path, a["id"])
    assert again["body"].strip() == "v2"


def test_delete_note_moves_to_trash(tmp_path):
    a = notes_store.create_note(tmp_path, "Doomed")
    assert notes_store.delete_note(tmp_path, a["id"]) is True
    assert notes_store.read_note(tmp_path, a["id"]) is None
    trash_files = list((tmp_path / notes_store.TRASH_DIRNAME).glob("*.md"))
    assert len(trash_files) == 1
    assert notes_store.delete_note(tmp_path, a["id"]) is False  # already gone


def test_list_notes_filters(tmp_path):
    notes_store.create_note(tmp_path, "Apple pie recipe", folder="Food", type="note")
    notes_store.create_note(tmp_path, "Standup", folder="Work", type="journal")
    assert len(notes_store.list_notes(tmp_path)) == 2
    assert len(notes_store.list_notes(tmp_path, folder="Food")) == 1
    assert len(notes_store.list_notes(tmp_path, type="journal")) == 1
    hits = notes_store.list_notes(tmp_path, q="apple")
    assert len(hits) == 1 and "body" not in hits[0]


def test_list_folders(tmp_path):
    notes_store.create_note(tmp_path, "a", folder="Work")
    notes_store.create_note(tmp_path, "b", folder="Work/Projects")
    folders = notes_store.list_folders(tmp_path)
    assert "Work" in folders and "Work/Projects" in folders


def test_link_meeting_toggle(tmp_path):
    a = notes_store.create_note(tmp_path, "Linked")
    up = notes_store.link_meeting(tmp_path, a["id"], "20260617_sync", add=True)
    assert up["linked_meetings"] == ["20260617_sync"]
    up2 = notes_store.link_meeting(tmp_path, a["id"], "20260617_sync", add=False)
    assert up2["linked_meetings"] == []


def test_rename_note_retitle(tmp_path):
    a = notes_store.create_note(tmp_path, "Old Title", folder="Inbox", body="keep me")
    up = notes_store.rename_note(tmp_path, a["id"], title="New Title")
    assert up["id"] == a["id"]
    assert up["title"] == "New Title"
    assert up["path"] == "Inbox/new-title.md"
    assert not (tmp_path / a["path"]).exists()  # old file gone
    assert notes_store.read_note(tmp_path, a["id"])["body"].strip() == "keep me"


def test_rename_note_move_folder(tmp_path):
    a = notes_store.create_note(tmp_path, "Movable", folder="Inbox")
    up = notes_store.rename_note(tmp_path, a["id"], folder="Archive")
    assert up["folder"] == "Archive"
    assert up["path"] == "Archive/movable.md"


def test_rename_note_noop_same(tmp_path):
    a = notes_store.create_note(tmp_path, "Stable", folder="Inbox")
    up = notes_store.rename_note(tmp_path, a["id"], title="Stable", folder="Inbox")
    assert up["path"] == a["path"]  # no -2 suffix churn


def test_append_to_body(tmp_path):
    a = notes_store.create_note(tmp_path, "Log", body="existing")
    up = notes_store.append_to_body(tmp_path, a["id"], "- [ ] new task")
    assert "existing" in up["body"] and "- [ ] new task" in up["body"]
    # appended after a blank line
    assert "existing\n\n- [ ] new task" in up["body"]
    assert notes_store.append_to_body(tmp_path, "n_missing", "x") is None


def test_append_to_empty_body(tmp_path):
    a = notes_store.create_note(tmp_path, "Empty", body="")
    up = notes_store.append_to_body(tmp_path, a["id"], "- [ ] first")
    assert up["body"].strip() == "- [ ] first"


def test_extract_wikilinks():
    body = "See [[Project Plan]] and [[Ideas|my ideas]] and [[Project Plan]] again."
    assert notes_store.extract_wikilinks(body) == ["Project Plan", "Ideas"]
    assert notes_store.extract_wikilinks("no links") == []


def test_resolve_wikilinks(tmp_path):
    target = notes_store.create_note(tmp_path, "Project Plan", folder="Work")
    res = notes_store.resolve_wikilinks(tmp_path, ["project plan", "Missing"])
    by_target = {r["target"]: r for r in res}
    assert by_target["project plan"]["note_id"] == target["id"]
    assert by_target["Missing"]["note_id"] is None


def test_backlinks(tmp_path):
    target = notes_store.create_note(tmp_path, "Hub", folder="")
    notes_store.create_note(tmp_path, "Spoke", folder="", body="links to [[Hub]] here")
    bl = notes_store.backlinks(tmp_path, target["id"])
    assert len(bl) == 1 and bl[0]["title"] == "Spoke"

def test_note_attachments_parses_both_forms(tmp_path):
    body = (
        "Here is a chart ![[chart-ab12cd.png]] and the spec "
        "[report.pdf](attachments/report-ff33aa.pdf). "
        "Also see [[Some Other Note]] (a wiki link, NOT an attachment)."
    )
    rec = notes_store.create_note(tmp_path, "N", body=body)
    got = notes_store.note_attachments(tmp_path, rec["id"])
    assert got == ["chart-ab12cd.png", "report-ff33aa.pdf"]


def test_note_attachments_dedups_and_orders(tmp_path):
    body = "![[a-1.png]] then attachments/b-2.pdf then again ![[a-1.png]]"
    rec = notes_store.create_note(tmp_path, "N", body=body)
    assert notes_store.note_attachments(tmp_path, rec["id"]) == ["a-1.png", "b-2.pdf"]


def test_note_attachments_missing_note_empty(tmp_path):
    assert notes_store.note_attachments(tmp_path, "n_missing") == []


def test_note_attachments_interleaved_body_order(tmp_path):
    rec = notes_store.create_note(tmp_path, "Order", body=(
        "See the spec [report.pdf](attachments/report-ff33aa.pdf) "
        "and this chart ![[chart-ab12cd.png]]."
    ))
    assert notes_store.note_attachments(tmp_path, rec["id"]) == [
        "report-ff33aa.pdf", "chart-ab12cd.png"]


def test_note_attachments_ignores_external_urls(tmp_path):
    rec = notes_store.create_note(tmp_path, "Ext", body=(
        "External https://cdn.example.com/attachments/photo.jpg link, "
        "but local [f](attachments/real-aa11bb.pdf) counts."
    ))
    assert notes_store.note_attachments(tmp_path, rec["id"]) == ["real-aa11bb.pdf"]


def test_note_attachments_trims_trailing_punctuation(tmp_path):
    rec = notes_store.create_note(tmp_path, "Punct", body="see attachments/report-cc22dd.pdf.")
    assert notes_store.note_attachments(tmp_path, rec["id"]) == ["report-cc22dd.pdf"]
