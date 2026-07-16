"""Notes & Tasks storage — Markdown files on disk are the source of truth.

Each note is a .md file with YAML frontmatter under NOTES_DIR (default /data/notes,
mounted from D:\\Documents\\Meetings\\Notes). Notebooks are subfolders. An
in-memory, signature-invalidated index (snapshotted to notes_index.json) makes
listing fast and picks up external edits (e.g. Obsidian). Deletes go to .trash/.
"""
import os
import hashlib
import re
import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

NOTES_DIR = Path(os.getenv("NOTES_DIR", "/data/notes"))
TRASH_DIRNAME = ".trash"
ATTACH_DIRNAME = "attachments"
INDEX_NAME = "notes_index.json"
NOTE_TYPES = ("note", "journal", "todo")


class NoteConflict(Exception):
    """Raised by update_note when expected_body_hash != the current on-disk hash.
    The PUT handler turns this into HTTP 409; the offline client makes a conflict
    copy. Carries the current server record so the client can keep it."""

    def __init__(self, current: dict):
        self.current = current
        super().__init__("note content hash mismatch")


def now_iso() -> str:
    """Current UTC time as YYYY-MM-DDTHH:MM:SSZ."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown doc into (meta dict, body). No/invalid frontmatter -> ({}, text)."""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, text or ""
    raw_meta, body = m.group(1), m.group(2)
    try:
        meta = yaml.safe_load(raw_meta)
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, body


def serialize_note(meta: dict, body: str) -> str:
    """Render frontmatter + body into a full .md document (trailing newline)."""
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    body = (body or "").strip("\n")
    return f"---\n{fm}\n---\n\n{body}\n"


def new_note_id() -> str:
    return "n_" + uuid.uuid4().hex[:12]


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    # Cap length so the filename stays under filesystem path limits (Windows MAX_PATH);
    # _unique_path handles any collisions truncation may introduce.
    if len(s) > 80:
        s = s[:80].rstrip("-")
    return s or "untitled"


def safe_dir(notes_dir, folder: str = "") -> Path:
    """Resolve notes_dir/folder, refusing to escape notes_dir."""
    base = Path(notes_dir).resolve()
    target = (base / (folder or "")).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"folder escapes notes dir: {folder!r}")
    return target


def attachments_dir(notes_dir) -> Path:
    """Return (creating if needed) the vault-global `attachments/` dir under notes_dir."""
    d = Path(notes_dir).resolve() / ATTACH_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_attachment(notes_dir, original_name: str, data: bytes) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(original_name).stem).strip("-") or "file"
    ext = re.sub(r"[^a-z0-9.]+", "", Path(original_name).suffix.lower())[:10]
    fname = f"{stem}-{uuid.uuid4().hex[:6]}{ext}"
    (attachments_dir(notes_dir) / fname).write_bytes(data)
    return fname


def attachment_path(notes_dir, filename: str) -> Path | None:
    """Resolve `filename` inside `attachments/`, or None if it escapes/doesn't exist."""
    base = attachments_dir(notes_dir)
    target = (base / filename).resolve()
    if target.parent != base or not target.exists():
        return None
    return target


# Attachment references in a note body: the Obsidian embed `![[file]]` and the
# markdown-link/path `attachments/file` form. Body is the source of truth for
# which attachments belong to a note (no frontmatter, no drift).
_ATTACH_EMBED_RE = re.compile(r"!\[\[([^\]|#]+?)\]\]")
_ATTACH_PATH_RE = re.compile(r"(?<![\w/])attachments/([^\s\)\]]+)")


def note_attachments(notes_dir, note_id: str) -> list:
    """Referenced attachment filenames for a note, de-duped in body order.
    Matches both `![[file]]` and `attachments/file`. Empty if the note is gone."""
    rec = read_note(notes_dir, note_id)
    if not rec:
        return []
    body = rec.get("body", "") or ""

    # Collect matches from both regexes as (position, name, is_path) tuples
    matches = []
    for m in _ATTACH_EMBED_RE.finditer(body):
        name = m.group(1).strip()
        if name:
            matches.append((m.start(), name, False))
    for m in _ATTACH_PATH_RE.finditer(body):
        name = m.group(1).strip()
        if name:
            # Trim trailing punctuation from path-form captures
            name = name.rstrip(".,;:")
            if name:  # re-check after trim
                matches.append((m.start(), name, True))

    # Sort by position and dedup keeping first occurrence
    matches.sort(key=lambda x: x[0])
    out = []
    seen = set()
    for _, name, _ in matches:
        if name not in seen:
            out.append(name)
            seen.add(name)
    return out


def _atomic_write(path: Path, content: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _unique_path(directory: Path, slug: str) -> Path:
    """Return a non-colliding `<slug>.md` (or `<slug>-N.md`) path in directory."""
    candidate = directory / f"{slug}.md"
    n = 2
    while candidate.exists():
        candidate = directory / f"{slug}-{n}.md"
        n += 1
    return candidate


def content_hash(title: str, body: str) -> str:
    """Version token for a note: sha1 of title + NUL + body. Changes iff the
    user-visible text changes; blind to tag-only / metadata-only server writes,
    so the background auto-tagger never triggers a false offline-sync conflict."""
    return hashlib.sha1(((title or "") + "\x00" + (body or "")).encode("utf-8")).hexdigest()


def _record(notes_dir, path: Path, meta: dict, body=None) -> dict:
    """Build a note record dict from a path + parsed meta. Includes body if given."""
    base = Path(notes_dir).resolve()
    rel = path.resolve().relative_to(base)
    folder = rel.parent.as_posix()
    rec = {
        "id": meta.get("id", ""),
        "title": meta.get("title", path.stem),
        "type": meta.get("type", "note"),
        "folder": "" if folder == "." else folder,
        "path": rel.as_posix(),
        "tags": meta.get("tags") or [],
        "linked_meetings": meta.get("linked_meetings") or [],
        "created": meta.get("created", ""),
        "updated": meta.get("updated", ""),
        "category": meta.get("category", ""),
    }
    if body is not None:
        rec["body"] = body
        # Hash the STRIPPED body: parse_frontmatter's regex leaves a trailing
        # newline (and can swallow leading whitespace) on round-trip, so an
        # unstripped hash would differ between write and re-read — making every
        # synced note look dirty. Do not "simplify" this back to raw body.
        rec["content_hash"] = content_hash(rec["title"], body.strip())
    return rec


_index_cache: dict = {}  # notes_dir(str) -> {"sig": tuple, "index": dict}


def _iter_note_files(base: Path):
    trash = base / TRASH_DIRNAME
    attachments = base / ATTACH_DIRNAME
    for p in base.rglob("*.md"):
        if trash in p.parents:
            continue
        if attachments in p.parents:
            continue
        yield p


def _dir_signature(base: Path) -> tuple:
    """Cheap change-detector: sorted (relpath, mtime_ns, size) for all note files."""
    items = []
    for p in _iter_note_files(base):
        try:
            st = p.stat()
            items.append((p.relative_to(base).as_posix(), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(sorted(items))


def build_index(notes_dir) -> dict:
    """Scan all .md files (excluding .trash) into an id->record index; snapshot to disk."""
    base = Path(notes_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    index = {}
    for p in _iter_note_files(base):
        try:
            meta, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        nid = meta.get("id")
        if not nid:
            continue
        index[nid] = _record(base, p, meta)
    try:
        _atomic_write(base / INDEX_NAME, json.dumps(index, indent=2))
    except OSError:
        pass
    return index


# Re-validating the on-disk signature means stat-ing every note file, which is
# slow over a large vault (esp. a Docker bind-mount). Within this window we trust
# a warm cache and skip the re-stat entirely; a write invalidates it (see
# _invalidate) so the next read re-checks and picks the change up.
_INDEX_TTL_S = 10.0


def get_index(notes_dir, force: bool = False) -> dict:
    """Return the id->record index, rebuilding only when files changed (or force).

    A warm cache is served without touching the filesystem for _INDEX_TTL_S
    seconds; after that we re-check the cheap directory signature. External
    edits appear within the TTL; in-process writes call _invalidate."""
    base = Path(notes_dir).resolve()
    key = str(base)
    cached = _index_cache.get(key)
    if not force and cached and (time.monotonic() - cached["checked"]) < _INDEX_TTL_S:
        return cached["index"]
    sig = _dir_signature(base)
    if not force and cached and cached["sig"] == sig:
        cached["checked"] = time.monotonic()
        return cached["index"]
    index = build_index(base)
    _index_cache[key] = {"sig": sig, "index": index, "checked": time.monotonic()}
    return index


def _invalidate(notes_dir) -> None:
    """Mark the warm caches stale after a write so the next read re-checks the
    filesystem. Cheap and never serves stale data: the (offloaded) read path
    re-stats the signature and rebuilds only if it actually changed. This
    replaces a synchronous full re-index, which is catastrophic over a slow
    bind-mount (a single write would block for seconds reading every note)."""
    key = str(Path(notes_dir).resolve())
    cached = _index_cache.get(key)
    if cached:
        cached["checked"] = 0.0  # force the signature re-check on next get_index
    _folders_cache.pop(key, None)


def find_path(notes_dir, note_id: str) -> Path | None:
    """Return the absolute path of a note by id, or None."""
    rec = get_index(notes_dir).get(note_id)
    if not rec:
        return None
    p = Path(notes_dir).resolve() / rec["path"]
    return p if p.exists() else None


def create_note(notes_dir, title: str, folder: str = "", type: str = "note", body: str = "") -> dict:
    """Create a new .md note with frontmatter; returns its record."""
    if type not in NOTE_TYPES:
        type = "note"
    ts = now_iso()
    meta = {
        "id": new_note_id(),
        "title": title or "Untitled",
        "type": type,
        "tags": [],
        "linked_meetings": [],
        "created": ts,
        "updated": ts,
    }
    directory = safe_dir(notes_dir, folder)
    directory.mkdir(parents=True, exist_ok=True)
    path = _unique_path(directory, slugify(title))
    _atomic_write(path, serialize_note(meta, body))
    _invalidate(notes_dir)  # new note -> visible on the next read
    return _record(notes_dir, path, meta, body)


def read_note(notes_dir, note_id: str) -> dict | None:
    p = find_path(notes_dir, note_id)
    if not p:
        return None
    meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    return _record(notes_dir, p, meta, body)


def update_note(notes_dir, note_id: str, *, title=None, body=None, tags=None,
                expected_body_hash=None) -> dict | None:
    p = find_path(notes_dir, note_id)
    if not p:
        return None
    meta, cur_body = parse_frontmatter(p.read_text(encoding="utf-8"))
    if expected_body_hash is not None:
        cur_title = meta.get("title", p.stem)
        if content_hash(cur_title, cur_body.strip()) != expected_body_hash:
            raise NoteConflict(_record(notes_dir, p, meta, cur_body))
    if title is not None:
        meta["title"] = title
    if tags is not None:
        meta["tags"] = list(tags)
    meta["updated"] = now_iso()
    new_body = cur_body if body is None else body
    _atomic_write(p, serialize_note(meta, new_body))
    _invalidate(notes_dir)  # file changed -> next read refreshes
    return _record(notes_dir, p, meta, new_body)


def delete_note(notes_dir, note_id: str) -> bool:
    p = find_path(notes_dir, note_id)
    if not p:
        return False
    trash = Path(notes_dir).resolve() / TRASH_DIRNAME
    trash.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(trash, p.stem)
    p.replace(dest)
    _invalidate(notes_dir)
    return True


def list_notes(notes_dir, *, folder=None, tag=None, type=None, q=None) -> list:
    """Return note records (no body) matching filters, newest-updated first.
    `q` matches title (from index) or note body (read on demand)."""
    index = get_index(notes_dir)
    out = []
    ql = q.lower() if q else None
    for rec in index.values():
        if folder is not None and rec["folder"] != folder:
            continue
        if type is not None and rec["type"] != type:
            continue
        if tag is not None and tag not in (rec["tags"] or []):
            continue
        if ql:
            if ql in (rec["title"] or "").lower():
                pass
            else:
                full = read_note(notes_dir, rec["id"])
                if not full or ql not in (full.get("body", "")).lower():
                    continue
        out.append(rec)
    out.sort(key=lambda r: r.get("updated", ""), reverse=True)
    return out


_folders_cache: dict = {}  # notes_dir(str) -> {"at": monotonic, "folders": list}


def list_folders(notes_dir) -> list:
    """Distinct relative folder paths containing notes (excludes root and .trash).

    Walking the vault is slow over a large notes dir, so the result is cached for
    _INDEX_TTL_S seconds (new folders appear within the TTL)."""
    base = Path(notes_dir).resolve()
    key = str(base)
    cached = _folders_cache.get(key)
    if cached and (time.monotonic() - cached["at"]) < _INDEX_TTL_S:
        return cached["folders"]
    folders = set()
    for p in _iter_note_files(base):
        rel = p.parent.resolve().relative_to(base).as_posix()
        if rel and rel != ".":
            folders.add(rel)
    result = sorted(folders)
    _folders_cache[key] = {"at": time.monotonic(), "folders": result}
    return result


def rename_note(notes_dir, note_id: str, *, title=None, folder=None) -> dict | None:
    """Retitle (-> new slug filename) and/or move a note to another folder."""
    p = find_path(notes_dir, note_id)
    if not p:
        return None
    meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    if title is not None:
        meta["title"] = title
    meta["updated"] = now_iso()
    cur_folder = _record(notes_dir, p, meta)["folder"]
    dest_folder = cur_folder if folder is None else folder
    directory = safe_dir(notes_dir, dest_folder)
    directory.mkdir(parents=True, exist_ok=True)
    slug = slugify(meta.get("title", p.stem))
    desired = directory / f"{slug}.md"
    if desired.resolve() == p.resolve():
        new_path = p
    elif desired.exists():
        new_path = _unique_path(directory, slug)
    else:
        new_path = desired
    _atomic_write(new_path, serialize_note(meta, body))
    if new_path.resolve() != p.resolve():
        p.unlink()
    _invalidate(notes_dir)
    return _record(notes_dir, new_path, meta, body)


def append_to_body(notes_dir, note_id: str, text: str) -> dict | None:
    """Append text (separated by a blank line) to a note's body."""
    rec = read_note(notes_dir, note_id)
    if rec is None:
        return None
    cur = (rec.get("body") or "").rstrip("\n")
    new_body = (cur + "\n\n" + text.strip()) if cur.strip() else text.strip()
    return update_note(notes_dir, note_id, body=new_body)


def append_task_line(notes_dir, note_id: str, line: str) -> dict | None:
    """Append a single checkbox task line to a note's body, joined tightly (one newline)
    so consecutive tasks stay in the same Markdown list. Returns the updated record."""
    rec = read_note(notes_dir, note_id)
    if rec is None:
        return None
    cur = (rec.get("body") or "").rstrip("\n")
    new_body = (cur + "\n" + line) if cur.strip() else line
    return update_note(notes_dir, note_id, body=new_body)


def link_meeting(notes_dir, note_id: str, meeting_id: str, add: bool = True) -> dict | None:
    p = find_path(notes_dir, note_id)
    if not p:
        return None
    meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    links = list(meta.get("linked_meetings") or [])
    if add and meeting_id not in links:
        links.append(meeting_id)
    elif not add and meeting_id in links:
        links.remove(meeting_id)
    meta["linked_meetings"] = links
    meta["updated"] = now_iso()
    _atomic_write(p, serialize_note(meta, body))
    _invalidate(notes_dir)
    return _record(notes_dir, p, meta, body)


_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_wikilinks(body: str) -> list:
    """Return de-duplicated [[wiki-link]] targets (alias after | stripped), in order."""
    out = []
    for m in _WIKILINK_RE.finditer(body or ""):
        target = m.group(1).split("|")[0].strip()
        if target and target not in out:
            out.append(target)
    return out


def resolve_wikilinks(notes_dir, targets) -> list:
    """Resolve link targets against note titles (case-insensitive), then slugs."""
    index = get_index(notes_dir)
    by_title = {(r["title"] or "").lower(): r for r in index.values()}
    by_slug = {Path(r["path"]).stem.lower(): r for r in index.values()}
    out = []
    for t in targets:
        key = (t or "").lower()
        rec = by_title.get(key) or by_slug.get(slugify(t))
        out.append({"target": t, "note_id": rec["id"] if rec else None,
                    "title": rec["title"] if rec else None})
    return out


def backlinks(notes_dir, note_id: str) -> list:
    """Notes whose body contains a [[wiki-link]] to this note's title."""
    target = read_note(notes_dir, note_id)
    if not target:
        return []
    title_l = (target["title"] or "").lower()
    out = []
    for rec in get_index(notes_dir).values():
        if rec["id"] == note_id:
            continue
        full = read_note(notes_dir, rec["id"])
        if not full:
            continue
        if any(l.lower() == title_l for l in extract_wikilinks(full.get("body", ""))):
            out.append(rec)
    return out


def apply_auto_tags(notes_dir, note_id: str, category: str, keywords) -> dict | None:
    """Merge keywords into frontmatter tags (slugified, deduped, capped at 15).
    Sets category, bumps updated, writes atomically, refreshes index."""
    p = find_path(notes_dir, note_id)
    if not p:
        return None
    meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    merged = list(meta.get("tags") or [])
    for k in keywords or []:
        slug = re.sub(r"[^a-z0-9]+", "-", str(k).strip().lower()).strip("-")
        if slug and slug not in merged:
            merged.append(slug)
    meta["tags"] = merged[:15]
    if category:
        meta["category"] = category
    meta["updated"] = now_iso()
    _atomic_write(p, serialize_note(meta, body))
    _invalidate(notes_dir)
    return _record(notes_dir, p, meta, body)
