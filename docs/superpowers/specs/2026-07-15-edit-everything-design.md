# Edit Everything — Pencil-Icon Inline Editing Across Meeting Surfaces — Design

**Date:** 2026-07-15
**Branch:** `feat/edit-everything`
**Status:** Approved for implementation

## Context

A completed meeting's text lives in **files under the meeting's `output_dir`**,
with a thin in-memory mirror. The pipeline writes `transcript.json` /
`transcript.srt` / `transcript.md` and `summary.json` / `summary.md`
(`process_meeting` storage step, `app.py:1984–2019`), then stores vectors in
Qdrant (`store_in_qdrant` call, `app.py:2024–2027`) and caches `m["summary"]` +
`m["transcript_text"]` on the meeting record (`app.py:2050–2051`). The
`meetings` dict is persisted to `MEETINGS_DIR/index.json` by `_save_index()`
(`app.py:717–729`); the on-disk folder name is derived from the title **once at
creation** (`_meeting_dir`, `app.py:705–712`) and thereafter referenced only via
the stored `output_dir`.

Today, almost none of this text is editable after processing:

- **Title** — no update endpoint exists (verified: the only meeting-mutating
  routes are tags, notes, links, insights, speakers, trim, retry, reprocess —
  grep of `@app.put/@app.patch` over `app.py`). The LLM-chosen title is written
  by the pipeline (`app.py:1944–1945`) and again by reprocess-summarize
  (`reprocess_meeting`, `app.py:4426–4427`).
- **Summary sections** — `GET /meetings/{id}/summary` serves `summary.json`
  verbatim (`meeting_summary`, `app.py:2570`); there is no write path. A code
  comment even assumes "summary.json is immutable post-processing"
  (`app.py:5008`) — already soft, since reprocess-summarize rewrites it.
- **Transcript segments** — `GET /meetings/{id}/transcript` serves
  `transcript.json` (`meeting_transcript`, `app.py:2552`); segment text has no
  write path. The transcript UI's only per-segment button is **annotate**
  (`.seg-annotate-btn`, glyph `&#9998;` ✎, `app.js:3074`) which creates a note,
  not an edit.
- **Speakers** — editing **already works**: `PUT /meetings/{id}/speakers`
  (`update_speakers`, `app.py:3555`) renames speakers across
  `transcript.json/.srt/.md`, `summary.json/.md`, and `speaker_info.json`, and
  accepts per-speaker `speaker_details` name/company/title edits
  (`app.py:3552`, `3717–3732`). But the affordance is invisible: you must know
  to click a speaker chip (`renderSpeakerMapBar` → `onclick="editSpeakerName(…)"`,
  `app.js:2769`; popover at `app.js:2802`).

**Vectors.** All meeting search/RAG reads Qdrant collection `meetings`
(`COLLECTION_NAME`, `vector.py:29`). `store_in_qdrant` (`vector.py:126`) embeds
transcript chunks, action items, decisions, questions, concerns, figures, and
the summary text — and stamps the meeting **title into every point's payload**
(`vector.py:148–158` etc.). Points get fresh UUIDs and are **appended** by
`upsert` (`vector.py:256–266`) — calling it twice duplicates points, so
re-embedding must delete-by-`meeting_id`-filter first (the pattern
`delete_meeting` already uses, `app.py:2754–2761`). Notably, **reprocess never
re-indexes**: `reprocess_meeting` rewrites `summary.json` (`app.py:4429`) but
leaves stale vectors — a gap this feature must close, or "edit segment →
re-run analysis → chat about it" would answer from the old summary.

**Downstream readers that must see edits:** meeting chat RAG reads Qdrant
payload text (`chat_with_meetings`, `app.py:5471–5487`); related-notes reads
`m["title"]` + `m["summary"]` from memory (`app.py:3084–3085`); exports serve
the on-disk files (`meeting_file`, `app.py:2587`); the To-Do list projects
`summary.json` `action_items` **by index** with a mutable overlay keyed on that
index (`task_overlay.json`; `_collect_meeting_tasks` /
`_require_meeting_index`, `app.py:5006–5110`).

**Guard precedent:** task-line edits use an optional `expected_text` compare
that 409s when the line changed underneath the client (`TaskToggle`,
`app.py:4661–4665`; used at `app.py:5029–5037`). Fire-and-forget background
indexing uses `_run_bg` (`app.py:4722`) as in `_index_note_safe`
(`app.py:4734–4746`).

This is a **single-user** app behind Authelia; two batch-1 features are being
implemented on this branch concurrently (offline notes/tasks sync + attachment
extraction — see their specs under `docs/superpowers/specs/2026-07-15-*.md`).
They own `notes-tasks.js`; this feature owns the meeting-detail surface in
`static/app.js`. Shared files, precisely: `static/sw.js` (`CACHE_NAME`,
currently `meetings-v16-offline-queue`, `sw.js:1`); **`app.py`** — attachment
Phase C rewrites the `step_summarize` call sites at `app.py:1943–1945` and
`4425–4427` (adding `context=…`), the same statement blocks whose adjacent
lines this spec's title-clobber gate edits (`app.py:1944–1945`, `4426–4427`) —
a known textual merge collision, see Rollout — and offline sync also touches
`app.py` (`NoteUpdate`/`content_hash` at `app.py:4650`, `/api/notes/export`).
`app.js` regions are disjoint.

## Goal

Every piece of meeting text the user reads is editable where they read it, via
a visible pencil affordance: **(1)** meeting title, **(2)** summary sections
(summary text, action items, decisions, concerns, open questions),
**(3)** transcript segment text — with a visible **"Re-run analysis"** button
after transcript edits, wired to the existing reprocess-summarize step — and
**(4)** speaker name/company/title (existing endpoint, made discoverable —
plus the re-index call it lacks today). All edits persist to the **same files
the pipeline writes**, and re-embed the meeting's vectors, so export, chat
RAG, search, and related-notes all see the edited text.

## Non-Goals

- **Editing topics, figures, sentiment, or tags** — tags already have an edit
  UI (`PUT /meetings/{id}/tags`, `app.py:2817`); the rest is low-value
  derivative text. The five approved summary fields only.
- **Adding/deleting/reordering summary items or segments** — edit-in-place
  only. Action items already have dismiss via the To-Do overlay
  (`DELETE /api/meetings/{id}/tasks`, `app.py:5143`); segment structure is
  owned by the pipeline (trim/reassign/merge exist for that).
- **Re-uploading edited summaries to the OpenWebUI knowledge base** — the
  pipeline's `_upload_to_openwebui` (`app.py:2097`) is a one-shot best-effort
  push; speaker edits don't re-push today either. Out of scope, consistent.
- **Offline meeting edits** — meeting-detail endpoints stay online-only,
  failing cleanly, exactly like meeting tasks in the offline-sync spec's
  Non-Goals.
- **Renaming the meeting's on-disk folder on title change** — `output_dir` is
  the stable pointer everything holds; the folder name is a creation-time slug.

## Core model: files are the source of truth; every text edit re-indexes

Each edit endpoint does the same four things. Steps 1, 2, and 4 mirror what
`update_speakers` already does for renames (`app.py:3595–3740`); step 3
(re-embed) exists **nowhere today** — not even in `update_speakers` — and this
feature adds it everywhere, including retrofitting `update_speakers` itself
(§4):

1. **Rewrite the pipeline's files atomically** (`_atomic_write`), regenerating
   derived files (`transcript.srt`/`transcript.md` via `_generate_srt` /
   `build_transcript_markdown`; `summary.md` via `build_summary_markdown` —
   same regeneration sites as `app.py:3615–3627` and `app.py:3684–3688`).
2. **Update the in-memory meeting record** (`m["title"]`, `m["summary"]`,
   `m["transcript_text"]`) and `_save_index()` — so `/meetings`, related-notes
   (`app.py:3084–3085`), and restarts see the edit.
3. **Re-embed** via a new `_reindex_meeting_safe(meeting_id)` (below),
   fire-and-forget so the HTTP response never waits on the GPU embedder.
4. **Guard**: `404` unknown meeting, `409` when `status != complete` (the same
   check every existing edit endpoint uses, e.g. `app.py:3562–3563`) — which
   also blocks edits while a reprocess is running, since reprocess moves the
   status off `complete` for its duration (`app.py:4322`, `4421`, restored at
   `4448/4459`).

### Endpoint shape: per-surface endpoints, not one nested PATCH

Chosen: `PATCH /meetings/{id}` for record metadata (title) + per-field
`PUT /meetings/{id}/summary/{field}` + `PUT /meetings/{id}/segments/{index}`.
Rejected: a single `PATCH /meetings/{id}` with nested `summary.*`/`segments.*`
fields. Reasons: (a) title lives canonically in `index.json` (with a
`summary.json` `title` mirror maintained by the PATCH — see §1), summary
fields in `summary.json`, segments in `transcript.json` — one endpoint per file family
keeps each handler a single read-modify-write with one atomic write set;
(b) nested PATCH needs an array merge policy, which is exactly the ambiguity
the `expected_text` guard exists to kill; (c) whole-field PUT is idempotent and
matches the UI granularity (one section / one segment at a time).

### Re-embed: `_reindex_meeting_safe(meeting_id)`

```python
_reindex_locks: dict[str, threading.Lock]   # + one guard lock; per-meeting

def _reindex_meeting_safe(meeting_id: str) -> None:
    # capture m and out_dir up front; then, in the worker:
    def work():
        with _lock_for(meeting_id):            # serialize delete→upsert pairs
            if meeting_id not in meetings:     # deleted while queued — never resurrect points
                return
            get_qdrant().delete(COLLECTION_NAME, points_selector=<meeting_id filter>)  # app.py:2754–2761 pattern
            segments = read out_dir/"transcript.json" ["segments"]
            summary  = read out_dir/"summary.json"
            store_in_qdrant(meeting_id, m, segments, summary)   # vector.py:126
        # except Exception: logger.warning(..., non-fatal)      # _index_note_safe pattern, app.py:4734
    _run_bg(work)                                                # app.py:4722
```

Full-meeting reindex (not per-chunk surgery) because a title edit invalidates
**every** point's payload anyway (`vector.py:148–158`), meetings are tens of
chunks, and `store_in_qdrant` batch-embeds in one Ollama call chain. The
per-meeting `threading.Lock` serializes overlapping jobs (edits in quick
succession) so a delete can't interleave with another job's upsert and leave
duplicate points; sequential delete→upsert is idempotent, last edit wins.

Two deliberate details. **Name resolution:** `work()` calls the
**app-namespace** `get_qdrant()` / `store_in_qdrant` names (imported at
`app.py:83–88`), never `vector.`-qualified ones, so the test harness's
app-level monkeypatches intercept them (see Testing). **Delete-vs-reindex
race:** `delete_meeting` (`app.py:2739–2782`) acquires the same
`_lock_for(meeting_id)` around its vector-delete + record-removal, and the
worker re-checks `meeting_id in meetings` inside the lock before touching
Qdrant — otherwise a reindex job queued behind a DELETE could re-upsert points
*after* the delete and leave permanent orphans that `/meetings/search` and
multi-meeting scopes would surface (no later event would ever clean them).

Also call `_reindex_meeting_safe(meeting_id)` once at the end of
`_run_reprocess`'s success path (beside `_auto_compute_link_suggestions`,
`app.py:4452–4456`) — closing the pre-existing staleness gap for all four
reprocess steps and making the "Re-run analysis" flow actually refresh RAG.

## Architecture — server

### 1. `PATCH /meetings/{meeting_id}` — title

`class MeetingPatch(BaseModel): title: str` — stripped, non-empty (400 if
blank). Effects: `m["title"] = title`; set **`m["title_edited"] = True`**;
`_save_index()`; **rewrite `summary.json`'s own `title` key** (read
`summary.json`, set `title`, `_atomic_write`) **before** regenerating the
markdown; then regenerate `transcript.md` + `summary.md`. The two markdowns
source the title differently: `transcript.md` takes it from the **meeting
record** (`build_transcript_markdown`, `app.py:1188`), while `summary.md`'s
H1 comes from **`summary.json`'s `title` key** (`build_summary_markdown`,
`app.py:1609–1614` — `summary.get("title", …)`), which is why `summary.json`
must be rewritten first: otherwise the regenerated `summary.md` and the
exported `summary.json` would both keep the stale LLM title.
`_reindex_meeting_safe`. Returns `{"detail": ..., "title": ...}`.

**Title-clobber gate:** both LLM title assignments —
pipeline (`app.py:1944–1945`) and reprocess-summarize (`app.py:4426–4427`) —
gain `and not meeting.get("title_edited")`. Otherwise the recommended
"edit segments → Re-run analysis" flow would silently revert a manual rename.

### 2. `PUT /meetings/{meeting_id}/summary/{field}` — summary sections

`class SummaryFieldPut(BaseModel): value: Any` (validated per field).
`field` ∈ `{summary, action_items, decisions, concerns, open_questions}`;
anything else → 400. Shallow validation: `summary` → non-empty `str`; the four
array fields → `list` of `dict`. This is a single-user app; the client sends
back arrays it received from the server with one item's text changed.

- **Legacy-alias canonicalization:** old meetings may hold `executive_summary`
  / `questions_raised` instead of `summary` / `open_questions` (both reader
  fallbacks: `renderSummary`, `app.js:2329/2383`; `build_summary_markdown`,
  `app.py:1617`). The endpoint writes the **canonical** key and deletes the
  legacy alias so every `x or legacy` reader picks up the edit.
- **Overlay protection:** for `action_items` only, the replacement list must
  have the **same length** as the current one (400 otherwise) —
  `task_overlay.json` entries are keyed by index into this array
  (`app.py:5006–5110`), and in-place edit-only is the approved scope anyway.
  **Overlay precedence:** action-item text already has a second edit path —
  the To-Do list's `api_meeting_task_edit` writes a per-index `text` override
  into `task_overlay.json` (`app.py:5125–5140`), which
  `apply_meeting_overlay` prefers in the projection (`app.py:5009`). Rule:
  the summary-side edit wins — for each index whose `task` text changed, this
  endpoint clears that overlay entry's `text`/`edited` keys (preserving
  `done`/`deleted` state), so the To-Do projection shows the new summary text
  instead of a stale override. To-Do-side edits still overlay-win until the
  next summary-side edit of the same index (they remain invisible to
  `summary.json`/RAG, as today).
- Effects: read `summary.json` from disk (404 if missing), replace field,
  `_atomic_write`; regenerate `summary.md`; `m["summary"] = updated` +
  `_save_index()` (related-notes reads the in-memory copy, `app.py:3084–3085`);
  `_reindex_meeting_safe`. Returns the updated summary dict (client re-renders
  from it).

No `expected_*` guard here — but not because writers are excluded:
`update_speakers` (`app.py:3684`) and merge/reassign rewrite `summary.json` /
`transcript.json` precisely while `status == complete`, so the status check
excludes only reprocess-summarize (whose re-run **intentionally** replaces
summary edits — the UI copy says so, see frontend). The actual safety
invariant is that every writer of these files performs its read-modify-write
synchronously on the one event loop with **no `await` between the file read
and `_atomic_write`**, so writes cannot interleave. Any future change that
introduces an await mid-RMW in one of these handlers must add a guard.

### 3. `PUT /meetings/{meeting_id}/segments/{index}` — transcript segment text

`class SegmentEdit(BaseModel): text: str; expected_text: Optional[str] = None`
— mirroring the `TaskToggle` guard (`app.py:4661–4665`). Rules:

- 404 if meeting unknown or `index` out of range of
  `transcript.json["segments"]`; 400 if `text` strips empty; 409 if
  `expected_text is not None` and `!= segments[index]["text"]` (detail:
  "Segment changed; refresh" — same UX contract as `app.py:5037`).
- Effects: `segments[index]["text"] = text.strip()`; `_atomic_write`
  `transcript.json`; regenerate `transcript.srt` + `transcript.md`
  (`app.py:3615–3627` pattern); refresh `m["transcript_text"] =
  build_transcript_text(segments)` (kept since `app.py:2051` and serialized
  into `index.json` by `_save_index`); set **`m["transcript_edited"] = True`**;
  `_save_index()`; `_reindex_meeting_safe`. Returns the updated segment.
- `raw_transcript.json` is deliberately untouched (it is the pre-cleanup
  source; see Error handling for the Re-cleanup interaction).
- `meeting_status` (`app.py:2530`) adds `"transcript_edited"` to its response
  so the frontend banner survives reloads; `_run_reprocess`'s summarize step
  sets it back to `False` on success.

### 4. Speakers — tiny server change + frontend affordance

`PUT /meetings/{meeting_id}/speakers` with `speaker_map` + `speaker_details`
already covers name/company/title **persistence** (`app.py:3549–3743`) — but
it never re-indexes, so Qdrant payloads keep the old names in their `text`
and the stale `speaker` field (`vector.py:153`, `170–207`), and chat RAG /
`/meetings/search` would answer from pre-edit names. The one server line:
`update_speakers` — and the merge/reassign endpoints (`app.py:3751`, `3837`),
which also rewrite transcript files — call `_reindex_meeting_safe(meeting_id)`
on success. Everything else is frontend work.

## Architecture — frontend (`static/app.js`, `static/index.html`)

House pencil affordance: a small `.pencil-btn` button (visible on
hover/desktop, always visible on mobile), `title` tooltip, that swaps the text
node for an inline `<input>`/`<textarea>` — Enter/Save commits, Esc cancels.
One shared helper `inlineEdit(el, {value, multiline, onSave})` added to
`app.js`; all four surfaces use it.

1. **Title** — `#detailTitle` / `#detailTitleMobile` (`index.html:287/466`),
   populated in `populateDetail` (`app.js:1586–1635`). Pencil beside the `<h2>`
   → `PATCH /meetings/{id}` → set `titleEl.textContent`, then
   `refreshGroupedView()` (`app.js:1427`) so the sidebar list shows the new
   title. Editor seeds from `status.title` (raw), not innerHTML.
2. **Summary sections** — `loadSummary` (`app.js:1733`) keeps the raw response
   in a module-level `currentSummaryData`; `renderSummary` (`app.js:2325`)
   adds: one pencil on the Summary heading (multiline editor for the whole
   text) and one pencil per item in Action Items (task text), Decisions
   (decision + context), Concerns (concern + notes), Open Questions (question).
   **Editors seed from the raw stored values in `currentSummaryData`** — not
   the displayed `applySpeakerMapToText(...)` output (`app.js:2333`) — so
   speaker-label mapping never gets baked into stored text. Save builds the
   updated field value (string, or a copy of the array with the one item
   replaced) → `PUT /meetings/{id}/summary/{field}` → replace
   `currentSummaryData` with the response and `renderSummary` it.
3. **Transcript segments** — `renderSegmentHtml` inside
   `renderTranscriptWithMap` (`app.js:3040–3076`) adds `.seg-edit-btn`
   (title "Edit text", glyph `&#9999;` ✏) **beside** the existing
   `.seg-annotate-btn` (`&#9998;` ✎ is already taken by annotate,
   `app.js:3074` — the two must stay visually distinct). Click swaps
   `.seg-text` for a textarea seeded from
   `currentOriginalSegments[segIdx].text`, and sends that same value as
   `expected_text`. On success: update `currentOriginalSegments[segIdx].text`,
   re-render via `renderTranscriptWithMap(currentOriginalSegments, id)`
   (virtual-scroll safe), and show the banner:
   - **"Re-run analysis" banner** — a bar at the top of the transcript tab,
     shown when `status.transcript_edited` is true (from
     `/meetings/{id}/status` in `populateDetail`) or after an in-session edit:
     *"Transcript edited since the last analysis — Re-run analysis
     (regenerates the summary; replaces any manual summary edits)."* Button
     calls the existing `reprocessStep(id, 'summarize')` (`app.js:2637`) — the
     same function behind the action-bar "Re-summarize" button
     (`app.js:1662–1670`). No new reprocess plumbing.
4. **Speakers** — `renderSpeakerMapBar` chips (`app.js:2746–2781`) gain a
   visible pencil glyph inside the chip and `title="Edit name, company,
   title"`; clicking still opens the existing `editSpeakerName` popover
   (`app.js:2802`) unchanged. The "click to rename" placeholder text stays for
   unnamed speakers; the pencil makes *named* speakers discoverable too.

### Service worker (`static/sw.js`)

No new static files — `app.js`/`index.html` are already in `SHELL_ASSETS`. The
change MUST bump `CACHE_NAME` to the **next unused, monotonic** version string
at land time. Rule, not a number: current deployed value is
`meetings-v16-offline-queue` (`sw.js:1`); batch-1 claims `v17`
(offline-notes-sync spec, Cross-feature coordination) and `v18`
(attachment spec, "Phase B … bump `sw.js` `CACHE_NAME` → `meetings-v18-attachments`").
Whichever of those has landed when this feature merges, take the next free
value (e.g. `meetings-v19-edit-everything`) — checking **all** landed
siblings, not just batch-1: three other 2026-07-15 specs (in-meeting-notes,
filler-removal, company-tag) also nominate `v19-*` names. Never reuse an
existing value or the SW will not re-cache.

## Data flow

1. **Edit** — pencil → inline editor → `PATCH`/`PUT` → server rewrites the
   pipeline's files + derived `.md`/`.srt`, updates `meetings[mid]` +
   `index.json`, responds; UI re-renders from the response.
2. **Re-embed (async)** — `_reindex_meeting_safe`: per-meeting lock →
   delete-by-filter → read `transcript.json` + `summary.json` from disk →
   `store_in_qdrant`. Chat RAG (`app.py:5471–5487`), `/meetings/search`, and
   insights now retrieve the edited text; related-notes already sees it via
   `m["summary"]`/`m["title"]`; exports serve the rewritten files.
3. **Segment edit → re-run** — edit sets `transcript_edited`; banner offers
   `reprocessStep(id,'summarize')`; reprocess regenerates `summary.json`
   from the **edited** transcript (`app.py:4414–4432`), preserves a manual
   title via the `title_edited` gate, clears `transcript_edited`, and
   re-indexes at the end of `_run_reprocess`.

## Error handling

- **404** unknown meeting / segment index / missing `summary.json`;
  **409** `status != complete` (also covers "reprocess in flight");
  **400** empty text, unknown summary field, wrong field type,
  `action_items` length change.
- **Stale segment (`expected_text` mismatch) → 409**; client toasts
  "Segment changed — refreshing", reloads the transcript
  (`loadTranscript`, `app.js:1700`), and the user re-opens the editor. Never
  writes on mismatch (file untouched) — same contract as task edits.
- **Reindex failure is non-fatal**: logged warning, files remain correct
  (the `_index_note_safe` posture, `app.py:4734–4746`); the next edit or
  reprocess re-indexes everything anyway (full delete→re-store).
- **Delete during a queued reindex**: closed, not accepted — `delete_meeting`
  takes `_lock_for(meeting_id)` around its vector-delete + record-removal and
  the reindex worker re-checks `meeting_id in meetings` inside the lock, so a
  queued job can never re-upsert points for a deleted meeting (such orphans
  would persist forever — no later event cleans them).
- **Known interaction — "Re-cleanup"**: reprocess `cleanup` re-derives segment
  text from `raw_transcript.json` (`app.py:4313–4317`) and will overwrite
  manual segment edits. Accepted: cleanup is an explicit destructive re-run of
  an earlier pipeline stage, same as re-summarize replacing summary edits. No
  extra guard; the reprocess buttons are deliberate actions.
- **Crash between file write and reindex**: files (source of truth) are
  updated, vectors stale until the next edit/reprocess reindex. Same exposure
  the pipeline already accepts for Qdrant failures (`app.py:2028–2031`).

## Testing (TDD, RED→GREEN)

**Server — pytest, new `tests/test_edit_endpoints.py`**, mirroring the
`tests/test_meeting_routes.py` harness: bare `TestClient(app.app)` (startup
events never fire), `MEETINGS_DIR` / `meetings` / `get_qdrant` / `get_embedder`
monkeypatched, `_seed_complete`-style fixtures writing real
`transcript.json`/`summary.json` into `tmp_path` (`test_meeting_routes.py:55–84`).
Tests additionally monkeypatch `app._run_bg` to run inline so reindex effects
are deterministic, and extend the fake Qdrant's `delete` to record the filter.
**Reindex observability — no live services:** app-level patches alone cannot
intercept the upsert half, because `store_in_qdrant` resolves
`get_embedder`/`get_qdrant` in the `vector` module's own namespace
(`vector.py:126–129`; its docstring notes tests don't patch them) — with
`_run_bg` inlined, an unpatched success path would open httpx to Ollama and a
real `QdrantClient`. So tests **also monkeypatch `vector.get_qdrant` /
`vector.get_embedder` to the same fakes** as the app-level patches
(alternatively: monkeypatch `app.store_in_qdrant` with a recorder and assert
its args). The delete half is observable through the fake precisely because
`_reindex_meeting_safe` is required to call the **app-namespace**
`get_qdrant()` / `store_in_qdrant` names (see the re-embed section) — never
`vector.`-qualified ones — so the app-level patches apply.

RED first, per endpoint:

- **Title:** 404 unknown; 409 non-complete; 400 blank. Success: `m["title"]`
  + `index.json` updated, `transcript.md`/`summary.md` headers regenerated,
  `title_edited` set, delete-filter targeted the meeting and re-upserted
  payloads carry the new title. **Gate:** with `title_edited=True`, a
  reprocess-summarize (monkeypatched `step_summarize` returning a different
  title; poll `/meetings/{id}/status` until complete — the step runs via
  `asyncio.create_task`, `app.py:4465`) leaves the manual title in place.
- **Summary field:** unknown field 400; wrong type 400; `action_items`
  length-change 400 (overlay-index regression guard). Success on each of the
  five fields: `summary.json` + `summary.md` rewritten, `m["summary"]`
  updated, reindexed payloads contain the edited text and not the old text.
  **Legacy alias:** seed `summary.json` with only `executive_summary`; `PUT
  field=summary` writes the canonical key, removes the alias, and
  `GET /meetings/{id}/summary` returns the edit.
- **Segment:** index 404; stale `expected_text` → 409 with `transcript.json`
  byte-identical; success rewrites `transcript.json`/`.srt`/`.md`, refreshes
  `m["transcript_text"]`, sets `transcript_edited` (visible in `/status`),
  reindexes. Reprocess-summarize clears `transcript_edited`.
- **Reindex-after-reprocess:** any reprocess step ends with a delete+re-store
  for that meeting (the `_run_reprocess` hook).

**JS:** no new pure-logic module — every new frontend behavior is a DOM-bound
fetch-and-rerender on existing render functions, so there is nothing for the
`queue-logic.js`-style dependency-free `node --test` harness to bite on. If a
pure helper does emerge during implementation (e.g. array-item replacement
grows rules), it goes in a dual-export module tested via `node --test` **run by
file path** (the Node 22/Windows directory-run quirk from the upload-queue work
still applies). Manual verification checklist instead: edit each surface on
desktop + mobile overlay, confirm sidebar title refresh, banner persistence
across reload, and chat answering from edited text after reindex.

## Rollout

1. Server: three endpoints + `_reindex_meeting_safe` + reprocess reindex hook +
   title/`transcript_edited` gates + tests. Deployable alone (API-first).
2. Frontend: `inlineEdit` helper, pencils on all four surfaces, Re-run banner,
   `sw.js` `CACHE_NAME` → next free version per the rule above.
3. Rebuild/redeploy the live container (static assets baked into the image).
4. Port to `craiglush/parley` **after** the batch-1 ports land there, in the
   same order. Overlap: `sw.js` (`CACHE_NAME`) and, in `app.py`, a **known
   collision** with attachment Phase C at the `step_summarize` call-site
   blocks (`app.py:1943–1945`, `4425–4427` — Phase C adds `context=…` to the
   call lines; this feature's title-clobber gate edits the adjacent title
   assignments at `1944–1945`/`4426–4427`). Land/rebase order for that hunk:
   Phase C first, then rebase the title-clobber gate onto the
   `context=`-bearing call sites — line numbers will drift, so anchor on the
   `step_summarize` calls, not on line numbers, and verify after rebase that
   both the gate and the `context=` parameter survived. Other
   `app.py`/`app.js` regions are disjoint; `notes-tasks.js` is untouched by
   this feature.

## Open questions

None. Endpoint shape, overlay-length guard, legacy-alias canonicalization,
title-clobber gate, and the reprocess reindex hook are all decided above;
add/delete/reorder of items and offline meeting edits are explicitly out of
scope.
