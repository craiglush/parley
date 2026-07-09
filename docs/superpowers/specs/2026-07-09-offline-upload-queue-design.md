# Offline Upload Queue — Phase 1 Design

**Date:** 2026-07-09
**Branch:** `feat/offline-upload-queue`
**Status:** Approved for implementation

## Context

The meeting service is an installed PWA. Recording already works fully offline:
`MediaRecorder` captures in-browser and every chunk is autosaved to IndexedDB
(`capStartSession` / `capAppendChunk`, `app.js:63–248`), surviving crashes,
reloads, and tab closes with a 14-day retention. A best-effort streaming shadow
backup mirrors chunks to the server as they record (`app.js:250+`).

The single remaining gap is **upload**: it is manual and one-shot. The upload
logic lives inside the inline `uploadBtn` click handler (`app.js:1098`) as a
single `XMLHttpRequest`. If the device is offline when the user hits Upload,
`xhr.onerror` fires, the UI prints "Upload failed," and nothing retries. Pending
sessions are surfaced on page load by `checkPendingRecording()` →
`renderRecoveryList()` (`app.js:3755–3907`), but every recovery path is manual —
even "Recover & upload" only *stages* the file back into the upload box for the
user to review and re-submit.

This is a **single-user app** behind Authelia, so there is no multi-device write
conflict to resolve. Phase 1 is purely additive queueing — no conflict logic.

## Goal

Record offline, and have the recording **upload automatically when the device
regains connectivity**, without the user having to notice and re-click. The app
also remains fully openable and usable (record + manage the queue) while offline.

This is Phase 1 of a three-phase offline-first effort. Phases 2 (offline read of
recent meetings) and 3 (offline edit of notes/tags/speakers with replay) are
designed separately and are **out of scope here**.

## Core model: the explicit `queued` flag

The critical rule that keeps auto-upload safe:

> **A session auto-uploads only if the user explicitly submitted it (hit Upload)
> and the upload failed for a network reason.** Crash-recovered sessions that
> were never submitted stay in the existing manual recover/discard list.

Rationale: today, when a recording stops it is *staged* for review (title,
speaker count, trim) before the user hits Upload. Auto-uploading everything
pending on reconnect would bypass that review and could fire off a recording the
user meant to trim or discard. Gating on "user already hit Upload" preserves the
review step while still delivering hands-free retry.

Implementation: the session's IndexedDB `meta` record gains a boolean field
`queued`. It is set `true` at the moment an upload attempt fails with a network
error (offline, connection dropped, or fetch/XHR-level failure — *not* a 4xx
validation rejection, which is a permanent failure the user must fix). It is
cleared implicitly when the session is deleted on successful upload
(`capClear` / `capStreamDelete`, already wired at `app.js:1157`).

Eligibility rule for the flush loop:

- `queued === true` → auto-flush.
- everything else (never-submitted recovered drafts) → manual list, unchanged.

## The three implementation pieces

### 1. Extract a reusable uploader

Pull the upload body out of the `uploadBtn` click handler (`app.js:1098–~1230`)
into a standalone global function:

```
async function uploadSession({ blob, meta, sid, fromQueue })
  → resolves { ok: true, meetingId } on 202
  → resolves { ok: false, kind: 'validation', detail } on 400/413  (permanent)
  → resolves { ok: false, kind: 'network' }                        (retryable)
```

- The existing click handler builds the `FormData` (file, title, speakers,
  context, `speaker_tags`/`speaker_roster` from `liveTags`) and calls
  `uploadSession`. That FormData assembly moves into a small
  `buildUploadForm(session)` helper so the queue path produces an identical
  payload from the persisted `meta` (which already stores `roster`/`markers`).
- On a **network** failure, `uploadSession` sets `meta.queued = true` (persist via
  the existing meta-write path used by `capMarkStopped`/`capSaveTags`) instead of
  only printing "Upload failed." The UI message becomes
  "Offline — queued, will upload automatically when connected."
- On **success**, the existing cleanup runs unchanged (`capClear`,
  `capStreamDelete`, reset staged state, `refreshMeetings`, `startPolling`).
- On **validation** failure, behave as today (show the detail, do not queue).

The XHR-based upload is kept for the interactive path so the existing upload
progress bar still works; the queue path may use the same XHR wrapped in a
promise, or `fetch` (no progress UI needed for background flush). Keep one code
path where practical — wrap the XHR in a promise and share it.

### 2. The flush loop

```
let flushing = false;
async function flushUploadQueue()
```

- Guarded by the `flushing` flag so overlapping triggers (an `online` event that
  coincides with the periodic tick) cannot double-run.
- Enumerates `capLoadAllPending()`, filters to `meta.queued === true`, and
  uploads them **sequentially** (one at a time — avoids launching several GPU
  transcription jobs at once and avoids parallel-upload bandwidth contention).
- Skips the session the current tab is actively recording (already excluded by
  `capLoadAllPending` via `capSessionId`).
- A session that returns a **network** result stays queued and stops the current
  pass (no point hammering while still offline); a **validation** result clears
  `queued` and surfaces the error in the recovery list (it needs human action).
- After each success, updates the recovery UI so the row disappears live.

Triggers:

- **App init** — folded into the existing `checkPendingRecording()` call so a
  queued session from a previous session flushes on next open.
- **`window` `online` event** — `window.addEventListener('online', flushUploadQueue)`.
- **Periodic retry** — `setInterval(() => { if (navigator.onLine) flushUploadQueue(); }, 60_000)`.
  Covers the case where the browser reports online but the *server* is still
  unreachable (box rebooting, VPN reconnecting, Authelia redirect).

### 3. Visible queue status

`renderRecoveryList()` gains a distinct rendering for `queued` sessions vs
crash-recovered drafts:

- **Queued** row: "📤 Queued — uploads automatically when connected", a spinner
  while a flush pass is uploading it, and buttons **Upload now** (calls
  `flushUploadQueue` / direct `uploadSession`) and **Discard**.
- **Recovered draft** row: unchanged ("Recover & upload" / Download / Discard).

When a recording is stopped while offline and the user hits Upload, instead of a
dead "Upload failed" the file leaves the upload box and appears in the queue
region with the queued status.

## Out of scope for Phase 1 (explicit)

- **Service-worker Background Sync** (uploading while the tab is fully closed).
  Phase 1 flushes only while the app is open in a tab. Deferred.
- **Offline reading** of past meetings and **offline editing** — Phases 2 & 3.
- **Silent re-authentication.** On reconnect the Authelia SSO cookie may have
  expired, so the upload POST is bounced to a login redirect (typically surfaces
  as a network-level failure or an unexpected non-202). Phase 1 behavior: treat
  it as a retryable network failure, keep the session `queued`, and let the
  periodic retry catch it after the user has re-authenticated by opening the app.
  No data loss; just not-yet-uploaded. Robust re-auth is a Phase 2 concern.

## Duplicate-upload defense (post-review hardening)

Adversarial review found the original per-page `flushing` boolean insufficient for
invariant #4 in two ways, both now addressed client-side:

- **Cross-context race.** The installed PWA window and a browser tab share one
  IndexedDB; two per-page booleans don't coordinate. Fixed with a **device-wide
  Web Locks mutex** (`navigator.locks.request('meeting-upload-flush',
  { ifAvailable: true })`) so only one context flushes at a time; falls back to
  the per-page flag where Web Locks is unavailable.
- **Sequential re-upload after a failed local delete.** `capClear` swallows its
  IndexedDB errors, so a delete that doesn't commit would leave `queued===true`
  and the next tick would re-POST. Fixed with a page-lifetime `uploadedSids` set
  (a sid is recorded the instant it returns 202, *before* the delete) that
  `selectQueuedSessions` excludes, plus awaiting `capClear`.

**Residual — CLOSED by Phase 1.5 (server-side idempotency key on the sid).**
The client's guards can't cover a 202 lost in transit or a failed local delete
surviving a page reload. That is now handled server-side:

- `buildUploadForm` sends the capture `sid` with every recorded upload.
- `POST /meetings/upload` accepts `sid`, stores it as `source_sid` on the meeting,
  and **before** creating a new meeting scans for an existing one with the same
  `source_sid` — returning that meeting (202) instead of a duplicate. The
  scan-then-insert runs with no `await` between them, so concurrent uploads of one
  sid can't both create a meeting. A malformed sid is ignored (not rejected, not
  used for dedup, not stored).
- `POST /captures/{sid}/adopt` (server-copy recovery) now also stores `source_sid`
  and dedups the same way, so an adopt racing a local-blob flush of the same sid
  collapses to one meeting.

This makes recorded-session upload effectively exactly-once across every path
(interactive retry, queue flush, second tab, adopt). Tests: `test_meeting_routes`
(4 upload-idempotency cases) + `test_captures` (2 adopt cases). Disk-picked files
have no sid and are unaffected.

## Data-loss safety invariants

These MUST hold — they are the whole point of the feature:

1. A queued session's IndexedDB data (`meta` + `chunks`) and its server shadow
   copy are deleted **only** after a confirmed `202` from `/meetings/upload`
   (existing behavior — do not weaken it).
2. A **network** failure never deletes local data and never clears `queued`.
3. A **validation** (4xx) failure never deletes local data; it clears `queued`
   and hands the session back to the manual list for the user to fix.
4. The `flushing` guard prevents the same session being uploaded twice
   concurrently (which would create duplicate meetings).
5. Marking `queued` must not disturb the existing streaming shadow-backup or the
   14-day expiry logic.

## Testing strategy

The frontend is a monolithic browser global script with no existing JS test
runner; the backend `/meetings/upload` endpoint is **unchanged** by this work.

1. **Pure decision-logic unit test (dependency-free Node).** Extract the queue
   selection/ordering as pure helpers
   (`selectQueuedSessions(metas)` → filtered+ordered list) and test them with a
   small `tests/js/queue_logic.test.mjs` run via `node --test` (no npm deps).
   Cases: only `queued` selected; recovered drafts excluded; ordering stable;
   empty list; the actively-recording session excluded.
2. **Backend regression.** Run the existing `pytest` suite — must stay green
   (proves the upload contract and surrounding routes are untouched).
3. **Browser-driven smoke (verification, not committed test).** With the app
   running, simulate offline (DevTools/network emulation), record a short clip,
   hit Upload → confirm it lands in the queue with queued status and local data
   is retained; restore connectivity → confirm it auto-uploads, a meeting is
   created, and the queue row clears. This is the end-to-end proof the flow
   works, per the verify-before-completion principle.

## Files touched

- `static/app.js` — extract `uploadSession`/`buildUploadForm`, add
  `flushUploadQueue` + triggers, `queued` flag in the capture-store meta path,
  queued rendering in `renderRecoveryList`, pure `selectQueuedSessions` helper.
- `static/sw.js` — bump `CACHE_NAME` (cache-bust) so the updated app.js ships;
  no logic change in Phase 1 (write-caching is Phase 2).
- `tests/js/queue_logic.test.mjs` — new, dependency-free.
- `docs/superpowers/specs/2026-07-09-offline-upload-queue-design.md` — this doc.
