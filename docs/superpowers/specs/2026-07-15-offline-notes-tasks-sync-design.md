# Offline Notes & Tasks Sync — Design

**Date:** 2026-07-15
**Branch:** `feat/offline-notes-tasks`
**Status:** Approved for implementation (revised after adversarial spec review)

## Context

The Notes & Tasks app stores each note as a **Markdown file with YAML
frontmatter** on disk under `NOTES_DIR` (default `/data/notes`, bind-mounted from
the host). The server is the source of truth; the same files
are also editable externally by Obsidian, so `notes_store` uses a
signature-invalidated in-memory index driven by file **mtime** (`_dir_signature`,
`notes_store.py:154–163`; `get_index`/`_invalidate`, `notes_store.py:200–224`) to
pick up outside edits.

**Tasks are checkbox lines inside note bodies.** A task is a GFM checkbox
(`- [ ]` / `- [x]`) with optional Obsidian-Tasks inline metadata
(`tasks_store.py:1–46`). At the *storage* layer a task edit is a note-body edit —
but at the *client* layer the Notes UI does **not** edit tasks through note CRUD.
It calls dedicated endpoints: `/api/tasks/toggle`, `/api/tasks` POST/PATCH/DELETE,
and (for meeting action items) `/api/meetings/{id}/tasks/*`
(`notes-tasks.js:264–388`). So "tasks come along for free" is true only
server-side; offline task support requires explicit client work (see Goal).

Today the Notes UI (`static/notes-tasks.js`) is **online-only**: the shared
`api()` fetch wrapper (`notes-tasks.js:21–31`) fronts ~20 endpoints and nothing is
persisted locally, so the surface is unusable offline.

The recently shipped **offline upload queue** (`app.js`) established the pattern
this feature reuses: an IndexedDB mirror, a `withFlushLock` Web-Locks mutex, and
`online` / interval / `checkPendingRecording` flush triggers.

This is a **single-user** app behind Authelia. The user may edit on more than one
device (or in Obsidian), but rarely the *same note* in two places between syncs.
So we need robust sync with a **safety net**, not concurrent-merge machinery.

## Goal

Open and use the Notes & Tasks surface fully offline — browse the note list, read
notes, create/edit/delete notes, and **toggle/edit note-sourced tasks** — and have
all offline changes **sync automatically to the server when connectivity returns**,
across devices and alongside Obsidian, **without ever silently losing a text
edit**.

## Non-Goals

- **Offline attachments** — creating/uploading attachments needs connectivity.
- **Offline meeting-sourced task edits** — `/api/meetings/{id}/tasks/*` action
  items live outside notes entirely; those stay online-only and must fail cleanly
  when offline.
- **Real-time concurrent editing / CRDT** — single user; content-hash
  last-write-wins with a conflict-copy net is sufficient.
- **Obsidian becoming a sync client** — Obsidian keeps editing files directly;
  the mirror re-pulls changed notes on the next sync.

**Known caveat (final-review fix): offline-tasks cold start.** The Tasks pillar's
dashboard rollup (`loadTasks()` → `GET /api/tasks`) is a **server-side aggregate**
across every note and meeting — it is not served from the notes mirror and has no
offline fallback of its own. It only has data to show offline if the browser
held it in memory from an earlier online load in the same session (`/api/tasks`
is NOT service-worker-cached — the SW's `/api` early-return excludes it; only
`/api/notes*` GETs are runtime-cached). Any offline cold start of the Tasks
pillar therefore sees an empty/failed dashboard. This does
**not** affect note-body task operations (toggle/add/edit/delete a checkbox line
inside an open note) — those go through the notes mirror and work fully offline
regardless of whether the dashboard has ever loaded.

## Core model: content-hash version token + conflict copy

**Why not `updated`:** the frontmatter `updated` timestamp is a poor version
token. Obsidian edits the file on disk without touching `updated` (the server
never rewrites it — `_record` reads it verbatim, `notes_store.py:133`), so an
Obsidian body change would be *invisible* to a timestamp guard and get silently
overwritten. Conversely the server's **own** background auto-tagger advances
`updated` after every edit (`apply_auto_tags` sets `meta['updated']=now_iso()`,
`notes_store.py:510`, run by `_tag_worker`), so a timestamp guard would raise
*spurious* conflicts on tag-only bumps — and the attachment-extraction feature
makes those bumps frequent and delayed.

**The token is a content hash.** Define
`content_hash = sha1((title + "\x00" + body).encode("utf-8")).hexdigest()` —
computed on the server and returned with every note record. It changes **iff the
user-visible text changes**, and is blind to tag-only / metadata-only server
writes. This makes both problem cases correct:

- **Obsidian (or another device) changed the body/title** → hash differs → the
  server rejects the offline push with **409 + its current record** → the client
  keeps the server version and saves the local text as a **new note**
  `"<Title> (conflict copy — Jul 14)"`, then toasts. *Nothing is overwritten.*
- **Server only re-tagged the note** (body/title unchanged) → hash matches → the
  offline body edit applies cleanly, **no false conflict**. `tags` are omitted
  from the push (`NoteUpdate.tags` stays `None`), so the server's newer auto-tags
  survive; the client reconciles them on the next pull.

  **Errata (final-review fix):** manual tag edits (`setTags` — add/remove a tag
  chip in the UI) are **not** routed through the mirror at all. They go straight
  to `PUT /api/notes/{id}` with `{tags}` (title/body omitted, so nothing else is
  touched), then the mirror is refreshed from the server via `pull()`. This was
  necessary because the mirror is genuinely local-only for tags (see above) — a
  mirror-only tag write would never reach the server and would look "reverted"
  as soon as the auto-tagger's next pass (or the 60 s pull) overwrote it. Offline,
  this PUT fails with a normal network error and surfaces a toast — manual
  retagging is online-only, same as `retag`/`analyze`.

## Architecture

### Client

**New pure module — `static/notes-sync-logic.js`** (mirrors `queue-logic.js`:
DOM/IDB-free, dual browser-global + CommonJS export for Node tests):

- `contentHash(title, body)` — the same `sha1(title\0body)` the server uses, so
  the client can detect its own dirty state and label conflicts.
- `selectDirtyNotes(records)` — notes needing push, ordered create → edit →
  delete, oldest-local-edit first.
- `resolvePush(localRecord, serverResult)` → `applied` / `conflict` / `remap`.
- `conflictCopyTitle(title, isoDate)` — deterministic conflict-copy naming.
- `mergeServerList(localById, serverRecords)` — reconcile a freshly pulled server
  list into the mirror: server wins for non-dirty notes (including newer tags);
  dirty notes keep their pending local edits.
- `applyTaskEditToBody(body, op)` — pure checkbox-line transform mirroring
  `tasks_store` format (toggle `[ ]`↔`[x]`; add/edit/delete a checkbox line with
  inline `📅`/priority/`@owner` metadata). This is how offline task ops become
  note-body edits.

**Dedicated IndexedDB database — `notes-mirror` (v1)**, opened separately from the
capture DB (`CAP_DB` in `app.js`) so the two version lifecycles stay decoupled.
One `notes` object store, key `id`, each entry:

```
{ id, record: {title, body, tags, type, folder, created, updated, content_hash},
  baseHash, dirty: bool, pendingOp: 'create'|'edit'|'delete'|null, localUpdated }
```

`baseHash` = the `content_hash` last pulled from the server. `dirty` is set when
`contentHash(local title, body) !== baseHash`. The note *list* and every note
*body* are mirrored (notes are kilobytes of text) via `GET /api/notes/export`.

**Sync engine — `static/notes-sync.js`** (DOM/IDB-bound; reuses `withFlushLock`
and the online/interval triggers from `app.js`):

- **Pull** — on load, on `online`, and on a 60 s interval: `GET
  /api/notes/export` → `mergeServerList` → refresh UI from the mirror.
- **Local writes** — the specific note-CRUD and note-sourced-task call sites are
  rerouted (see below) to write the mirror immediately (marking `dirty` +
  `pendingOp`), update the UI optimistically, then trigger a flush.
- **Flush** (under `withFlushLock('notes-sync-flush')`), per dirty note in
  `selectDirtyNotes` order:
  - `create` → `POST /api/notes`; on success remap temp id `n_local_<rand>` →
    server id in the mirror.
  - `edit` → `PUT /api/notes/{id}` with `{title?, body, expected_body_hash:
    baseHash}`; **409 → conflict copy**.
  - `delete` → `DELETE /api/notes/{id}` (deletes go to server `.trash/`, so a race
    is recoverable).
  - Network error → stop the flush (leave `dirty`), retry on the next trigger.

**Client reroute (narrow allowlist — NOT the shared `api()` helper).** The `api()`
wrapper fronts ~20 endpoints (search, folders, attachments, retag, links, meeting
tasks, …) that have no offline semantics; rerouting it wholesale would break them.
Instead, reroute only these specific call sites through the mirror:

- Note CRUD: `POST /api/notes` (`notes-tasks.js:660/673`), `PUT /api/notes/{id}`
  (`630/641/651`), `DELETE /api/notes/{id}` (`686`), and reads from the mirror.
- Note-sourced tasks: `/api/tasks/toggle` (`264`) and `/api/tasks` POST/PATCH/
  DELETE (`344/358/385`) → translated via `applyTaskEditToBody` into a mirror body
  edit on the owning note.

**Everything else passes straight through to network** and surfaces a normal error
when offline — including attachment upload/list/delete, `analyze`/`analysis`
(attachment feature), `retag`, search, folders, links, and all
`/api/meetings/{id}/tasks/*` meeting action items.

### Server (minimal)

- **`content_hash` in records** — `notes_store._record` computes and includes
  `content_hash = sha1(title + "\x00" + body)`. Returned by `read_note` and the
  export endpoint.
- **`NoteUpdate` + `update_note` gain `expected_body_hash`** (one consistent wire
  key end-to-end — client body field, Pydantic model, handler). When present and
  `!= sha1(current title\0body)`, the endpoint returns **409** with the current
  record and does **not** write. When absent, behavior is unchanged (online edits
  stay last-write-wins). *(`NoteUpdate` currently has no such field; Pydantic
  drops unknown keys, so the field must actually be added — verified at
  `app.py:4650`.)*
- **New `GET /api/notes/export`** → all note records **with bodies + content_hash**,
  built on `notes_store.get_index` / `read_note` (which already exclude the
  `attachments/` subtree and match only `*.md`), offloaded to the threadpool like
  `api_list_notes` (`app.py:4781–4790`). **Body-only — never inlines attachment
  extracted text.** Do not hand-roll a raw directory glob.

### Service worker (`static/sw.js`)

- Bump `CACHE_NAME` → `meetings-v17-notes-offline` (fork: `meetings-v15-…`). This
  is a **distinct, monotonic** string; the attachment feature's later frontend
  change must bump to a *different* value (`v18`) — never reuse a value, or the SW
  won't re-cache. See Cross-feature coordination.
- Add `/static/notes-sync-logic.js` and `/static/notes-sync.js` to `SHELL_ASSETS`;
  add `/api/notes/export` + `/api/notes*` GETs to the runtime cache as a
  first-paint fallback (the IDB mirror remains the real offline source).

## Data flow

1. **Online steady state** — pull keeps the mirror fresh; writes flush immediately.
2. **Offline** — reads from mirror; note + task edits accumulate as `dirty` body
   edits; non-note endpoints error cleanly.
3. **Reconnect** — `online` → flush oldest-first; hash-mismatches become conflict
   copies; UI refreshes; server auto-tag bumps reconcile without conflict.
4. **Obsidian edits a note while offline** — next pull: server wins for non-dirty
   notes; if that note was also edited offline, the push 409s → conflict copy.

## Error handling

- **Network error mid-flush** → stop, keep `dirty`, retry next trigger; Web Locks
  (`ifAvailable`) prevents overlapping flushes across tabs.
- **409 conflict** → conflict copy + toast; never destructive.
- **Create id-remap failure** (crash between POST success and mirror write) → note
  keeps its temp id + `dirty`; next flush re-POSTs (at most a duplicate, never a
  loss). A client idempotency key on create is a deliberate follow-up.

## Testing (TDD, RED→GREEN)

- **Pure logic** — `tests/js/notes_sync_logic.test.mjs` (dependency-free
  `node --test`, run by **file path**, not directory — the Node 22/Windows quirk
  from the upload-queue work still applies):
  - `contentHash` matches the server's `sha1(title\0body)` on shared vectors.
  - `selectDirtyNotes` ordering; `resolvePush` applied/conflict/remap;
    `conflictCopyTitle`; `mergeServerList` (server wins for clean incl. new tags,
    dirty retains local).
  - `applyTaskEditToBody` — toggle, add, edit, delete, with inline metadata.
- **Server** — pytest (bare `TestClient`, monkeypatched `NOTES_DIR`):
  - `PUT` with matching `expected_body_hash` applies; stale → 409 + current
    record, file unchanged; absent → unchanged behavior.
  - A **tag-only** `apply_auto_tags` bump leaves `content_hash` unchanged, so a
    subsequent body push with the pre-bump hash still applies (regression guard
    for the false-conflict fix).
  - `GET /api/notes/export` returns bodies + `content_hash` and excludes anything
    under `attachments/`.

## Rollout

1. Server: `content_hash`, `expected_body_hash` guard, `/api/notes/export` + tests.
2. Client: mirror DB + sync engine + narrow reroute + task-body translation + SW
   bump.
3. Rebuild/redeploy the live container (static assets baked into the image).

## Cross-feature coordination (with the attachment-extraction spec)

These two features touch overlapping files; land them in this order to avoid
conflicts:

1. **Attachment Phase A** (backend-only: extraction, deps, Docker rebuild) — no
   `notes-tasks.js`/`sw.js` changes; orthogonal, can land first or in parallel.
2. **Offline sync (this spec)** in full — establishes the narrow `api()` reroute
   and bumps `sw.js` → `v17`.
3. **Attachment Phase B** (Analyze button) — rebases its `notes-tasks.js` edits on
   the rerouted helper, adds its new endpoints on the **pass-through** side, and
   bumps `sw.js` → `v18` (distinct value).
4. **Attachment Phase C** (backend meeting-context) — independent.

The content-hash token is **required** for offline
because Phase A's deferred extraction re-tags notes minutes after an edit — do not
ship offline keyed on `updated`.

## Open questions

None. Content-hash-on-409 is the agreed model; attachments-offline and
meeting-task edits offline are out of scope.
