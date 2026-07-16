"""Perf-cache tests: single scandir walk parity, incremental refresh read-counts,
body-cache consumption, RMW write-path freshness, atomic swap.

Read counting: ALL cache-path reads funnel through notes_store._read_text, which
tests monkeypatch-wrap. No timing assertions anywhere (policy)."""
import os
import re
from pathlib import Path

import pytest

import notes_store as ns


# ---------------------------------------------------------------- helpers

def _bump_mtime(p: Path):
    """Force a visible (mtime_ns) change even on coarse filesystem clocks."""
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


@pytest.fixture
def read_counter(monkeypatch):
    """Counts cache-path file reads. Reset calls['n'] = 0 after warming."""
    calls = {"n": 0}
    real = ns._read_text

    def counted(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(ns, "_read_text", counted)
    return calls


def _old_glob_reference(base: Path) -> set:
    """Today's _iter_note_files semantics, inlined as the parity oracle."""
    trash = base / ns.TRASH_DIRNAME
    attach = base / ns.ATTACH_DIRNAME
    out = set()
    for p in base.rglob("*.md"):
        if trash in p.parents or attach in p.parents:
            continue
        out.add(p.relative_to(base).as_posix())
    return out


# ---------------------------------------------------------------- the walk

def test_walk_matches_old_glob_semantics(tmp_path):
    ns.create_note(tmp_path, "Root note")
    ns.create_note(tmp_path, "Nested", folder="Work/Projects")
    # NO dotfile exclusion today: rglob matches .hidden.md and notes in dot-dirs
    (tmp_path / ".hidden.md").write_text(
        "---\nid: n_hidden\ntitle: H\n---\n\nx", encoding="utf-8")
    dotdir = tmp_path / ".obsidian"
    dotdir.mkdir()
    (dotdir / "inside-dot-dir.md").write_text("plain", encoding="utf-8")
    # excluded: ROOT-level .trash and attachments only
    (tmp_path / ns.TRASH_DIRNAME).mkdir()
    (tmp_path / ns.TRASH_DIRNAME / "trashed.md").write_text("x", encoding="utf-8")
    (tmp_path / ns.ATTACH_DIRNAME).mkdir()
    (tmp_path / ns.ATTACH_DIRNAME / "not-a-note.md").write_text("x", encoding="utf-8")
    # NOT excluded: a NESTED folder that happens to be called attachments
    nested_att = tmp_path / "Work" / "attachments"
    nested_att.mkdir(parents=True)
    (nested_att / "counts.md").write_text(
        "---\nid: n_na\ntitle: NA\n---\n\nx", encoding="utf-8")
    # non-.md ignored
    (tmp_path / "notes_index.json").write_text("{}", encoding="utf-8")

    base = tmp_path.resolve()
    walk = ns._walk_notes(base)
    assert set(walk) == _old_glob_reference(base)
    assert ".hidden.md" in walk
    assert ".obsidian/inside-dot-dir.md" in walk
    assert "Work/attachments/counts.md" in walk
    assert not any(k.startswith((".trash/", "attachments/")) for k in walk)
    # values are (mtime_ns, size) ints
    mt, sz = walk["Work/attachments/counts.md"]
    assert isinstance(mt, int) and isinstance(sz, int) and sz > 0


def test_walk_skips_symlinked_dirs(tmp_path):
    real = tmp_path / "Real"
    real.mkdir()
    (real / "a.md").write_text("x", encoding="utf-8")
    link = tmp_path / "Linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")
    walk = ns._walk_notes(tmp_path.resolve())
    assert "Real/a.md" in walk
    assert not any(k.startswith("Linked/") for k in walk)


# ---------------------------------------------------------------- incremental refresh

def test_refresh_rereads_only_the_changed_file(tmp_path, read_counter):
    recs = [ns.create_note(tmp_path, f"Note {i}", body=f"body {i}") for i in range(3)]
    ns.get_index(tmp_path, force=True)              # warm: reads all 3
    read_counter["n"] = 0
    p = tmp_path / recs[1]["path"]
    p.write_text(p.read_text(encoding="utf-8").replace("body 1", "body 1 EDITED EXTERNALLY"),
                 encoding="utf-8")
    _bump_mtime(p)
    ns._invalidate(tmp_path)                        # skip the TTL wait
    idx = ns.get_index(tmp_path)
    assert len(idx) == 3
    assert read_counter["n"] == 1                   # ONLY the touched file was re-read
    assert "EDITED EXTERNALLY" in ns.read_note(tmp_path, recs[1]["id"])["body"]


def test_unchanged_refresh_does_zero_reads(tmp_path, read_counter):
    ns.create_note(tmp_path, "A", body="x")
    ns.create_note(tmp_path, "B", body="y")
    ns.get_index(tmp_path, force=True)
    read_counter["n"] = 0
    ns._invalidate(tmp_path)                        # simulate TTL expiry
    idx = ns.get_index(tmp_path)                    # sweep runs, nothing changed
    assert len(idx) == 2
    assert read_counter["n"] == 0


def test_deleted_file_drops_without_reads(tmp_path, read_counter):
    a = ns.create_note(tmp_path, "Stays", body="x")
    b = ns.create_note(tmp_path, "Goes", body="y")
    ns.get_index(tmp_path, force=True)
    read_counter["n"] = 0
    (tmp_path / b["path"]).unlink()                 # external delete
    ns._invalidate(tmp_path)
    idx = ns.get_index(tmp_path)
    assert a["id"] in idx and b["id"] not in idx
    assert read_counter["n"] == 0


def test_new_external_file_read_once(tmp_path, read_counter):
    ns.create_note(tmp_path, "Existing", body="x")
    ns.get_index(tmp_path, force=True)
    read_counter["n"] = 0
    (tmp_path / "dropped-in.md").write_text(
        "---\nid: n_external1\ntitle: Dropped\n---\n\nfrom obsidian\n", encoding="utf-8")
    ns._invalidate(tmp_path)
    idx = ns.get_index(tmp_path)
    assert "n_external1" in idx
    assert read_counter["n"] == 1


# ---------------------------------------------------------------- cache consumption

def test_read_note_serves_cached_body_without_reads(tmp_path, read_counter):
    rec = ns.create_note(tmp_path, "Cached", body="hello cache")
    ns.get_index(tmp_path, force=True)
    read_counter["n"] = 0
    full = ns.read_note(tmp_path, rec["id"])
    assert full["body"].strip() == "hello cache"
    assert full["content_hash"] == ns.content_hash("Cached", "hello cache")
    assert read_counter["n"] == 0


def test_read_note_falls_back_to_disk_on_cache_miss(tmp_path, read_counter):
    rec = ns.create_note(tmp_path, "Fallback", body="from disk")
    ns.get_index(tmp_path, force=True)
    # Manufacture a miss: drop the file entry but keep the index entry
    key = str(Path(tmp_path).resolve())
    ns._index_cache[key]["files"].pop(rec["path"])
    read_counter["n"] = 0
    full = ns.read_note(tmp_path, rec["id"])
    assert full is not None and full["body"].strip() == "from disk"
    assert read_counter["n"] == 1


def test_list_notes_q_searches_cached_bodies(tmp_path, read_counter):
    hit = ns.create_note(tmp_path, "Opaque title", body="the needle is here")
    ns.create_note(tmp_path, "Other", body="nothing")
    ns.get_index(tmp_path, force=True)
    read_counter["n"] = 0
    out = ns.list_notes(tmp_path, q="needle")
    assert [r["id"] for r in out] == [hit["id"]]
    assert "body" not in out[0] and "content_hash" not in out[0]
    assert read_counter["n"] == 0


def test_get_bodies_matches_read_note_shape_zero_reads_warm(tmp_path, read_counter):
    a = ns.create_note(tmp_path, "A", body="alpha")
    b = ns.create_note(tmp_path, "B", body="beta")
    ns.get_index(tmp_path, force=True)
    read_counter["n"] = 0
    bodies = ns.get_bodies(tmp_path)
    assert set(bodies) == {a["id"], b["id"]}
    assert bodies[a["id"]] == ns.read_note(tmp_path, a["id"])    # 1:1 with read_note
    assert read_counter["n"] == 0


def test_no_id_file_cached_but_not_indexed(tmp_path, read_counter):
    ns.create_note(tmp_path, "Real", body="x")
    (tmp_path / "no-frontmatter.md").write_text("just plain text\n", encoding="utf-8")
    idx = ns.get_index(tmp_path, force=True)
    assert len(idx) == 1                            # no-id file is not indexed...
    read_counter["n"] = 0
    ns._invalidate(tmp_path)
    ns.get_index(tmp_path)
    assert read_counter["n"] == 0                   # ...and NOT re-read every sweep


def test_external_edit_is_picked_up(tmp_path):
    rec = ns.create_note(tmp_path, "Live", body="old body")
    ns.get_index(tmp_path, force=True)
    p = tmp_path / rec["path"]
    p.write_text(p.read_text(encoding="utf-8").replace("old body", "new body from obsidian"),
                 encoding="utf-8")
    _bump_mtime(p)
    ns._invalidate(tmp_path)
    got = ns.read_note(tmp_path, rec["id"])
    assert got["body"].strip() == "new body from obsidian"
    assert got["content_hash"] == ns.content_hash("Live", "new body from obsidian")


# ---------------------------------------------------------------- signature / ETag feed

def test_index_signature_stable_then_changes_on_write(tmp_path):
    rec = ns.create_note(tmp_path, "Sig", body="v1")
    s1 = ns.index_signature(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{40}", s1)
    assert ns.index_signature(tmp_path) == s1       # unchanged vault -> stable
    ns.update_note(tmp_path, rec["id"], body="v2 which is longer")
    assert ns.index_signature(tmp_path) != s1


# ---------------------------------------------------------------- HARD CONSTRAINT regression

@pytest.mark.parametrize("op", ["update_meta", "rename", "link", "auto_tags"])
def test_rmw_write_paths_use_fresh_disk_reads_not_cache(tmp_path, op):
    """HARD CONSTRAINT (perf-cache spec): a metadata-only server write landing
    right after an external body edit must preserve the NEW body — the four RMW
    functions read fresh from disk, never from the body cache.

    'EXTERNAL EDIT' and 'original body' are the SAME LENGTH on purpose, and we
    do NOT bump mtime or invalidate: even an edit the signature cannot see must
    be preserved, because the protection is the fresh read itself."""
    rec = ns.create_note(tmp_path, "Target", body="original body")
    ns.get_index(tmp_path, force=True)              # body cache holds "original body"
    p = tmp_path / rec["path"]
    p.write_text(p.read_text(encoding="utf-8").replace("original body", "EXTERNAL EDIT"),
                 encoding="utf-8")
    if op == "update_meta":
        out = ns.update_note(tmp_path, rec["id"], tags=["t"])          # metadata-only
    elif op == "rename":
        out = ns.rename_note(tmp_path, rec["id"], title="Renamed Target")
    elif op == "link":
        out = ns.link_meeting(tmp_path, rec["id"], "20260716_mtg", add=True)
    else:
        out = ns.apply_auto_tags(tmp_path, rec["id"], "planning", ["kw"])
    assert "EXTERNAL EDIT" in out["body"]
    on_disk = (tmp_path / out["path"]).read_text(encoding="utf-8")
    assert "EXTERNAL EDIT" in on_disk and "original body" not in on_disk


# ---------------------------------------------------------------- thread-safety shape

def test_refresh_swaps_maps_atomically_never_mutating_in_place(tmp_path):
    rec = ns.create_note(tmp_path, "Swap", body="v1")
    ns.get_index(tmp_path, force=True)
    key = str(Path(tmp_path).resolve())
    old_entry = ns._index_cache[key]
    old_files, old_index = old_entry["files"], old_entry["index"]
    old_body = old_files[rec["path"]]["record"]["body"]
    ns.update_note(tmp_path, rec["id"], body="v2 changed and longer")
    ns.get_index(tmp_path)                          # refresh after the invalidate
    new_entry = ns._index_cache[key]
    assert new_entry is not old_entry               # whole entry swapped by reference
    assert new_entry["files"] is not old_files
    assert new_entry["index"] is not old_index
    # a racing reader still holding the OLD maps sees a consistent snapshot
    assert old_files[rec["path"]]["record"]["body"] == old_body
