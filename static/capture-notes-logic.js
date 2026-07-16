// Pure capture-notes helpers — shared by app.js (loaded as a browser global via
// a <script> tag BEFORE app.js, next to queue-logic.js) and the Node unit test
// (tests/js/capture_notes_logic.test.mjs). Deliberately free of DOM / IndexedDB
// access so it can be unit-tested in isolation.
//
// captureNoteTitle(title, context, isoDate): auto-title for the in-meeting note
// created on the first keystroke — "<base> — notes (YYYY-MM-DD)" where base is
// the trimmed meeting title, else the trimmed meeting context, else "Meeting".
// Evaluated at first keystroke; if created with the bare fallback and the user
// fills the title before Upload, app.js retitles ONCE (frontmatter title only —
// the vault filename keeps its creation-time slug).
function captureNoteTitle(title, context, isoDate) {
  const base = String(title || '').trim() || String(context || '').trim() || 'Meeting';
  return base + ' — notes (' + String(isoDate || '').slice(0, 10) + ')';
}

// isTempNoteId(id): true iff the id is a batch-1 NotesSync mirror temp id
// ('n_local_' prefix — 'local' is not hex, so it can never collide with a real
// 'n_' + 12-hex id). TEMP-ID RULE: a temp id is never sent to the server in any
// payload — senders resolve it (flush + re-read meta) or omit the field.
function isTempNoteId(id) {
  return typeof id === 'string' && id.indexOf('n_local_') === 0;
}

// Dual export: attach to the browser global (so app.js can call them bare after
// the <script> load, exactly like queue-logic.js's selectQueuedSessions) and
// expose via CommonJS for the Node test runner. No ES-module syntax.
if (typeof window !== 'undefined') {
  window.captureNoteTitle = captureNoteTitle;
  window.isTempNoteId = isTempNoteId;
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { captureNoteTitle, isTempNoteId };
}
