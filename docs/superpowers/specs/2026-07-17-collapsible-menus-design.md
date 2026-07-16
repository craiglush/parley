# Collapsible Menus — Design

**Date:** 2026-07-17
**Branch:** `feat/collapsible-menus`
**Status:** Approved scope (user-scoped 2026-07-16): three surfaces, **session-only state** (no persistence — explicit user decision; do NOT add localStorage for the new collapses).

## Goal

Every major content section on three surfaces can be collapsed/expanded with a
visible chevron affordance, so long meetings and busy panels stay scannable.
State lives in module memory: it survives re-renders within a session and
resets on reload.

## Surfaces & scope

### 1. Meeting detail sections (app.js + styles.css)

All dynamically rendered section blocks get a clickable heading with a chevron:

| Wrapper class | Headings (examples) | Renderer |
|---|---|---|
| `.summary-section` | Summary, Key Topics, Action Items, Decisions, Open Questions, Concerns & Risks, Key Figures & Dates, Sentiment | `renderSummary` (app.js:2943) |
| `.tags-section` | Category, Keywords, Entities, Related Meetings | `renderTags` (app.js:2295) |
| `.notes-section` | Notes, Transcript Annotations | `renderNotes` (app.js:4010) |
| `.link-section` | Linked Meetings, Suggested Links, Related Notes | `loadRelated` (app.js:2397+) |

**Mechanism — no renderer rewrites.** The section markup is innerHTML
template strings; do not convert them to createElement or add wrapper divs.
Instead:

- **CSS-only chevron:** a `::before` glyph on the section `h3` (`▾`, rotated
  `-90deg` when the wrapper has `.collapsed`), `cursor:pointer` on the h3.
  Reuse the app's `.collapsed` idiom (meeting-list groups styles.css:1652-1655,
  notes-tree folders notes-tasks.css:108-109).
- **Hide rule per wrapper class** (direct-child selectors, so nested markup is
  untouched):
  - `.summary-section.collapsed > :not(h3) { display:none }`
  - `.tags-section.collapsed > :not(h3) { display:none }`
  - `.link-section.collapsed > :not(h3) { display:none }`
  - `.notes-section.collapsed > :not(.notes-section-header) { display:none }`
    (its h3 sits inside `.notes-section-header`)
- **One delegated click handler** (document-level, capture NOT needed) that
  matches clicks on those headings, toggles `.collapsed` on the closest
  wrapper, and records state. It must IGNORE clicks originating on
  `button`/`a`/`input` inside a heading (the notes-section header contains an
  add button).
- **Session state + re-apply:** module-scoped
  `const collapsedDetailSections = {}` keyed by
  `sectionKey = wrapperClass + ':' + headingText.trim()` — GLOBAL across
  meetings (collapsing "Key Topics" keeps it collapsed on every meeting this
  session; a layout preference, not per-item data). A small
  `applyDetailCollapse(containerEl)` walks the four wrapper classes and
  re-adds `.collapsed` from the map; call it immediately after each renderer
  writes innerHTML (`renderSummary`, `renderTags`, `renderNotes`, and each
  `loadRelated` injection — desktop AND `…Mobile` containers, which are
  DUPLICATED DOM so both need the pass).
- Accessibility: the heading gets `role="button"`, `tabindex="0"`,
  `aria-expanded`, and Enter/Space triggers the same toggle (add these
  attributes inside `applyDetailCollapse` so template strings stay untouched).
- The transcript tab has no section headings — out of scope (tab switching
  already isolates it). Chat likewise.

### 2. Recording panel sections (index.html + app.js + styles.css)

Match the batch-2 capture-notes pattern (index.html:149-165, app.js:625+,
styles.css:2250-2285): `button.aria-expanded` head + `▸/▾` chevron span +
body `[hidden]` toggle — but **session-only: no localStorage read/write** for
the two NEW sections (capture-notes keeps its existing localStorage behavior;
do not change it).

- **Live speakers roster** `#liveSpeakers` (index.html:129): add a
  `.capture-notes-head`-style header row ("Speakers" + chevron button
  `id="liveSpeakersToggle"`) above the chips; chips container
  `#liveSpeakerChips` moves inside a new `#liveSpeakersBody` (initially NOT
  hidden — default open). The whole `#liveSpeakers[hidden]` show/hide gate is
  unchanged (roster only appears when attendees exist).
- **Upload details** `#uploadFields` (index.html:171): add the same header row
  ("Details" + chevron `id="uploadFieldsToggle"`) wrapping title / speakers /
  context / upload button into `#uploadFieldsBody`. Default open. Collapsing
  hides the form but NEVER blocks programmatic reads (`hidden` only affects
  display; JS reads of `.value` still work — verify the upload path reads
  values fine while collapsed).
- Generalize: extract the toggle wiring into a tiny helper
  `wireCollapse(toggleEl, bodyEl)` in app.js (sets `hidden`, `aria-expanded`,
  chevron glyph) used by both new sections; capture-notes keeps its existing
  `captureNotes.setOpen` untouched.
- Reuse the existing `.capture-notes-toggle`/`.capture-notes-chevron` CSS
  classes on the new headers (rename-free reuse; add a shared alias class only
  if styling needs to diverge — it shouldn't).

### 3. Notes workspace tree pane (index.html + notes-tasks.js + notes-tasks.css)

Per-folder collapse already exists and is already session-only
(notes-tasks.js:445,949) — unchanged. The gap is the **whole tree pane on
desktop**:

- Add a desktop pane toggle: show the existing `.nt-tree-toggle` (☰) button on
  desktop too (notes-tasks.css:146 currently `display:none` outside mobile).
  On desktop it toggles class `tree-collapsed` on `.notes-view`;
  CSS `.notes-view.tree-collapsed .notes-tree { display:none }` (desktop media
  query ≥769px only). On mobile it keeps the existing `tree-open` drawer
  behavior (notes-tasks.js:1045-1048) — branch on the existing
  `isMobileWidth()` helper inside the click handler.
- Session-only by nature (class on a DOM element; no storage).
- The editor pane must expand to fill the freed width (the layout is
  flex/grid — verify `.notes-doc` grows; add `flex:1`/`grid-template` fix if
  needed).

## Non-goals

- No persistence (localStorage/sessionStorage) for ANY new collapse state.
- No changes to tab switching, the mobile drawer, meeting-list group collapse,
  per-folder collapse, or capture-notes' existing localStorage behavior.
- No `<details>/<summary>` conversion; no backend changes; no new endpoints.

## Versioning (assign at land time: current value +1)

- `static/app.js` `?v=19 → 20`; `static/styles.css` `?v=16 → 17`;
  `static/notes-tasks.js` `?v=15 → 16`; `static/notes-tasks.css` `?v=12 → 13`;
  `sw.js` `CACHE_NAME meetings-v24-capture-notes → meetings-v25-collapsible`.
- index.html is modified (recording-panel markup + ?v bumps) — served fresh,
  no version tag of its own.

## Testing (per the project's test policy)

- Frontend-only feature: `node --check` on app.js and notes-tasks.js per task.
- Pure-logic extraction is NOT warranted (state maps are one-line toggles) —
  do not create a new *-logic.js file for this.
- Final integration checkpoint (last task): full python suite (must stay
  442+1skip — proves zero backend impact), all four JS suites by file path,
  node --check on both touched JS files.
- Manual browser verification is the user's walkthrough (session-collapse
  behavior, re-render survival, mobile drawer regression check).

## Risks

- The `…Mobile` duplicated detail containers: applying collapse state must hit
  both copies or desktop/mobile drift (the delegated handler works on both for
  free; the re-apply pass must be called for both).
- `renderNotes` header contains interactive buttons — the delegation guard
  (ignore button/a/input clicks) is load-bearing.
- Collapsing `#uploadFields` must not break upload (values read while hidden).
