# Attachment Extraction + AI Integration — Design

**Date:** 2026-07-15
**Branch:** `feat/attachment-extraction-ai`
**Status:** Approved for implementation (revised after adversarial spec review)

## Context

Note attachments today are **opaque bytes**. `POST /api/notes/{note_id}/attachments`
(`api_add_attachment`, `app.py:5192–5203`) reads the upload, `notes_store.save_attachment`
(`notes_store.py:85–90`) writes it to a **vault-global** `attachments/` folder with
a UUID-suffixed name, and the endpoint returns a markdown embed (`![[file]]` for
images, `[file](attachments/file)` otherwise). `GET /api/notes/attachments/{filename}`
(`app.py:5206–5211`) serves it back. Limits: a 50 MiB cap (`ATTACH_MAX_BYTES`,
`app.py:589`) and **no type restriction**. There is **no text extraction**, **no
attachment→AI wiring**, and **no attachment list or delete endpoint**; attachments
link to a note only implicitly, by filename references in the body.

Notes touch AI in exactly two narrow, body-text-only spots today:
- **Vector index** — `notes_vectors.index_note` (`notes_vectors.py:53`) embeds the
  body into the Qdrant `notes` collection via `qwen3-embedding:0.6b` (1024-dim).
- **Auto-tagging** — `auto_tag_note` (`app.py:1056–1085`) sends
  `(f'{title}\n\n{body}')[:16000]` to Ollama `/api/generate` with the
  `analysis_pass_g` schema. Enqueued via `_enqueue_tag` → `_tag_worker`
  (`app.py:4759–4778`), gated behind `_pipeline_busy()`.

The **meeting analysis** pipeline is separate: `step_summarize` (`app.py:863`) →
`_run_analysis_passes` (`app.py:876`) runs six text-only `/api/generate` passes
whose only prompt input is the transcript
(`prompt = template.replace('{transcript}', transcript_text)`, `app.py:900`).

**Available extraction primitives (verified in the codebase):**
- **ffmpeg** (Dockerfile) + **Parakeet ASR** (`parakeet-asr:5092`, OpenAI-compatible
  `/v1/audio/transcriptions`, `stt.py:55`) + **pyannote** — `stt.py` already turns
  any audio/video into a transcript. Reusable as-is.
- **Text-only LLM body builder** `_build_generate_body` (`llm.py:36–64`) — no
  `images` param; wiring one in is the only code gap for vision.
- **No** PDF/Office/OCR libraries exist (`requirements.txt`).

**Runtime assumption — local vision models (NOT a repo fact).** A live
`ollama list` during design showed vision models (`qwen3-vl:8b`, `qwen2.5vl:7b`,
~6 GB each) pulled on the host Ollama at `http://host.docker.internal:11434`
(`OLLAMA_URL`, confirmed). Nothing in the repo records this, so the implementation
**must verify availability at runtime** and degrade gracefully (see Guards). The
`VISION_MODEL` env var is overridable; the image/scanned-PDF paths are only
"guaranteed" insofar as a vision model is actually present.

## Goal

Turn attachments of **any format** into text, and feed that text — together with
the note body — into the note's AI features: **semantic search / related-notes**,
**auto-tagging**, a new **"Analyze note"** action, and (for notes linked to a
meeting) the **meeting's own analysis**.

**Scope decisions (confirmed):** all four AI feed-points; true any-format
including **local image vision** (subject to the runtime assumption above);
**cheap extraction on upload, GPU work (STT + vision) deferred** so it never
competes with a live meeting transcription.

## Non-Goals

- Offline attachment upload (covered by — and excluded in — the offline sync spec).
- Cloud/paid extraction or OCR. Everything stays local.
- Auto-running vision/STT on every upload (deferred by design).
- A document-extraction microservice (Tika/unstructured).

## Format coverage

| Family | Extensions | Method | When |
|---|---|---|---|
| Plaintext | txt, md, csv, json, log, yaml, code, **svg** (as XML text) | decode UTF-8 (`errors='replace'`) | **inline (in executor)** |
| PDF (text layer) | pdf | `pypdf` | **inline (in executor)** |
| Office | docx, xlsx, pptx | `python-docx`, `openpyxl`, `python-pptx` | **inline (in executor)** |
| Audio / video | mp3, wav, m4a, mp4, mov, … | reuse `stt.py` (ffmpeg + Parakeet) | **deferred** (idle/on-analyze) |
| Images (raster) | png, jpg, jpeg, gif, webp | `VISION_MODEL` via Ollama `images` | **deferred** (idle/on-analyze) |
| Scanned PDF | pdf w/ empty text layer | `pypdfium2` render page → `VISION_MODEL` | **deferred** |
| Unknown/binary | * | none (stored, no text) | — |

`svg` is XML **text**, not raster — it is decoded as text, never sent to the vision
model (note: `_IMAGE_EXTS` includes `.svg` for *embed/serve* purposes, so the list
endpoint's `is_image` stays true for svg, but extraction treats it as text).

## Architecture

### New: `extract.py` (extraction core)

- `extract_text(path, filename) -> Extraction` where `Extraction = {text, method,
  chars, status}`, `status ∈ {done, pending, empty, failed}`.
- Dispatch by extension (light MIME/`ffprobe` sniff for A/V):
  - plaintext (incl. svg) → decode.
  - pdf → `pypdf`; if the text layer is near-empty → `status='pending'`,
    `method='vision'` (scanned → deferred; rasterized with `pypdfium2`).
  - docx/xlsx/pptx → respective library.
  - audio/video → `status='pending'`, `method='stt'` (deferred).
  - raster image → `status='pending'`, `method='vision'` (deferred).
  - unknown → `status='empty'`.
- **Instant** methods run in a **thread executor** (`run_in_executor` / `_run_bg`
  style, matching `api_list_notes` at `app.py:4789`) — a 50 MiB docx/pptx parse
  must not block the async handler. **Deferred** methods only set `pending`.
- Every extractor is wrapped so a parse error yields `status='failed'` and never
  raises — attachment storage must never break.

**New pinned deps** in `requirements.txt`: `pypdf`, `python-docx`, `openpyxl`,
`python-pptx`, **`pypdfium2`** (scanned-PDF rasterizer — wheel-only, no apt
package). All install as cp311 manylinux wheels on `python:3.11-slim` with **no
build tools/apt** required (python-docx/pptx pull `lxml`; python-pptx pulls
`Pillow` — both wheel-only). ffmpeg is already in the image. **Docker rebuild
required.**

### Sidecar storage (Obsidian-safe)

- Extracted text → `attachments/.extracted/<stored-filename>.json` =
  `{text, method, chars, extracted_at, status}` (**per-attachment**).
- Analysis results (Phase B) → `attachments/.analysis/<note_id>.json`
  (**per-note**).
- **No exclusion-list edits are needed:** both dirs sit under `attachments/`, which
  `_iter_note_files` already skips (and it only globs `*.md`), and `list_folders`
  already excludes anything under `attachments/`. Sidecars are invisible to notes,
  folders, `/api/notes/export`, and Obsidian by construction.

### Vision wiring (`llm.py`)

- Extend `_build_generate_body` (`llm.py:36`) to accept optional `images:
  list[str]` (base64) and include it in the body only when present (text passes
  unchanged — regression-guarded).
- New `describe_image(path, *, prompt) -> str` posting to Ollama `/api/generate`
  with `model=VISION_MODEL` (new env var, default `qwen3-vl:8b`),
  `images=[b64(path)]`, through the existing `_retry_ollama_call`. Concurrency
  limited to 1 (GPU). Used for raster images and `pypdfium2`-rendered scanned-PDF
  pages.

### Attachment↔note association (body is source of truth)

- New `notes_store.note_attachments(notes_dir, note_id) -> list[str]` — parse the
  note body for both reference forms (`attachments/<f>` and `![[<f>]]`) and return
  the referenced filenames. No frontmatter, no drift, Obsidian-consistent.

### New endpoints

- `GET /api/notes/{note_id}/attachments` — list referenced attachments with
  `{filename, is_image, size, extraction_status}`.
- `DELETE /api/notes/{note_id}/attachments/{filename}` — delete the bytes **and
  only that attachment's per-attachment `.extracted` sidecar**. The note-level
  `.analysis/<note_id>.json` is left as stale (re-run Analyze to refresh) — never
  deleted on a single-attachment delete, since it may derive from the note body
  and other attachments. The client removes the markdown embed from the editor;
  the endpoint does not rewrite the note body in v1.
- Orphan GC on note delete — **deferred** (follow-up).

---

## Phase A — Extraction + search/tags

Delivers the extraction core, sidecars, vision wiring, list/delete endpoints, and
the two **passive** AI feeds. **Backend-only — no `notes-tasks.js`/`sw.js`
changes** (so it can land first/parallel to offline sync; see coordination).

- **Upload change** (`api_add_attachment`): after `save_attachment`, run instant
  extraction **in the executor** and write the `.extracted` sidecar; deferred
  formats get a `pending` sidecar and are enqueued to an **idle worker** mirroring
  `_tag_worker` (gated on `_pipeline_busy()`). Response gains `{extracted: bool,
  status}`.
- **Search feed:** when building a note's indexing corpus (`_index_note_safe` →
  `notes_vectors.index_note`), append the referenced attachments' extracted text
  (capped) so attachment content is semantically searchable and surfaces in
  related-notes.
- **Tag feed:** `auto_tag_note` includes attachment text in its input blob,
  respecting the 16 000-char cap (body first, then attachment text until the cap).
  *Note:* this re-tags the note, which advances `updated` — the offline sync spec's
  content-hash token is designed precisely so these bumps don't trigger false
  conflicts.

## Phase B — "Analyze note" action

- **New prompt + schema:** `DEFAULT_PROMPTS['note_analysis']` +
  `ANALYSIS_SCHEMAS['note_analysis']` → structured `{summary, key_points[],
  action_items[], insights[]}` (Ollama `format`).
- **New endpoint** `POST /api/notes/{note_id}/analyze`:
  1. Ensure deferred extraction for the note's attachments is resolved now
     (explicit user action → allowed to use GPU; still queued if a meeting is
     mid-pipeline, with status reported).
  2. Build corpus = note body + all attachment text, **capped by an explicit
     char/token constant** (`ANALYSIS_CORPUS_MAX`) so it fits the context window;
     `_ctx_for_text` (`llm.py:23`) is used only to *size* `num_ctx` for the call
     (it returns a tier, it does not truncate).
  3. Run one `/api/generate` pass; write `attachments/.analysis/<note_id>.json`;
     return the result.
- Runs as a **background job** with `GET /api/notes/{note_id}/analysis` for
  status/result (reuses the enqueue→idle-worker→LLM pattern), so the HTTP call
  never blocks on slow STT/vision.
- **UI** (`notes-tasks.js`): an **"Analyze"** button in the note editor → result
  panel, with an "insert into note" option. **This edit must bump `sw.js`
  `CACHE_NAME` → `meetings-v18-attachments`** (distinct from offline's `v17`) or
  the new button won't reach already-installed PWAs. Rebase these edits on top of
  the offline sync reroute (land order below).

## Phase C — Meeting analysis integration

- **Prepend, don't templatize.** Thread an optional `context: str` through
  `step_summarize` (`app.py:863`) → `_run_analysis_passes` (`app.py:876`) →
  `_hierarchical_summarize` (`app.py:1108`). Inside, **prepend `context` to
  `transcript_text`** before the existing `template.replace('{transcript}', …)` at
  `app.py:900` (mirroring the existing `prefix` at `app.py:1171`). **Do not add a
  `{context}` placeholder to the templates** — `load_settings` serves user-saved
  prompts verbatim (`app.py:503–507`), so a new placeholder would be silently
  dropped for any customized prompt. Prepending needs no template edits and makes
  the empty-context case reproduce today's prompt **byte-for-byte** (regression
  guard).
- At both call-sites — fresh run (`app.py:1943`) and `/reprocess` (`app.py:4425`) —
  gather the meeting's **explicitly linked** notes (`linked_meetings` frontmatter;
  deterministic, user-controlled — *not* fuzzy related-notes), concatenate their
  body + attachment text, cap hard (`MEETING_CONTEXT_MAX`), and pass as `context`.

## Guards & safety

- **Truncation:** per-attachment char cap (`ATTACH_TEXT_MAX`, e.g. 20 000);
  per-note-analysis total (`ANALYSIS_CORPUS_MAX`); meeting `context`
  (`MEETING_CONTEXT_MAX`). All named constants; `_ctx_for_text` sizes `num_ctx`
  separately.
- **GPU discipline:** vision/STT never run on upload; deferred behind
  `_pipeline_busy()`; vision concurrency = 1; `VISION_MODEL` overridable.
- **Vision preflight / graceful degrade:** on first vision use, check the model is
  present (cache the result); if absent, mark affected attachments
  `status='failed'` with a clear reason rather than erroring — text/PDF/Office/STT
  paths are unaffected.
- **Non-fatal extraction:** any failure → `status='failed'`, attachment still
  stored, AI features skip it. Upload never fails because of extraction.
- **Obsidian-safe:** sidecars live under the already-excluded `attachments/` dir.
- **Backfill:** a one-shot re-extract for pre-feature attachments — surface (small
  script vs endpoint) decided in the plan.

## Testing (TDD, RED→GREEN)

- **`extract.py`** unit tests with tiny fixtures per format (txt, csv, svg, pdf,
  docx, xlsx, pptx); STT and vision paths **mocked** (monkeypatch the
  Parakeet/Ollama callouts) to assert dispatch + sidecar writing without a GPU;
  a failing parser yields `status='failed'`, not an exception.
- **`llm.py`** — `_build_generate_body` includes `images` when passed, omits it
  otherwise (byte-for-byte identical text body — regression).
- **Association** — `note_attachments` parses both `![[x]]` and
  `[x](attachments/x)` forms.
- **Endpoints** — attachments list/delete (delete removes only the `.extracted`
  sidecar, leaves `.analysis`); `analyze` with a mocked LLM returns the structured
  shape; existing note CRUD/export unaffected.
- **Meeting context** — `step_summarize(..., context=…)` prepends context to the
  prompt sent to a mocked Ollama; **empty/None context reproduces today's prompt
  exactly** (regression).

## Rollout & cross-feature coordination

Order (shared with the offline sync spec):

1. **Attachment Phase A** — backend-only; new pip deps → **image rebuild**. Land
   first or parallel to offline (orthogonal; no frontend files touched).
2. **Offline sync** (its own spec) — establishes the narrow `api()` reroute; bumps
   `sw.js` → `v17`.
3. **Attachment Phase B** — Analyze button rebased on the rerouted `api()`; new
   endpoints on the **pass-through** side; bumps `sw.js` → `v18`.
4. **Attachment Phase C** — backend meeting-context; independent.

Rebuild/redeploy the live container after Phase A (new Python deps → image rebuild,
not just static assets).

## Open questions

None blocking. Deferred by choice: orphan-attachment GC; pre-feature backfill
trigger surface.
