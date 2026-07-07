# Streaming capture failsafe (server shadow backup) — design

**Date:** 2026-07-07
**Status:** Implemented (2026-07-08)

## Problem

Autosave v2 (multi-session IndexedDB, shipped 2026-07-07 after a real data loss)
protects a recording against tab close, crash, reload, and record-over-the-top —
but **only on the device that recorded it**. Until the user presses Upload, the
sole copy of a meeting lives in one browser profile. Failures it cannot survive:
the laptop dying/being lost mid-meeting, profile/disk corruption, storage
eviction, or the user simply never reopening that browser. A long meeting
deserves a server-side copy *while it is being recorded*.

## Goal

Best-effort stream every recorded chunk to the meeting-service as it is
produced, so that at any moment the server holds a recoverable copy up to the
last-sent chunk. Zero change to the primary record → stage → upload flow; the
stream is a **shadow backup**, never the upload path. Recording must keep
working exactly as today when the server is unreachable (IndexedDB remains the
first line of defence).

## Design

### Client (`static/app.js`)

- `capStartSession()` additionally fires `POST /captures`
  `{sid, mimeType, source, startedAt}` (fire-and-forget).
- `mediaRecorder.ondataavailable` — alongside `capAppendChunk(blob)` — enqueues
  `{seq, blob}` on an in-memory send queue. A single-flight sender loop POSTs
  `/captures/{sid}/chunks/{seq}` (raw blob body). Retry with exponential
  backoff; after ~5 consecutive failures go quiet (keep queueing) and probe
  again every 60 s — the recorder and IndexedDB autosave are never blocked or
  slowed by network state. Chunks may arrive out of order; `seq` is
  authoritative.
- Stop → `POST /captures/{sid}/stop {durationLabel, fileName}` (after flushing
  the queue best-effort).
- Successful normal upload (202) or confirmed discard → `DELETE /captures/{sid}`
  (best-effort), mirroring `capClear(sid)`.
- **Recovery list**: `checkPendingRecording()` also fetches `GET /captures` and
  merges server-side pending captures into the banner (rows tagged
  "server copy", deduped against local sids). Their actions:
  - **Recover on server** → `POST /captures/{sid}/adopt {title?}` — the server
    assembles and queues processing directly, no re-upload of bytes.
  - **Discard** → `DELETE /captures/{sid}` (confirmed).
  Because the list is served by the backend, a capture streamed from the dying
  laptop is recoverable **from any device**.
- Settings toggle "Stream backup while recording" (default on), persisted with
  the existing settings mechanism.

### Server (`app.py`)

Staging layout: `MEETINGS_DIR/_captures/{sid}/meta.json` + `chunks/{seq:06d}.part`.

| Route | Behaviour |
| --- | --- |
| `POST /captures` | create staging dir + meta.json; 409 if sid exists |
| `POST /captures/{sid}/chunks/{seq}` | write chunk file; enforce per-chunk cap (2 MB) and per-capture cap (`MAX_UPLOAD_SIZE`); update meta `bytes`,`last_seq`,`updated_at` |
| `POST /captures/{sid}/stop` | mark stopped + store duration/fileName |
| `GET /captures` | list pending captures (sid, startedAt, stopped, bytes, chunk count) |
| `POST /captures/{sid}/adopt` | concat chunks in seq order → `_upload_{new_id}.{ext}` → create meeting + `process_meeting` (same shape as `/trim`); delete staging on success |
| `DELETE /captures/{sid}` | remove staging dir |

- `sid` is validated (`^[A-Za-z0-9-]{8,64}$`) and never used outside
  `_captures/` (path-traversal safe).
- Concatenating MediaRecorder chunks in order yields a valid stream — identical
  to the client's `new Blob(chunks)` recovery, proven by autosave v2.
- **GC**: on startup and daily, prune captures with `updated_at` older than
  14 days (mirrors client `CAP_MAX_AGE_MS`); log what is pruned. Also prune
  adopted/empty leftovers.
- Auth surface unchanged: same-origin endpoints behind Authelia/Caddy.

### What this buys (failure matrix)

| Failure mid/post-recording | autosave v2 | + streaming |
| --- | --- | --- |
| Tab close / crash / reload / record-over | recovered | recovered |
| Machine dies, disk lost, profile wiped | **lost** | recovered to last sent chunk |
| Never reopening that browser | effectively lost | recoverable from any device |
| Server down while recording | recovered (IDB) | recovered (IDB); stream resumes when back |

### Non-goals (v1)

- Not a resumable-upload replacement for the normal upload path.
- No live/incremental transcription of in-progress captures.
- No cross-device *live* handoff (recover happens after stop/crash, not mid-take).

## Testing

- Unit (`tests/`): chunk write → adopt assembles bytes in seq order (mocked
  `process_meeting`); size caps enforced (413); sid validation (400); GC prunes
  only stale; adopt of a never-stopped capture works (crash case); DELETE on
  upload success.
- E2E: record 30 s with streaming on → kill the tab mid-recording → `GET
  /captures` lists it → adopt from another browser → meeting processes; record
  with the service stopped → local recovery still intact, no recorder errors;
  restart service mid-recording → sender resumes without user action.

## Estimate

~200 lines server + tests, ~120 lines client. One focused session.
