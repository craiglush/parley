# Notes/Tasks Performance — Incremental Index + Body Cache + Export ETag

**Date:** 2026-07-16
**Branch:** `feat/notes-perf-cache`
**Status:** Approved for implementation

## Context (measured on the live container, 1,095-note vault)

| Operation | Cold | Warm |
|---|---|---|
| `/api/notes` | 12.8 s | 28 ms |
| `/api/tasks` | 14.3 s | **13.3 s every call** |
| `/api/notes/export` | 14.5 s | **14.3 s every call** |
| `_dir_signature` stat-sweep | 2.5 s | — |
| `get_index` after invalidate | 15.7 s | 1 ms |

Root cause: **per-file IO over the Docker Desktop bind-mount costs ~12 ms/file**
(the host filesystem → `/data/notes`). Three compounding problems:

1. **`build_index` is all-or-nothing** (`notes_store.py`): any signature change
   (including the auto-tagger's own writes after every note edit) discards the
   whole index and re-reads all 1,095 files (~15.7 s).
2. **Bodies are never cached.** The index keeps records only (no body), so
   `/api/tasks` (checkbox parse of every body, `app.py`) and
   `/api/notes/export` (bodies for the offline mirror) re-read all 1,095 files
   on **every call**. `list_notes(q=…)` search does the same.
3. **The offline mirror pulls `/api/notes/export` every 60 s per open tab**
   (`notes-sync.js`) — a recurring 14 s / 12 MB of bind-mount churn.

## Goal

Warm-path Notes/Tasks/export in **milliseconds**; worst-case (external edits)
bounded by one stat-sweep plus re-reading only *changed* files. Kill the 60 s
background churn. No change to the on-disk format, no move off the bind mount
(Obsidian keeps its Windows path), no behavioral change to any endpoint's
response body.

## Non-Goals

- Moving the vault off the bind mount or into a database.
- File-watcher (inotify doesn't propagate reliably across the mount) — we stay
  with signature polling.
- A general meetings-store cache. HOWEVER — correction from verification: the
  meeting side of `/api/tasks` (`_collect_meeting_tasks`) reads `summary.json`
  + `task_overlay.json` from disk per meeting per call, on the SAME bind mount
  (`MEETINGS_DIR`, bind-mounted from the host), so it is IN scope narrowly: a
  small shared `(mtime_ns, size)`-keyed JSON-read cache helper applied to
  those two reads. Nothing else meeting-side changes.

## Design

### 1. Incremental index with body cache (`notes_store.py`)

Replace the all-or-nothing `build_index` refresh with a per-file incremental
merge, and carry bodies:

- The cache entry per vault becomes
  `{sig, checked, index: {id → record}, files: {relpath → {mtime_ns, size, id, record, body}}}`.
- On refresh (TTL expiry or `_invalidate`): walk the tree **once** (see §2),
  producing `{relpath → (mtime_ns, size)}`. For each file: unchanged
  `(mtime_ns, size)` → keep the cached entry (no read); changed/new → read +
  parse that file only; missing → drop. Rebuild `index` from the merged map.
- `read_note` consults the body cache first (via the file map) and falls back
  to disk only on miss; `list_notes(q=…)` searches cached bodies.
- **HARD CONSTRAINT — write-path reads stay uncached.** `update_note`,
  `rename_note`, `link_meeting`, and `apply_auto_tags` each do a
  read-modify-write with a FRESH `p.read_text()` immediately before writing —
  deliberately, so a metadata-only write preserves the *current* on-disk body
  (incl. a seconds-old Obsidian edit) and so `expected_body_hash` compares
  against reality. These four internal reads MUST NOT be routed through the
  cache — a stale cached body here silently clobbers an external edit (data
  loss). Mark each with a comment; add a test that an external body rewrite
  followed immediately by `apply_auto_tags` preserves the new body.
- New helper `get_bodies(notes_dir) -> dict[id → record]` (records already
  embed `body`, matching `read_note`'s shape 1:1) for bulk consumers — export
  and tasks.
- Files with no frontmatter `id` are kept in the `files` map (so they aren't
  re-read every sweep) but excluded from the `index` rebuild, matching today's
  skip.
- **Thread safety (required):** notes_store is called concurrently from
  executor threads AND the event-loop thread. The refresh must build a fresh
  `files`/`index` and install them with ONE atomic reference swap (never
  mutate the cached dicts in place), and take a `threading.Lock` around the
  refresh so racing callers don't do redundant full sweeps (thundering herd at
  TTL expiry with multiple tabs).
- Correctness: all server writes already funnel through `_atomic_write` +
  `_invalidate`; external (Obsidian) edits are caught by `(mtime_ns, size)`
  drift exactly as the current signature does. Same-mtime-same-size external
  rewrite is the same theoretical blind spot the current signature already has
  — unchanged risk, accepted.
- Memory: ~12 MB of bodies for 1,095 notes — fine.
- The hot-path contract stays: `content_hash` still computed in `_record` from
  the (stripped) body; cached entries carry the already-computed record, so
  hashes are stable.

### 2. Single `os.scandir` walk (`notes_store.py`)

`_dir_signature` (per-file `Path.stat`) and `_iter_note_files` (glob) each
sweep the tree. Replace both with one shared recursive `os.scandir` walk that
returns `{relpath → (mtime_ns, size)}` for `*.md` files with EXACTLY today's
exclusions: root-level `.trash` and `attachments/` only — there is NO dotfile
exclusion today (`rglob` matches `.hidden.md` and notes inside dot-dirs) and
the walk must not add one. One parity exception is mandatory: `rglob` does not
descend into symlinked directories, so the walk must check
`entry.is_symlink()` and skip before recursing (also prevents symlink-cycle
loops). `entry.stat()` off a scandir dirent batches far better over the mount
than fresh `Path.stat()` calls. The signature hash = sha1 over the sorted
`(relpath, mtime_ns, size)` tuples; the walk feeds §1's refresh directly (one
sweep per refresh, not two). Known cost note: `move_folder` renames preserve
mtime but change relpaths, so a folder move re-reads that subtree once —
O(subtree), accepted.

### 3. Export ETag / If-None-Match (`app.py` + `notes-sync.js` + `sw.js`)

- `GET /api/notes/export` computes `ETag = "<sha1 of the walk signature>"`
  (changes iff any note file changed). The handler restructures into three
  phases: refresh index/compute ETag → compare `If-None-Match`
  (`request.headers.get("if-none-match")`, case-insensitive) → **only on
  mismatch** assemble bodies via `get_bodies`. A match returns **304** with an
  empty body AND the `ETag` header repeated (Caddy sits in front; the header
  must survive). New ETag is sent on every 200.
- `notes-sync.js` pull: remember the last ETag (module var), send
  `If-None-Match`; **the current code has two 304 hazards that must BOTH be
  fixed** — it throws on `!resp.ok` (304 is not ok) and then calls
  `resp.json()` unconditionally (rejects on an empty body). The 304
  short-circuit must come BEFORE both: `if (resp.status === 304) return;`
  (skip merge + UI refresh entirely). First pull of a session is unchanged.
- **`sw.js` guard (required, not optional):** the notes runtime-cache branch
  currently `cache.put`s any response; a 304 (or error) response would poison
  the offline fallback. Add `response.ok` gating before `cache.put` in that
  branch — non-ok responses pass through without touching the cache.

### 4. Consumers pick up the cache for free

- `/api/tasks` (`api_list_tasks` → per-note body reads) switches to
  `get_bodies`/cached reads, and — found in verification — the endpoint runs
  its collection SYNCHRONOUSLY on the event loop today (unlike its offloaded
  siblings), so the whole service stalls for its full duration. Add
  `run_in_executor` offload around the collection as part of this change. The
  meeting-side reads use the narrow JSON cache (see Non-Goals correction).
- `/api/notes/export` assembles from cached bodies (fast even on ETag miss).
- `auto_tag_note`'s `_run_tag_job` note read and `_note_attachment_text`'s
  note read benefit automatically via `read_note`'s cache hit.

## Expected results (same vault)

| Operation | Today | After |
|---|---|---|
| `/api/tasks` | ~14 s every call | ms warm; ≤ sweep (~1-2 s) after external edits |
| `/api/notes/export` | ~14 s every call | **304 in ms** when unchanged; ms-to-sweep otherwise |
| `get_index` after one note write | ~15.7 s | one stat-sweep + 1 file read |
| 60 s mirror pulls | 14 s churn each | 304, ~0 |

## Testing (per the new test policy: focused per task; full suite at the final integration checkpoint only)

- **Incremental refresh:** monkeypatch-count actual file reads — touch ONE
  file, assert exactly one re-read; delete a file, assert it drops; unchanged
  TTL-expiry refresh does zero reads (only the sweep).
- **Body cache correctness:** `update_note`/`apply_auto_tags`/simulated
  external write (rewrite file + bump mtime) → `read_note`/`list_notes(q)`
  reflect new content; `content_hash` stays round-trip stable (existing
  regression suite must stay green at the checkpoint).
- **Walk equivalence:** scandir walk output (set of files + exclusions) matches
  the old glob/signature behavior on a fixture tree incl. `.trash`,
  `attachments/`, nested folders.
- **ETag:** first GET → 200 + ETag; If-None-Match same → 304 empty; after a
  note write → 200 + new ETag. Client logic: pure decision helper if practical,
  else `node --check` + trace. `sw.js`: `node --check` + the `response.ok`
  guard asserted by reading (no SW test harness).
- **No timing assertions** (flaky) — read-count assertions stand in for perf.

## Rollout

1. Land on `feat/notes-perf-cache` (4 tasks; the last is the integration
   checkpoint with the full suite + all JS tests).
2. Re-measure in-container (same probe as the diagnosis) — before/after in the
   PR/commit notes.
3. Deploy (image rebuild not needed for deps — static + py only — but the
   image bakes code, so rebuild + recreate as usual).

## Open questions

None. The sw.js `response.ok` guard is folded in here because the ETag change
makes the existing gap actively harmful rather than latent.
