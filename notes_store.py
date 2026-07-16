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
import threading
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


_index_cache: dict = {}
# notes_dir(str) -> {"sig": str,                     # sha1 of the walk signature
#                    "checked": float,               # monotonic time of last check
#                    "index": {id: record},          # body-LESS records (list shape)
#                    "files": {relpath: {"mtime_ns": int, "size": int,
#                                        "id": str | None,
#                                        "record": dict | None,  # FULL record w/ body
#                                        "body": str}}}
_index_lock = threading.Lock()


def _read_text(p: Path) -> str:
    """ALL cache-path file reads funnel through here so tests can count them.
    The four RMW write-path reads (update_note/rename_note/link_meeting/
    apply_auto_tags) deliberately do NOT — they must stay fresh."""
    return p.read_text(encoding="utf-8")


def _walk_notes(base: Path) -> dict:
    """One-pass change scan: {relpath(posix): (mtime_ns, size)} for every *.md
    under base, excluding ONLY the root-level .trash/ and attachments/ subtrees
    — exact parity with the old rglob sweep: dotfiles and dot-dirs are INCLUDED
    (there was never a dotfile exclusion), and a NESTED folder named
    'attachments' is NOT excluded. Symlinked directories are not descended
    (rglob's `**` doesn't follow them either; also prevents symlink cycles).
    os.scandir batches stat info per directory, which is far cheaper over the
    Docker Desktop bind-mount than a fresh Path.stat() per file."""
    out: dict = {}
    stack = [(base, "", True)]  # (dir, rel_prefix_with_trailing_slash, is_root)
    while stack:
        d, prefix, is_root = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            rel = f"{prefix}{name}"
            try:
                if entry.is_dir(follow_symlinks=False):
                    if is_root and name in (TRASH_DIRNAME, ATTACH_DIRNAME):
                        continue
                    stack.append((Path(entry.path), rel + "/", False))
                elif name.endswith(".md") and entry.is_file():
                    st = entry.stat()
                    out[rel] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
    return out


def _signature(files: dict) -> str:
    """sha1 over the sorted (relpath, mtime_ns, size) tuples — the vault's
    change detector, and (quoted) the /api/notes/export ETag."""
    h = hashlib.sha1()
    for rel in sorted(files):
        mtime_ns, size = files[rel]
        h.update(f"{rel}\x00{mtime_ns}\x00{size}\n".encode("utf-8"))
    return h.hexdigest()


def _slim(rec: dict) -> dict:
    """Index-shaped copy of a full record: everything except body/content_hash.
    List endpoints must not leak bodies (tests pin `"body" not in` results)."""
    return {k: v for k, v in rec.items() if k not in ("body", "content_hash")}


def _refresh(base: Path, force: bool = False) -> dict:
    """Refresh the cache entry for `base`: ONE scandir sweep, then re-read only
    the changed/new files, merging everything else from the previous entry.
    Returns the (possibly reused) cache entry.

    Thread safety: notes_store is called from executor threads AND the event-
    loop thread. New `files`/`index` dicts are built aside and installed with a
    single atomic reference swap — the previous entry's dicts are NEVER mutated
    in place, so a racing reader holding the old maps sees a consistent
    snapshot. The whole refresh runs under _index_lock so callers racing at TTL
    expiry (several open tabs) don't do redundant full sweeps.

    Known cost note: move_folder renames preserve mtime but change relpaths, so
    a folder move re-reads that subtree once — O(subtree), accepted (spec)."""
    key = str(base)
    with _index_lock:
        cached = _index_cache.get(key)
        # Herd guard: another caller may have refreshed while we waited.
        if not force and cached and (time.monotonic() - cached["checked"]) < _INDEX_TTL_S:
            return cached
        base.mkdir(parents=True, exist_ok=True)
        walk = _walk_notes(base)
        sig = _signature(walk)
        if not force and cached and cached["sig"] == sig:
            cached["checked"] = time.monotonic()
            return cached
        old_files = cached.get("files", {}) if (cached and not force) else {}
        files: dict = {}
        for rel, (mtime_ns, size) in walk.items():
            old = old_files.get(rel)
            if old is not None and old["mtime_ns"] == mtime_ns and old["size"] == size:
                files[rel] = old  # unchanged -> keep cached entry, no read
                continue
            p = base / rel
            try:
                meta, body = parse_frontmatter(_read_text(p))
            except OSError:
                continue
            nid = meta.get("id") or None
            files[rel] = {
                "mtime_ns": mtime_ns, "size": size, "id": nid,
                # No-frontmatter-id files stay in the map (so they aren't
                # re-read every sweep) but are excluded from the index below,
                # matching the old build_index skip.
                "record": _record(base, p, meta, body) if nid else None,
                "body": body,
            }
        index = {f["id"]: _slim(f["record"]) for f in files.values() if f["id"]}
        entry = {"sig": sig, "checked": time.monotonic(), "index": index, "files": files}
        _index_cache[key] = entry  # ONE atomic reference swap
        try:
            _atomic_write(base / INDEX_NAME, json.dumps(index, indent=2))
        except OSError:
            pass
        return entry


def build_index(notes_dir) -> dict:
    """Full rescan: re-read EVERY note file into the id->record index (records
    body-less) and snapshot to disk. This is the force-rescan escape hatch
    (POST /api/notes/rescan) — it also covers the same-mtime-same-size external
    rewrite blind spot the incremental signature cannot see."""
    base = Path(notes_dir).resolve()
    return _refresh(base, force=True)["index"]


# Re-validating the on-disk signature means stat-ing every note file, which is
# slow over a large vault (esp. a Docker bind-mount). Within this window we trust
# a warm cache and skip the re-stat entirely; a write invalidates it (see
# _invalidate) so the next read re-checks and picks the change up.
_INDEX_TTL_S = 10.0


def get_index(notes_dir, force: bool = False) -> dict:
    """Return the id->record index (records body-less), refreshing only when
    files changed (or force). A warm cache is served without touching the
    filesystem for _INDEX_TTL_S seconds; after that ONE scandir sweep re-checks
    the signature and only *changed* files are re-read. External edits appear
    within the TTL; in-process writes call _invalidate."""
    base = Path(notes_dir).resolve()
    cached = _index_cache.get(str(base))
    if not force and cached and (time.monotonic() - cached["checked"]) < _INDEX_TTL_S:
        return cached["index"]
    return _refresh(base, force=force)["index"]


def index_signature(notes_dir) -> str:
    """Current vault signature (40-char sha1 hex) — refreshed under get_index's
    TTL rules first. Changes iff any note file's (relpath, mtime_ns, size)
    changed; the export handler quotes it as the HTTP ETag."""
    base = Path(notes_dir).resolve()
    get_index(base)
    cached = _index_cache.get(str(base))
    return cached["sig"] if cached else ""


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
    """Full record (with body + content_hash) for a note. Served from the body
    cache when warm; falls back to a disk read only on a cache miss. Returns a
    shallow copy — callers must not mutate nested lists in place."""
    base = Path(notes_dir).resolve()
    slim = get_index(base).get(note_id)
    if not slim:
        return None
    cached = _index_cache.get(str(base))
    f = cached["files"].get(slim["path"]) if cached else None
    if f and f.get("id") == note_id and f.get("record") is not None:
        return dict(f["record"])
    p = base / slim["path"]
    if not p.exists():
        return None
    meta, body = parse_frontmatter(_read_text(p))
    return _record(base, p, meta, body)


def get_bodies(notes_dir) -> dict:
    """Bulk body access for export/tasks: id -> FULL record (same shape as
    read_note 1:1 — body + content_hash included), served from the cache: at
    most one sweep, zero per-note reads when warm. Iteration order matches
    get_index. Values are shallow copies — do not mutate nested lists."""
    base = Path(notes_dir).resolve()
    index = get_index(base)
    cached = _index_cache.get(str(base))
    files = cached["files"] if cached else {}
    out = {}
    for nid, slim in index.items():
        f = files.get(slim["path"])
        if f and f.get("id") == nid and f.get("record") is not None:
            out[nid] = dict(f["record"])
        else:
            full = read_note(base, nid)
            if full is not None:
                out[nid] = full
    return out


def update_note(notes_dir, note_id: str, *, title=None, body=None, tags=None,
                expected_body_hash=None) -> dict | None:
    p = find_path(notes_dir, note_id)
    if not p:
        return None
    # HARD CONSTRAINT (perf-cache spec): FRESH disk read on purpose — never
    # route this through the index/body cache (_read_text). A metadata-only
    # write must preserve the CURRENT on-disk body (incl. a seconds-old
    # Obsidian edit) and expected_body_hash must compare against reality; a
    # stale cached body here silently clobbers an external edit (data loss).
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
    `q` matches title (from index) or note body (from the body cache)."""
    base = Path(notes_dir).resolve()
    index = get_index(base)
    cached = _index_cache.get(str(base))
    files = cached["files"] if cached else {}
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
                f = files.get(rec["path"])
                body = f.get("body") if f else None
                if body is None:
                    full = read_note(base, rec["id"])
                    body = (full or {}).get("body", "")
                if ql not in (body or "").lower():
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
    for rel in _walk_notes(base):
        parent = Path(rel).parent.as_posix()
        if parent and parent != ".":
            folders.add(parent)
    result = sorted(folders)
    _folders_cache[key] = {"at": time.monotonic(), "folders": result}
    return result


def rename_note(notes_dir, note_id: str, *, title=None, folder=None) -> dict | None:
    """Retitle (-> new slug filename) and/or move a note to another folder."""
    p = find_path(notes_dir, note_id)
    if not p:
        return None
    # HARD CONSTRAINT (perf-cache spec): FRESH disk read on purpose — never
    # route this through the index/body cache (_read_text). A metadata-only
    # write must preserve the CURRENT on-disk body (incl. a seconds-old
    # Obsidian edit) and expected_body_hash must compare against reality; a
    # stale cached body here silently clobbers an external edit (data loss).
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
    # HARD CONSTRAINT (perf-cache spec): FRESH disk read on purpose — never
    # route this through the index/body cache (_read_text). A metadata-only
    # write must preserve the CURRENT on-disk body (incl. a seconds-old
    # Obsidian edit) and expected_body_hash must compare against reality; a
    # stale cached body here silently clobbers an external edit (data loss).
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
    # HARD CONSTRAINT (perf-cache spec): FRESH disk read on purpose — never
    # route this through the index/body cache (_read_text). A metadata-only
    # write must preserve the CURRENT on-disk body (incl. a seconds-old
    # Obsidian edit) and expected_body_hash must compare against reality; a
    # stale cached body here silently clobbers an external edit (data loss).
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
