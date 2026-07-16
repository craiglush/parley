# Transcript Filler Removal — Design

**Date:** 2026-07-15
**Branch:** `feat/filler-removal`
**Status:** Approved for implementation

## Context

The processing pipeline runs an LLM **transcript cleanup** step over the raw STT
segments. The editable system preamble `DEFAULT_PROMPTS["cleanup_system"]`
(`app.py:126–142`) currently instructs the model to *"Leave filler as-is unless
it is clearly a recognition error"* (`app.py:136`), so ums/uhs/errs survive into
`transcript.json`, the transcript markdown, the SRT, and everything downstream
(summaries quote the transcript; search indexes it).

There are **two cleanup call paths**, both funneling through
`_build_cleanup_prompt` (`app.py:1232`):

- **Live pipeline** — an inline batching loop (`app.py:1812–1886`) that
  duplicates the batching logic for progress reporting. It deep-copies
  `raw_segments` *before* cleanup (`app.py:1813`) and later writes them to
  `raw_transcript.json` (`app.py:1977–1984`), so the true STT output is always
  preserved on disk. Cleaned segments are swapped in **only when
  `changes_made > 0`** (`app.py:1873–1879`), which also sets
  `meeting["transcript_cleaned"]` and rebuilds `transcript_text`.
- **Reprocess** — `step_cleanup_transcript` (`app.py:1300`), called by the
  existing `/meetings/{id}/reprocess` endpoint. **Verified:** `step="cleanup"`
  is already in the allowed branch list `("cleanup", "identify_speakers",
  "summarize", "tagging")` (`reprocess_meeting`, `app.py:4306`), it reads
  `raw_transcript.json` preferentially (`app.py:4313–4319`), and the frontend
  already has a **Re-cleanup** button (`app.js:1663`, `reprocessStep`,
  `app.js:2637`). No new step or button is needed.

Two constraints shape the design:

- `_parse_cleanup_response` requires **exactly one non-empty line per input
  segment** (`app.py:1275–1297`; empty texts are discarded at `app.py:1291`, and
  a count mismatch rejects the whole batch). The LLM therefore **cannot delete a
  pure-filler segment** — only a pre-pass that edits the segment list can.
- `load_settings` serves user-saved prompt templates **verbatim**
  (`app.py:503–507`), so behavior toggles must modify prompts **at prompt-build
  time**, never by editing saved templates — the same "prepend, don't
  templatize" principle the attachment feature uses for meeting context
  (`docs/superpowers/specs/2026-07-15-attachment-extraction-ai-design.md`,
  "Prepend, don't templatize").

Settings plumbing to copy: the `diarize` boolean is the exact pattern — default
in `DEFAULT_SETTINGS` (`app.py:464–491`), type-checked merge in `load_settings`
(`app.py:514–515`), `Optional[bool]` on `SettingsRequest` (`app.py:4497–4504`),
guarded assignment in `update_settings` (`app.py:4583–4584`), checkbox
`#settingsDiarize` in the settings panel (`static/index.html:611–613`) wired
into the save body (`app.js:3800`), `populateSettingsForm` (`app.js:3869`), and
`markSettingsDirty` on change (`app.js:3697`).

## Goal

A settings toggle **`remove_filler` (default ON)** that removes disfluencies
(um, uh, er, ah, hmm, mm…) and semantically empty filler from cleaned
transcripts, in two layers:

1. a **deterministic regex pre-pass** that strips standalone filler tokens from
   segments *before* the LLM cleanup batches (saves prompt/completion tokens on
   the contended GPU, and still works when the LLM step fails);
2. a **directive appended to the cleanup preamble at prompt-build time** that
   instructs the LLM to remove remaining disfluencies and meaning-free filler
   phrases ("you know", "like", "sort of" when semantically empty) while never
   paraphrasing.

Existing meetings get filler removal through the **existing** Re-cleanup
reprocess step. The raw pre-cleanup transcript stays recoverable on disk.

## Non-Goals

- **No editing of saved prompt templates.** The stored `cleanup_system` template
  (including its "Leave filler as-is" line) is untouched; the directive
  overrides it at build time and disappears when the toggle is OFF.
- **No aggressive deterministic phrase removal.** "you know" / "like" / "sort
  of" are context-dependent; only the LLM layer handles them. The regex strips
  only unambiguous standalone tokens.
- **No refactor of the duplicated cleanup loop.** The pipeline's inline batch
  loop and `step_cleanup_transcript` stay separate (surgical change; both get
  the pre-pass, both already share `_build_cleanup_prompt`).
- **No per-meeting override.** One global toggle; re-cleanup with the toggle
  flipped covers the rare exception.
- **No new reprocess step or UI.** `step="cleanup"` and the Re-cleanup button
  already exist.

## Core model: two layers, one toggle, raw always recoverable

- **Layer 1 (deterministic, cheap):** regex strips standalone filler tokens
  from segment text before batching. Segments reduced to nothing (pure filler,
  e.g. a lone "Um.") are **dropped** — the only mechanism that can remove them,
  since the LLM output contract forbids line deletion (see Context).
- **Layer 2 (LLM, contextual):** a directive appended to the system preamble
  when the toggle is ON handles what regex can't judge: stutters, false starts,
  repetitions, and filler phrases that are only sometimes meaningless.
- **OFF is byte-for-byte today's behavior:** no pre-pass runs and
  `_build_cleanup_prompt` output is identical to the current implementation —
  a hard regression guard.
- **Raw recoverability:** the pipeline deep-copies `raw_segments` *before* the
  pre-pass runs (the pre-pass is inserted after `app.py:1813`), so
  `raw_transcript.json` keeps the unstripped STT output. The reprocess cleanup
  path only *reads* `raw_transcript.json` and rewrites `transcript.json` /
  `.srt` / `.md`. For **legacy meetings without `raw_transcript.json`** —
  whose cleanup source falls back to `transcript.json` (`app.py:4317`) — the
  reprocess branch first **snapshots `transcript.json` →
  `raw_transcript.json`** before any destructive re-cleanup (see "Existing
  meetings"). Turning the toggle OFF and hitting Re-cleanup therefore restores
  an unstripped cleaned transcript from raw at any time — for new and legacy
  meetings alike (a legacy meeting's "raw" being its pre-strip cleaned text,
  the best raw that still exists).

## Architecture

### Setting: `remove_filler`

- `DEFAULT_SETTINGS["remove_filler"] = True` (`app.py:464`).
- `load_settings`: merge with the `diarize` pattern —
  `if isinstance(saved.get("remove_filler"), bool): settings["remove_filler"] = saved["remove_filler"]`.
  A missing key (all existing `settings.json` files) resolves to **ON**.
- `SettingsRequest` gains `remove_filler: Optional[bool] = None`;
  `update_settings` assigns `settings["remove_filler"] = bool(body.remove_filler)`
  when present. `GET /api/settings` returns it automatically (whole-dict
  response, `get_settings`, `app.py:4513`).

### Regex pre-pass (Layer 1)

New module-level pieces next to the cleanup section (`app.py`, "Transcript
cleanup (Phase 3)"):

```python
_FILLER_RE = re.compile(r"(?<![\w'-])(?:um+|uh+|er+m?|ah+|hm+|mm+)(?![\w'-])",
                        re.IGNORECASE)
```

Covers the approved token list — um, uh, er, err, ah, hmm, mm — plus natural
elongations (umm, uhhh, erm, ahh, hmmm, mmm). The `[\w'-]` guards make it
word-boundary safe **and** hyphen/apostrophe safe: "umbrella", "summer",
"ahead", "ermine" are untouched, and the affirmations **"uh-huh" / "mm-hmm" are
deliberately preserved** (they carry meaning: agreement). Accepted tradeoff:
the rare verb "err" ("to err is human") is stripped — it is on the approved
list and essentially never appears standalone in meeting speech.

`strip_fillers(text: str) -> str` — pure; applies, in order:

1. Remove all `_FILLER_RE` matches.
2. Collapse comma runs left behind (`", , "` → `", "`; `",,"` → `","`).
3. Remove space before `, . ; : ! ?`.
4. Strip leading whitespace/commas/periods/semicolons ("Um, so we…" → "so we…").
5. Collapse internal whitespace runs; trim.
6. If a filler was removed at the start of the segment, uppercase the first
   alphabetic character of the result.

`apply_filler_prepass(segments: list[dict]) -> tuple[list[dict], int]` — pure,
**non-mutating** (returns new dicts `{**seg, "text": new_text}`; callers' lists
untouched). Per segment: run `strip_fillers`; if the result contains no
alphanumeric character, **drop the segment** (it was pure filler — timestamps
and speaker label go with it, exactly as if STT had never emitted it). Returns
the new list and a count of modified-plus-dropped segments. Safety guard: if
*every* segment would be dropped (pathological input), return the input
unchanged with count 0 and log a warning — never produce an empty transcript.

**Wiring — both call paths, gated on the toggle:**

- **Live pipeline:** immediately after `raw_segments = copy.deepcopy(segments)`
  (`app.py:1813`) and the `cleanup_settings = load_settings()` that follows:
  `segments, prepass_changes = apply_filler_prepass(segments)` when
  `cleanup_settings.get("remove_filler", True)`. When `prepass_changes > 0`,
  do the pre-pass bookkeeping **immediately, before the LLM `try:` block**
  (`app.py:1822`): rebuild `transcript_text` from the stripped list, refresh
  `meeting["segment_count"] = len(segments)` (recorded pre-cleanup at
  `app.py:1804`; dropped segments change it), and set
  `meeting["transcript_cleaned"] = True`. This ordering matters: the cleanup
  block's outer catch-all (`app.py:1881–1883`) swallows a total LLM failure,
  and if the bookkeeping lived inside the swap-in gate, such a failure would
  leave stripped `segments` paired with an unstripped `transcript_text`, a
  stale count, and `"cleaned": false` — speaker ID / summary / Qdrant would
  then run on divergent inputs. The batch loop runs on the stripped list
  unchanged, and the swap-in gate at `app.py:1873` keeps its
  `changes_made > 0` condition for LLM edits (the pre-pass bookkeeping is
  already done by then).
- **`step_cleanup_transcript`** (`app.py:1300`, the reprocess path): at the top,
  after its existing `load_settings()`, apply the same gated pre-pass to a copy
  of the input. The rest of the function (prompt building, per-index change
  comparison, `cleaned_segments`) operates on the stripped list unchanged. The
  returned list may be **shorter** than the input — see the reprocess fix below.

### Prompt directive (Layer 2)

New module constant next to `DEFAULT_PROMPTS`:

```python
FILLER_DIRECTIVE = (
    "\n"
    "Filler removal (active — this overrides the earlier instruction to leave filler as-is):\n"
    "- Remove remaining disfluencies: um, uh, er, ah, hmm, stutters, false starts,\n"
    "  and immediate word repetitions (\"we we should\" -> \"we should\").\n"
    "- Remove semantically empty filler phrases — \"you know\", \"like\", \"sort of\",\n"
    "  \"kind of\", \"I mean\", \"basically\" — ONLY where they carry no meaning.\n"
    "  Keep them when meaningful (\"I like this plan\", \"sort of a hybrid approach\").\n"
    "- Never paraphrase, shorten, or tighten real content; keep the speaker's wording.\n"
    "- Never return an empty line: if a line is nothing but filler, return it unchanged."
)
```

In `_build_cleanup_prompt` (`app.py:1232`), which already calls
`load_settings()`: when `settings.get("remove_filler", True)`, append
`FILLER_DIRECTIVE` to the preamble part (after the `{meeting_context}`
replacement at `app.py:1244`, before the blank line that follows it). Saved
user templates are still served **verbatim** — the directive is build-time
only, so it also composes correctly with customized cleanup prompts. Because
both call paths build prompts through this one function, directive coverage is
automatic for live processing *and* reprocess. When OFF, the assembled prompt
is **byte-for-byte identical** to today's.

The "never return an empty line" rule exists because `_parse_cleanup_response`
discards empty texts and then fails the whole batch on count mismatch
(`app.py:1291–1295`); pure-filler lines mostly never reach the LLM anyway
(dropped by the pre-pass), but the directive must not induce blank lines for
LLM-judged phrases either.

### Existing meetings: the existing Re-cleanup step (+ two fixes and a legacy snapshot)

`step="cleanup"` already re-runs `step_cleanup_transcript` from
`raw_transcript.json` and rewrites `transcript.json` / `.srt` / `.md`
(`reprocess_meeting`, `app.py:4313–4336`). With filler removal this mostly
Just Works; the reprocess cleanup branch needs three additions:

- **Length-aware change detection.** The current guard
  `changes = sum(1 for a, b in zip(segments, cleaned) if a["text"] != b["text"])`
  (`app.py:4329`) undercounts when the pre-pass **drops** segments — `zip`
  truncates to the shorter list, so a run whose only effect was dropping
  pure-filler segments could report zero changes and skip the rewrite. Replace
  with a small helper used by that branch:

  ```python
  def _segment_texts_differ(before: list[dict], after: list[dict]) -> bool:
      return len(before) != len(after) or any(
          a["text"] != b["text"] for a, b in zip(before, after))
  ```

- **Stale `segment_count`.** Inside the same `_segment_texts_differ` gate, add
  `m["segment_count"] = len(cleaned)` — mirroring the live-path fix. The
  reprocess path is where the pre-pass introduces the first-ever length change
  (pre-feature cleanup could never drop segments), so without this the meeting
  index would report a stale count after a Re-cleanup that dropped
  pure-filler segments.

- **Legacy snapshot (the undo guarantee for old meetings).** The cleanup
  source falls back to `transcript.json` when `raw_transcript.json` is absent
  (`source_path = raw_path if raw_path.exists() else transcript_path`,
  `app.py:4317`) — and such legacy meetings demonstrably exist (the
  identify-speakers branch carries an explicit no-raw fallback,
  `app.py:4361–4372`). Without a guard, a toggle-ON Re-cleanup would strip
  the *only* copy in place, and a later toggle-OFF Re-cleanup would re-read
  the already-stripped file — the unstripped text unrecoverable short of full
  re-transcription. Fix: **before the first destructive re-cleanup of such a
  meeting, snapshot `transcript.json` → `raw_transcript.json`**
  (`_atomic_write`; only when raw is absent), then proceed with that snapshot
  as the source. Raw recoverability then holds for legacy meetings too.

Operational note (pre-existing behavior, unchanged): reprocess cleanup sources
`raw_transcript.json`, which intentionally keeps original `SPEAKER_XX` labels
(`app.py:1921` comment), so after **Re-cleanup** the transcript shows raw
labels until **Re-identify Speakers** is run; then **Re-summarize** if the
summary should reflect the cleaned text. All three buttons already exist side
by side (`app.js:1663–1665`) — this ordering goes in the rollout verification,
not new code.

Known limitation (pre-existing, unchanged by this feature): **no reprocess
step re-indexes Qdrant or refreshes `m["transcript_text"]`** —
`store_in_qdrant` runs only in the live pipeline (`app.py:2026`) and
`m["transcript_text"]` is set once at completion (`app.py:2051`). Even after
Re-cleanup → Re-identify → Re-summarize, vector search / chat RAG still
retrieve the filler-laden text for that meeting. Out of scope here; the
concurrent edit-everything spec's `_run_reprocess` reindex hook closes the
Qdrant half when it lands.

### Settings UI (one checkbox)

In the existing **"Transcript Cleanup Prompt"** settings section
(`static/index.html:624–635`), above the `#promptCleanup` textarea:

```html
<label style="display:block;margin-bottom:8px">
  <input type="checkbox" id="settingsRemoveFiller">
  Remove filler words (um, uh, er…) from transcripts
</label>
```

`static/app.js` wiring, copying `#settingsDiarize` exactly:

- save body: `remove_filler: $('settingsRemoveFiller').checked`
  (`settingsSave` listener, `app.js:3794–3822`);
- populate: `$('settingsRemoveFiller').checked = settings.remove_filler !== false;`
  (`populateSettingsForm`, `app.js:3864`) — default-ON semantics for missing key;
- dirty flag: `$('settingsRemoveFiller').addEventListener('change', markSettingsDirty);`
  (`app.js:3697` block).

`index.html` and `app.js` are service-worker shell assets (`static/sw.js:5–19`),
so this frontend change requires a `CACHE_NAME` bump to the **next unused
monotonic value** at land time. Current value is `meetings-v16-offline-queue`
(`sw.js:1`); the offline-sync spec reserves `v17` and attachment Phase B
reserves `v18` (see "Cross-feature coordination" in
`docs/superpowers/specs/2026-07-15-offline-notes-tasks-sync-design.md`). This
feature takes the **next unused monotonic value at land time**, checked
against `sw.js:1` **and any same-day sibling spec that landed first** —
in-meeting-notes, edit-everything, and company-tag each also nominate a
`v19-*` name — so `meetings-v19-filler-removal` applies only if no sibling
has claimed `v19`; never a reused value.

## Data flow

1. **New meeting, toggle ON** — STT segments → `raw_segments` deep copy (→
   `raw_transcript.json`, unstripped) → regex pre-pass (tokens stripped,
   pure-filler segments dropped) → LLM cleanup batches with
   preamble+`FILLER_DIRECTIVE` → cleaned segments → `transcript.json` /
   `.srt` / `.md`, `segment_count` refreshed.
2. **New meeting, toggle OFF** — identical to today, byte-for-byte prompts.
3. **Existing meeting** — user clicks **Re-cleanup** → (legacy meeting
   without `raw_transcript.json`: `transcript.json` is snapshotted to
   `raw_transcript.json` first) → raw segments from `raw_transcript.json` →
   same pre-pass + directive path → rewrite; then **Re-identify Speakers** →
   **Re-summarize** as desired.
4. **Undo** — toggle OFF → **Re-cleanup**: raw is re-cleaned without
   stripping. Once `raw_transcript.json` exists it is never modified by any
   of these paths; the single write that can *create* it is the legacy
   snapshot (`transcript.json` → `raw_transcript.json`, only when raw is
   absent, before a legacy meeting's first re-cleanup).

## Error handling

- **LLM batch failure / parse mismatch (toggle ON):** unchanged non-fatal
  behavior — the batch keeps its input text, which is now the *pre-passed*
  text, so deterministic filler stripping survives total LLM failure. Because
  the pre-pass bookkeeping (`transcript_text`, `segment_count`,
  `transcript_cleaned`) runs **before** the LLM `try:` block, even the outer
  catch-all (`app.py:1881–1883`) cannot leave stripped `segments` paired with
  an unstripped `transcript_text` or a stale count.
- **Pre-pass drops every segment:** guard returns input unchanged (count 0),
  logs a warning; the meeting is never emptied.
- **Legacy meeting (no `raw_transcript.json`):** the reprocess cleanup branch
  snapshots `transcript.json` → `raw_transcript.json` before rewriting; if
  the snapshot write fails, the re-cleanup aborts with an error — never strip
  the only remaining copy of the transcript.
- **Toggle OFF:** zero behavior change anywhere (no pre-pass, identical
  prompts, reprocess unchanged apart from the length-aware detection fix,
  which is a no-op for equal-length lists).
- **Settings file without the key:** `load_settings` fills `True` (default ON);
  non-bool garbage in `settings.json` is ignored by the `isinstance` guard.

## Testing (TDD, RED→GREEN)

House style: pytest with bare `TestClient` + `monkeypatch` (settings tests
monkeypatch `MEETINGS_DIR`/`SETTINGS_PATH` onto `tmp_path`, per
`tests/test_model_config.py::_settings_client`). No new JS logic module is
added — the checkbox is declarative DOM wiring — so no `node --test` file for
this feature.

New file `tests/test_filler_removal.py`:

- **`strip_fillers` vectors** (the deterministic contract):
  - `"Um, I think we should ship."` → `"I think we should ship."`
  - `"I think, um, that works."` → `"I think, that works."`
  - `"So, uh, yeah."` → `"So, yeah."`
  - `"UM, UH, moving on"` → `"Moving on"` (case-insensitive; comma-run collapse;
    recapitalization)
  - `"Ummm... errr let me think."` → `"Let me think."` (elongations; leading
    punctuation strip)
  - `"hmm, well, mm, fine"` → `"Well, fine"`
  - Word-boundary negatives, all unchanged: `"Take my umbrella."`,
    `"This summer we go ahead."`, `"The ermine hid."`
  - Affirmations preserved: `"Uh-huh, agreed."`, `"Mm-hmm."` unchanged.
- **`apply_filler_prepass`:**
  - Pure-filler segment (`"Um."`) is dropped; neighbors keep `start`/`end`/
    `speaker`; returned count includes drops.
  - Input list and its dicts are not mutated.
  - All-pure-filler input returns the input unchanged, count 0.
- **`_build_cleanup_prompt` toggle (regression-critical):** with settings on
  `tmp_path`:
  - toggle **OFF** → prompt contains no `FILLER_DIRECTIVE` text and equals the
    toggle-ON prompt with the directive excised — i.e. OFF output is
    byte-for-byte the pre-feature assembly (assert exact equality against a
    prompt built from the same inputs with the directive stripped);
  - toggle **ON** → directive appears exactly once, immediately after the
    preamble and before the meeting-context / segments blocks;
  - a **custom saved `cleanup_system` template** still appears verbatim in both
    cases (templates are never edited).
- **`step_cleanup_transcript` pre-pass:** monkeypatch `_retry_ollama_call` to
  echo input lines back; toggle ON → returned segments are stripped and the
  pure-filler segment is gone; toggle OFF → returned segments identical to
  input.
- **Reprocess path:**
  - `_segment_texts_differ`: equal lists → `False`; same texts but one list
    shorter (the dropped-segment case `zip` used to miss) → `True`; single text
    change → `True`.
  - `POST /meetings/{id}/reprocess` with `step="cleanup"` on a fixture meeting
    (tmp `output_dir` containing `raw_transcript.json` + `transcript.json`,
    status `complete`) returns `200` with the processing detail; and with
    `step_cleanup_transcript` monkeypatched to drop one segment while leaving
    the rest identical, the background task rewrites `transcript.json`
    (shorter, `"cleaned": true`) while `raw_transcript.json` stays
    byte-identical, and `m["segment_count"]` reflects the shorter list.
  - **Legacy snapshot:** the same reprocess on a fixture meeting *without*
    `raw_transcript.json` first snapshots `transcript.json` →
    `raw_transcript.json` (byte-identical to the pre-run `transcript.json`)
    before rewriting `transcript.json`.
- **Settings roundtrip:** `PUT /api/settings {"remove_filler": false}` persists
  across a fresh `GET`; a `settings.json` written *without* the key loads as
  `True` (default-ON migration).

RED first: write the vectors/tests against the not-yet-existing
`strip_fillers` / `apply_filler_prepass` / `FILLER_DIRECTIVE` /
`_segment_texts_differ`, watch them fail, then implement.

## Rollout

1. **Backend:** setting plumb (`DEFAULT_SETTINGS`, `load_settings`,
   `SettingsRequest`, `update_settings`) + `_FILLER_RE` / `strip_fillers` /
   `apply_filler_prepass` + `FILLER_DIRECTIVE` in `_build_cleanup_prompt` +
   pre-pass wiring in both call paths + `_segment_texts_differ` fix + tests.
2. **Frontend:** `#settingsRemoveFiller` checkbox (index.html + app.js save/
   populate/dirty wiring) + `sw.js` `CACHE_NAME` bump to the next unused value
   (see coordination note above).
3. **Deploy:** rebuild/redeploy the container (static assets baked into the
   image); verify on a real meeting that ums are gone and
   `raw_transcript.json` still has them; verify Re-cleanup → Re-identify
   Speakers → Re-summarize on one existing meeting (note: reprocess does not
   refresh Qdrant vectors or `m["transcript_text"]` — see the known
   limitation above — so search/chat-RAG keep the old text for that meeting
   until the reprocess-reindex gap is closed); verify a legacy meeting
   *without* `raw_transcript.json` gains the snapshot on Re-cleanup (the raw
   file appears, byte-identical to the pre-run `transcript.json`) and that a
   toggle-OFF Re-cleanup afterwards restores its unstripped text.
4. **Port to `craiglush/parley`** after the batch-1 features, in the same
   order, so the `sw.js` bump and `app.js` settings hunks rebase cleanly on
   their already-ported changes.

## Open questions

None. Default-ON, the two-layer split, the approved token list (with the
"err"-verb tradeoff), reuse of the existing reprocess step, and
prompt-build-time directive injection are all user-confirmed decisions.
