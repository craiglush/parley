# In-Meeting Notes & Attachments — Design

**Date:** 2026-07-15
**Branch:** `feat/in-meeting-notes`
**Status:** Approved for implementation (user-confirmed design)
**Depends on:** batch-1 **offline notes sync** (the `NotesSync` mirror — spec
`2026-07-15-offline-notes-tasks-sync-design.md`) and **attachment Phase C**
(meeting-context folding — spec `2026-07-15-attachment-extraction-ai-design.md`).
This feature lands **after** both; where it references their interfaces it cites
those specs/plans, not code.

## Context

The recording panel (`static/index.html:89–170`: record area → viz row with the
live-speaker chips → `uploadFields`) is owned by `static/app.js`. Every recording
gets a capture **session** in IndexedDB (`CAP_DB = 'meeting-capture'`,
`app.js:69`) whose meta record already accumulates state via small
read-modify-write helpers: `capSaveTags` (roster/markers, `app.js:190`) and
`capSetQueued` (queued flag + title/speakers/context snapshot, `app.js:211`).
The interactive Upload and the offline queue flush share one payload builder,
`buildUploadForm` (`app.js:1141`), and one attempt path, `uploadSession`
(`app.js:1195`); `flushUploadQueue` (`app.js:1272`) retries queued sessions on
`online`/interval triggers (`app.js:4141–4142`).

Server side, `upload_meeting` (`app.py:2149`) already takes an **optional `sid`
form field** for idempotency — the Phase 1.5 precedent for adding an optional
form field: a malformed value is ignored, not rejected, and a duplicate sid
early-returns the existing meeting (`app.py:2187–2195`). `capture_adopt`
(`app.py:4205`) assembles a streamed capture into a meeting, reads
title/context/tags fallbacks from the server capture meta, and has the same
sid-dedup early-return (`app.py:4221–4228`). `capture_tags` (`app.py:4153`)
mirrors the live roster/markers/title/context onto that server meta while
recording (posted by `liveTags._flush`, `app.js:495–516`).

Notes are vault markdown files: `notes_store.link_meeting`
(`notes_store.py:433`) toggles a meeting id in a note's `linked_meetings`
frontmatter and is **idempotent** (membership check before append,
`notes_store.py:439–442`). `find_path` resolves a note id purely through the
index — the raw id is never joined into a filesystem path
(`notes_store.py:227–233`). Real note ids are `"n_" + uuid4.hex[:12]`
(`new_note_id`, `notes_store.py:56`). `api_link_meeting` (`app.py:4896`) is the
precedent for calling `notes_store` synchronously from an async handler.
Attachments upload via `POST /api/notes/{note_id}/attachments`
(`api_add_attachment`, `app.py:5192`), returning a markdown `embed` string.

**Batch-1 interfaces this feature consumes** (cite: offline-sync spec +
`docs/superpowers/plans/2026-07-15-offline-notes-tasks-sync.md`, Task 9):
`window.NotesSync.createNote({title, folder, type, body})` creates a mirror
note **offline** under a temp id `n_local_<rand>`; `updateNote(id, {title?,
body?})` edits it; `flush()` pushes creates and **remaps** temp id → server id
(surfaced today only via the `notes-tasks.js`-owned `onRemap` hook);
`readNote(id)` reads from the mirror. A temp id can never collide with a real
id (`local` is not hex). The meetings page and the Notes surface are **one
page** — `index.html` loads `notes-tasks.js` (line 22, `defer`) and
`queue-logic.js`/`app.js` (lines 882–883), and batch-1 inserts the sync scripts
on the same page — so `window.NotesSync` is available to the capture UI, but
only **after** deferred scripts run: `app.js` must feature-detect it lazily at
event time, never at top level.

**Attachment Phase C** (cite: attachment spec Phase C +
`docs/superpowers/plans/2026-07-15-attachment-meeting-context-phase-c.md`,
Tasks 2–4) already folds every **explicitly linked** note's body + attachment
text into meeting analysis via `_gather_meeting_context` →
`step_summarize(..., context=…)` at both pipeline call sites. **This feature
adds no new AI wiring** — creating the link is the entire AI integration.

`sw.js` currently pins `CACHE_NAME = 'meetings-v16-offline-queue'`
(`sw.js:1`); the batch-1 coordination plan assigns `v17` (offline sync) and
`v18` (attachment Phase B).

## Goal

Take notes and attach files **during** a meeting recording, in the recording
panel itself, and have them ride the meeting through analysis: the first
keystroke creates a **real vault note** (offline-capable, Obsidian-visible),
attachments go onto that note, and the note is **auto-linked to the meeting on
upload** — including uploads that flush later from the offline queue — so Phase
C folds the notes + attachment text into the meeting's AI analysis with zero
extra user action.

## Non-Goals

- **Offline attachment upload** — excluded by both batch-1 specs; the attach
  button is online-only and says so.
- **New AI wiring** — Phase C already consumes `linked_meetings`; nothing here
  touches prompts, passes, or the pipeline.
- **Editing the note from the meeting page after upload** — post-meeting
  editing happens in the Notes surface (or Obsidian), which already exists.
- **Renaming the note when the meeting title changes later** — the title is set
  at creation, upgraded once at Upload (see auto-title), and never chased.
- **Two-way live sync between the capture textarea and an open Notes editor** —
  single user; the mirror's normal conflict handling is the safety net.

## Core model: note-first capture, link rides the upload

The note is **not** a draft owned by the capture UI — from the first keystroke
it is a real vault note living in the batch-1 mirror (temp id offline, real id
seconds later when online). The capture session meta stores `note_id` exactly
like it stores roster/title/context, so the association survives reload, crash,
and the offline queue for free. The link itself is made **server-side**: a new
optional `note_id` form/body field on `POST /meetings/upload` and
`POST /captures/{sid}/adopt` triggers one idempotent
`notes_store.link_meeting` call after the meeting is created. Because the
queued upload replays the persisted meta, **offline note→meeting linking rides
the existing upload queue with no new queue machinery**.

**Temp-id rule:** a temp id (`n_local_` prefix) is never sent to the server in
any payload. Senders resolve it (flush the note, then re-read the rewritten
meta) or omit the field.

**Auto-title convention** (pure, testable):
`captureNoteTitle(title, context, isoDate)` →
`"<base> — notes (YYYY-MM-DD)"` where `base` = trimmed meeting title, else
trimmed meeting context, else `"Meeting"`. Examples:
`"Sprint Planning — notes (2026-07-15)"`, `"Meeting — notes (2026-07-15)"`.
Evaluated at first keystroke. If the note was created with the bare fallback
(no title/context typed yet) and the user has filled the title by the time they
hit Upload, the note is **retitled once** via `NotesSync.updateNote` (a mirror
op, so it also works offline); after that the title is never touched again.
The retitle changes the note's **frontmatter title only** — the vault `.md`
filename keeps its creation-time slug (`update_note` rewrites the existing
path, `notes_store.py:266–279`; file renames are the separate online-only
`/api/notes/{id}/rename` endpoint, `app.py:5155`, outside the mirror).
Accepted explicitly: Obsidian's file explorer shows the slug-derived filename
(e.g. `meeting-notes-2026-07-15.md`), while the displayed title is correct in
the Notes UI and in the note content itself.

## Architecture

### Client — UI sketch (`static/index.html`)

A collapsible **Notes** section inserted between the viz row
(`cb-viz-row`, `index.html:124–139`) and the `captureWarning` div
(`index.html:142`). Hidden until recording starts (or a local session with a
`note_id` is recovered — see the Recovery bullet); stays visible while a
recording is staged; hidden + cleared after upload, remove, **or
offline-queueing**. Not shown for disk-picked files (no capture session) or
when `window.NotesSync` is absent.

```
┌─ ▸ Notes ─────────────────────── Saved · Sprint Planning — notes (2026-07-15) ┐
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │ (textarea) Type meeting notes… saved straight to your vault              │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│  [ 📎 Attach ]   Attachments need a connection — notes save offline.          │
└────────────────────────────────────────────────────────────────────────────────┘
```

- Header row = toggle button (chevron + "Notes") plus a status span
  (`captureNoteStatus`): empty → `Saved · <note title>` once created →
  `Saved locally · syncs when online` while the create is unflushed/offline.
  Collapsed/expanded state persists in `localStorage('captureNotesOpen')`
  (same pattern as `streamBackupEnabled`, `app.js:286`).
- Footer: an **Attach** button + hidden `<input type="file">` + a hint span.
  Attach is disabled (with the hint shown) when offline.
- Names/titles rendered via `textContent` only — same XSS rule as the live-tag
  chips (`app.js:389`).

### Client — behavior (`static/app.js` + new `static/capture-notes-logic.js`)

**New pure module `static/capture-notes-logic.js`** (dual browser-global +
CommonJS export, exactly like `queue-logic.js`):
- `captureNoteTitle(title, context, isoDate)` — the convention above.
- `isTempNoteId(id)` — true iff the id has the `n_local_` prefix (batch-1 plan
  Task 9's documented temp-id shape); used to enforce the temp-id rule.

Loaded as a plain `<script>` before `app.js` (next to `queue-logic.js`,
`index.html:882`), cache-busted `?v=1`; `app.js` bumps to `?v=16`.

**Capture-notes state in `app.js`** — module vars `captureNoteId`,
`captureNoteCreate` (in-flight create promise, single-flight guard) and
`captureNoteTitleAuto` (bool: created with the bare fallback title):

- **Show/hide**: `setupRecorderFromStream` first **unconditionally resets**
  the capture-notes state (`captureNoteId`, `captureNoteCreate`,
  `captureNoteTitleAuto`, textarea, status span) — belt-and-braces so stale
  state from any earlier path can never bleed into a new recording — then
  shows the section. Three paths hide and reset it: upload-success, the
  remove/discard handler, and the **offline-queued branch** of the upload
  handler (network failure → session queued, `app.js:1359–1379` — the third
  form-clear path; it already clears the rest of the staging UI and must
  clear the Notes section with it, or back-to-back offline meetings would
  append meeting B's notes to meeting A's note). Resetting the UI state never
  touches the data: the queued session's meta keeps its `note_id`, and the
  note itself stays safe in the vault/mirror. Discarding a recording **never
  deletes the note** — it is a real vault note.
- **First keystroke** (`input` on the textarea, when `captureNoteId` is null
  and no create in flight): `NotesSync.createNote({title:
  captureNoteTitle(...), folder: 'Meetings', type: 'note', body: text})`
  (`create_note` mkdirs the folder server-side, `notes_store.py:250–251`).
  Store the returned (temp) id in `captureNoteId`, persist it with a new
  `capSetNoteId(sid, noteId)` helper — the same read-modify-write shape as
  `capSaveTags` — against `capSessionId || stagedSessionId`.
- **Subsequent keystrokes**: debounced (~600 ms) `NotesSync.updateNote(id,
  {body})`. The mirror owns flushing; the capture UI never talks to
  `/api/notes` directly. If `updateNote` rejects with the mirror's
  "note not in mirror" error while `captureNoteId` is a temp id, another
  context (installed PWA vs browser tab) won the notes-sync flush lock and
  remapped the id — the `notes-sync:remap` CustomEvent is window-local, so
  this context never heard it. Recovery: re-read the session's IDB meta
  (IDB **is** shared, and the flushing context's `capRewriteNoteId` has
  rewritten it), adopt the new id into `captureNoteId`, and retry the update
  once.
- **Attach click**: if no note yet, create one first (empty body is fine). If
  `isTempNoteId(captureNoteId)`, `await NotesSync.flush()` and re-check (the
  remap listener below rewrites state). Still temp or offline → show the hint
  and stop. Otherwise open the picker, `POST
  /api/notes/{id}/attachments` (FormData), and on success append the returned
  `embed` markdown to the **textarea** — the debounced `updateNote` then
  persists it, keeping the body the source of truth for attachment
  association (attachment spec: `note_attachments` parses body references).
  413 → "Attachment too large" hint.
- **Remap listener**: on `notes-sync:remap` (below), if `detail.tempId`
  matches `captureNoteId`, swap in `detail.serverId`; always run
  `capRewriteNoteId(tempId, serverId)` — iterate every capture meta record and
  rewrite matching `note_id`s (covers queued sessions from earlier recordings)
  — then re-post the tags mirror (next bullet). The listener pushes each
  in-flight `capRewriteNoteId` promise into a module-level
  **`captureNoteRemapPending`** array so `uploadSession` can await the IDB
  rewrite itself — the event dispatch is synchronous but the listener's
  read-modify-write is async, so dispatch alone proves nothing about when the
  rewrite commits.
- **Tags mirror**: the `liveTags._flush` payload gains `note_id`, included
  **only when `captureNoteId` is a real id**. The remap listener re-posts the
  full payload with an explicit sid (works for the staged session too, where
  `capSessionId` is already null) so the server capture meta learns the note
  id — this is what makes dead-device `adopt` linking possible at all, since
  a server-only capture by definition has no local meta left.
- **Upload payload**: `buildUploadForm` appends `note_id` when
  `session.note_id ?? meta.note_id` is a real id. Temp ids are omitted
  **client-side** — a new rule per the temp-id rule, not a mirror of existing
  behavior; the server additionally ignores unknown/malformed ids, which is
  what mirrors the malformed-sid precedent (sids are ignored server-side, not
  withheld by the client). The **uploadBtn click handler sets
  `session.note_id = captureNoteId`** when it builds the session object — on
  both the live-recording and the staged-file path — so the interactive path
  carries the id even though its synthetic meta (`{mimeType, fileName}`) has
  none; `capSetQueued` copies `session.note_id` into meta alongside
  title/context (one more line in its existing copy block), which is how the
  queued path persists it. `uploadSession` gains a resolve step before
  building the form: if the effective note id is temp and `NotesSync` exists,
  run the **awaited barrier**
  `await NotesSync.flush(); await Promise.all(captureNoteRemapPending.splice(0));`
  — `flush()` resolves without waiting for its `notes-sync:remap` listeners,
  so the second await is what guarantees the listener's async IDB rewrite has
  committed — then re-read the session's meta. **Last-resort guard**: if the
  effective id is *still* temp after the barrier (the note flush failed),
  omit `note_id` from the form but **leave the queued metadata's `note_id` in
  place**, so a later retry — or the sid-dedup link-repair path — can still
  make the link.
- **Recovery**: when a local session is recovered into the staging box
  (`renderRecoveryList` local rows), **show the Notes section** — a second
  show trigger: recovery stages via `selectFile()` and never calls
  `setupRecorderFromStream` (`app.js:4020–4028`), so without this the section
  would stay hidden in exactly the state it must be usable in — then restore
  `captureNoteId` from `meta.note_id` and repopulate the textarea via
  `NotesSync.readNote`. Without this, typing after a recovery would silently
  create a second note.
- **Upload success / silent notes**: on 202 the section resets with the rest of
  the form (`app.js:1341–1352`). If the user never typed, no note ever exists.

### Client — one additive hook in `notes-sync.js`

Batch-1's create-flush remap path calls the `notes-tasks.js`-owned
`hooks.onRemap(tempId, serverId)` (batch-1 plan, Task 9) — a single consumer.
The capture UI needs the same signal, so this feature adds **one line** in that
same branch:

```js
window.dispatchEvent(new CustomEvent('notes-sync:remap',
  { detail: { tempId: entry.id, serverId: rec.id } }));
```

Additive and hook-independent. The CustomEvent itself is **window-local** —
only the flushing context's listeners fire — but IndexedDB is shared across
contexts, so whichever tab runs the flush rewrites the shared capture **meta**
for all of them; a non-flushing context's in-memory `captureNoteId` catches up
via the updateNote-failure retry (see the Subsequent-keystrokes bullet).

### Server (`app.py`)

- **`upload_meeting` gains `note_id: Optional[str] = Form(default=None)`** —
  the same optional-form-field pattern as `sid`. After the meeting is inserted
  and `_save_index()` runs, and **before**
  `asyncio.create_task(process_meeting(...))` (so the link exists before Phase
  C's `_gather_meeting_context` could ever look — belt-and-braces on top of
  transcription taking minutes anyway), call
  `await asyncio.to_thread(notes_store.link_meeting, notes_store.NOTES_DIR,
  note_id, meeting_id, True)` — **on the threadpool, not inline**:
  `link_meeting → find_path → get_index` can trigger a synchronous full index
  rebuild (seconds over the bind mount, per `notes_store`'s own `_invalidate`
  notes), and `upload_meeting` is the hot path offline-queue flushes and
  mobile reconnects hit, so an inline call could stall the event loop and
  block every concurrent request. The heavier note endpoints already offload
  the same way (`app.py:4788/4810`); `api_link_meeting` (`app.py:4896`)
  remains the precedent for calling `notes_store` from a handler, but it is a
  small user-initiated call and tolerates the inline cost this hot path must
  avoid. Wrapped so it can never fail the upload: unknown note → `None` → log
  a warning and continue. No format validation beyond a 64-char length cap — the id is only
  ever an index key (`find_path` never joins it into a path), so malformed and
  temp ids simply miss the lookup, mirroring the ignored-malformed-sid rule.
- **Dedup path also links.** The sid early-return now runs the same idempotent
  (threadpool-wrapped) link call before returning the existing meeting. This repairs the one lossy
  edge (first attempt uploaded before the note had flushed; retry now carries
  the real id) and can never double-link — `link_meeting` checks membership
  (`notes_store.py:439`).
- **`capture_adopt`**: `CaptureAdoptRequest` gains `note_id: Optional[str] =
  None`; the effective id is `body.note_id or meta.get("note_id")`, mirroring
  the existing title fallback (`app.py:4244`). Same link-after-insert,
  same dedup-path repair. The existing recovery UI keeps posting `{}`
  (`app.js:4079–4083`) — the meta fallback does the work.
- **`capture_tags`**: `CaptureTagsRequest` gains `note_id: Optional[str] =
  None`; the handler stores it **only when present**
  (`if body.note_id is not None: meta["note_id"] = body.note_id`) so the
  frequent tag-only re-posts from `liveTags._flush` can never wipe a
  previously mirrored note id. Roster/markers overwrite semantics are
  unchanged.

### Service worker + cache busts

`sw.js` `CACHE_NAME` → the **next unused monotonic value at land time**
(nominally `meetings-v19-capture-notes`), checked against `sw.js:1` **and any
sibling that landed first**: batch-1 reserves `v17`/`v18`, and three other
2026-07-15 specs (filler-removal, edit-everything, company-tag) each nominate
a `v19-*` name, so `v19` may already be taken — distinct values, never
reused, per the batch-1 coordination rule. Add
`/static/capture-notes-logic.js` to `SHELL_ASSETS`.

## Offline behavior matrix

| Action during/after recording | Online | Offline |
|---|---|---|
| Type notes text | Mirror write → real vault note within seconds (create flush + remap) | Mirror write under temp id; syncs on reconnect (batch-1 machinery) |
| Attach a file | Uploads; `embed` appended to note body | Button disabled + hint; note text keeps saving |
| Upload & Process | `note_id` form field → server links immediately | Upload queued with `note_id` in meta; link happens when the flush lands (awaited-barrier temp-id resolve) |
| Tags mirror carries `note_id` | Posted once the id is real | Skipped (temp ids never leave the device) |
| Discard recording | Note survives in the vault | Same |

## Data flow

1. **Online meeting** — keystroke → mirror note (temp) → batch-1 flush →
   remap event → `captureNoteId` + capture meta + server capture meta all
   updated to the real id. Attach works. Upload sends `note_id`; server links;
   Phase C folds note body + attachment text into analysis.
2. **Offline meeting** — keystroke → mirror note (temp id); attach disabled;
   Upload fails → session queued with the temp id persisted, and the Notes
   section hidden + reset (the third form-clear path — the note stays in the
   mirror). On reconnect the notes flush and the upload flush are ordered by
   an **awaited barrier**, not left to race: `uploadSession` resolves the temp
   id itself — `await NotesSync.flush()`, then
   `await Promise.all(captureNoteRemapPending.splice(0))` so the remap
   listener's IDB rewrite has provably committed — then re-reads the meta and
   sends the real id. Server links on the queued upload. If the id is still
   temp (the note flush failed), `note_id` is omitted from the form but stays
   in the queued meta for the next retry / dedup repair.
3. **Device dies mid-meeting** — audio chunks are already on the server
   (streaming shadow backup) and the tags mirror carried the note id once it
   was real. Another device adopts: `capture_adopt` links via the server
   meta's `note_id`.
4. **Retry after lost 202** — sid dedup returns the existing meeting and
   re-runs the idempotent link (repairing it if the first attempt predated the
   note's flush).

## Error handling

- **Note create/update fails** (mirror/IDB error): toast, capture untouched —
  notes are strictly best-effort next to the recording, same doctrine as
  `capAppendChunk`.
- **`window.NotesSync` absent** (batch-1 not deployed / script failed): the
  Notes section never renders; zero behavior change elsewhere.
- **Temp id unresolved at upload time** (server reachable for the meeting but
  the note flush failed): upload proceeds **without** `note_id`, and the
  queued metadata's `note_id` is deliberately left in place — the meeting
  (GPU work) is never held hostage to a note link. The link can be made
  manually in the Notes UI (`api_link_meeting` already exists); the dedup-path
  repair also catches a retried upload, which will carry the by-then-real id
  from the untouched meta. Accepted, tiny window (both flushes need the same
  network).
- **Server can't find `note_id`**: warn + continue; upload/adopt never fail on
  linking.
- **Note edited in Obsidian mid-meeting**: batch-1's content-hash conflict
  copy applies unchanged; the capture meta keeps pointing at the original note
  (the conflict copy is a new note), which stays the linked one.

## Idempotency

- Re-upload with the same `sid` → dedup early-return; the link call on that
  path is safe because `link_meeting` is a membership add — **the note's
  `linked_meetings` can only ever contain the meeting id once**, however many
  retries, flush races, or adopt/upload overlaps occur.
- Adopt after a blob-flush upload (and vice versa) → existing sid dedup plus
  the same idempotent link.
- Fast typing can't double-create the note — single-flight create promise.
- `capSetNoteId` / `capRewriteNoteId` are read-modify-write on the meta store,
  the established `capSaveTags` pattern.
- `uploadSession`'s temp-id resolve is an **awaited barrier** (`flush()` plus
  the pending `capRewriteNoteId` promises in `captureNoteRemapPending`), not a
  race — the re-read meta is guaranteed post-rewrite regardless of which
  `online`-triggered flush fired first.

## Testing (TDD, RED→GREEN)

**Server** — pytest, bare `TestClient`: reuse the `tests/test_meeting_routes.py`
`_client(tmp_path, monkeypatch)` harness with `process_meeting` stubbed (as in
`tests/test_captures.py:98–109`) and `monkeypatch.setattr(notes_store,
"NOTES_DIR", tmp_path)` (as in `tests/test_notes_api.py:36`):

- Upload with `note_id` → 202 and the note record's `linked_meetings` contains
  the new meeting id (`notes_store.read_note`).
- Upload with an unknown/malformed/temp `note_id` → still 202, no link, no
  error.
- **Idempotency:** second upload with the same `sid` + `note_id` → same
  meeting id, `linked_meetings` has exactly one entry.
- **Dedup repair:** first upload with `sid` and no `note_id`, retry with
  `note_id` → the dedup path links the existing meeting.
- Adopt with `note_id` in the body → linked; adopt with `{}` after `POST
  /captures/{sid}/tags` carried `note_id` → linked via meta fallback; adopt
  dedup path idempotent.
- `capture_tags` stores `note_id` and a subsequent tags-only post (no
  `note_id` key) leaves it intact.

**Pure client logic** — `tests/js/capture_notes_logic.test.mjs`,
dependency-free `node --test` run **by file path** (the Node-on-Windows quirk
from the upload-queue work: directory runs don't discover the file):

- `captureNoteTitle`: title wins; context fallback; bare fallback
  `"Meeting — notes (YYYY-MM-DD)"`; trimming; date rendering from the ISO
  string.
- `isTempNoteId`: `n_local_ab12cd34ef` → true; `n_1a2b3c4d5e6f` → false;
  `''`/`null` → false.

The DOM/IDB-bound wiring in `app.js` and the one-line `notes-sync.js` event are
validated by `node --check` on every touched script plus a manual E2E pass
(record online → note appears in the Notes surface and Obsidian; record in
DevTools-offline → reconnect → note synced, meeting uploaded and linked;
attach a file online; adopt path with a mirrored `note_id`).

## Rollout

1. Land **after** batch-1 offline sync (provides `NotesSync`) — attachment
   Phase C only gates the AI payoff, not the linking, but per the shared
   coordination order it will already be in.
2. Server first: three request-model/handler changes + link helper + tests.
3. Client: `capture-notes-logic.js`, `app.js` capture-notes section, the
   `notes-sync.js` remap event, `index.html` markup + script tag +
   cache-busts, `sw.js` → the next free `CACHE_NAME` per the rule above.
4. Rebuild/redeploy the live container (static assets are baked into the
   image; no new Python deps).
5. Port to `craiglush/parley` after the batch-1 ports, in the same order
   (expect capture-UI divergence; hand-port rejected hunks as before).

## Open questions

None blocking. Deferred by choice: renaming the note if the meeting title is
edited after upload (Notes UI handles it); a richer notes editor in the capture
panel (the vault note is one click away in the Notes surface).
