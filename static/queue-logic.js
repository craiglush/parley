// Pure queue-selection logic — shared by app.js (loaded as a browser global via
// a <script> tag BEFORE app.js) and the Node unit test (tests/js/queue_logic.test.mjs).
// Deliberately free of DOM / IndexedDB access so it can be unit-tested in isolation.
//
// selectQueuedSessions(metas, activeSid, excludeSids): given the raw meta records
// from the capture store, return only the sessions eligible for AUTOMATIC upload —
// those explicitly flagged queued===true — excluding the session the current tab
// is actively recording (k === activeSid) and any sid in excludeSids (a Set or
// array of sids already uploaded this page-session, so a silently-failed local
// delete can't cause a duplicate re-upload). Newest-first by startedAt.
// Never-submitted crash-recovered drafts (no queued / queued:false) are omitted;
// they stay in the manual recover/discard list.
function selectQueuedSessions(metas, activeSid, excludeSids) {
  const exclude = excludeSids instanceof Set ? excludeSids
    : Array.isArray(excludeSids) ? new Set(excludeSids) : null;
  return (Array.isArray(metas) ? metas : [])
    .filter(m => m && m.queued === true && m.k !== activeSid && !(exclude && exclude.has(m.k)))
    .slice()
    .sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
}

// Dual export: attach to the browser global (so app.js can call it after the
// <script> load) and expose via CommonJS for the Node test runner. No ES-module
// syntax is used so the plain <script> load stays valid in the browser.
if (typeof window !== 'undefined') { window.selectQueuedSessions = selectQueuedSessions; }
if (typeof module !== 'undefined' && module.exports) { module.exports = { selectQueuedSessions }; }
