const API = '';  // Same origin

// --- State ---
let selectedFile = null;
let pollTimer = null;
let currentMeetingId = null;
let titleFilterDebounce = null;
let currentView = 'week';   // week | speaker | keyword | category | linked
let groupedData = null;
let captureCollapsed = true;
let linkPickerMeetingId = null;
let allMeetingsCache = [];  // cache for link picker

// Notes & Chat state
let currentNotes = [];
let chatHistory = [];
let chatScope = null;
let chatAbortController = null;
let insightsCache = {};  // { meetingId: { list: [...], activeId: null } }

// --- DOM refs ---
const $ = id => document.getElementById(id);
let audioPlayerMeetingId = null;
const dropZone = $('dropZone');
const fileInput = $('fileInput');
const uploadFields = $('uploadFields');
const uploadBtn = $('uploadBtn');
const detailOverlay = $('detailOverlay');
const searchInput = $('searchInput');
const searchResults = $('searchResults');

// --- Recording ---
let mediaRecorder = null;
let recordedChunks = [];
let recordingStartTime = null;
let recordTimerInterval = null;
let audioContext = null;
let analyserNode = null;
let vizAnimFrame = null;
let recordSource = 'both'; // 'mic', 'screen', or 'both'

// Pause/resume state
let pausedDuration = 0;
let pauseStartTime = null;

// Extra streams to clean up for combined mode
let extraStreams = [];

// Live signal monitoring (drives the "no audio detected" safety net).
let recordingPeakLevel = 0;   // max audio level seen this session (0..255 scale)
let lastSignalTime = 0;       // timestamp of the last frame with real signal
let noAudioWarned = false;    // whether the no-audio warning is currently shown

// True when a *recording* is staged but not yet uploaded (vs a drag-dropped
// file, which still exists on disk). Drives the beforeunload / nav "are you
// sure" prompts. stagedSilent gates the pre-upload silent-recording confirm.
let stagedFromRecording = false;
let stagedSilent = false;

// ---------------------------------------------------------------------------
// Capture store (IndexedDB) — autosave the in-progress recording so it survives
// navigating away, switching functions, reload, or a crash. v2: every recording
// gets its OWN session — starting a new recording never wipes a previous one,
// and chunks persist every second (not just on stop). A session is removed only
// after its successful upload, an explicit confirmed discard, or a 14-day
// expiry sweep. No backend involvement.
// ---------------------------------------------------------------------------
const CAP_DB = 'meeting-capture';
const CAP_VERSION = 2;
const CAP_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;
let capDbPromise = null;

function capOpenDB() {
  if (capDbPromise) return capDbPromise;
  capDbPromise = new Promise((resolve, reject) => {
    let req;
    try { req = indexedDB.open(CAP_DB, CAP_VERSION); }
    catch (e) { reject(e); return; }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta', { keyPath: 'k' });
      if (!db.objectStoreNames.contains('chunks')) db.createObjectStore('chunks', { keyPath: ['sid', 'seq'] });
    };
    req.onblocked = () => console.warn('meeting-capture upgrade blocked — close other tabs of this app so autosave can migrate');
    req.onsuccess = () => {
      const db = req.result;
      // Release our connection if another tab needs to upgrade the schema,
      // so that tab's autosave isn't silently blocked. Next use reopens.
      db.onversionchange = () => { try { db.close(); } catch (_) {} capDbPromise = null; };
      resolve(db);
    };
    req.onerror = () => reject(req.error);
  });
  return capDbPromise;
}

function capTx(db, mode, stores, fn) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(stores, mode);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
    fn(tx);
  });
}

let capSeq = 0;
// Session this tab's recorder is currently writing (null when not recording).
let capSessionId = null;
// Session backing the staged/recovered file in the upload box (null when the
// staged file came from disk instead of the recorder/recovery).
let stagedSessionId = null;

// --- In-meeting notes (capture panel) state ---
// The note is a REAL vault note in the NotesSync mirror from the first
// keystroke; this state only binds the current recording to it. Reset by
// setupRecorderFromStream (new recording — unconditional, belt-and-braces),
// upload success, the remove/discard handler, and the offline-queued branch.
// Resetting NEVER touches the data: the queued session's meta keeps its
// note_id and the note itself stays in the vault/mirror.
let captureNoteId = null;          // temp (n_local_…) or real (n_…) note id
let captureNoteCreate = null;      // in-flight createNote promise (single-flight guard)
let captureNoteTitleAuto = false;  // created with the bare "Meeting — notes" fallback
let captureNoteSaveTimer = null;   // debounce timer for NotesSync.updateNote
// In-flight capRewriteNoteId promises pushed by the notes-sync:remap listener.
// uploadSession awaits these AFTER NotesSync.flush() (the awaited barrier):
// the CustomEvent dispatch is synchronous but the listener's IDB
// read-modify-write is async, so flush() resolving alone proves nothing about
// when the meta rewrite commits.
const captureNoteRemapPending = [];

// All chunk keys for one session: [sid] <= key <= [sid, <anything>].
function capSessionRange(sid) {
  return IDBKeyRange.bound([sid], [sid, []]);
}

// Start a fresh autosave session for a new recording. Previous sessions are
// left untouched — they stay recoverable until uploaded or discarded.
async function capStartSession(meta) {
  capSeq = 0;
  capSessionId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  try {
    const db = await capOpenDB();
    const sid = capSessionId;
    await capTx(db, 'readwrite', ['meta'], tx => {
      tx.objectStore('meta').put({ k: sid, status: 'recording', ...meta });
    });
  } catch (e) { console.warn('capStartSession failed (autosave disabled):', e); }
}

// Persist one recorded chunk (called for every MediaRecorder dataavailable).
async function capAppendChunk(blob) {
  if (!capSessionId) return;
  try {
    const db = await capOpenDB();
    const sid = capSessionId, seq = capSeq++;
    await capTx(db, 'readwrite', ['chunks'], tx => {
      tx.objectStore('chunks').put({ sid, seq, blob });
    });
  } catch (e) { /* best-effort; don't disrupt recording */ }
}

// Mark the active session stopped (kept for recovery until uploaded or discarded).
async function capMarkStopped(extra) {
  const sid = capSessionId;
  if (!sid) return;
  try {
    const db = await capOpenDB();
    const meta = await new Promise((res) => {
      const tx = db.transaction(['meta'], 'readonly');
      const r = tx.objectStore('meta').get(sid);
      r.onsuccess = () => res(r.result); r.onerror = () => res(null);
    });
    if (!meta) return;
    await capTx(db, 'readwrite', ['meta'], tx => {
      tx.objectStore('meta').put({ ...meta, status: 'stopped', ...extra });
    });
  } catch (e) { /* best-effort */ }
}

// Persist the current live-tag roster/markers onto the active session's meta
// record, so they survive a reload/crash exactly like the audio chunks do.
async function capSaveTags(sid, roster, markers) {
  if (!sid) return;
  try {
    const db = await capOpenDB();
    const meta = await new Promise((res) => {
      const tx = db.transaction(['meta'], 'readonly');
      const r = tx.objectStore('meta').get(sid);
      r.onsuccess = () => res(r.result); r.onerror = () => res(null);
    });
    if (!meta) return;
    await capTx(db, 'readwrite', ['meta'], tx => {
      tx.objectStore('meta').put({ ...meta, roster, markers });
    });
  } catch (e) { /* best-effort */ }
}

// Set (or clear) the auto-upload `queued` flag on a session's meta record, using
// the same read-modify-write path as capSaveTags. When queueing (queued=true) we
// also persist the just-submitted title/speakers/context so the background flush
// reproduces the exact payload the user hit Upload with (meta already stores the
// live-tag roster/markers via capSaveTags). Best-effort; never throws.
async function capSetQueued(sid, queued, session) {
  if (!sid) return;
  try {
    const db = await capOpenDB();
    const meta = await new Promise((res) => {
      const tx = db.transaction(['meta'], 'readonly');
      const r = tx.objectStore('meta').get(sid);
      r.onsuccess = () => res(r.result); r.onerror = () => res(null);
    });
    if (!meta) return;
    const next = { ...meta, queued: !!queued };
    if (queued && session) {
      if (session.title != null) next.title = session.title;
      if (session.speakers != null) next.speakers = session.speakers;
      if (session.context != null) next.context = session.context;
      if (Array.isArray(session.markers)) next.markers = session.markers;
      if (Array.isArray(session.roster)) next.roster = session.roster;
      if (session.note_id != null) next.note_id = session.note_id;
    }
    await capTx(db, 'readwrite', ['meta'], tx => {
      tx.objectStore('meta').put(next);
    });
  } catch (e) { /* best-effort */ }
}

// Read one session's meta record (null on any failure). Used by the
// capture-notes temp-id resolve/retry paths.
async function capGetMeta(sid) {
  if (!sid) return null;
  try {
    const db = await capOpenDB();
    return await new Promise((res) => {
      const tx = db.transaction(['meta'], 'readonly');
      const r = tx.objectStore('meta').get(sid);
      r.onsuccess = () => res(r.result || null); r.onerror = () => res(null);
    });
  } catch (_) { return null; }
}

// Persist the in-meeting note id onto a session's meta record — the same
// read-modify-write shape as capSaveTags, so the note→meeting association
// survives reload, crash, and the offline queue for free. Best-effort.
async function capSetNoteId(sid, noteId) {
  if (!sid) return;
  try {
    const db = await capOpenDB();
    const meta = await capGetMeta(sid);
    if (!meta) return;
    await capTx(db, 'readwrite', ['meta'], tx => {
      tx.objectStore('meta').put({ ...meta, note_id: noteId });
    });
  } catch (e) { /* best-effort */ }
}

// After a NotesSync create-flush remap (temp id → server id), rewrite EVERY
// capture meta record still holding the temp id — covers queued sessions from
// earlier recordings, not just the active one. The remap listener parks the
// returned promise in captureNoteRemapPending so uploadSession can await the
// rewrite commit before re-reading the meta.
async function capRewriteNoteId(tempId, serverId) {
  try {
    const db = await capOpenDB();
    const metas = await new Promise((res) => {
      const tx = db.transaction(['meta'], 'readonly');
      const r = tx.objectStore('meta').getAll();
      r.onsuccess = () => res(r.result || []); r.onerror = () => res([]);
    });
    for (const meta of metas) {
      if (meta.note_id === tempId) {
        await capTx(db, 'readwrite', ['meta'], tx => {
          tx.objectStore('meta').put({ ...meta, note_id: serverId });
        });
      }
    }
  } catch (e) { /* best-effort */ }
}

// Remove ONE saved session (after its successful upload or explicit discard).
async function capClear(sid) {
  if (!sid) return;
  try {
    const db = await capOpenDB();
    await capTx(db, 'readwrite', ['meta', 'chunks'], tx => {
      tx.objectStore('meta').delete(sid);
      tx.objectStore('chunks').delete(capSessionRange(sid));
    });
  } catch (e) { /* best-effort */ }
}

// Load every recoverable session (newest first). Returns [{meta, blob}, ...].
async function capLoadAllPending() {
  try {
    const db = await capOpenDB();
    const metas = await new Promise((res) => {
      const tx = db.transaction(['meta'], 'readonly');
      const r = tx.objectStore('meta').getAll();
      r.onsuccess = () => res(r.result || []); r.onerror = () => res([]);
    });
    const out = [];
    for (const meta of metas) {
      const sid = meta.k;
      if (sid === capSessionId) continue;   // the recording this tab is writing right now
      const chunks = await new Promise((res) => {
        const tx = db.transaction(['chunks'], 'readonly');
        const r = tx.objectStore('chunks').getAll(capSessionRange(sid));
        r.onsuccess = () => res(r.result || []); r.onerror = () => res([]);
      });
      if (!chunks.length) { capClear(sid); continue; }   // stale meta with no data
      if ((meta.startedAt || 0) < Date.now() - CAP_MAX_AGE_MS) {
        console.warn('Expiring autosaved recording older than 14 days:', meta.startedAt, meta.fileName || '');
        capClear(sid);
        continue;
      }
      chunks.sort((a, b) => a.seq - b.seq);
      out.push({ meta, blob: new Blob(chunks.map(c => c.blob), { type: meta.mimeType || 'audio/webm' }) });
    }
    out.sort((a, b) => (b.meta.startedAt || 0) - (a.meta.startedAt || 0));
    return out;
  } catch (e) { return []; }
}

// One-time import: older builds saved a single backup in DB 'meeting-service' /
// store 'unsaved-recordings' (whole blob, written on stop). Move any pending
// entry into the session store so it shows in the recovery list, then remove
// the old copy so it isn't imported twice.
async function capImportLegacyBackup() {
  try {
    if (indexedDB.databases) {
      const dbs = await indexedDB.databases();
      if (!dbs.some(d => d.name === 'meeting-service')) return;
    }
    const old = await new Promise((res, rej) => {
      const r = indexedDB.open('meeting-service', 1);
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    });
    if (!old.objectStoreNames.contains('unsaved-recordings')) { old.close(); return; }
    const entry = await new Promise((res) => {
      const tx = old.transaction('unsaved-recordings', 'readonly');
      const q = tx.objectStore('unsaved-recordings').get('current');
      q.onsuccess = () => res(q.result); q.onerror = () => res(null);
    });
    if (entry && entry.blob && entry.blob.size) {
      const sid = 'legacy-' + (entry.createdAt || Date.now());
      const db = await capOpenDB();
      await capTx(db, 'readwrite', ['meta', 'chunks'], tx => {
        tx.objectStore('meta').put({
          k: sid, status: 'stopped',
          mimeType: entry.mimeType || 'audio/webm',
          startedAt: entry.createdAt || Date.now(),
          durationLabel: entry.duration, fileName: entry.fileName,
        });
        tx.objectStore('chunks').put({ sid, seq: 0, blob: entry.blob });
      });
    }
    const tx2 = old.transaction('unsaved-recordings', 'readwrite');
    tx2.objectStore('unsaved-recordings').delete('current');
    await new Promise((res) => { tx2.oncomplete = res; tx2.onerror = res; tx2.onabort = res; });
    old.close();
  } catch (e) { /* best-effort migration */ }
}

// ---------------------------------------------------------------------------
// Streaming shadow backup — mirror each recorded chunk to the server AS it
// records, so a copy survives even if this device dies before upload. Purely
// additive and best-effort: it never blocks the recorder or the IndexedDB
// autosave, and does nothing when disabled or the server is unreachable.
// See docs/superpowers/specs/2026-07-07-streaming-capture-design.md.
// ---------------------------------------------------------------------------
function streamBackupEnabled() {
  return localStorage.getItem('captureStreamBackup') !== 'off';   // default on
}

const capStream = { sid: null, seq: 0, queue: [], sending: false, fails: 0, quietUntil: 0, announced: false, startMeta: null };

// Begin a new capture. sid matches the local autosave session id, so the server
// copy and the local copy share one identity (dedup + delete-by-sid). The actual
// "announce" POST is deferred to the sender so it is guaranteed to complete
// BEFORE any chunk is sent (chunk seq 0 carries the webm header — it must land
// first and must never be dropped).
function capStreamStart(sid, meta) {
  Object.assign(capStream, { sid: null, seq: 0, queue: [], sending: false, fails: 0, quietUntil: 0, announced: false, startMeta: null });
  if (!streamBackupEnabled()) return;
  capStream.sid = sid;
  capStream.startMeta = { sid, mimeType: meta.mimeType, source: meta.source, startedAt: meta.startedAt };
}

function capStreamEnqueue(blob) {
  if (!capStream.sid) return;
  capStream.queue.push({ seq: capStream.seq++, blob });
  capStreamPump();
}

// Single-flight sender: announces the capture (once), then drains the queue in
// strict seq order. On any transient failure it backs off and KEEPS the chunk
// queued — only a 413 (a chunk/capture that can never fit) is dropped — so a
// slow announce or a brief outage can never discard the header chunk.
async function capStreamPump() {
  if (capStream.sending || !capStream.sid) return;
  if (Date.now() < capStream.quietUntil) { capStreamScheduleRetry(); return; }
  capStream.sending = true;
  try {
    if (!capStream.announced) {
      const r = await fetch(`${API}/captures`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(capStream.startMeta),
      });
      if (!r.ok) throw new Error('announce HTTP ' + r.status);
      capStream.announced = true;
    }
    while (capStream.queue.length) {
      const { seq, blob } = capStream.queue[0];
      const resp = await fetch(`${API}/captures/${capStream.sid}/chunks/${seq}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: blob,
      });
      if (resp.ok) { capStream.queue.shift(); continue; }
      if (resp.status === 413) { capStream.queue.shift(); continue; }  // never fits — safe to skip
      throw new Error('chunk HTTP ' + resp.status);   // 404 race / 5xx / etc: retry, don't drop
    }
    capStream.fails = 0;
  } catch (_) {
    capStream.fails++;
    capStream.quietUntil = Date.now() + (capStream.fails >= 5 ? 60000 : Math.min(1000 * 2 ** capStream.fails, 30000));
  } finally {
    capStream.sending = false;
  }
  if (capStream.sid && capStream.queue.length) capStreamScheduleRetry();
}

let _capStreamTimer = null;
function capStreamScheduleRetry() {
  if (_capStreamTimer) return;
  const wait = Math.max(250, capStream.quietUntil - Date.now());
  _capStreamTimer = setTimeout(() => { _capStreamTimer = null; capStreamPump(); }, wait);
}

// Flush best-effort, then mark the server capture stopped.
async function capStreamStop(extra) {
  const sid = capStream.sid;
  if (!sid) return;
  await capStreamPump();
  try {
    await fetch(`${API}/captures/${sid}/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ durationLabel: extra && extra.durationLabel, fileName: extra && extra.fileName }),
    });
  } catch (_) { /* best-effort */ }
}

// Drop the server-side copy (after a successful normal upload or a discard).
function capStreamDelete(sid) {
  if (!sid) return;
  fetch(`${API}/captures/${sid}`, { method: 'DELETE' }).catch(() => {});
}

// Recorded-elapsed seconds — the same timeline as transcript segment start/end
// (pause time excluded). Mirrors updateRecordTimer's basis.
function recordedElapsedSeconds() {
  if (!recordingStartTime) return 0;
  const paused = pausedDuration + (pauseStartTime ? Date.now() - pauseStartTime : 0);
  return Math.max(0, (Date.now() - recordingStartTime - paused) / 1000);
}

// ---------------------------------------------------------------------------
// Live speaker tagging — capture WHO is talking during the call. Markers are
// timestamped in recorded-elapsed seconds and reconciled to diarized clusters
// server-side after processing. Purely additive; no tags == today's behavior.
// Names are always rendered via textContent (never innerHTML) — XSS-safe.
// ---------------------------------------------------------------------------
const liveTags = {
  roster: [],        // [{name, company, title}]
  markers: [],       // [{t, name}]
  activeName: null,  // last-tapped (active-speaker highlight)
  _suggestions: [],
  el: null, chipsEl: null, addEl: null, listEl: null,

  reset() {
    this.roster = []; this.markers = []; this.activeName = null;
    if (this.chipsEl) this.chipsEl.innerHTML = '';   // static clear
  },

  start() {
    this.el = document.getElementById('liveSpeakers');
    this.chipsEl = document.getElementById('liveSpeakerChips');
    this.addEl = document.getElementById('liveSpeakerAdd');
    this.listEl = document.getElementById('liveSpeakerList');
    if (!this.el) return;
    this.reset();
    this.el.hidden = false;
    this._loadSuggestions();          // populate datalist from GET /people (Task 8)
    this.addEl.onkeydown = (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        this.addPerson(this.addEl.value);
        this.addEl.value = '';
      }
    };
    this.render();
  },

  stop() {
    if (this.el) this.el.hidden = true;
    this._flush();                    // final push to server (Task 8)
  },

  addPerson(name) {
    name = (name || '').trim();
    if (!name) return null;
    let p = this.roster.find(r => r.name.toLowerCase() === name.toLowerCase());
    if (!p) {
      const known = (this._suggestions || []).find(s => s.name.toLowerCase() === name.toLowerCase());
      p = { name, company: known ? known.company : '', title: known ? known.title : '' };
      this.roster.push(p);
      this.render();
      this._flush();
    }
    return p;
  },

  removePerson(name) {
    this.roster = this.roster.filter(r => r.name !== name);
    this.markers = this.markers.filter(m => m.name !== name);
    if (this.activeName === name) this.activeName = null;
    this.render();
    this._flush();
  },

  tag(name) {
    const p = this.addPerson(name);
    if (!p) return;
    this.markers.push({ t: Math.round(recordedElapsedSeconds() * 10) / 10, name: p.name });
    this.activeName = p.name;         // persist highlight for active-speaker mode
    this.render();
    this._flush();
  },

  render() {
    if (!this.chipsEl) return;
    this.chipsEl.innerHTML = '';      // static clear; children rebuilt below
    for (const p of this.roster) {
      const count = this.markers.filter(m => m.name === p.name).length;
      const chip = document.createElement('div');
      chip.className = 'live-speaker-chip' + (this.activeName === p.name ? ' active' : '');
      chip.onclick = (e) => { if (!e.target.classList.contains('chip-remove')) this.tag(p.name); };
      // Static markup only (no user data interpolated); the name is set via
      // textContent immediately after — verified-safe DOM construction.
      chip.innerHTML =
        `<span class="chip-name"></span>` +
        (count ? `<span class="chip-count">${count}</span>` : '') +
        `<span class="chip-remove" title="Remove">×</span>`;
      chip.querySelector('.chip-name').textContent = p.name;   // user value -> textContent
      chip.querySelector('.chip-remove').onclick = () => this.removePerson(p.name);
      this.chipsEl.appendChild(chip);
    }
  },

  async _loadSuggestions() {
    try {
      const r = await fetch(`${API}/people`);
      this._suggestions = r.ok ? await r.json() : [];
    } catch (_) { this._suggestions = []; }
    if (this.listEl) {
      this.listEl.innerHTML = '';     // static clear
      for (const s of (this._suggestions || [])) {
        const opt = document.createElement('option');
        opt.value = s.name;           // user value -> property, not HTML
        this.listEl.appendChild(opt);
      }
    }
  },

  // Best-effort mirror of the current tags to the server capture (survives
  // device death → adopt). Debounced; never blocks the UI.
  _flush() {
    if (!capSessionId) return;
    clearTimeout(this._flushTimer);
    this._flushTimer = setTimeout(() => {
      // Local save always happens — this is what lets tags survive a
      // reload/crash even when server streaming backup is switched off.
      capSaveTags(capSessionId, this.roster.slice(), this.markers.slice());
      if (!streamBackupEnabled()) return;
      const titleEl = document.getElementById('meetingTitle');
      const ctxEl = document.getElementById('meetingContext');
      // TEMP-ID RULE: only a REAL note id ever leaves the device. A temp id is
      // meaningless to the server; the remap listener re-posts once it's real.
      const noteId = (captureNoteId && !isTempNoteId(captureNoteId)) ? captureNoteId : null;
      fetch(`${API}/captures/${capSessionId}/tags`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          markers: this.markers,
          roster: this.roster,
          title: titleEl ? titleEl.value.trim() : '',
          context: ctxEl ? ctxEl.value.trim() : '',
          ...(noteId ? { note_id: noteId } : {}),
        }),
      }).catch(() => {});
    }, 400);
  },
};

// Re-post the tags mirror with the now-real note id so the SERVER capture meta
// learns it (dead-device adopt linking depends on this — a server-only capture
// has no local meta left). Explicit sid: works for the staged session too,
// where capSessionId is already null. capture_tags stores note_id
// only-when-present, so this can never wipe roster/title state.
function capturePostTagsMirror(sid, noteId) {
  if (!sid || !streamBackupEnabled()) return;
  const titleEl = document.getElementById('meetingTitle');
  const ctxEl = document.getElementById('meetingContext');
  fetch(`${API}/captures/${sid}/tags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      markers: liveTags.markers,
      roster: liveTags.roster,
      title: titleEl ? titleEl.value.trim() : '',
      context: ctxEl ? ctxEl.value.trim() : '',
      note_id: noteId,
    }),
  }).catch(() => {});
}

// Shared collapse/expand wiring for capture-notes-style header+body pairs.
// Session-only: no localStorage — used by the recording-panel sections below
// (liveSpeakers, uploadFields). captureNotes keeps its own setOpen()/
// localStorage logic untouched (it predates and is unrelated to this helper).
function wireCollapse(toggleEl, bodyEl) {
  if (!toggleEl || !bodyEl) return;
  const chevronEl = toggleEl.querySelector('.capture-notes-chevron');
  toggleEl.addEventListener('click', () => {
    const open = bodyEl.hidden;   // hidden now -> clicking opens it
    bodyEl.hidden = !open;
    toggleEl.setAttribute('aria-expanded', String(open));
    if (chevronEl) chevronEl.textContent = open ? '▾' : '▸';
  });
}

// ---------------------------------------------------------------------------
// In-meeting Notes — collapsible panel in the recording area writing a REAL
// vault note through window.NotesSync (batch-1 offline mirror) from the first
// keystroke. The note id rides the capture session meta (note_id) so upload /
// offline queue / adopt can link note → meeting server-side. NotesSync is
// feature-detected lazily at event time (it loads via defer, after app.js);
// when absent the section never renders — zero behavior change elsewhere.
// Names/titles rendered via textContent only (the live-tag chips XSS rule).
// ---------------------------------------------------------------------------
const captureNotes = {
  el: null, toggleEl: null, chevronEl: null, statusEl: null, bodyEl: null,
  textEl: null, attachBtn: null, fileEl: null, hintEl: null,
  _title: '',   // title the note was created with / retitled to (status line)

  _bind() {
    if (this.el) return true;
    this.el = document.getElementById('captureNotes');
    if (!this.el) return false;
    this.toggleEl = document.getElementById('captureNotesToggle');
    this.chevronEl = this.el.querySelector('.capture-notes-chevron');
    this.statusEl = document.getElementById('captureNoteStatus');
    this.bodyEl = document.getElementById('captureNotesBody');
    this.textEl = document.getElementById('captureNotesText');
    this.attachBtn = document.getElementById('captureNotesAttach');
    this.fileEl = document.getElementById('captureNotesFile');
    this.hintEl = document.getElementById('captureNotesHint');
    this.toggleEl.addEventListener('click', () => this.setOpen(this.bodyEl.hidden));
    this.textEl.addEventListener('input', () => this._onInput());
    this.attachBtn.addEventListener('click', () => this._onAttachClick());
    this.fileEl.addEventListener('change', () => this._onFilePicked());
    window.addEventListener('online', () => this._updateAttachState());
    window.addEventListener('offline', () => this._updateAttachState());
    return true;
  },

  // Collapsed/expanded persists in localStorage — the streamBackupEnabled
  // pattern (default OPEN; only an explicit 'off' collapses).
  setOpen(open) {
    this.bodyEl.hidden = !open;
    this.toggleEl.setAttribute('aria-expanded', String(open));
    if (this.chevronEl) this.chevronEl.textContent = open ? '▾' : '▸';
    try { localStorage.setItem('captureNotesOpen', open ? 'on' : 'off'); } catch (_) {}
  },

  // Show for a new recording or a recovered session. Never shown for
  // disk-picked files (no capture session) or when NotesSync is absent.
  show() {
    if (!window.NotesSync || !this._bind()) return;
    this.el.style.display = '';
    let open = true;
    try { open = localStorage.getItem('captureNotesOpen') !== 'off'; } catch (_) {}
    this.setOpen(open);
    this._updateAttachState();
  },

  // Hide + clear ALL panel/UI state. NEVER touches the vault note or the
  // session meta — a queued session keeps its note_id, and discarding a
  // recording never deletes the note (it is a real vault note).
  reset() {
    captureNoteId = null;
    captureNoteCreate = null;
    captureNoteTitleAuto = false;
    clearTimeout(captureNoteSaveTimer);
    captureNoteSaveTimer = null;
    this._title = '';
    if (!this._bind()) return;
    this.el.style.display = 'none';
    this.textEl.value = '';
    this.statusEl.textContent = '';
    this.hintEl.textContent = 'Attachments need a connection — notes save offline.';
  },

  // Recovery: a local session was staged via selectFile() — which never calls
  // setupRecorderFromStream — so this is the second show trigger. Rebinds
  // meta.note_id and repopulates the textarea from the mirror; without it,
  // typing after a recovery would silently create a SECOND note.
  async restoreForSession(meta) {
    if (!window.NotesSync) return;
    this.reset();
    this.show();
    if (!this.el) return;
    if (meta && meta.note_id) {
      captureNoteId = meta.note_id;
      captureNoteTitleAuto = false;   // a recovered note keeps its creation title
      try {
        const rec = await NotesSync.readNote(captureNoteId);
        if (rec) { this.textEl.value = rec.body || ''; this._title = rec.title || ''; }
      } catch (_) { /* body restore is best-effort; the id binding is what matters */ }
      this._updateStatus();
    }
  },

  _updateStatus() {
    if (!this.statusEl) return;
    if (!captureNoteId) { this.statusEl.textContent = ''; return; }
    this.statusEl.textContent = isTempNoteId(captureNoteId)
      ? 'Saved locally · syncs when online'
      : 'Saved · ' + (this._title || 'note');
  },

  _hint(msg) { if (this.hintEl) this.hintEl.textContent = msg; },

  _updateAttachState() {
    if (!this.attachBtn) return;
    this.attachBtn.disabled = !navigator.onLine;
    if (!navigator.onLine) this._hint('Attachments need a connection — notes save offline.');
  },

  _onInput() {
    if (!window.NotesSync) return;
    if (!captureNoteId && !captureNoteCreate) {
      // FIRST KEYSTROKE: create the real vault note (temp id offline; the
      // batch-1 flush remaps to a server id seconds later when online).
      // Single-flight: fast typing can't double-create.
      const title = $('meetingTitle') ? $('meetingTitle').value : '';
      const context = $('meetingContext') ? $('meetingContext').value : '';
      captureNoteTitleAuto = !String(title).trim() && !String(context).trim();
      this._title = captureNoteTitle(title, context, new Date().toISOString());
      captureNoteCreate = NotesSync.createNote({
        title: this._title, folder: 'Meetings', type: 'note', body: this.textEl.value,
      }).then((rec) => {
        captureNoteId = rec.id;
        captureNoteCreate = null;
        // Persist the association like roster/title/context do.
        capSetNoteId(capSessionId || stagedSessionId, rec.id);
        this._updateStatus();
        this._saveSoon();   // pick up anything typed while the create was in flight
      }).catch((e) => {
        // Best-effort next to the recording (capAppendChunk doctrine): the
        // capture is untouched; the next keystroke retries the create.
        captureNoteCreate = null;
        console.warn('capture note create failed:', e);
        this._hint('Note save failed — will retry as you type');
      });
      return;
    }
    this._saveSoon();
  },

  _saveSoon() {
    clearTimeout(captureNoteSaveTimer);
    captureNoteSaveTimer = setTimeout(() => this._saveNow(), 600);
  },

  async _saveNow() {
    if (!captureNoteId || !window.NotesSync) return;
    const body = this.textEl.value;
    try {
      await NotesSync.updateNote(captureNoteId, { body });
    } catch (err) {
      // "note not in mirror" on a TEMP id: another context (installed PWA vs
      // browser tab) won the notes-sync flush lock and remapped it — the
      // notes-sync:remap CustomEvent is window-local, so we never heard it.
      // IDB IS shared: the flushing context's capRewriteNoteId already
      // rewrote the capture meta. Re-read it, adopt the real id, retry ONCE.
      if (isTempNoteId(captureNoteId)) {
        const meta = await capGetMeta(capSessionId || stagedSessionId);
        if (meta && meta.note_id && meta.note_id !== captureNoteId) {
          captureNoteId = meta.note_id;
          this._updateStatus();
          try { await NotesSync.updateNote(captureNoteId, { body }); return; } catch (_) {}
        }
      }
      console.warn('capture note save failed:', err);
      this._hint('Note save failed — will retry as you type');
      return;
    }
    this._updateStatus();
  },

  async _onAttachClick() {
    if (!window.NotesSync) return;
    // No note yet? Create one first (empty body is fine) via the same
    // single-flight path the first keystroke uses.
    if (!captureNoteId) {
      if (!captureNoteCreate) this._onInput();
      try { await captureNoteCreate; } catch (_) {}
      if (!captureNoteId) return;
    }
    // Attachments are ONLINE-ONLY (spec non-goal): the endpoint needs a real
    // note id. Still temp → flush + await the parked remap rewrites, re-check.
    if (isTempNoteId(captureNoteId)) {
      try {
        await NotesSync.flush();
        await Promise.all(captureNoteRemapPending.splice(0));
      } catch (_) { /* re-check below decides */ }
    }
    if (isTempNoteId(captureNoteId) || !navigator.onLine) {
      this._hint('Attachments need a connection — notes save offline.');
      return;
    }
    this.fileEl.click();
  },

  async _onFilePicked() {
    const file = this.fileEl.files && this.fileEl.files[0];
    this.fileEl.value = '';
    if (!file || !captureNoteId || isTempNoteId(captureNoteId)) return;
    const form = new FormData();
    form.append('file', file);
    this._hint('Uploading attachment…');
    try {
      const resp = await fetch(`${API}/api/notes/${encodeURIComponent(captureNoteId)}/attachments`, {
        method: 'POST', body: form,
      });
      if (resp.status === 413) { this._hint('Attachment too large'); return; }
      if (!resp.ok) { this._hint(`Attachment upload failed (${resp.status})`); return; }
      const data = await resp.json();
      // Append the embed markdown to the TEXTAREA — the debounced updateNote
      // persists it, keeping the note BODY the source of truth for attachment
      // association (Phase A/C parse body references, not a sidecar).
      const sep = this.textEl.value && !this.textEl.value.endsWith('\n') ? '\n' : '';
      this.textEl.value += sep + data.embed + '\n';
      this._hint('Attached ' + (data.filename || file.name));
      this._saveSoon();
    } catch (_) {
      this._hint('Attachment upload failed — check connection');
    }
  },
};

// NotesSync flushed a created note: temp id → real server id (only the
// flushing context hears this — the event is window-local). Swap the in-memory
// pointer, rewrite EVERY persisted capture meta holding the temp id, then
// re-post the tags mirror so the SERVER capture meta learns the real id. The
// capRewriteNoteId promise is parked in captureNoteRemapPending: uploadSession
// awaits the parked promises (its awaited barrier) before re-reading the meta,
// because the dispatch is synchronous but this rewrite is async.
window.addEventListener('notes-sync:remap', (e) => {
  const { tempId, serverId } = (e && e.detail) || {};
  if (!tempId || !serverId) return;
  if (captureNoteId === tempId) captureNoteId = serverId;
  const p = capRewriteNoteId(tempId, serverId).then(() => {
    const sid = capSessionId || stagedSessionId;
    if (sid && captureNoteId === serverId) capturePostTagsMirror(sid, serverId);
    captureNotes._updateStatus();
  });
  captureNoteRemapPending.push(p);
});

const recordBtn = $('recordBtn');
const recordArea = $('recordArea');
const recordLabel = $('recordLabel');
const recordTimer = $('recordTimer');
const recordingIndicator = $('recordingIndicator');
const recordingIndicatorText = $('recordingIndicatorText');
const pauseBtn = $('pauseBtn');
const audioViz = $('audioViz');

// Create visualizer bars
const VIZ_BARS = 24;
for (let i = 0; i < VIZ_BARS; i++) {
  const bar = document.createElement('div');
  bar.className = 'viz-bar';
  bar.style.height = '3px';
  audioViz.appendChild(bar);
}

// Source toggle
document.querySelectorAll('.source-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) return;
    document.querySelectorAll('.source-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    recordSource = btn.dataset.source;
  });
});

recordBtn.addEventListener('click', async () => {
  if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) {
    stopRecording();
  } else if (selectedFile) {
    if (!confirm('Discard your unsaved recording and start a new one?')) return;
    capClear(stagedSessionId);
    capStreamDelete(stagedSessionId);   // drop the server shadow copy too
    stagedSessionId = null;
    selectedFile = null;
    fileInput.value = '';
    uploadFields.classList.remove('visible');
    await startRecording();
  } else {
    await startRecording();
  }
});

pauseBtn.addEventListener('click', togglePause);

// --- Loopback Device Management ---
const LOOPBACK_PATTERNS = ['cable output', 'vb-audio', 'blackhole', 'loopback', 'virtual'];

async function getAudioInputDevices() {
  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    tempStream.getTracks().forEach(t => t.stop());
  } catch (e) { /* permission denied - labels will be empty */ }

  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter(d => d.kind === 'audioinput');
}

function isLikelyLoopbackDevice(device) {
  const label = (device.label || '').toLowerCase();
  return LOOPBACK_PATTERNS.some(p => label.includes(p));
}

async function populateDeviceSelectors() {
  const devices = await getAudioInputDevices();
  const micSelect = $('micDeviceSelect');
  const loopbackSelect = $('loopbackDeviceSelect');

  const savedMicId = localStorage.getItem('meeting_mic_device_id') || '';
  const savedLoopbackId = localStorage.getItem('meeting_loopback_device_id') || '';

  // Preserve current selections before clearing
  const currentMicVal = micSelect.value;
  const currentLoopbackVal = loopbackSelect.value;

  // Clear and rebuild mic selector
  micSelect.innerHTML = '<option value="">Default</option>';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Microphone (${d.deviceId.slice(0, 8)}...)`;
    micSelect.appendChild(opt);
  });

  // Clear and rebuild loopback selector
  loopbackSelect.innerHTML = '<option value="">None (use screen share)</option>';
  let autoDetectedId = '';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Device (${d.deviceId.slice(0, 8)}...)`;
    loopbackSelect.appendChild(opt);
    if (!autoDetectedId && isLikelyLoopbackDevice(d)) {
      autoDetectedId = d.deviceId;
    }
  });

  // Restore selection: saved > current > auto-detected
  if (savedMicId && [...micSelect.options].some(o => o.value === savedMicId)) {
    micSelect.value = savedMicId;
  } else if (currentMicVal && [...micSelect.options].some(o => o.value === currentMicVal)) {
    micSelect.value = currentMicVal;
  }

  if (savedLoopbackId && [...loopbackSelect.options].some(o => o.value === savedLoopbackId)) {
    loopbackSelect.value = savedLoopbackId;
  } else if (currentLoopbackVal && [...loopbackSelect.options].some(o => o.value === currentLoopbackVal)) {
    loopbackSelect.value = currentLoopbackVal;
  } else if (autoDetectedId && !savedLoopbackId) {
    // Auto-select likely loopback device on first visit
    loopbackSelect.value = autoDetectedId;
    localStorage.setItem('meeting_loopback_device_id', autoDetectedId);
  }

  updateLoopbackIndicator();
}

function updateLoopbackIndicator() {
  const loopbackSelect = $('loopbackDeviceSelect');
  const indicator = $('loopbackActiveIndicator');
  const hint = $('loopbackHint');

  if (loopbackSelect.value) {
    const deviceName = loopbackSelect.options[loopbackSelect.selectedIndex].textContent;
    indicator.textContent = 'Loopback active: System Audio and Mic + System will use "' + deviceName + '" instead of screen share.';
    indicator.style.display = 'block';
    hint.style.display = 'none';
  } else {
    indicator.style.display = 'none';
    hint.style.display = 'block';
  }
}

// Save device selections to localStorage
$('micDeviceSelect').addEventListener('change', () => {
  localStorage.setItem('meeting_mic_device_id', $('micDeviceSelect').value);
});

$('loopbackDeviceSelect').addEventListener('change', () => {
  localStorage.setItem('meeting_loopback_device_id', $('loopbackDeviceSelect').value);
  updateLoopbackIndicator();
});

$('refreshDevices').addEventListener('click', () => populateDeviceSelectors());

$('loopbackHelpToggle').addEventListener('click', () => {
  $('loopbackHelpContent').classList.toggle('visible');
});

// Loopback gear popover toggle
$('loopbackGearBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  $('loopbackPopover').classList.toggle('visible');
});
document.addEventListener('click', (e) => {
  const pop = $('loopbackPopover');
  if (pop && pop.classList.contains('visible') && !pop.contains(e.target) && e.target !== $('loopbackGearBtn')) {
    pop.classList.remove('visible');
  }
});

// Listen for device changes (hot-plug)
if (navigator.mediaDevices && navigator.mediaDevices.ondevicechange !== undefined) {
  navigator.mediaDevices.addEventListener('devicechange', () => populateDeviceSelectors());
}

// Initialize device selectors on page load
populateDeviceSelectors();

// --- System Audio Helper (loopback-aware) ---
async function getSystemAudioStream() {
  const loopbackId = $('loopbackDeviceSelect').value;
  if (loopbackId) {
    return await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: { exact: loopbackId } }
    });
  }
  return await getScreenAudioStream();
}

function getSelectedMicConstraints() {
  const micId = $('micDeviceSelect').value;
  if (micId) {
    return { audio: { deviceId: { exact: micId } } };
  }
  return { audio: true };
}

function getLoopbackDeviceName() {
  const sel = $('loopbackDeviceSelect');
  if (sel.value) {
    return sel.options[sel.selectedIndex].textContent;
  }
  return null;
}

async function getScreenAudioStream() {
  let stream = await navigator.mediaDevices.getDisplayMedia({
    video: false,
    audio: true,
  });
  if (!stream.getAudioTracks().length) {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: true,
    });
    stream.getVideoTracks().forEach(t => t.stop());
  }
  return stream;
}

async function startRecording() {
  try {
    let stream;
    extraStreams = [];
    pausedDuration = 0;
    pauseStartTime = null;

    const loopbackName = getLoopbackDeviceName();
    const usingLoopback = !!$('loopbackDeviceSelect').value;

    if (recordSource === 'both') {
      let micStream, sysStream;
      try {
        micStream = await navigator.mediaDevices.getUserMedia(getSelectedMicConstraints());
      } catch (micErr) {
        recordLabel.textContent = 'Mic access denied';
        console.error('Mic error:', micErr);
        return;
      }

      try {
        sysStream = await getSystemAudioStream();
      } catch (sysErr) {
        if (usingLoopback) {
          console.error('Loopback device error:', sysErr);
          recordLabel.textContent = 'Loopback device error - recording mic only';
        } else {
          console.warn('Screen share cancelled, falling back to mic-only:', sysErr);
          recordLabel.textContent = 'Screen share cancelled - recording mic only';
        }
        stream = micStream;
        extraStreams = [];
        setupRecorderFromStream(stream);
        return;
      }

      if (!sysStream.getAudioTracks().length) {
        recordLabel.textContent = 'No system audio - recording mic only';
        sysStream.getTracks().forEach(t => t.stop());
        stream = micStream;
        setupRecorderFromStream(stream);
        return;
      }

      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      // Created AFTER awaiting getUserMedia + the screen-share/loopback prompt,
      // so Chrome starts it SUSPENDED — a suspended context pulls no samples
      // through the destination, making the mixed recording (and visualizer)
      // silent. Resume before wiring up. Root cause of empty 'Both' recordings.
      if (ctx.state === 'suspended') {
        try { await ctx.resume(); } catch (e) { console.warn('AudioContext resume failed:', e); }
      }
      const dest = ctx.createMediaStreamDestination();
      const micSource = ctx.createMediaStreamSource(micStream);
      const sysSource = ctx.createMediaStreamSource(sysStream);
      micSource.connect(dest);
      sysSource.connect(dest);

      stream = dest.stream;
      extraStreams = [micStream, sysStream];
      audioContext = ctx;

    } else if (recordSource === 'screen') {
      stream = await getSystemAudioStream();
      if (!stream.getAudioTracks().length) {
        recordLabel.textContent = 'No audio track - try Mic instead';
        return;
      }
    } else {
      // mic-only: use selected mic device
      stream = await navigator.mediaDevices.getUserMedia(getSelectedMicConstraints());
    }

    setupRecorderFromStream(stream, usingLoopback, loopbackName);

  } catch (err) {
    if (err.name === 'NotAllowedError') {
      recordLabel.textContent = 'Permission denied - allow microphone access';
    } else if (err.name === 'NotFoundError') {
      recordLabel.textContent = 'No microphone found';
    } else {
      recordLabel.textContent = 'Error: ' + err.message;
    }
    console.error('Recording error:', err);
  }
}

function setupRecorderFromStream(stream, usingLoopback, loopbackName) {
    window.__meetingRecordingActive = () => !!mediaRecorder && mediaRecorder.state === 'recording';
    recordedChunks = [];

    // Reset signal monitoring + clear any stale no-audio warning.
    recordingPeakLevel = 0;
    lastSignalTime = 0;
    noAudioWarned = false;
    stagedSilent = false;
    hideCaptureWarning();

    // In-meeting notes: UNCONDITIONALLY reset any stale panel state from an
    // earlier path (belt-and-braces — no prior path may leak into a new
    // recording), then show the section for this recording.
    captureNotes.reset();
    captureNotes.show();

    const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
      .find(m => MediaRecorder.isTypeSupported(m)) || '';

    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});

    // Begin autosaving this recording to IndexedDB (survives navigation/reload).
    const capMeta = { mimeType: mimeType || 'audio/webm', source: recordSource, startedAt: Date.now() };
    capStartSession(capMeta);
    // Also mirror this recording to the server as it records (best-effort shadow backup).
    capStreamStart(capSessionId, capMeta);
    liveTags.start();

    mediaRecorder.ondataavailable = e => {
      if (e.data.size > 0) {
        recordedChunks.push(e.data);
        capAppendChunk(e.data);   // best-effort persist (local)
        capStreamEnqueue(e.data); // best-effort mirror (server)
      }
    };

    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      extraStreams.forEach(s => s.getTracks().forEach(t => t.stop()));
      extraStreams = [];
      onRecordingStopped();
    };

    stream.getAudioTracks().forEach(track => {
      track.addEventListener('ended', () => {
        if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) {
          stopRecording();
        }
      });
    });
    extraStreams.forEach(s => {
      s.getAudioTracks().forEach(track => {
        track.addEventListener('ended', () => {
          if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) {
            stopRecording();
          }
        });
      });
    });

    mediaRecorder.start(1000);

    recordBtn.classList.add('recording');
    recordArea.classList.add('recording');
    $('captureBanner').classList.add('recording');
    recordTimer.classList.add('visible');
    recordingIndicator.classList.add('visible');
    pauseBtn.classList.add('visible');
    pauseBtn.classList.remove('paused');
    pauseBtn.innerHTML = '&#9646;&#9646;';
    pauseBtn.title = 'Pause';
    audioViz.classList.add('visible');
    $('sourceToggle').style.display = 'none';
    $('loopbackSettings').style.display = 'none';
    $('loopbackGearBtn').style.display = 'none';

    // Set recording label based on source mode and loopback status
    if (usingLoopback && loopbackName) {
      if (recordSource === 'both') {
        recordLabel.textContent = 'Recording mic + loopback (' + loopbackName + ')';
        recordingIndicatorText.textContent = 'Mic + Loopback';
      } else if (recordSource === 'screen') {
        recordLabel.textContent = 'Recording loopback (' + loopbackName + ')';
        recordingIndicatorText.textContent = 'Loopback';
      } else {
        recordLabel.textContent = 'Click to stop';
        recordingIndicatorText.textContent = 'Recording';
      }
    } else {
      recordLabel.textContent = 'Click to stop';
      recordingIndicatorText.textContent = 'Recording';
    }

    recordingStartTime = Date.now();
    pausedDuration = 0;
    recordTimerInterval = setInterval(updateRecordTimer, 1000);
    updateRecordTimer();

    setupAudioViz(stream);
}

function stopRecording() {
  if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) {
    mediaRecorder.stop();
  }
  clearInterval(recordTimerInterval);
  cancelAnimationFrame(vizAnimFrame);
  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }
}

function togglePause() {
  if (!mediaRecorder) return;

  if (mediaRecorder.state === 'recording') {
    mediaRecorder.pause();
    pauseStartTime = Date.now();
    clearInterval(recordTimerInterval);
    cancelAnimationFrame(vizAnimFrame);

    pauseBtn.classList.add('paused');
    pauseBtn.innerHTML = '&#9654;';
    pauseBtn.title = 'Resume';
    recordingIndicatorText.textContent = 'Paused';
    document.querySelector('.rec-dot').style.animationPlayState = 'paused';

  } else if (mediaRecorder.state === 'paused') {
    mediaRecorder.resume();
    pausedDuration += Date.now() - pauseStartTime;
    pauseStartTime = null;
    recordTimerInterval = setInterval(updateRecordTimer, 1000);

    if (analyserNode) {
      const bars = audioViz.querySelectorAll('.viz-bar');
      const dataArray = new Uint8Array(analyserNode.frequencyBinCount);
      function draw() {
        vizAnimFrame = requestAnimationFrame(draw);
        analyserNode.getByteFrequencyData(dataArray);
        updateSignalLevel(dataArray);
        const step = Math.max(1, Math.floor(dataArray.length / VIZ_BARS));
        for (let i = 0; i < VIZ_BARS; i++) {
          const val = dataArray[Math.min(i * step, dataArray.length - 1)];
          const height = Math.max(3, (val / 255) * 40);
          bars[i].style.height = height + 'px';
        }
      }
      draw();
    }

    pauseBtn.classList.remove('paused');
    pauseBtn.innerHTML = '&#9646;&#9646;';
    pauseBtn.title = 'Pause';
    recordingIndicatorText.textContent = 'Recording';
    document.querySelector('.rec-dot').style.animationPlayState = '';
  }
}

function onRecordingStopped() {
  recordBtn.classList.remove('recording');
  recordArea.classList.remove('recording');
  $('captureBanner').classList.remove('recording');
  recordLabel.textContent = 'Click to record';
  recordTimer.classList.remove('visible');
  recordingIndicator.classList.remove('visible');
  pauseBtn.classList.remove('visible', 'paused');
  audioViz.classList.remove('visible');
  $('sourceToggle').style.display = '';
  $('loopbackSettings').style.display = '';
  $('loopbackGearBtn').style.display = '';
  document.querySelector('.rec-dot').style.animationPlayState = '';
  hideCaptureWarning();
  liveTags.stop();

  if (!recordedChunks.length) return;

  const mimeType = mediaRecorder.mimeType || 'audio/webm';
  const ext = mimeType.includes('ogg') ? '.ogg' : mimeType.includes('mp4') ? '.m4a' : '.webm';
  const blob = new Blob(recordedChunks, { type: mimeType });
  const elapsed = recordTimer.textContent.replace(/:/g, '');
  const fileName = `recording_${new Date().toISOString().slice(0,10)}_${elapsed}${ext}`;
  const file = new File([blob], fileName, { type: mimeType });

  selectFile(file);
  stagedFromRecording = true;
  capMarkStopped({ durationLabel: recordTimer.textContent, fileName });
  capStreamStop({ durationLabel: recordTimer.textContent, fileName });   // flush + mark server copy stopped
  stagedSessionId = capSessionId;   // the staged file is backed by this session
  capSessionId = null;              // recording finished — nothing being written now

  // Surface a silent capture before the user uploads (and later transcribes) it.
  stagedSilent = (recordingPeakLevel <= SIGNAL_THRESHOLD);
  if (stagedSilent) {
    showCaptureWarning('&#9888;&#65039; This recording appears to contain no audio (silent capture). Check your mic/system-audio source — upload anyway only if you expected silence.');
  }
}

// --- Live signal monitoring (no-audio safety net) ---
const SIGNAL_THRESHOLD = 6;   // byte-FFT bin value above the silence noise floor

function updateSignalLevel(dataArray) {
  let max = 0;
  for (let i = 0; i < dataArray.length; i++) if (dataArray[i] > max) max = dataArray[i];
  if (max > SIGNAL_THRESHOLD) {
    lastSignalTime = Date.now();
    if (max > recordingPeakLevel) recordingPeakLevel = max;
    if (noAudioWarned) hideCaptureWarning();
  }
}

function showCaptureWarning(msg) {
  noAudioWarned = true;
  const el = $('captureWarning');
  if (el) {
    // innerHTML is safe here: callers only ever pass a trusted static literal
    // (the entities below render the warning glyph); no user input reaches this.
    el.innerHTML = msg || '&#9888;&#65039; No audio detected — check your microphone / system-audio source. This recording may be silent.';
    el.style.display = 'block';
  }
}

function hideCaptureWarning() {
  noAudioWarned = false;
  const el = $('captureWarning');
  if (el) el.style.display = 'none';
}

function updateRecordTimer() {
  const elapsed = Math.floor((Date.now() - recordingStartTime - pausedDuration) / 1000);
  const h = Math.floor(elapsed / 3600);
  const m = Math.floor((elapsed % 3600) / 60);
  const s = elapsed % 60;
  recordTimer.textContent =
    `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;

  // No-audio safety net: a few seconds in with no real signal -> warn the user
  // immediately instead of letting them find out after an empty transcription.
  // (This tick is paused while recording is paused, so it won't false-fire.)
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    const noSignal = lastSignalTime === 0 || (Date.now() - lastSignalTime > 3000);
    if (elapsed >= 4 && noSignal) showCaptureWarning();
  }
}

function setupAudioViz(stream) {
  const ctx = audioContext || new (window.AudioContext || window.webkitAudioContext)();
  audioContext = ctx;
  // Created after media-prompt awaits -> may start suspended; resume so the
  // analyser produces data (also powers the no-audio detector).
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  const source = ctx.createMediaStreamSource(stream);
  analyserNode = ctx.createAnalyser();
  analyserNode.fftSize = 64;
  source.connect(analyserNode);

  const bars = audioViz.querySelectorAll('.viz-bar');
  const dataArray = new Uint8Array(analyserNode.frequencyBinCount);

  function draw() {
    vizAnimFrame = requestAnimationFrame(draw);
    analyserNode.getByteFrequencyData(dataArray);
    updateSignalLevel(dataArray);
    const step = Math.max(1, Math.floor(dataArray.length / VIZ_BARS));
    for (let i = 0; i < VIZ_BARS; i++) {
      const val = dataArray[Math.min(i * step, dataArray.length - 1)];
      const height = Math.max(3, (val / 255) * 40);
      bars[i].style.height = height + 'px';
    }
  }
  draw();
}

// --- Upload ---
dropZone.addEventListener('click', () => fileInput.click());

dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});

dropZone.addEventListener('dragleave', () => {
  dropZone.classList.remove('drag-over');
});

dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
});

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) selectFile(fileInput.files[0]);
});

function selectFile(file) {
  selectedFile = file;
  // A drag-dropped / picked file still exists on disk, so it's not "unsaved".
  // onRecordingStopped() and the recovery flow re-set this to true after calling us.
  stagedFromRecording = false;
  $('fileName').textContent = file.name;
  $('fileSize').textContent = formatBytes(file.size);
  uploadFields.classList.add('visible');
}

$('removeFile').addEventListener('click', () => {
  if (stagedFromRecording) {
    // This is an un-uploaded recording — removing it destroys the only copy.
    if (!confirm('Permanently discard this recording? It has not been uploaded.')) return;
    capClear(stagedSessionId);
    capStreamDelete(stagedSessionId);   // drop the server shadow copy too
    stagedSessionId = null;
  }
  selectedFile = null;
  fileInput.value = '';
  uploadFields.classList.remove('visible');
  hideCaptureWarning();
  stagedFromRecording = false;
  stagedSilent = false;
  captureNotes.reset();
});

// ---------------------------------------------------------------------------
// Offline upload queue — the interactive Upload and the background flush share
// ONE upload code path (buildUploadForm + uploadSession). A session auto-uploads
// ONLY if the user explicitly hit Upload and it failed for a NETWORK reason
// (gated on the persisted meta.queued flag). Crash-recovered-but-never-submitted
// drafts stay in the manual recover/discard list. See
// docs/superpowers/specs/2026-07-09-offline-upload-queue-design.md.
// ---------------------------------------------------------------------------

// Assemble the multipart body shared by the interactive upload and the queue
// flush, so both produce an IDENTICAL payload. `session` carries the audio blob
// plus persisted meta; optional title/speakers/context/markers/roster override
// the meta fallbacks (the interactive path passes the live form values, the
// queue path relies on what was persisted at queue time via capSetQueued).
function buildUploadForm(session) {
  const meta = session.meta || {};
  const blob = session.blob;
  const mt = meta.mimeType || (blob && blob.type) || 'audio/webm';
  const name = meta.fileName || 'recording.webm';
  const file = (blob instanceof File) ? blob : new File([blob], name, { type: mt });

  const form = new FormData();
  form.append('file', file);

  // Capture session id → server-side upload idempotency: a retry after a lost
  // 202, or a second context flushing the same queued session, dedups to one
  // meeting instead of creating duplicates. Absent for disk-picked files.
  if (session.sid) form.append('sid', session.sid);

  const title = String(session.title != null ? session.title : (meta.title || '')).trim();
  if (title) form.append('title', title);

  const speakers = String(session.speakers != null ? session.speakers : (meta.speakers || ''));
  if (speakers) {
    form.append('min_speakers', speakers);
    form.append('max_speakers', speakers);
  }

  const context = String(session.context != null ? session.context : (meta.context || '')).trim();
  if (context) form.append('meeting_context', context);

  // Live speaker tags (only present when the file came from a tagged recording).
  const markers = Array.isArray(session.markers) ? session.markers
                : Array.isArray(meta.markers) ? meta.markers : [];
  const roster = Array.isArray(session.roster) ? session.roster
               : Array.isArray(meta.roster) ? meta.roster : [];
  if (markers.length) {
    form.append('speaker_tags', JSON.stringify(markers));
    form.append('speaker_roster', JSON.stringify(roster));
    // Prefill #speakers from roster size if the user left it blank. Only set
    // max — leave min unset so pyannote can auto-detect the minimum (avoids
    // force-splitting a real speaker if a pre-added attendee never speaks).
    if (!speakers && roster.length) {
      form.set('max_speakers', String(roster.length));
    }
  }
  // In-meeting note link. TEMP-ID RULE (client-side, new with this feature):
  // temp n_local_ ids are never sent — uploadSession's awaited barrier resolves
  // them first or the field is omitted. The server additionally ignores
  // unknown/malformed ids (that half mirrors the malformed-sid precedent).
  const noteId = session.note_id != null ? session.note_id : meta.note_id;
  if (noteId && !isTempNoteId(noteId)) form.append('note_id', noteId);
  return form;
}

// Perform ONE upload attempt for a captured session. Wraps the XHR in a promise
// so the interactive path keeps its progress bar (via session.onProgress) and
// the queue path can await it. Resolves:
//   { ok:true, meetingId }                  on 202
//   { ok:false, kind:'validation', detail } on 400/413 (permanent — user must fix)
//   { ok:false, kind:'network' }            on connection failure/timeout/5xx/redirect
// Side effect: on a network result it flags meta.queued=true (persist for the
// flush loop); on a validation result it clears meta.queued. Success cleanup is
// left to the callers (they own the staged UI / recovery-list state).
async function uploadSession(session) {
  const sid = session.sid;
  // Resolve a still-temp note id BEFORE building the form (queued sessions
  // replay persisted meta that may predate the note's flush). AWAITED BARRIER,
  // not a race: flush() resolves without waiting for its notes-sync:remap
  // listeners, so the second await — the parked capRewriteNoteId promises —
  // is what guarantees the listener's async IDB rewrite has committed before
  // the meta re-read. LAST-RESORT GUARD: if the id is STILL temp after the
  // barrier (the note flush failed), buildUploadForm omits note_id but the
  // queued meta keeps it, so a later retry — or the server's sid-dedup
  // link-repair path — can still make the link. The meeting (GPU work) is
  // never held hostage to a note link.
  const effNoteId = session.note_id != null ? session.note_id : (session.meta || {}).note_id;
  if (effNoteId && isTempNoteId(effNoteId) && window.NotesSync && sid) {
    try {
      await NotesSync.flush();
      await Promise.all(captureNoteRemapPending.splice(0));
      const fresh = await capGetMeta(sid);
      if (fresh && fresh.note_id) session.note_id = fresh.note_id;
    } catch (_) { /* best-effort; the form builder omits temp ids */ }
  }
  const form = buildUploadForm(session);
  const result = await new Promise((resolve) => {
    let settled = false;
    const done = (r) => { if (!settled) { settled = true; resolve(r); } };
    try {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API}/meetings/upload`);
      if (session.onProgress) {
        xhr.upload.addEventListener('progress', e => {
          if (e.lengthComputable) session.onProgress(Math.round((e.loaded / e.total) * 100));
        });
      }
      xhr.onload = () => {
        if (xhr.status === 202) {
          let meetingId = null;
          try { meetingId = JSON.parse(xhr.responseText).meeting_id; } catch (_) {}
          done({ ok: true, meetingId });
        } else if (xhr.status === 400 || xhr.status === 413) {
          let detail = `Upload rejected (${xhr.status})`;
          try { detail = JSON.parse(xhr.responseText).detail || detail; } catch (_) {}
          done({ ok: false, kind: 'validation', detail });
        } else {
          // 5xx / unexpected status / auth redirect: retryable, treat as network.
          done({ ok: false, kind: 'network' });
        }
      };
      xhr.onerror = () => done({ ok: false, kind: 'network' });
      xhr.ontimeout = () => done({ ok: false, kind: 'network' });
      xhr.send(form);
    } catch (_) {
      done({ ok: false, kind: 'network' });
    }
  });

  if (!result.ok && result.kind === 'network') {
    await capSetQueued(sid, true, session);       // arm auto-retry (idempotent)
  } else if (!result.ok && result.kind === 'validation') {
    await capSetQueued(sid, false);               // permanent: never auto-retry
  }
  return result;
}

// Sessions successfully uploaded during THIS page's lifetime. Belt-and-braces
// against a silently-failed capClear (the IndexedDB delete swallows its errors):
// even if the local delete never commits, we never re-POST a sid we already got a
// 202 for, so a later periodic tick can't turn one recording into two meetings.
// (Reset on reload; true exactly-once across reloads would need a server-side
// idempotency key on the sid — noted as a follow-up in the design doc.)
const uploadedSids = new Set();

// Run `fn` under a device-wide exclusive lock so only ONE app context flushes at
// a time. The installed PWA window and a browser tab share the same IndexedDB, so
// a per-page boolean can't stop two contexts racing the same queued session into
// duplicate meetings. `ifAvailable` means a second context skips its pass rather
// than queueing behind the first. Falls back to a per-page flag where the Web
// Locks API is unavailable (non-secure context / older engines).
let flushing = false;
function withFlushLock(fn) {
  if (navigator.locks && navigator.locks.request) {
    return navigator.locks.request('meeting-upload-flush', { ifAvailable: true }, (lock) => {
      if (!lock) return undefined;        // another context already holds the flush lock
      return fn();
    });
  }
  if (flushing) return Promise.resolve();
  flushing = true;
  return Promise.resolve().then(fn).finally(() => { flushing = false; });
}

// Sequentially upload every queued session (meta.queued===true), newest-first.
// Serialized across every app context by withFlushLock so overlapping triggers
// (online event + periodic tick + a second tab) can't create duplicate meetings.
// A network result keeps the session queued and STOPS the pass (no point hammering
// while still offline); a validation result clears queued and hands the session
// back to the manual list. After each success the recovery UI is refreshed.
async function flushUploadQueue() {
  return withFlushLock(async () => {
    try {
      const pending = await capLoadAllPending();                 // [{meta, blob}], excludes active recording
      const queued = selectQueuedSessions(pending.map(p => p.meta), capSessionId, uploadedSids);
      for (const meta of queued) {
        const entry = pending.find(p => p.meta.k === meta.k);
        if (!entry) continue;
        const result = await uploadSession({ blob: entry.blob, meta, sid: meta.k, fromQueue: true });
        if (result.ok) {
          uploadedSids.add(meta.k);        // mark BEFORE the delete so a failed delete can't cause a re-upload
          await capClear(meta.k);          // await so the delete commits before we re-read the list
          capStreamDelete(meta.k);
          refreshMeetings();
          startPolling();
          await checkPendingRecording();   // refresh the recovery/queue list live
        } else if (result.kind === 'network') {
          break;                            // still offline — stop, retry later
        } else {
          // validation: uploadSession cleared queued; leave it for the manual list.
          await checkPendingRecording();
        }
      }
    } catch (e) { /* best-effort */ }
  });
}

uploadBtn.addEventListener('click', async () => {
  if (!selectedFile) return;

  // Guard against accidentally queueing a silent recording for transcription.
  if (stagedFromRecording && stagedSilent) {
    if (!confirm('This recording appears to contain no audio. Upload and process it anyway?')) return;
  }

  uploadBtn.disabled = true;
  const progress = $('uploadProgress');
  progress.classList.add('visible');

  // In-meeting note: retitle ONCE if it was created with the bare fallback and
  // the user has since filled the title/context (a mirror op — works offline).
  // Frontmatter title only; the vault filename keeps its creation-time slug.
  // After this the title is never touched again.
  if (captureNoteId && captureNoteTitleAuto && window.NotesSync) {
    const t = $('meetingTitle').value.trim();
    const c = $('meetingContext').value.trim();
    if (t || c) {
      const newTitle = captureNoteTitle(t, c, new Date().toISOString());
      try {
        await NotesSync.updateNote(captureNoteId, { title: newTitle });
        captureNotes._title = newTitle;
      } catch (_) { /* best-effort */ }
      captureNoteTitleAuto = false;   // retitled once — never touched again
      // (left true when title/context are still blank, so a validation-failure
      // retry where the user then fills the title can still retitle once)
    }
  }

  // Assemble the session from the live form. The queue path reproduces the same
  // payload from persisted meta (capSetQueued stores title/speakers/context/tags).
  const tagged = !!(stagedSessionId && liveTags.markers.length);
  const session = {
    blob: selectedFile,
    meta: { mimeType: selectedFile.type, fileName: selectedFile.name },
    sid: stagedSessionId,
    note_id: captureNoteId,
    title: $('meetingTitle').value.trim(),
    speakers: $('numSpeakers').value,
    context: $('meetingContext').value.trim(),
    markers: tagged ? liveTags.markers.slice() : [],
    roster: tagged ? liveTags.roster.slice() : [],
    onProgress: (pct) => {
      $('progressFill').style.width = pct + '%';
      $('progressText').textContent = pct < 100 ? `Uploading... ${pct}%` : 'Processing started...';
    },
  };

  const result = await uploadSession(session);

  if (result.ok) {
    $('progressText').textContent = `Queued! Meeting ID: ${result.meetingId}`;
    // Persisted server-side now — drop this session's autosave + shadow copy + any silent warning.
    stagedFromRecording = false;
    stagedSilent = false;
    hideCaptureWarning();
    if (stagedSessionId) uploadedSids.add(stagedSessionId);  // a failed local delete must not trigger a flush re-upload
    capClear(stagedSessionId);
    capStreamDelete(stagedSessionId);
    stagedSessionId = null;
    setTimeout(() => {
      selectedFile = null;
      fileInput.value = '';
      uploadFields.classList.remove('visible');
      progress.classList.remove('visible');
      uploadBtn.disabled = false;
      $('meetingTitle').value = '';
      $('numSpeakers').value = '';
      $('meetingContext').value = '';
      liveTags.reset();
      captureNotes.reset();
      $('progressFill').style.width = '0%';
    }, 2000);
    refreshMeetings();
    startPolling();
  } else if (result.kind === 'validation') {
    // Permanent rejection — behave as today; uploadSession already cleared queued.
    $('progressText').textContent = result.detail || 'Upload rejected';
    uploadBtn.disabled = false;
  } else if (stagedSessionId) {
    // Network failure on a recording: uploadSession has flagged it queued and it
    // is safe in IndexedDB. Clear the staging box; the flush loop uploads it
    // automatically once connectivity returns.
    $('progressText').textContent = 'Offline — queued, will upload automatically when connected';
    stagedFromRecording = false;
    stagedSilent = false;
    hideCaptureWarning();
    stagedSessionId = null;
    selectedFile = null;
    fileInput.value = '';
    liveTags.reset();
    // Third form-clear path: without this, back-to-back offline meetings would
    // append meeting B's notes to meeting A's note. UI-only — the queued
    // session's meta keeps note_id (persisted by capSetQueued inside
    // uploadSession BEFORE this branch ran) and the note stays in the mirror.
    captureNotes.reset();
    setTimeout(() => {
      uploadFields.classList.remove('visible');
      progress.classList.remove('visible');
      uploadBtn.disabled = false;
      $('meetingTitle').value = '';
      $('numSpeakers').value = '';
      $('meetingContext').value = '';
      $('progressFill').style.width = '0%';
    }, 2500);
    checkPendingRecording();   // surface the queued row in the recovery list
  } else {
    // A file picked from disk isn't autosaved, so it can't be queued — keep it
    // in the box for a manual retry, exactly as before.
    $('progressText').textContent = 'Upload error. Check connection.';
    uploadBtn.disabled = false;
  }
});

// --- Sidebar Toggle & Sections ---
function toggleSidebarSection(sectionId) {
  const body = $(sectionId + 'Body');
  const toggle = $(sectionId + 'Toggle');
  if (body.classList.contains('collapsed')) {
    body.classList.remove('collapsed');
    body.style.maxHeight = body.scrollHeight + 'px';
    toggle.classList.remove('collapsed');
  } else {
    body.classList.add('collapsed');
    body.style.maxHeight = '0';
    toggle.classList.add('collapsed');
  }
}

// Hamburger for mobile
$('hamburgerBtn').addEventListener('click', () => {
  const sidebar = $('sidebar');
  const backdrop = $('sidebarBackdrop');
  sidebar.classList.toggle('open');
  backdrop.classList.toggle('visible');
});
$('sidebarBackdrop').addEventListener('click', () => {
  $('sidebar').classList.remove('open');
  $('sidebarBackdrop').classList.remove('visible');
});

// --- View Switcher ---
document.querySelectorAll('.view-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentView = btn.dataset.view;
    refreshGroupedView();
  });
});

// --- Grouped Meeting List ---
async function refreshGroupedView() {
  try {
    const resp = await fetch(`${API}/meetings/grouped?group_by=${currentView}`);
    groupedData = await resp.json();
    renderGroupedList(groupedData);

    // Also check for in-progress meetings for polling
    const flatResp = await fetch(`${API}/meetings`);
    const flatData = await flatResp.json();
    allMeetingsCache = flatData;
    rebuildCompanyFilter(flatData);
    const inProgress = flatData.some(m => !['complete', 'error'].includes(m.status));
    if (inProgress) startPolling(); else stopPolling();
  } catch (err) {
    console.error('Failed to refresh grouped view:', err);
  }
}

function renderGroupedList(data) {
  const container = $('sidebarMeetingList');
  const groups = data.groups || [];

  if (!groups.length) {
    const msg = currentView === 'linked'
      ? 'No linked meetings yet.'
      : 'No meetings match your filters.';
    container.innerHTML = `<div class="empty-state" style="padding:24px">${msg}</div>`;
    return;
  }

  // Apply client-side filters
  const statusFilter = $('meetingStatusFilter').value;
  const titleFilter = $('meetingTitleFilter').value.trim().toLowerCase();
  const companyFilter = $('meetingCompanyFilter').value.toLowerCase();

  let html = '';
  groups.forEach((group, gi) => {
    let meetings = group.meetings || [];

    // Filter within group
    if (statusFilter) meetings = meetings.filter(m => m.status === statusFilter);
    if (titleFilter) meetings = meetings.filter(m => (m.title || '').toLowerCase().includes(titleFilter));
    if (companyFilter) meetings = meetings.filter(m => (m.company || '').toLowerCase() === companyFilter);

    if (!meetings.length) return;

    html += `<div class="group-header" onclick="toggleGroup(${gi})">
      <div style="display:flex;align-items:center;gap:8px">
        <span class="group-label">${escHtml(group.label)}</span>
        <span class="group-count">${meetings.length}</span>
      </div>
      <span class="group-chevron" id="groupChevron${gi}">&#9660;</span>
    </div>`;
    html += `<div class="group-meetings" id="groupMeetings${gi}">`;
    meetings.forEach(m => {
      const isActive = m.id === currentMeetingId;
      const statusCls = `status-${m.status}`;
      html += `<div class="sidebar-meeting-item${isActive ? ' active' : ''}" onclick="openMeeting('${m.id}')">
        <div class="smi-info">
          <div class="smi-title">${escHtml(m.title || 'Untitled')}</div>
          <div class="smi-meta">
            <span>${m.date || ''}</span>
            <span>${m.duration_formatted || ''}</span>
          </div>
        </div>
        <span class="smi-status ${statusCls}">${formatStatus(m.status)}</span>
      </div>`;
    });
    html += '</div>';
  });

  if (data.unlinked_count !== undefined) {
    html += `<div style="padding:12px 16px;font-size:12px;color:var(--text-muted)">${data.unlinked_count} unlinked meeting${data.unlinked_count !== 1 ? 's' : ''}</div>`;
  }

  container.innerHTML = html || '<div class="empty-state" style="padding:24px">No meetings match your filters.</div>';
}

function rebuildCompanyFilter(meetings) {
  // Distinct confirmed companies -> dropdown options. Selection survives the
  // 3s polling rebuild. Values are the server-normalized display forms;
  // matching is case-insensitive exact (same rule as GET /meetings?company=).
  const sel = $('meetingCompanyFilter');
  if (!sel) return;
  const current = sel.value;
  const seen = new Map();               // lowercase key -> first-seen display form
  (meetings || []).forEach(m => {
    const c = (m.company || '').trim();
    if (c && !seen.has(c.toLowerCase())) seen.set(c.toLowerCase(), c);
  });
  const names = [...seen.values()].sort((a, b) => a.localeCompare(b));

  sel.innerHTML = '';
  const optAll = document.createElement('option');
  optAll.value = '';
  optAll.textContent = 'All companies';
  sel.appendChild(optAll);

  for (const c of names) {
    const opt = document.createElement('option');
    opt.value = c;
    opt.textContent = c;
    sel.appendChild(opt);
  }

  if (current && seen.has(current.toLowerCase())) sel.value = current;
}

function toggleGroup(index) {
  const el = $('groupMeetings' + index);
  const chevron = $('groupChevron' + index);
  if (el.classList.contains('collapsed')) {
    el.classList.remove('collapsed');
    chevron.classList.remove('collapsed');
  } else {
    el.classList.add('collapsed');
    chevron.classList.add('collapsed');
  }
}

// Keep old refreshMeetings as alias for polling compatibility
async function refreshMeetings() { return refreshGroupedView(); }

function formatStatus(s) {
  if (s === 'preprocessing') return 'Pre-processing';
  if (s === 'transcribing') return 'Transcribing...';
  if (s === 'cleaning_transcript') return 'Cleaning...';
  if (s === 'identifying_speakers') return 'Speakers...';
  if (s === 'summarizing') return 'Summarizing...';
  if (s === 'tagging') return 'Tagging...';
  if (s === 'storing') return 'Storing...';
  return s;
}

// Meeting list filter events
$('meetingStatusFilter').addEventListener('change', refreshGroupedView);
$('meetingCompanyFilter').addEventListener('change', refreshGroupedView);
$('meetingTitleFilter').addEventListener('input', () => {
  clearTimeout(titleFilterDebounce);
  titleFilterDebounce = setTimeout(() => {
    // Re-render from cached data (client-side filter)
    if (groupedData) renderGroupedList(groupedData);
  }, 300);
});

// --- Polling ---
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refreshGroupedView, 3000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// --- Detail View ---
function isMobile() { return window.innerWidth < 768; }

// --- Detail section collapse (session-only: module memory, resets on
// reload — per design spec, NO localStorage for these). Global across
// meetings: sectionKey = wrapperClass + ':' + headingText, so collapsing
// "Key Topics" stays collapsed for every meeting viewed this session.
const DETAIL_WRAPPER_CLASSES = ['summary-section', 'tags-section', 'notes-section', 'link-section'];
const collapsedDetailSections = {};   // sectionKey -> true when collapsed

// Desktop/Mobile container id pairs for each wrapper class — mirrors the ids
// used by renderSummary/renderTags/loadRelated/renderNotes. Used to re-sync
// the duplicated copy of a section after a toggle (see toggleDetailSection).
const DETAIL_CONTAINER_IDS = {
  'summary-section': ['summaryContent', 'summaryContentMobile'],
  'tags-section': ['tagsContent', 'tagsContentMobile'],
  'link-section': ['relatedContent', 'relatedContentMobile'],
  'notes-section': ['notesContent', 'notesContentMobile'],
};

function detailSectionHeadingFromTarget(target) {
  const header = target.closest('.notes-section-header');
  if (header && header.parentElement && header.parentElement.classList.contains('notes-section')) {
    return header;
  }
  const h3 = target.closest('h3');
  if (!h3 || !h3.parentElement) return null;
  const wrapperClass = DETAIL_WRAPPER_CLASSES.find(cls => h3.parentElement.classList.contains(cls));
  return wrapperClass ? h3 : null;
}

function toggleDetailSection(headingEl) {
  const section = headingEl.parentElement;
  if (!section) return;
  const wrapperClass = DETAIL_WRAPPER_CLASSES.find(cls => section.classList.contains(cls));
  if (!wrapperClass) return;
  const h3 = headingEl.tagName === 'H3' ? headingEl : headingEl.querySelector('h3');
  if (!h3) return;
  const key = wrapperClass + ':' + h3.textContent.trim();
  const collapsed = !collapsedDetailSections[key];
  collapsedDetailSections[key] = collapsed;
  section.classList.toggle('collapsed', collapsed);
  headingEl.setAttribute('aria-expanded', String(!collapsed));
  // Sync the duplicated desktop/mobile copy of this section (F3) — without
  // this the other viewport's copy keeps its stale class until next render.
  (DETAIL_CONTAINER_IDS[wrapperClass] || []).forEach(id => applyDetailCollapse($(id)));
}

// Delegated (document-level; capture not needed) so it works for headings
// inside innerHTML template strings without per-render listener rebinding.
// MUST ignore clicks on button/a/input inside a heading — e.g. the
// pencil-edit button that lives inside the Summary <h3> (app.js:2951).
document.addEventListener('click', (e) => {
  if (e.target.closest('button, a, input')) return;
  const heading = detailSectionHeadingFromTarget(e.target);
  if (heading) toggleDetailSection(heading);
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  if (e.target.closest('button, a, input')) return;
  const heading = detailSectionHeadingFromTarget(e.target);
  if (heading) { e.preventDefault(); toggleDetailSection(heading); }
});

// Re-applies collapsedDetailSections to a freshly-rendered container. Call
// immediately after any renderer sets that container's innerHTML — the
// wrapper divs are template strings, not persistent DOM nodes, so collapse
// state (and the a11y attrs below) do not survive a re-render on their own.
function applyDetailCollapse(containerEl) {
  if (!containerEl) return;
  DETAIL_WRAPPER_CLASSES.forEach(cls => {
    containerEl.querySelectorAll(':scope > .' + cls).forEach(section => {
      const h3 = section.querySelector('h3');
      if (!h3) return;
      const headingEl = cls === 'notes-section' ? section.querySelector(':scope > .notes-section-header') : h3;
      if (!headingEl) return;
      const key = cls + ':' + h3.textContent.trim();
      const collapsed = !!collapsedDetailSections[key];
      section.classList.toggle('collapsed', collapsed);
      headingEl.setAttribute('role', 'button');
      headingEl.setAttribute('tabindex', '0');
      headingEl.setAttribute('aria-expanded', String(!collapsed));
    });
  });
}

async function openMeeting(id) {
  currentMeetingId = id;
  if (typeof updateFloatingChatScope === 'function') updateFloatingChatScope();

  // Highlight in sidebar
  document.querySelectorAll('.sidebar-meeting-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.sidebar-meeting-item').forEach(el => {
    if (el.onclick && el.onclick.toString().includes(id)) el.classList.add('active');
  });
  // Better: re-render grouped list to update active state
  if (groupedData) renderGroupedList(groupedData);

  // Close mobile sidebar if open
  $('sidebar').classList.remove('open');
  $('sidebarBackdrop').classList.remove('visible');

  if (isMobile()) {
    // Use overlay on mobile
    detailOverlay.classList.add('visible');
    document.body.style.overflow = 'hidden';
    await populateDetail(id, true);
  } else {
    // Inline in main content
    $('mainEmptyState').style.display = 'none';
    $('inlineDetail').style.display = 'block';
    await populateDetail(id, false);
  }
}

// Title + pencil (PATCH /meetings/{id}); editor seeds from the raw status.title
// (textContent — never innerHTML — so the value round-trips unescaped).
function setDetailTitle(titleEl, id, title, editable) {
  if (!titleEl) return;
  titleEl.innerHTML = '<span class="detail-title-text"></span>';  // static markup only
  const span = titleEl.querySelector('.detail-title-text');
  span.textContent = title;
  if (!editable) return;
  const btn = document.createElement('button');
  btn.className = 'pencil-btn';
  btn.title = 'Edit title';
  btn.innerHTML = '&#9999;';
  btn.addEventListener('click', () => {
    inlineEdit(span, {
      value: span.textContent,
      onSave: async (v) => {
        const resp = await fetch(`${API}/meetings/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: v }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        span.textContent = data.title;
        refreshGroupedView();   // sidebar list shows the new title
      },
    });
  });
  titleEl.appendChild(btn);
}

async function populateDetail(id, mobile) {
  const prefix = mobile ? 'Mobile' : '';
  const titleEl = $('detailTitle' + prefix);
  const dateEl = $('detailDate' + prefix);
  const durationEl = $('detailDuration' + prefix);
  const statusEl = $('detailStatus' + prefix);
  const companyEl = $('detailCompany' + prefix);
  const transcriptEl = $('transcriptContent' + prefix);
  const summaryEl = $('summaryContent' + prefix);
  const notesEl = $('notesContent' + prefix);
  const chatEl = $('chatContent' + prefix);
  const tagsEl = $('tagsContent' + prefix);
  const relatedEl = $('relatedContent' + prefix);
  const downloadsEl = $('downloadsBar' + prefix);
  const actionBarEl = $('actionButtonsBar' + prefix);
  const tabContainer = mobile ? $('tabsMobile') : $('inlineDetail');

  // Reset tabs to transcript
  const tabBtns = (mobile ? $('tabsMobile') : $('inlineDetail')).querySelectorAll('.tab-btn');
  tabBtns.forEach(b => b.classList.remove('active'));
  tabBtns[0].classList.add('active');

  const tabIds = ['transcript', 'summary', 'notes', 'chat', 'tags', 'related'];
  const suffix = mobile ? '-mobile' : '';
  tabIds.forEach((t, i) => {
    const el = $('tab-' + t + suffix);
    if (el) el.classList.toggle('active', i === 0);
  });

  // Reset chat state for new meeting
  chatHistory = [];
  chatScope = null;
  if (chatAbortController) { chatAbortController.abort(); chatAbortController = null; }

  const loading = '<div style="text-align:center;padding:40px"><div class="spinner"></div> Loading...</div>';
  if (transcriptEl) transcriptEl.innerHTML = loading;
  if (summaryEl) summaryEl.innerHTML = loading;
  if (notesEl) notesEl.innerHTML = '';
  if (chatEl) chatEl.innerHTML = '';
  if (tagsEl) tagsEl.innerHTML = loading;
  if (relatedEl) relatedEl.innerHTML = loading;
  if (downloadsEl) downloadsEl.style.display = 'none';
  if (actionBarEl) { actionBarEl.classList.remove('visible'); actionBarEl.innerHTML = ''; }

  try {
    const statusResp = await fetch(`${API}/meetings/${id}/status`);
    const status = await statusResp.json();
    setDetailTitle(titleEl, id, status.title || 'Meeting', status.status === 'complete');
    transcriptEditedSinceAnalysis = !!status.transcript_edited;  // Re-run banner survives reloads
    if (dateEl) dateEl.textContent = status.date || '';
    if (durationEl) durationEl.textContent = status.duration_formatted || '';
    if (statusEl) statusEl.innerHTML = `<span class="status-badge status-${status.status}">${formatStatus(status.status)}</span>`;
    if (companyEl) renderCompanyChip(companyEl, id, status);

    if (status.status === 'error') {
      const msg = `<div style="text-align:center;padding:40px;color:var(--red)"><p>Error: ${escHtml(status.error || 'Unknown error')}</p></div>`;
      if (transcriptEl) transcriptEl.innerHTML = msg;
      if (summaryEl) summaryEl.innerHTML = msg;
      if (actionBarEl) {
        actionBarEl.innerHTML = `<button class="action-btn retry-btn" onclick="retryMeeting('${id}')">Retry Processing</button>`;
        actionBarEl.classList.add('visible');
      }
      return;
    }

    if (status.status !== 'complete') {
      const progressInfo = status.progress_detail ? ` - ${escHtml(status.progress_detail)}` : '';
      const pctBar = status.progress_percent > 0
        ? `<div style="margin-top:12px;max-width:300px;margin-left:auto;margin-right:auto">
            <div class="progress-bar"><div class="progress-bar-fill" style="width:${status.progress_percent}%"></div></div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:4px">${status.progress_percent}%${progressInfo}</div>
          </div>` : '';
      const msg = `<div style="text-align:center;padding:40px"><div class="spinner"></div> ${formatStatus(status.status)}${pctBar}</div>`;
      if (transcriptEl) transcriptEl.innerHTML = msg;
      if (summaryEl) summaryEl.innerHTML = msg;
      return;
    }

    if (actionBarEl) {
      actionBarEl.innerHTML = `
        <button class="action-btn" onclick="reprocessStep('${id}', 'cleanup')">Re-cleanup</button>
        <button class="action-btn" onclick="reprocessStep('${id}', 'identify_speakers')">Re-identify Speakers</button>
        <button class="action-btn" onclick="reprocessStep('${id}', 'summarize')">Re-summarize</button>
        <button class="action-btn" onclick="reprocessStep('${id}', 'tagging')">Re-tag</button>
        <button class="action-btn" onclick="openTrimModal('${id}')">Trim</button>
      `;
      actionBarEl.classList.add('visible');
    }
  } catch (err) {
    if (transcriptEl) transcriptEl.innerHTML = `<div style="color:var(--red)">Failed to load: ${escHtml(err.message)}</div>`;
    return;
  }

  // Load content in parallel
  loadTranscript(id);
  loadSummary(id);
  loadNotes(id);
  initChat(id);
  loadTags(id);
  loadRelated(id);
  initAudioPlayer(id, mobile);

  // Download links
  const dlMap = {
    'dlTranscript': 'transcript.json',
    'dlRawTranscript': 'raw_transcript.json',
    'dlSrt': 'transcript.srt',
    'dlTranscriptMd': 'transcript.md',
    'dlSummary': 'summary.md',
  };
  for (const [elId, file] of Object.entries(dlMap)) {
    const el = $(elId + prefix);
    if (el) el.href = `${API}/meetings/${id}/files/${file}`;
  }
  if (downloadsEl) downloadsEl.style.display = 'flex';
}

// --- Company tag chip (detail header) ---

function renderCompanyChip(el, meetingId, status) {
  // Three states (spec): confirmed = solid chip; suggested = dashed chip
  // "<name> ?" plus a confirm button; neither = muted "+ Company". Hidden
  // until the meeting is complete. Suggestions are never persisted server-
  // side — only an explicit PATCH confirms one.
  el.innerHTML = '';   // safe: constant empty string (clear before re-render)
  if (status.status !== 'complete') { el.style.display = 'none'; return; }
  el.style.display = '';
  const confirmed = status.company || null;
  const suggested = status.company_suggestion || null;
  const chip = document.createElement('span');
  if (confirmed) {
    chip.className = 'company-chip confirmed';
    chip.textContent = confirmed;
    chip.title = 'Company — click to edit';
    chip.onclick = () => editCompany(meetingId, confirmed);
    el.appendChild(chip);
  } else if (suggested) {
    chip.className = 'company-chip suggested';
    chip.textContent = suggested + ' ?';
    chip.title = 'Suggested company — click to edit';
    chip.onclick = () => editCompany(meetingId, suggested);
    el.appendChild(chip);
    const ok = document.createElement('button');
    ok.className = 'company-chip-confirm';
    ok.textContent = '✓';
    ok.title = 'Confirm suggested company';
    ok.onclick = () => patchCompany(meetingId, suggested);
    el.appendChild(ok);
  } else {
    chip.className = 'company-chip empty';
    chip.textContent = '+ Company';
    chip.title = 'Set company';
    chip.onclick = () => editCompany(meetingId, '');
    el.appendChild(chip);
  }
}

function editCompany(meetingId, current) {
  // prompt() is the established pattern for small header-adjacent edits
  // (speaker merge/reassign). Empty input clears; Cancel does nothing.
  const input = prompt('Company', current || '');
  if (input === null) return;
  patchCompany(meetingId, input.trim());
}

async function patchCompany(meetingId, value) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/company`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ company: value || null }),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    // Re-render the chip from the response; a CLEAR needs the lazily-computed
    // suggestion back, which only the status endpoint carries.
    let state = { status: 'complete', company: data.company, company_suggestion: null };
    if (!data.company) {
      state = await (await fetch(`${API}/meetings/${meetingId}/status`)).json();
    }
    ['detailCompany', 'detailCompanyMobile'].forEach(id => {
      const el = $(id);
      if (el) renderCompanyChip(el, meetingId, state);
    });
    refreshGroupedView();   // list + filter payloads pick up the new value
  } catch (err) {
    console.error('Company update failed:', err);
    alert('Failed to update company: ' + err.message);
  }
}

async function loadTranscript(id) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/transcript`);
    const data = await resp.json();
    const segments = data.segments || [];

    currentOriginalSegments = segments;
    currentSpeakerMap = {};
    currentSpeakerInfo = {};
    currentSpeakerInfoByName = {};

    try {
      const speakerResp = await fetch(`${API}/meetings/${id}/speakers`);
      if (speakerResp.ok) {
        const speakerData = await speakerResp.json();
        currentSpeakerInfo = speakerData.speaker_info || {};
        for (const [label, info] of Object.entries(currentSpeakerInfo)) {
          if (info.name) {
            currentSpeakerMap[label] = info.name;
            // Build reverse lookup so we can find info by display name too
            currentSpeakerInfoByName[info.name] = info;
          }
        }
      }
    } catch (_) {}

    renderTranscriptWithMap(segments, id);
  } catch (err) {
    const errHtml = `<div style="color:var(--red)">Failed to load transcript: ${escHtml(err.message)}</div>`;
    [$('transcriptContent'), $('transcriptContentMobile')].filter(Boolean).forEach(el => el.innerHTML = errHtml);
  }
}

async function loadSummary(id) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/summary`);
    const data = await resp.json();
    currentSummaryMeetingId = id;
    renderSummary(data);
  } catch (err) {
    const errHtml = `<div style="color:var(--red)">Failed to load summary: ${escHtml(err.message)}</div>`;
    [$('summaryContent'), $('summaryContentMobile')].filter(Boolean).forEach(el => el.innerHTML = errHtml);
  }
}

async function loadTags(id) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/tags`);
    const data = await resp.json();
    renderTags(data, id);
  } catch (err) {
    const errHtml = `<div style="color:var(--red)">Failed to load tags: ${escHtml(err.message)}</div>`;
    [$('tagsContent'), $('tagsContentMobile')].filter(Boolean).forEach(el => el.innerHTML = errHtml);
  }
}

let currentEditTags = {};  // current tags state for editing

function renderTags(tags, meetingId) {
  currentEditTags = JSON.parse(JSON.stringify(tags || {}));
  let html = '';

  const CATEGORIES = ['standup','planning','sprint_review','retrospective','sales','brainstorm','interview','training','one_on_one','all_hands','workshop','demo','other'];

  // Category — dropdown
  const category = tags.category || 'other';
  const options = CATEGORIES.map(c =>
    `<option value="${c}" ${c === category ? 'selected' : ''}>${c.replace(/_/g, ' ')}</option>`
  ).join('');
  html += `<div class="tags-section">
    <h3>Category</h3>
    <select class="category-select" onchange="saveTagCategory('${meetingId}', this.value)">${options}</select>
  </div>`;

  // Keywords — deletable badges + add input
  const keywords = tags.keywords || [];
  const kwBadges = keywords.map(k =>
    `<span class="tag-badge tag-badge-keyword tag-badge-delete" onclick="deleteTag('${meetingId}', 'keyword', '${escHtml(k)}')" title="Click to remove">${escHtml(k)} &times;</span>`
  ).join(' ');
  html += `<div class="tags-section">
    <h3>Keywords</h3>
    <div>${kwBadges || '<span style="color:var(--text-muted)">No keywords</span>'} <button class="tag-add-btn" onclick="showTagInput(this, '${meetingId}', 'keyword')">+ add</button></div>
  </div>`;

  // Entities — deletable badges + add input per type
  const entities = tags.entities || {};
  const entityTypes = ['people', 'companies', 'projects', 'technologies', 'dates'];

  html += `<div class="tags-section"><h3>Entities</h3>`;
  for (const etype of entityTypes) {
    const items = entities[etype] || [];
    const badges = items.map(e =>
      `<span class="tag-badge tag-badge-entity tag-badge-delete" onclick="deleteTag('${meetingId}', '${etype}', '${escHtml(e)}')" title="Click to remove">${escHtml(e)} &times;</span>`
    ).join(' ');
    html += `<div class="tags-entity-group">
      <div class="tags-entity-label">${etype}</div>
      <div>${badges || '<span style="color:var(--text-muted)">none</span>'} <button class="tag-add-btn" onclick="showTagInput(this, '${meetingId}', '${etype}')">+ add</button></div>
    </div>`;
  }
  html += '</div>';

  // Actions
  html += `<div class="tags-actions">
    <button class="action-btn" onclick="reprocessStep('${meetingId}', 'tagging')">Re-generate Tags</button>
  </div>`;

  // Related meetings
  html += `<div class="tags-section" style="margin-top:24px">
    <h3>Related Meetings</h3>
    <div id="relatedMeetings"><div class="spinner"></div> Loading...</div>
  </div>`;

  [$('tagsContent'), $('tagsContentMobile')].filter(Boolean).forEach(el => { el.innerHTML = html; applyDetailCollapse(el); });

  // Load related meetings async
  loadRelatedMeetings(meetingId);
}

async function loadRelatedMeetings(meetingId) {
  const relContainers = document.querySelectorAll('#relatedMeetings');
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/related?limit=5`);
    const data = await resp.json();

    if (!data.length) {
      relContainers.forEach(c => c.innerHTML = '<div style="color:var(--text-muted)">No related meetings found.</div>');
      return;
    }

    const html = data.map(r => {
      const sharedBadges = [
        ...r.shared_keywords.map(k => `<span class="tag-badge tag-badge-keyword">${escHtml(k)}</span>`),
        ...r.shared_entities.slice(0, 3).map(e => `<span class="tag-badge tag-badge-entity">${escHtml(e)}</span>`),
      ].join(' ');

      return `<div class="related-meeting-item" onclick="openMeeting('${r.meeting_id}')">
        <span class="meeting-date">${r.date || ''}</span>
        <span class="meeting-title" style="flex:1">${escHtml(r.title || 'Untitled')}</span>
        <span class="meeting-tags">${sharedBadges}</span>
        <span class="related-score">Score: ${r.score}</span>
      </div>`;
    }).join('');
    relContainers.forEach(c => c.innerHTML = html);
  } catch (err) {
    relContainers.forEach(c => c.innerHTML = `<div style="color:var(--red)">Failed to load: ${escHtml(err.message)}</div>`);
  }
}

// --- Related Tab (Phase 6) ---
async function loadRelated(meetingId) {
  const containers = [$('relatedContent'), $('relatedContentMobile')].filter(Boolean);
  if (!containers.length) return;
  containers.forEach(el => el.innerHTML = '<div style="text-align:center;padding:20px"><div class="spinner"></div> Loading links...</div>');

  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/links`);
    const data = await resp.json();
    let html = '';

    // Manual links
    html += '<div class="link-section"><h3>Linked Meetings</h3>';
    if (data.manual && data.manual.length) {
      data.manual.forEach(link => {
        html += `<div class="link-item">
          <div class="link-item-info" onclick="openMeeting('${link.meeting_id}')">
            <div class="link-title">${escHtml(link.title || 'Untitled')}</div>
            <div class="link-meta">${link.date || ''} &middot; ${link.duration_formatted || ''}</div>
          </div>
          <button class="link-action-btn danger" onclick="unlinkMeeting('${meetingId}', '${link.meeting_id}')">Unlink</button>
        </div>`;
      });
    } else {
      html += '<div style="color:var(--text-muted);font-size:13px;margin-bottom:8px">No linked meetings yet.</div>';
    }
    html += `<button class="link-add-btn" onclick="showLinkPicker('${meetingId}')">+ Link a Meeting</button>`;
    if (data.manual && data.manual.length) {
      html += `<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="action-btn" onclick="generateInsights('${meetingId}')">New Insights</button>
        <button class="action-btn" onclick="generateInsightsWithPrompt('${meetingId}')">Insights with Custom Focus...</button>
      </div>`;
      html += `<div id="insightsGenerating" class="insights-generating" style="display:none">
        <div class="spinner"></div> Generating cross-meeting insights...
      </div>`;
      html += `<div id="insightsHistoryBar" class="insights-history-bar"></div>`;
      html += `<div id="insightsResult" style="margin-top:8px"></div>`;
    }
    html += '</div>';

    // Suggestions
    if (data.suggestions && data.suggestions.length) {
      html += '<div class="link-section"><h3>Suggested Links</h3>';
      data.suggestions.forEach(s => {
        const sharedBadges = [
          ...(s.shared_keywords || []).map(k => `<span class="tag-badge tag-badge-keyword">${escHtml(k)}</span>`),
          ...(s.shared_entities || []).slice(0, 3).map(e => `<span class="tag-badge tag-badge-entity">${escHtml(e)}</span>`),
        ].join(' ');

        // Look up title from cache
        const cached = allMeetingsCache.find(m => m.id === s.meeting_id);
        const title = cached ? cached.title : 'Meeting';
        const date = cached ? cached.date : '';

        html += `<div class="link-item">
          <div class="link-item-info" onclick="openMeeting('${s.meeting_id}')">
            <div class="link-title">${escHtml(title)}</div>
            <div class="link-meta">${date} &middot; Score: ${s.score}</div>
            <div class="link-shared-tags">${sharedBadges}</div>
          </div>
          <div style="display:flex;gap:4px;flex-shrink:0">
            <button class="link-action-btn accept" onclick="acceptSuggestion('${meetingId}', '${s.meeting_id}')">Accept</button>
            <button class="link-action-btn danger" onclick="dismissSuggestion('${meetingId}', '${s.meeting_id}')">Dismiss</button>
          </div>
        </div>`;
      });
      html += '</div>';
    }

    containers.forEach(el => { el.innerHTML = html; applyDetailCollapse(el); });

    // Load insights history
    if (data.manual && data.manual.length) {
      await loadInsightsHistory(meetingId);
    }

    // Append related notes block (safe: all user data run through escHtml before insertion)
    try {
      const notesResp = await fetch(`${API}/meetings/${meetingId}/related-notes`);
      const notesData = await notesResp.json();
      const relNotes = (notesData && notesData.related) || [];
      if (relNotes.length) {
        let notesHtml = '<div class="link-section"><h3>Related Notes</h3>';
        relNotes.forEach(n => {
          const noteId = escHtml(n.note_id); // escaped for safe use in onclick attribute string
          notesHtml += '<div class="link-item">'
            + '<div class="link-item-info" onclick="window.openNoteFromMeeting(\'' + noteId + '\')">'
            + '<div class="link-title">' + escHtml(n.title || n.note_id) + '</div>'
            + '<div class="link-meta">' + escHtml(n.folder || '') + (n.score != null ? ' &middot; Score: ' + n.score : '') + '</div>'
            + '</div>'
            + '</div>';
        });
        notesHtml += '</div>';
        // safe: notesHtml built entirely with escHtml-escaped values
        containers.forEach(el => { el.insertAdjacentHTML('beforeend', notesHtml); applyDetailCollapse(el); });
      }
    } catch (e) { /* related notes are best-effort */ }
  } catch (err) {
    const errHtml = '<div style="color:var(--red)">Failed to load links: ' + escHtml(err.message) + '</div>';
    containers.forEach(el => el.innerHTML = errHtml);
  }
}

async function acceptSuggestion(meetingId, targetId) {
  try {
    await fetch(`${API}/meetings/${meetingId}/links`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_meeting_id: targetId }),
    });
    loadRelated(meetingId);
    pollForNewInsight(meetingId);
  } catch (err) { console.error('Accept suggestion failed:', err); }
}

async function dismissSuggestion(meetingId, targetId) {
  try {
    await fetch(`${API}/meetings/${meetingId}/links/suggestions/${targetId}/dismiss`, { method: 'POST' });
    loadRelated(meetingId);
  } catch (err) { console.error('Dismiss suggestion failed:', err); }
}

async function unlinkMeeting(meetingId, targetId) {
  try {
    await fetch(`${API}/meetings/${meetingId}/links/${targetId}`, { method: 'DELETE' });
    loadRelated(meetingId);
  } catch (err) { console.error('Unlink failed:', err); }
}

// --- Cross-Meeting Insights ---
async function generateInsights(meetingId, customPrompt) {
  const genEl = document.getElementById('insightsGenerating');
  if (genEl) genEl.style.display = 'flex';
  const containers = document.querySelectorAll('#insightsResult');

  try {
    const body = {};
    if (customPrompt) body.custom_prompt = customPrompt;

    const resp = await fetch(`${API}/meetings/${meetingId}/insights`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (genEl) genEl.style.display = 'none';

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      containers.forEach(el => el.innerHTML = `<div style="color:var(--red)">Failed: ${escHtml(err.detail || resp.statusText)}</div>`);
      return;
    }

    await loadInsightsHistory(meetingId);
  } catch (err) {
    if (genEl) genEl.style.display = 'none';
    containers.forEach(el => el.innerHTML = `<div style="color:var(--red)">Error: ${escHtml(err.message)}</div>`);
  }
}

function generateInsightsWithPrompt(meetingId) {
  const customPrompt = prompt(
    'What should the insights focus on? (e.g., "track progress on SOC2 audit", "summarize all action items and their owners", "identify recurring blockers")\n\nLeave empty for general insights:'
  );
  if (customPrompt === null) return;
  generateInsights(meetingId, customPrompt || undefined);
}

function renderInsights(data, meetingId) {
  const insights = data.insights || {};
  const count = data.meetings_analyzed || 0;
  let html = `<div class="insights-panel">`;
  const label = data.label || 'General';
  const ts = data.timestamp ? new Date(data.timestamp).toLocaleString() : '';
  const triggerBadge = data.trigger === 'auto_link' ? ' <span style="font-size:11px;color:var(--green)">(auto)</span>' : '';
  html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <div>
      <h3 style="color:var(--accent);margin:0">${escHtml(label)}${triggerBadge}</h3>
      <div style="font-size:11px;color:var(--text-muted)">${ts} — ${count} meetings analyzed</div>
    </div>
    ${data.id ? `<button class="link-action-btn danger" onclick="deleteInsight('${meetingId}', '${data.id}')" style="flex-shrink:0">Delete</button>` : ''}
  </div>`;

  if (insights.executive_summary) {
    html += `<div class="insights-section">
      <h4>Executive Summary</h4>
      <p>${escHtml(insights.executive_summary)}</p>
    </div>`;
  }

  if (insights.recurring_themes && insights.recurring_themes.length) {
    html += `<div class="insights-section"><h4>Recurring Themes</h4>`;
    for (const t of insights.recurring_themes) {
      html += `<div class="insights-item">
        <strong>${escHtml(t.theme || '')}</strong>
        <p>${escHtml(t.details || '')}</p>
        ${t.meetings ? `<div class="insights-meetings">${t.meetings.map(m => `<span class="tag-badge tag-badge-keyword">${escHtml(m)}</span>`).join(' ')}</div>` : ''}
      </div>`;
    }
    html += `</div>`;
  }

  if (insights.progress_tracking && insights.progress_tracking.length) {
    html += `<div class="insights-section"><h4>Progress Tracking</h4>`;
    for (const p of insights.progress_tracking) {
      const statusColor = p.status === 'completed' ? 'var(--green)' : p.status === 'stalled' ? 'var(--red)' : 'var(--yellow)';
      html += `<div class="insights-item">
        <strong>${escHtml(p.item || '')}</strong> <span style="color:${statusColor};font-size:12px">[${escHtml(p.status || '')}]</span>
        <p>${escHtml(p.history || '')}</p>
      </div>`;
    }
    html += `</div>`;
  }

  if (insights.unresolved_items && insights.unresolved_items.length) {
    html += `<div class="insights-section"><h4>Unresolved Items</h4>`;
    for (const u of insights.unresolved_items) {
      html += `<div class="insights-item">
        <strong>${escHtml(u.item || '')}</strong>
        <p>First raised: ${escHtml(u.first_raised || 'unknown')} — Status: ${escHtml(u.current_status || 'unknown')}</p>
      </div>`;
    }
    html += `</div>`;
  }

  if (insights.key_relationships && insights.key_relationships.length) {
    html += `<div class="insights-section"><h4>Key Relationships</h4>`;
    for (const r of insights.key_relationships) {
      html += `<div class="insights-item"><p>${escHtml(r.description || '')}</p></div>`;
    }
    html += `</div>`;
  }

  if (insights.recommendations && insights.recommendations.length) {
    html += `<div class="insights-section"><h4>Recommendations</h4>`;
    for (const r of insights.recommendations) {
      html += `<div class="insights-item"><p>${escHtml(r.recommendation || '')}</p></div>`;
    }
    html += `</div>`;
  }

  html += `</div>`;

  document.querySelectorAll('#insightsResult').forEach(el => el.innerHTML = html);
}

async function loadInsightsHistory(meetingId) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/insights`);
    if (!resp.ok) return;
    const data = await resp.json();
    const list = data.insights || [];
    insightsCache[meetingId] = { list, activeId: list.length ? list[0].id : null };
    renderInsightsChips(meetingId);
    if (list.length) {
      await loadInsightDetail(meetingId, list[0].id);
    } else {
      document.querySelectorAll('#insightsResult').forEach(el => el.innerHTML = '');
    }
  } catch (_) {}
}

function renderInsightsChips(meetingId) {
  const bar = document.getElementById('insightsHistoryBar');
  if (!bar) return;
  const cache = insightsCache[meetingId];
  if (!cache || !cache.list.length) {
    bar.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">No insights generated yet.</div>';
    return;
  }
  bar.innerHTML = cache.list.map(ins => {
    const active = ins.id === cache.activeId ? ' active' : '';
    const date = ins.timestamp ? new Date(ins.timestamp).toLocaleDateString() : '';
    const time = ins.timestamp ? new Date(ins.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    const autoIcon = ins.trigger === 'auto_link' ? '<span class="chip-auto"></span>' : '';
    return `<button class="insights-chip${active}" onclick="loadInsightDetail('${meetingId}', '${ins.id}')">
      ${autoIcon}<span class="chip-label">${escHtml(ins.label)}</span>
      <span class="chip-date">${date} ${time}</span>
    </button>`;
  }).join('');
}

async function loadInsightDetail(meetingId, insightId) {
  if (insightsCache[meetingId]) {
    insightsCache[meetingId].activeId = insightId;
    renderInsightsChips(meetingId);
  }
  const containers = document.querySelectorAll('#insightsResult');
  containers.forEach(el => el.innerHTML = '<div style="text-align:center;padding:20px"><div class="spinner"></div></div>');
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/insights?insight_id=${insightId}`);
    if (!resp.ok) {
      containers.forEach(el => el.innerHTML = '<div style="color:var(--red)">Failed to load insight</div>');
      return;
    }
    const entry = await resp.json();
    renderInsights(entry, meetingId);
  } catch (err) {
    containers.forEach(el => el.innerHTML = `<div style="color:var(--red)">Error: ${escHtml(err.message)}</div>`);
  }
}

async function deleteInsight(meetingId, insightId) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/insights/${insightId}`, { method: 'DELETE' });
    if (!resp.ok) return;
    await loadInsightsHistory(meetingId);
  } catch (err) {
    console.error('Delete insight failed:', err);
  }
}

function pollForNewInsight(meetingId) {
  const genEl = document.getElementById('insightsGenerating');
  if (genEl) genEl.style.display = 'flex';
  let attempts = 0;
  const maxAttempts = 60;
  const existingCount = insightsCache[meetingId]?.list?.length || 0;
  const timer = setInterval(async () => {
    attempts++;
    try {
      const resp = await fetch(`${API}/meetings/${meetingId}/insights`);
      if (resp.ok) {
        const data = await resp.json();
        const newCount = (data.insights || []).length;
        if (newCount > existingCount) {
          clearInterval(timer);
          if (genEl) genEl.style.display = 'none';
          await loadInsightsHistory(meetingId);
          return;
        }
      }
    } catch (_) {}
    if (attempts >= maxAttempts) {
      clearInterval(timer);
      if (genEl) genEl.style.display = 'none';
    }
  }, 5000);
}

// --- Link Picker ---
async function showLinkPicker(meetingId) {
  linkPickerMeetingId = meetingId;
  $('linkPickerOverlay').classList.add('visible');
  $('linkPickerSearch').value = '';
  renderLinkPickerList('');
  $('linkPickerSearch').focus();

  // Refresh cache so picker always has current meetings
  try {
    const resp = await fetch(`${API}/meetings`);
    allMeetingsCache = await resp.json();
    renderLinkPickerList($('linkPickerSearch').value.trim().toLowerCase());
  } catch (_) {}
}

function closeLinkPicker() {
  $('linkPickerOverlay').classList.remove('visible');
  linkPickerMeetingId = null;
}

$('linkPickerSearch').addEventListener('input', (e) => {
  renderLinkPickerList(e.target.value.trim().toLowerCase());
});

function renderLinkPickerList(filter) {
  const list = $('linkPickerList');
  const completeMeetings = allMeetingsCache.filter(m =>
    m.status === 'complete' && m.id !== linkPickerMeetingId
  );

  const filtered = filter
    ? completeMeetings.filter(m => (m.title || '').toLowerCase().includes(filter) || (m.date || '').includes(filter))
    : completeMeetings;

  if (!filtered.length) {
    list.innerHTML = '<div style="padding:16px;color:var(--text-muted);text-align:center">No meetings found.</div>';
    return;
  }

  list.innerHTML = filtered.slice(0, 20).map(m => `
    <div class="link-picker-item" onclick="pickLink('${m.id}')">
      <span class="lpi-date">${m.date || ''}</span>
      <span class="lpi-title">${escHtml(m.title || 'Untitled')}</span>
    </div>
  `).join('');
}

async function pickLink(targetId) {
  if (!linkPickerMeetingId) return;
  try {
    await fetch(`${API}/meetings/${linkPickerMeetingId}/links`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_meeting_id: targetId }),
    });
    closeLinkPicker();
    loadRelated(linkPickerMeetingId);
    pollForNewInsight(linkPickerMeetingId);
  } catch (err) { console.error('Link failed:', err); }
}

$('linkPickerOverlay').addEventListener('click', (e) => {
  if (e.target === $('linkPickerOverlay')) closeLinkPicker();
});

async function saveTagUpdate(meetingId, body) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/tags`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (resp.ok) {
      const data = await resp.json();
      renderTags(data.tags, meetingId);
      refreshMeetings();
    } else {
      const data = await resp.json().catch(() => ({}));
      console.error('Tag update failed:', data.detail);
    }
  } catch (err) {
    console.error('Tag update failed:', err);
  }
}

function saveTagCategory(meetingId, category) {
  saveTagUpdate(meetingId, { category });
}

function deleteTag(meetingId, type, value) {
  if (type === 'keyword') {
    const keywords = (currentEditTags.keywords || []).filter(k => k !== value);
    saveTagUpdate(meetingId, { keywords });
  } else {
    // Entity type
    const entities = currentEditTags.entities || {};
    const items = (entities[type] || []).filter(e => e !== value);
    saveTagUpdate(meetingId, { entities: { [type]: items } });
  }
}

function showTagInput(btn, meetingId, type) {
  if (btn.nextElementSibling && btn.nextElementSibling.classList.contains('tag-add-input')) return;
  const input = document.createElement('input');
  input.className = 'tag-add-input';
  input.placeholder = type === 'keyword' ? 'new keyword' : `new ${type.slice(0, -1)}`;
  btn.after(input);
  input.focus();

  const add = () => {
    const val = input.value.trim();
    input.remove();
    if (!val) return;
    if (type === 'keyword') {
      const keywords = [...(currentEditTags.keywords || []), val];
      saveTagUpdate(meetingId, { keywords });
    } else {
      const entities = currentEditTags.entities || {};
      const items = [...(entities[type] || []), val];
      saveTagUpdate(meetingId, { entities: { [type]: items } });
    }
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); add(); }
    if (e.key === 'Escape') input.remove();
  });
  input.addEventListener('blur', add);
}

// --- Inline edit (pencil-icon editing; all four edit surfaces use this) ---
let currentSummaryData = null;       // raw /summary response — editors seed from
                                     // this, never from displayed speaker-mapped text
let currentSummaryMeetingId = null;
let transcriptEditedSinceAnalysis = false;  // drives the "Re-run analysis" banner

// Swaps el's content for input(s)/textarea(s). Enter commits (Ctrl/Cmd+Enter in
// textareas), Esc or Cancel restores the original content. opts is either
// {value, multiline?, onSave(str)} or {fields: [{name,label,value,multiline?}],
// onSave(valuesByName)}. onSave may return a Promise; on rejection the original
// content is restored and the error alerted. On success the caller re-renders.
// innerHTML is safe here: static template text only (no interpolated dynamic
// values); the restore path re-assigns the element's own previously-rendered
// markup (prevHtml), which was itself produced by escHtml()-wrapped renderers.
function inlineEdit(el, opts) {
  if (!el || el.dataset.editing === '1') return;
  el.dataset.editing = '1';
  const fields = opts.fields ||
    [{ name: 'value', label: null, value: opts.value || '', multiline: !!opts.multiline }];
  const prevHtml = el.innerHTML;

  const wrap = document.createElement('div');
  wrap.className = 'inline-edit';
  const editors = {};
  for (const f of fields) {
    if (f.label) {
      const lab = document.createElement('label');
      lab.className = 'inline-edit-label';
      lab.textContent = f.label;
      wrap.appendChild(lab);
    }
    const editor = document.createElement(f.multiline ? 'textarea' : 'input');
    editor.className = 'inline-edit-input';
    editor.value = f.value || '';
    if (f.multiline) editor.rows = Math.min(10, Math.max(3, String(f.value || '').split('\n').length + 1));
    wrap.appendChild(editor);
    editors[f.name] = editor;
  }
  const actions = document.createElement('div');
  actions.className = 'inline-edit-actions';
  // Safe: static markup only, no interpolated values.
  actions.innerHTML = '<button type="button" class="action-btn ie-save">Save</button>' +
                      '<button type="button" class="action-btn ie-cancel">Cancel</button>';
  wrap.appendChild(actions);

  el.innerHTML = '';
  el.appendChild(wrap);
  const first = editors[fields[0].name];
  first.focus();
  if (first.tagName === 'INPUT') first.select();

  // Safe: prevHtml is this element's own previously-rendered markup (produced by
  // an escHtml()-wrapped renderer elsewhere), not attacker-controlled input.
  const cancel = () => { el.innerHTML = prevHtml; delete el.dataset.editing; };
  const commit = async () => {
    actions.querySelector('.ie-save').disabled = true;
    try {
      if (opts.fields) {
        const values = {};
        for (const [name, ed] of Object.entries(editors)) values[name] = ed.value;
        await opts.onSave(values);
      } else {
        await opts.onSave(editors.value.value);
      }
      delete el.dataset.editing;   // success: the caller re-renders this surface
    } catch (err) {
      cancel();
      alert('Save failed: ' + (err && err.message ? err.message : err));
    }
  };
  actions.querySelector('.ie-save').addEventListener('click', commit);
  actions.querySelector('.ie-cancel').addEventListener('click', cancel);
  wrap.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    if (e.key === 'Enter' && (e.target.tagName === 'INPUT' || e.ctrlKey || e.metaKey)) {
      e.preventDefault(); commit();
    }
  });
}

function applySpeakerMapToText(text) {
  let result = text;
  for (const [original, renamed] of Object.entries(currentSpeakerMap)) {
    if (renamed) result = result.replaceAll(original, renamed);
  }
  return result;
}

function renderSummary(s) {
  currentSummaryData = s;   // raw values — inline editors seed from these
  let html = '';

  // Summary (new) or Executive Summary (legacy)
  const summaryText = s.summary || s.executive_summary;
  if (summaryText) {
    html += `<div class="summary-section">
      <h3>Summary <button class="pencil-btn" title="Edit summary" onclick="editSummaryText(this)">&#9999;</button></h3>
      <p class="summary-text">${escHtml(applySpeakerMapToText(summaryText))}</p>
    </div>`;
  }

  // Key Topics (new: topics with outcome) or legacy key_topics
  const topics = s.topics || s.key_topics;
  if (topics && topics.length) {
    html += `<div class="summary-section"><h3>Key Topics</h3><ul>`;
    for (const t of topics) {
      const outcome = t.outcome ? ` <span style="color:var(--text-muted)">→ ${escHtml(t.outcome)}</span>` : '';
      html += `<li><strong>${escHtml(t.topic || '')}</strong>: ${escHtml(applySpeakerMapToText(t.summary || ''))}${outcome}</li>`;
    }
    html += `</ul></div>`;
  }

  // Action Items (new: task/who/deadline or legacy: description/assigned_to)
  if (s.action_items && s.action_items.length) {
    html += `<div class="summary-section"><h3>Action Items</h3><ul>`;
    s.action_items.forEach((a, i) => {
      const priorityClass = `priority-${a.priority || 'medium'}`;
      const task = a.task || a.description || '';
      const who = applySpeakerMapToText(a.who || a.assigned_to || 'Unassigned');
      const deadline = a.deadline ? ` &middot; Deadline: ${escHtml(a.deadline)}` : '';
      html += `<li><div class="action-item">
        <input type="checkbox">
        <div>
          <div>${escHtml(task)}</div>
          <div class="action-meta">
            Assigned: ${escHtml(who)}
            &middot; Priority: <span class="${priorityClass}">${a.priority || 'medium'}</span>${deadline}
          </div>
        </div>
        <button class="pencil-btn" title="Edit task" onclick="editActionItem(this, ${i})">&#9999;</button>
      </div></li>`;
    });
    html += `</ul></div>`;
  }

  // Decisions
  if (s.decisions && s.decisions.length) {
    html += `<div class="summary-section"><h3>Decisions</h3><ul>`;
    s.decisions.forEach((d, i) => {
      html += `<li>
        <strong>${escHtml(applySpeakerMapToText(d.decision || ''))}</strong>
        <button class="pencil-btn" title="Edit decision" onclick="editDecision(this, ${i})">&#9999;</button>
        <div class="decision-context">${escHtml(applySpeakerMapToText(d.context || ''))}</div>
      </li>`;
    });
    html += `</ul></div>`;
  }

  // Open Questions (new) or Questions Raised (legacy)
  const questions = s.open_questions || s.questions_raised;
  if (questions && questions.length) {
    html += `<div class="summary-section"><h3>Open Questions</h3><ul>`;
    questions.forEach((q, i) => {
      const badge = q.answered
        ? '<span style="color:var(--green)">[Answered]</span>'
        : '<span style="color:var(--yellow)">[Open]</span>';
      const askedBy = q.asked_by ? ` <span style="color:var(--text-muted)">(${escHtml(applySpeakerMapToText(q.asked_by))})</span>` : '';
      html += `<li>${escHtml(applySpeakerMapToText(q.question || ''))}${askedBy} ${badge} <button class="pencil-btn" title="Edit question" onclick="editOpenQuestion(this, ${i})">&#9999;</button></li>`;
    });
    html += `</ul></div>`;
  }

  // Concerns & Risks (new from Pass D)
  if (s.concerns && s.concerns.length) {
    html += `<div class="summary-section"><h3>Concerns & Risks</h3><ul>`;
    s.concerns.forEach((c, i) => {
      const raisedBy = c.raised_by ? ` <span style="color:var(--text-muted)">(${escHtml(applySpeakerMapToText(c.raised_by))})</span>` : '';
      const resolvedBadge = c.resolved
        ? '<span style="color:var(--green)">[Resolved]</span>'
        : '<span style="color:var(--yellow)">[Open]</span>';
      const notes = c.notes ? `<div style="color:var(--text-secondary);font-size:13px;margin-top:2px">${escHtml(applySpeakerMapToText(c.notes))}</div>` : '';
      html += `<li>${escHtml(applySpeakerMapToText(c.concern || ''))}${raisedBy} ${resolvedBadge} <button class="pencil-btn" title="Edit concern" onclick="editConcern(this, ${i})">&#9999;</button>${notes}</li>`;
    });
    html += `</ul></div>`;
  }

  // Key Figures & Dates (new from Pass E)
  if (s.figures && s.figures.length) {
    html += `<div class="summary-section"><h3>Key Figures & Dates</h3><ul>`;
    for (const f of s.figures) {
      const saidBy = f.said_by ? ` <span style="color:var(--text-muted)">(${escHtml(applySpeakerMapToText(f.said_by))})</span>` : '';
      html += `<li><strong>${escHtml(f.figure || '')}</strong>: ${escHtml(applySpeakerMapToText(f.context || ''))}${saidBy}</li>`;
    }
    html += `</ul></div>`;
  }

  // Sentiment (new) or sentiment_overview (legacy)
  const sentiment = s.sentiment || s.sentiment_overview;
  if (sentiment) {
    const sentClass = `sentiment-${sentiment.overall || 'neutral'}`;
    html += `<div class="summary-section"><h3>Sentiment</h3>
      <p><span class="sentiment-badge ${sentClass}">${sentiment.overall || 'N/A'}</span></p>`;
    if (sentiment.notable_moments && sentiment.notable_moments.length) {
      html += '<ul>';
      for (const m of sentiment.notable_moments) {
        if (typeof m === 'object' && m.moment) {
          html += `<li>${escHtml(applySpeakerMapToText(m.moment))} <span style="color:var(--text-muted)">— ${escHtml(m.tone || '')}</span></li>`;
        } else {
          html += `<li>${escHtml(applySpeakerMapToText(typeof m === 'string' ? m : ''))}</li>`;
        }
      }
      html += '</ul>';
    }
    html += '</div>';
  }

  const summaryHtml = html || '<div class="empty-state">No summary data.</div>';
  [$('summaryContent'), $('summaryContentMobile')].filter(Boolean).forEach(el => { el.innerHTML = summaryHtml; applyDetailCollapse(el); });
}

// --- Summary field editing (PUT /meetings/{id}/summary/{field}) ---
// Editors seed from currentSummaryData (raw stored values), never from the
// displayed applySpeakerMapToText(...) output, so speaker-label mapping never
// gets baked into stored text. Server returns the updated summary; re-render.
async function saveSummaryField(field, value) {
  const resp = await fetch(`${API}/meetings/${currentSummaryMeetingId}/summary/${field}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  renderSummary(data);
}

// Replace item i (in a copy) of the field's canonical-or-legacy array and PUT it.
function replaceSummaryItem(field, i, updated) {
  const arr = (field === 'open_questions')
    ? (currentSummaryData.open_questions || currentSummaryData.questions_raised || [])
    : (currentSummaryData[field] || []);
  const copy = arr.map((it, idx) => (idx === i ? updated : it));
  return saveSummaryField(field, copy);
}

function editSummaryText(btn) {
  const section = btn.closest('.summary-section');
  if (section.classList.contains('collapsed')) toggleDetailSection(section.querySelector('h3'));
  const p = section.querySelector('.summary-text');
  const raw = currentSummaryData.summary || currentSummaryData.executive_summary || '';
  inlineEdit(p, { value: raw, multiline: true,
                  onSave: (v) => saveSummaryField('summary', v) });
}

function editActionItem(btn, i) {
  const el = btn.closest('.action-item');
  const item = currentSummaryData.action_items[i];
  inlineEdit(el, {
    value: item.task || item.description || '', multiline: true,
    onSave: (v) => {
      const updated = { ...item, task: v };
      delete updated.description;   // canonical field wins everywhere it's read
      return replaceSummaryItem('action_items', i, updated);
    },
  });
}

function editDecision(btn, i) {
  const el = btn.closest('li');
  const d = currentSummaryData.decisions[i];
  inlineEdit(el, {
    fields: [
      { name: 'decision', label: 'Decision', value: d.decision || '' },
      { name: 'context', label: 'Context', value: d.context || '', multiline: true },
    ],
    onSave: (v) => replaceSummaryItem('decisions', i,
      { ...d, decision: v.decision, context: v.context }),
  });
}

function editConcern(btn, i) {
  const el = btn.closest('li');
  const c = currentSummaryData.concerns[i];
  inlineEdit(el, {
    fields: [
      { name: 'concern', label: 'Concern', value: c.concern || '', multiline: true },
      { name: 'notes', label: 'Notes', value: c.notes || '', multiline: true },
    ],
    onSave: (v) => replaceSummaryItem('concerns', i,
      { ...c, concern: v.concern, notes: v.notes }),
  });
}

function editOpenQuestion(btn, i) {
  const el = btn.closest('li');
  const arr = currentSummaryData.open_questions || currentSummaryData.questions_raised || [];
  const q = arr[i];
  inlineEdit(el, {
    value: q.question || '',
    onSave: (v) => replaceSummaryItem('open_questions', i, { ...q, question: v }),
  });
}

// Close detail
$('closeDetail').addEventListener('click', closeDetail);
detailOverlay.addEventListener('click', e => {
  if (e.target === detailOverlay) closeDetail();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    // Close overlays in z-index order: link picker > settings > detail
    const lp = document.getElementById('linkPickerOverlay');
    if (lp && lp.classList.contains('visible')) {
      closeLinkPicker();
    } else {
      const so = document.getElementById('settingsOverlay');
      if (so && so.classList.contains('visible')) {
        if (typeof closeSettings === 'function') closeSettings();
      } else if (detailOverlay.classList.contains('visible')) {
        closeDetail();
      }
    }
  }
});

function closeDetail() {
  ['', 'Mobile'].forEach(p => {
    const a = $('meetingAudio' + p);
    if (a) { a.pause(); a.src = ''; }
    const b = $('audioPlayerBar' + p);
    if (b) b.classList.remove('visible');
  });
  audioPlayerMeetingId = null;

  // Mobile overlay
  detailOverlay.classList.remove('visible');
  document.body.style.overflow = '';

  // Desktop inline
  $('inlineDetail').style.display = 'none';
  $('mainEmptyState').style.display = 'flex';

  currentMeetingId = null;
  if (typeof updateFloatingChatScope === 'function') updateFloatingChatScope();
  // Update sidebar active state
  document.querySelectorAll('.sidebar-meeting-item').forEach(el => el.classList.remove('active'));
}

// Tabs - use event delegation for both inline and mobile tabs
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.tab-btn');
  if (!btn) return;
  const tabContainer = btn.closest('.main-content-inner') || btn.closest('.detail-panel');
  if (!tabContainer) return;
  tabContainer.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  tabContainer.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  const tabEl = tabContainer.querySelector('#tab-' + btn.dataset.tab) ||
                tabContainer.querySelector('#tab-' + btn.dataset.tab + '-mobile');
  if (tabEl) tabEl.classList.add('active');

  // Load related tab lazily
  if (btn.dataset.tab === 'related' && currentMeetingId) {
    loadRelated(currentMeetingId);
  }
  // Load notes tab lazily
  if (btn.dataset.tab === 'notes' && currentMeetingId) {
    loadNotes(currentMeetingId);
  }
});

// Clicking a transcript timestamp seeks the audio to that point
document.addEventListener('click', (e) => {
  const timeEl = e.target.closest('.seg-time');
  if (!timeEl) return;
  const seg = timeEl.closest('.transcript-segment');
  if (!seg) return;
  const t = parseFloat(seg.dataset.segStart);
  if (isNaN(t)) return;
  const isMobile = !!timeEl.closest('.detail-panel');
  const audio = $('meetingAudio' + (isMobile ? 'Mobile' : ''));
  if (!audio || !audio.src) return;
  audio.currentTime = t;
  audio.play();
  e.stopPropagation();
});

// --- Search ---
$('searchBtn').addEventListener('click', doSearch);
searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
searchInput.addEventListener('focus', () => {
  $('searchFilters').classList.add('visible');
});

async function doSearch() {
  const q = searchInput.value.trim();
  if (!q) {
    searchResults.classList.remove('visible');
    return;
  }

  // Build query params with filters
  const params = new URLSearchParams({ q, limit: '10' });
  const speaker = $('searchSpeaker').value.trim();
  const dateFrom = $('searchDateFrom').value;
  const dateTo = $('searchDateTo').value;
  const chunkType = $('searchChunkType').value;
  const showContext = $('searchShowContext').checked;

  if (speaker) params.set('speaker', speaker);
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
  if (chunkType) params.set('chunk_type', chunkType);
  if (showContext) params.set('include_context', 'true');

  searchResults.classList.add('visible');
  searchResults.innerHTML = '<div style="padding:12px;color:var(--text-muted)"><div class="spinner"></div> Searching...</div>';

  try {
    const resp = await fetch(`${API}/meetings/search?${params.toString()}`);
    const data = await resp.json();

    if (resp.ok && data.length) {
      searchResults.innerHTML = data.map(r => {
        let contextHtml = '';
        if (r.context && r.context.length) {
          contextHtml = `<div class="search-context">${
            r.context.map(seg => {
              const ts = formatTimestamp(seg.start || 0);
              const sp = seg.speaker || 'UNKNOWN';
              return `<div class="search-context-seg"><strong>${ts} ${escHtml(sp)}:</strong> ${escHtml(seg.text || '')}</div>`;
            }).join('')
          }</div>`;
        }
        return `<div class="search-result-item" style="cursor:pointer" onclick="openMeeting('${r.meeting_id}')">
          <div class="search-result-meta">
            <span>${r.date || ''}</span>
            <span>${escHtml(r.title || '')}</span>
            <span>${r.chunk_type || ''}</span>
            ${r.speaker ? `<span>${escHtml(r.speaker)}</span>` : ''}
            <span>Score: ${(r.score || 0).toFixed(3)}</span>
          </div>
          <div class="search-result-text">${escHtml(r.text || '')}</div>
          ${contextHtml}
        </div>`;
      }).join('');
    } else if (resp.ok) {
      searchResults.innerHTML = '<div style="padding:16px;color:var(--text-muted)">No results found.</div>';
    } else {
      searchResults.innerHTML = `<div style="padding:16px;color:var(--red)">Search error: ${escHtml(data.detail || 'Unknown error')}</div>`;
    }
  } catch (err) {
    searchResults.innerHTML = `<div style="padding:16px;color:var(--red)">Search failed: ${escHtml(err.message)}</div>`;
  }
}

// --- Delete Meeting ---
async function deleteMeeting(id) {
  if (!confirm('Are you sure you want to delete this meeting? This cannot be undone.')) return;

  try {
    const resp = await fetch(`${API}/meetings/${id}`, { method: 'DELETE' });
    if (resp.ok) {
      if (currentMeetingId === id) closeDetail();
      refreshMeetings();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('Delete failed: ' + (data.detail || resp.statusText));
    }
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

$('detailDeleteBtn').addEventListener('click', () => {
  if (currentMeetingId) deleteMeeting(currentMeetingId);
});

// --- Retry & Reprocess ---
async function retryMeeting(id) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/retry`, { method: 'POST' });
    if (resp.ok) {
      closeDetail();
      refreshMeetings();
      startPolling();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('Retry failed: ' + (data.detail || resp.statusText));
    }
  } catch (err) {
    alert('Retry failed: ' + err.message);
  }
}

async function reprocessStep(id, step) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/reprocess`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step }),
    });
    if (resp.ok) {
      // Refresh after a short delay to show progress
      startPolling();
      setTimeout(() => openMeeting(id), 1000);
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('Reprocess failed: ' + (data.detail || resp.statusText));
    }
  } catch (err) {
    alert('Reprocess failed: ' + err.message);
  }
}

// --- Trim audio -> new meeting ---
let trimMeetingId = null;

// The <audio> element openMeeting initialized for the current view (desktop or mobile).
function activeMeetingAudio() {
  const mob = $('meetingAudioMobile');
  if (mob && mob.getAttribute('src')) return mob;
  return $('meetingAudio');
}

// Accepts "ss", "mm:ss", or "h:mm:ss" (decimals allowed in the last part).
function parseTimeInput(str) {
  const s = (str || '').trim();
  if (!s || !/^[\d:.]+$/.test(s)) return NaN;
  const parts = s.split(':').map(Number);
  if (parts.some(isNaN)) return NaN;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

function openTrimModal(id) {
  trimMeetingId = id;
  const audio = activeMeetingAudio();
  $('trimStart').value = '0:00:00';
  $('trimEnd').value = (audio && isFinite(audio.duration) && audio.duration > 0)
    ? formatTimestamp(Math.floor(audio.duration)) : '';
  $('trimTitle').value = '';
  $('trimError').style.display = 'none';
  $('trimOverlay').classList.add('visible');
}

function closeTrimModal() {
  $('trimOverlay').classList.remove('visible');
  trimMeetingId = null;
}

$('trimStartFromPlayer').addEventListener('click', () => {
  const audio = activeMeetingAudio();
  if (audio) $('trimStart').value = formatTimestamp(Math.floor(audio.currentTime));
});

$('trimEndFromPlayer').addEventListener('click', () => {
  const audio = activeMeetingAudio();
  if (audio) $('trimEnd').value = formatTimestamp(Math.floor(audio.currentTime));
});

$('trimSubmitBtn').addEventListener('click', async () => {
  if (!trimMeetingId) return;
  const errEl = $('trimError');
  const showErr = (msg) => { errEl.textContent = msg; errEl.style.display = ''; };
  const start = parseTimeInput($('trimStart').value);
  const end = parseTimeInput($('trimEnd').value);
  if (isNaN(start) || isNaN(end)) { showErr('Enter times as h:mm:ss (e.g. 0:12:30).'); return; }
  if (end - start < 1) { showErr('End must be at least 1 second after start.'); return; }
  const audio = activeMeetingAudio();
  if (audio && isFinite(audio.duration) && audio.duration > 0 && start >= audio.duration) {
    showErr('Start is beyond the end of the audio.'); return;
  }
  errEl.style.display = 'none';

  const btn = $('trimSubmitBtn');
  btn.disabled = true;
  try {
    const resp = await fetch(`${API}/meetings/${trimMeetingId}/trim`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start, end, title: $('trimTitle').value.trim() || null }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 202) {
      closeTrimModal();
      startPolling();
      refreshMeetings();
      alert(`Trimmed copy queued as a new meeting: "${data.title}"`);
    } else {
      showErr(data.detail || resp.statusText);
    }
  } catch (err) {
    showErr(err.message);
  } finally {
    btn.disabled = false;
  }
});

// --- Speaker Name Mapping ---
let currentSpeakerMap = {};
let currentSpeakerInfo = {};
let currentSpeakerInfoByName = {};  // reverse lookup: display name -> speaker info entry
let currentOriginalSegments = [];

function renderSpeakerMapBar(segments, meetingId) {
  const speakers = [];
  const seen = new Set();
  for (const seg of segments) {
    const sp = seg.speaker || 'UNKNOWN';
    if (!seen.has(sp)) {
      seen.add(sp);
      speakers.push(sp);
    }
  }

  if (!speakers.length) return '';

  const chips = speakers.map((sp, idx) => {
    const colorClass = `speaker-${idx % 8}`;
    const info = currentSpeakerInfo[sp] || currentSpeakerInfoByName[sp];
    const isAutoDetected = info && info.auto_detected;
    const autoStar = isAutoDetected ? '<span class="speaker-chip-auto" title="Auto-detected by AI">&#9733;</span>' : '';
    const detailParts = [];
    if (info && info.title) detailParts.push(info.title);
    if (info && info.company) detailParts.push(info.company);
    const detailText = detailParts.length ? `<span class="speaker-chip-detail">(${escHtml(detailParts.join(', '))})</span>` : '';
    const nameText = info ? info.name : (currentSpeakerMap[sp] || '');
    return `<span class="speaker-chip ${colorClass}" data-original="${escHtml(sp)}" title="Edit name, company, title" data-edit-speaker="${escHtml(sp)}" data-meeting="${escHtml(meetingId)}">
      <span class="speaker-chip-label">${escHtml(sp)}:</span>
      <span class="speaker-chip-name">${escHtml(nameText || 'click to rename')}${detailText}${autoStar}</span>
      <span class="pencil-btn" aria-hidden="true">&#9999;</span>
    </span>`;
  }).join('');

  const mergeBtn = speakers.length >= 2
    ? `<button class="action-btn" style="font-size:0.75rem;padding:2px 8px;margin-left:8px" onclick="showMergeSpeakers('${meetingId}')" title="Merge two speakers into one">Merge Speakers</button>`
    : '';
  const reassignBtn = `<button class="action-btn" style="font-size:0.75rem;padding:2px 8px;margin-left:4px" onclick="toggleReassignMode('${meetingId}')" title="Click segments to reassign them to a different speaker">Reassign Segments</button>`;

  return `<div class="speaker-map-bar">${chips}${mergeBtn}${reassignBtn}</div>`;
}

function editSpeakerName(chipEl, originalName, meetingId) {
  if (chipEl.querySelector('.speaker-chip-input')) return;

  const info = currentSpeakerInfo[originalName] || currentSpeakerInfoByName[originalName];
  const currentName = currentSpeakerMap[originalName] || (info ? info.name : '') || '';
  const nameSpan = chipEl.querySelector('.speaker-chip-name');
  const oldText = nameSpan.textContent;

  const input = document.createElement('input');
  input.className = 'speaker-chip-input';
  input.value = currentName;
  input.placeholder = 'Enter name';

  nameSpan.textContent = '';
  nameSpan.appendChild(input);
  input.focus();
  input.select();

  const save = async () => {
    const newName = input.value.trim();
    if (newName) {
      currentSpeakerMap[originalName] = newName;
    } else {
      delete currentSpeakerMap[originalName];
    }

    try {
      const resp = await fetch(`${API}/meetings/${meetingId}/speakers`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speaker_map: currentSpeakerMap }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        console.error('Failed to save speaker names:', data.detail);
      }
    } catch (err) {
      console.error('Failed to save speaker names:', err);
    }

    renderTranscriptWithMap(currentOriginalSegments, meetingId);
    if (currentMeetingId === meetingId) {
      loadSummary(meetingId);
    }
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    if (e.key === 'Escape') {
      nameSpan.textContent = oldText;
    }
  });

  input.addEventListener('blur', save);
}

// Delegated click handler for speaker chips (avoids inline onclick w/ interpolated,
// user/LLM-sourced speaker names — see data-edit-speaker / data-meeting attrs above).
document.addEventListener('click', (e) => {
  const chip = e.target.closest('[data-edit-speaker]');
  if (!chip) return;
  editSpeakerName(chip, chip.dataset.editSpeaker, chip.dataset.meeting);
});

let reassignMode = false;
let reassignMeetingId = null;
let selectedSegmentIndices = new Set();

function showMergeSpeakers(meetingId) {
  // Collect current speakers from transcript
  const speakers = [];
  const seen = new Set();
  for (const seg of currentOriginalSegments) {
    const sp = currentSpeakerMap[seg.speaker] || seg.speaker || 'UNKNOWN';
    if (!seen.has(sp)) { seen.add(sp); speakers.push(sp); }
  }
  if (speakers.length < 2) { alert('Need at least 2 speakers to merge.'); return; }

  const srcLabel = 'Select speaker to REMOVE (will be merged into target):';
  const src = prompt(srcLabel + '\n\nSpeakers:\n' + speakers.map((s, i) => `${i + 1}. ${s}`).join('\n') + '\n\nEnter number:');
  if (!src) return;
  const srcIdx = parseInt(src) - 1;
  if (isNaN(srcIdx) || srcIdx < 0 || srcIdx >= speakers.length) return;

  const remaining = speakers.filter((_, i) => i !== srcIdx);
  const tgtLabel = 'Select TARGET speaker (keeps this name):';
  const tgt = prompt(tgtLabel + '\n\n' + remaining.map((s, i) => `${i + 1}. ${s}`).join('\n') + '\n\nEnter number:');
  if (!tgt) return;
  const tgtIdx = parseInt(tgt) - 1;
  if (isNaN(tgtIdx) || tgtIdx < 0 || tgtIdx >= remaining.length) return;

  const source = speakers[srcIdx];
  const target = remaining[tgtIdx];

  fetch(`${API}/meetings/${meetingId}/speakers/merge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speakers: [source, target], target }),
  }).then(r => r.json()).then(data => {
    if (data.detail) {
      // Reload the meeting to reflect changes
      loadMeeting(meetingId);
    }
  }).catch(err => console.error('Merge failed:', err));
}

function toggleReassignMode(meetingId) {
  reassignMode = !reassignMode;
  reassignMeetingId = reassignMode ? meetingId : null;
  selectedSegmentIndices.clear();

  // Update button style
  document.querySelectorAll('.transcript-segment').forEach(el => {
    el.classList.remove('seg-selected');
  });

  if (reassignMode) {
    // Add reassign toolbar
    const bar = document.querySelector('.speaker-map-bar');
    if (bar && !document.getElementById('reassign-toolbar')) {
      const toolbar = document.createElement('div');
      toolbar.id = 'reassign-toolbar';
      toolbar.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:8px;padding:8px;background:var(--surface);border-radius:8px;border:1px solid var(--accent)';
      toolbar.innerHTML = `
        <span style="font-size:0.8rem;color:var(--accent)">Click segments to select, then:</span>
        <button class="action-btn" style="font-size:0.75rem;padding:2px 8px" onclick="applyReassign()">Assign to Speaker...</button>
        <button class="action-btn" style="font-size:0.75rem;padding:2px 8px" onclick="toggleReassignMode()">Cancel</button>
        <span id="reassign-count" style="font-size:0.75rem;color:var(--text-secondary)">0 selected</span>
      `;
      bar.after(toolbar);
    }

    // Make segments clickable for selection
    document.querySelectorAll('.transcript-segment').forEach((el, idx) => {
      el.style.cursor = 'pointer';
      el.onclick = (e) => {
        e.stopPropagation();
        if (selectedSegmentIndices.has(idx)) {
          selectedSegmentIndices.delete(idx);
          el.classList.remove('seg-selected');
        } else {
          selectedSegmentIndices.add(idx);
          el.classList.add('seg-selected');
        }
        const countEl = document.getElementById('reassign-count');
        if (countEl) countEl.textContent = `${selectedSegmentIndices.size} selected`;
      };
    });
  } else {
    const toolbar = document.getElementById('reassign-toolbar');
    if (toolbar) toolbar.remove();
    document.querySelectorAll('.transcript-segment').forEach(el => {
      el.style.cursor = '';
      el.onclick = null;
    });
  }
}

function applyReassign() {
  if (!selectedSegmentIndices.size) return;

  const speakers = [];
  const seen = new Set();
  for (const seg of currentOriginalSegments) {
    const sp = currentSpeakerMap[seg.speaker] || seg.speaker || 'UNKNOWN';
    if (!seen.has(sp)) { seen.add(sp); speakers.push(sp); }
  }

  const input = prompt(
    'Assign selected segments to which speaker?\n\n' +
    speakers.map((s, i) => `${i + 1}. ${s}`).join('\n') +
    '\n\nEnter number, or type a new speaker name:'
  );
  if (!input) return;

  const num = parseInt(input);
  const newSpeaker = (!isNaN(num) && num >= 1 && num <= speakers.length)
    ? speakers[num - 1]
    : input.trim();

  if (!newSpeaker) return;

  fetch(`${API}/meetings/${reassignMeetingId}/speakers/reassign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ segment_indices: [...selectedSegmentIndices], new_speaker: newSpeaker }),
  }).then(r => r.json()).then(data => {
    if (data.detail) {
      toggleReassignMode();
      loadMeeting(reassignMeetingId);
    }
  }).catch(err => console.error('Reassign failed:', err));
}

function applyMapToSegments(segments) {
  return segments.map(seg => {
    const sp = seg.speaker || 'UNKNOWN';
    return {
      ...seg,
      speaker: currentSpeakerMap[sp] || sp,
    };
  });
}

// --- Virtual scroll for large transcripts ---
const VIRTUAL_SCROLL_THRESHOLD = 200;
const VIRTUAL_SCROLL_BATCH = 50;

function renderTranscriptWithMap(originalSegments, meetingId) {
  const containers = [$('transcriptContent'), $('transcriptContentMobile')].filter(Boolean);
  if (!originalSegments.length) {
    containers.forEach(el => el.innerHTML = '<div class="empty-state">No transcript segments.</div>');
    return;
  }

  const mapped = applyMapToSegments(originalSegments);
  const speakerMapBarHtml = renderSpeakerMapBar(originalSegments, meetingId);

  const bannerHtml = transcriptEditedSinceAnalysis
    ? `<div class="reanalysis-banner">Transcript edited since the last analysis —
        <button class="action-btn" onclick="reprocessStep('${meetingId}', 'summarize')">Re-run analysis</button>
        <span class="reanalysis-note">(regenerates the summary; replaces any manual summary edits)</span>
      </div>`
    : '';

  const speakerColorMap = {};
  let speakerIdx = 0;

  // Pre-compute color map
  for (const seg of originalSegments) {
    const sp = seg.speaker || 'UNKNOWN';
    if (!(sp in speakerColorMap)) {
      speakerColorMap[sp] = speakerIdx++;
    }
  }

  function renderSegmentHtml(seg, origSeg, segIdx) {
    const speaker = seg.speaker || 'UNKNOWN';
    const origSp = origSeg.speaker || 'UNKNOWN';
    const colorClass = `speaker-${(speakerColorMap[origSp] || 0) % 8}`;
    const time = formatTimestamp(seg.start);
    const anns = getSegmentAnnotations(seg.start);
    const badge = anns.length > 0
      ? `<span class="seg-annotation-badge" onclick="event.stopPropagation();switchToNotesTab()" title="${anns.length} annotation${anns.length > 1 ? 's' : ''}">${anns.length}</span>`
      : '';
    return `<div class="transcript-segment" data-seg-start="${seg.start}">
      <span class="seg-time">${time}</span>
      <span class="seg-speaker ${colorClass}">${escHtml(speaker)}${badge}</span>
      <span class="seg-text">${escHtml(seg.text)}</span>
      <button class="seg-edit-btn" onclick="event.stopPropagation();editSegmentText(this, ${segIdx}, '${meetingId}')" title="Edit text">&#9999;</button>
      <button class="seg-annotate-btn" onclick="event.stopPropagation();showAnnotationForm(${segIdx}, ${seg.start}, '${meetingId}')" title="Annotate">&#9998;</button>
    </div>`;
  }

  // Full HTML for mobile (no virtual scroll) and small transcripts
  const fullSegmentsHtml = bannerHtml + speakerMapBarHtml + mapped.map((seg, i) =>
    renderSegmentHtml(seg, originalSegments[i], i)
  ).join('');

  // Mobile container always gets full render
  const mobileEl = $('transcriptContentMobile');
  if (mobileEl) mobileEl.innerHTML = fullSegmentsHtml;

  // Desktop: use virtual scroll for large transcripts
  const desktopEl = $('transcriptContent');
  if (!desktopEl) return;

  if (mapped.length > VIRTUAL_SCROLL_THRESHOLD) {
    desktopEl.innerHTML = bannerHtml + speakerMapBarHtml;

    const segContainer = document.createElement('div');
    desktopEl.appendChild(segContainer);

    let renderedCount = 0;

    function renderBatch() {
      const end = Math.min(renderedCount + VIRTUAL_SCROLL_BATCH, mapped.length);
      let batchHtml = '';
      for (let i = renderedCount; i < end; i++) {
        batchHtml += renderSegmentHtml(mapped[i], originalSegments[i], i);
      }
      segContainer.insertAdjacentHTML('beforeend', batchHtml);
      renderedCount = end;

      // Remove old sentinel if any
      const oldSentinel = segContainer.querySelector('.scroll-sentinel');
      if (oldSentinel) oldSentinel.remove();

      // Add sentinel if more segments remain
      if (renderedCount < mapped.length) {
        const sentinel = document.createElement('div');
        sentinel.className = 'scroll-sentinel';
        segContainer.appendChild(sentinel);

        const observer = new IntersectionObserver((entries) => {
          if (entries[0].isIntersecting) {
            observer.disconnect();
            renderBatch();
          }
        }, { root: null, rootMargin: '200px' });
        observer.observe(sentinel);
      }
    }

    renderBatch();
  } else {
    desktopEl.innerHTML = fullSegmentsHtml;
  }
}

// Segment text edit (PUT /meetings/{id}/segments/{idx}). Seeds from
// currentOriginalSegments (raw) and sends that same value as expected_text —
// a stale editor gets a 409 and we refresh instead of clobbering.
function editSegmentText(btn, segIdx, meetingId) {
  const segEl = btn.closest('.transcript-segment');
  const textEl = segEl && segEl.querySelector('.seg-text');
  const orig = currentOriginalSegments[segIdx];
  if (!textEl || !orig) return;
  const original = orig.text || '';
  inlineEdit(textEl, {
    value: original, multiline: true,
    onSave: async (v) => {
      const resp = await fetch(`${API}/meetings/${meetingId}/segments/${segIdx}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: v, expected_text: original }),
      });
      if (resp.status === 409) {
        alert('Segment changed — refreshing the transcript.');
        loadTranscript(meetingId);
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      currentOriginalSegments[segIdx].text = data.text;
      transcriptEditedSinceAnalysis = true;   // show the Re-run analysis banner
      renderTranscriptWithMap(currentOriginalSegments, meetingId);  // virtual-scroll safe
    },
  });
}

// --- Utilities ---
function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  // textContent->innerHTML round-trip escapes &, <, > but NOT quotes (they're
  // only special in attribute-value context, not text-node serialization).
  // Escape them too so escHtml() output is safe to interpolate into HTML
  // attribute values (e.g. data-original="${escHtml(sp)}") and single-quoted
  // inline-handler string literals, not just text-node contexts — closes the
  // attr-breakout class at app.js's speaker-chip / speaker-edit-popover
  // templates. Safe in text-node contexts too: innerHTML text nodes decode
  // entities, so a stray &quot;/&#39; still renders as a literal quote.
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}

function formatTimestamp(sec) {
  if (!isFinite(sec) || isNaN(sec) || sec < 0) return '00:00:00';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

// ── Audio Player ──────────────────────────────────────────────────────────

async function initAudioPlayer(meetingId, mobile) {
  const p = mobile ? 'Mobile' : '';
  const bar   = $('audioPlayerBar' + p);
  const audio = $('meetingAudio' + p);
  if (!bar || !audio) return;

  // Reset
  audio.pause();
  audio.src = '';
  bar.classList.remove('visible');

  // Check whether audio exists for this meeting
  const url = `${API}/meetings/${meetingId}/audio`;
  try {
    const ac = new AbortController();
    const probe = await fetch(url, { headers: { 'Range': 'bytes=0-0' }, signal: ac.signal });
    ac.abort();
    if (!probe.ok && probe.status !== 206) return;
  } catch (_) { return; }

  audio.src = url;
  bar.classList.add('visible');
  audioPlayerMeetingId = meetingId;

  const playBtn    = $('audioPlayBtn'      + p);
  const scrubber   = $('audioScrubber'     + p);
  const curTimeEl  = $('audioCurrentTime'  + p);
  const totTimeEl  = $('audioTotalTime'    + p);
  const skipBack   = $('audioSkipBack'     + p);
  const skipFwd    = $('audioSkipFwd'      + p);
  const speedSel   = $('audioSpeedSelect'  + p);

  speedSel.value = '1';
  audio.playbackRate = 1;

  const updateDuration = () => {
    if (isFinite(audio.duration) && audio.duration > 0) {
      scrubber.max = audio.duration;
      totTimeEl.textContent = formatTimestamp(Math.floor(audio.duration));
    }
  };
  audio.onloadedmetadata = updateDuration;
  audio.ondurationchange = updateDuration;

  audio.ontimeupdate = () => {
    if (!audio.seeking) scrubber.value = audio.currentTime;
    curTimeEl.textContent = formatTimestamp(Math.floor(audio.currentTime));
    syncTranscriptToAudio(audio.currentTime, mobile);
  };

  audio.onplay  = () => { playBtn.innerHTML = '&#9646;&#9646;'; };
  audio.onpause = () => { playBtn.innerHTML = '&#9654;'; };
  audio.onended = () => { playBtn.innerHTML = '&#9654;'; };

  playBtn.onclick  = () => { audio.paused ? audio.play() : audio.pause(); };
  skipBack.onclick = () => { audio.currentTime = Math.max(0, audio.currentTime - 10); };
  skipFwd.onclick  = () => { audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + 10); };
  scrubber.oninput = () => { audio.currentTime = parseFloat(scrubber.value); };
  speedSel.onchange = () => { audio.playbackRate = parseFloat(speedSel.value); };
}

function syncTranscriptToAudio(currentTime, mobile) {
  // Determine if Transcript tab is currently active
  const container = mobile
    ? document.querySelector('.detail-panel')
    : document.querySelector('.main-content-inner');
  const activeBtn = container && container.querySelector('.tab-btn.active');
  const transcriptActive = activeBtn && activeBtn.dataset.tab === 'transcript';

  // Re-query each call: virtual scroll adds segments progressively
  const segs = document.querySelectorAll('.transcript-segment');
  if (!segs.length) return;

  let activeEl = null;
  for (let i = segs.length - 1; i >= 0; i--) {
    const t = parseFloat(segs[i].dataset.segStart);
    if (!isNaN(t) && t <= currentTime) { activeEl = segs[i]; break; }
  }

  segs.forEach(s => s.classList.remove('audio-active'));
  if (!activeEl) return;
  activeEl.classList.add('audio-active');

  if (transcriptActive) {
    const r = activeEl.getBoundingClientRect();
    if (r.top < 80 || r.bottom > window.innerHeight - 40) {
      activeEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }
}

// --- Notes ---
async function loadNotes(meetingId) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/notes`);
    const data = await resp.json();
    currentNotes = data.notes || [];
    renderNotes(meetingId);
  } catch (err) {
    currentNotes = [];
    renderNotes(meetingId);
  }
}

function renderNotes(meetingId) {
  const containers = [$('notesContent'), $('notesContentMobile')].filter(Boolean);
  const freeNotes = currentNotes.filter(n => n.type === 'free');
  const annotations = currentNotes.filter(n => n.type === 'annotation');

  const html = `
    <div class="notes-section">
      <div class="notes-section-header">
        <h3>Notes</h3>
      </div>
      <div id="noteFormSlot"></div>
      <button class="add-note-btn" onclick="showNoteForm('${meetingId}')">+ Add Note</button>
      ${freeNotes.length === 0 ? '<div class="empty-notes">No notes yet</div>' :
        freeNotes.map(n => renderNoteCard(n, meetingId)).join('')}
    </div>
    <div class="notes-section">
      <div class="notes-section-header">
        <h3>Transcript Annotations</h3>
      </div>
      ${annotations.length === 0 ? '<div class="empty-notes">No annotations yet. Click the pencil icon on transcript segments to add annotations.</div>' :
        annotations.sort((a, b) => (a.segment_start || 0) - (b.segment_start || 0))
          .map(n => renderAnnotationCard(n, meetingId)).join('')}
    </div>
  `;
  containers.forEach(c => { c.innerHTML = html; applyDetailCollapse(c); });
}

function renderNoteCard(note, meetingId) {
  const date = new Date(note.created_at).toLocaleString();
  return `<div class="note-card" id="note-${note.id}">
    <div class="note-card-header">
      <span class="note-meta">${date}</span>
      <div class="note-actions">
        <button onclick="editNote('${meetingId}', '${note.id}')" title="Edit">Edit</button>
        <button onclick="deleteNote('${meetingId}', '${note.id}')" title="Delete">Del</button>
      </div>
    </div>
    <div class="note-content" id="note-content-${note.id}">${escHtml(note.content)}</div>
  </div>`;
}

function renderAnnotationCard(note, meetingId) {
  const date = new Date(note.created_at).toLocaleString();
  const time = note.segment_start != null ? formatTimestamp(note.segment_start) : '';
  return `<div class="note-card" id="note-${note.id}">
    <div class="note-card-header">
      <span class="note-meta">${date}</span>
      <div class="note-actions">
        <button onclick="editNote('${meetingId}', '${note.id}')" title="Edit">Edit</button>
        <button onclick="deleteNote('${meetingId}', '${note.id}')" title="Delete">Del</button>
      </div>
    </div>
    <div class="note-content" id="note-content-${note.id}">${escHtml(note.content)}</div>
    ${time ? `<div class="note-segment-ref" onclick="jumpToTranscriptTime(${note.segment_start})">@ ${time}</div>` : ''}
  </div>`;
}

function showNoteForm(meetingId, existingNoteId) {
  const existing = existingNoteId ? currentNotes.find(n => n.id === existingNoteId) : null;
  const slot = document.getElementById('noteFormSlot');
  if (!slot) return;
  slot.innerHTML = `<div class="note-form">
    <textarea id="noteFormText" placeholder="Type your note...">${existing ? escHtml(existing.content) : ''}</textarea>
    <div class="note-form-actions">
      <button class="note-save-btn" onclick="${existing ? `saveEditNote('${meetingId}', '${existingNoteId}')` : `createNote('${meetingId}')`}">${existing ? 'Save' : 'Add'}</button>
      <button class="note-cancel-btn" onclick="document.getElementById('noteFormSlot').innerHTML=''">Cancel</button>
    </div>
  </div>`;
  document.getElementById('noteFormText').focus();
}

async function createNote(meetingId) {
  const text = document.getElementById('noteFormText');
  if (!text || !text.value.trim()) return;
  try {
    await fetch(`${API}/meetings/${meetingId}/notes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'free', content: text.value.trim() }),
    });
    loadNotes(meetingId);
  } catch (err) { console.error('Failed to create note:', err); }
}

function editNote(meetingId, noteId) {
  showNoteForm(meetingId, noteId);
}

async function saveEditNote(meetingId, noteId) {
  const text = document.getElementById('noteFormText');
  if (!text || !text.value.trim()) return;
  try {
    await fetch(`${API}/meetings/${meetingId}/notes/${noteId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: text.value.trim() }),
    });
    loadNotes(meetingId);
  } catch (err) { console.error('Failed to update note:', err); }
}

async function deleteNote(meetingId, noteId) {
  if (!confirm('Delete this note?')) return;
  try {
    await fetch(`${API}/meetings/${meetingId}/notes/${noteId}`, { method: 'DELETE' });
    loadNotes(meetingId);
  } catch (err) { console.error('Failed to delete note:', err); }
}

function getSegmentAnnotations(segStart) {
  return currentNotes.filter(n => n.type === 'annotation' && Math.abs((n.segment_start || 0) - segStart) < 0.5);
}

function showAnnotationForm(segIndex, segStart, meetingId) {
  // Remove any existing annotation form
  document.querySelectorAll('.annotation-inline-form').forEach(el => el.remove());
  const segments = document.querySelectorAll('.transcript-segment');
  const seg = segments[segIndex];
  if (!seg) return;
  const form = document.createElement('div');
  form.className = 'annotation-inline-form';
  form.innerHTML = `<textarea id="annotationFormText" placeholder="Add annotation for this segment..."></textarea>
    <div class="note-form-actions">
      <button class="note-save-btn" onclick="createAnnotation('${meetingId}', ${segStart}, ${segIndex})">Add</button>
      <button class="note-cancel-btn" onclick="this.closest('.annotation-inline-form').remove()">Cancel</button>
    </div>`;
  seg.after(form);
  form.querySelector('textarea').focus();
}

async function createAnnotation(meetingId, segStart, segIndex) {
  const text = document.getElementById('annotationFormText');
  if (!text || !text.value.trim()) return;
  try {
    await fetch(`${API}/meetings/${meetingId}/notes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'annotation', content: text.value.trim(), segment_start: segStart, segment_index: segIndex }),
    });
    document.querySelectorAll('.annotation-inline-form').forEach(el => el.remove());
    loadNotes(meetingId);
    // Reload transcript to show badges
    loadTranscript(meetingId);
  } catch (err) { console.error('Failed to create annotation:', err); }
}

function switchToNotesTab() {
  const tabContainer = document.querySelector('.main-content-inner') || document.querySelector('.detail-panel');
  if (!tabContainer) return;
  tabContainer.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === 'notes');
  });
  tabContainer.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  const notesTab = tabContainer.querySelector('#tab-notes') || tabContainer.querySelector('#tab-notes-mobile');
  if (notesTab) notesTab.classList.add('active');
  if (currentMeetingId) loadNotes(currentMeetingId);
}

function jumpToTranscriptTime(segStart) {
  // Switch to transcript tab
  const tabContainer = document.querySelector('.main-content-inner') || document.querySelector('.detail-panel');
  if (!tabContainer) return;
  tabContainer.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === 'transcript');
  });
  tabContainer.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  const transcriptTab = tabContainer.querySelector('#tab-transcript') || tabContainer.querySelector('#tab-transcript-mobile');
  if (transcriptTab) transcriptTab.classList.add('active');

  // Find and scroll to the segment
  const segments = document.querySelectorAll('.transcript-segment');
  for (const seg of segments) {
    const timeEl = seg.querySelector('.seg-time');
    if (!timeEl) continue;
    // Parse time string back to seconds for approximate match
    const parts = timeEl.textContent.split(':').map(Number);
    const t = (parts[0] || 0) * 3600 + (parts[1] || 0) * 60 + (parts[2] || 0);
    if (Math.abs(t - segStart) < 1) {
      seg.scrollIntoView({ behavior: 'smooth', block: 'center' });
      seg.style.background = 'var(--accent-dim)';
      setTimeout(() => seg.style.background = '', 2000);
      return;
    }
  }
}


// --- Chat ---
async function initChat(meetingId) {
  chatHistory = [];
  chatScope = { scope: 'meeting', meeting_id: meetingId };
  if (chatAbortController) { chatAbortController.abort(); chatAbortController = null; }

  const containers = [$('chatContent'), $('chatContentMobile')].filter(Boolean);
  if (!containers.length) return;

  // Build scope options based on meeting data
  let scopeOptions = [{ label: 'This Meeting', scope: 'meeting', meeting_id: meetingId }];

  try {
    const statusResp = await fetch(`${API}/meetings/${meetingId}/status`);
    const status = await statusResp.json();
    const meeting = status;

    // Check for linked meetings
    try {
      const linksResp = await fetch(`${API}/meetings/${meetingId}/links`);
      const linksData = await linksResp.json();
      const manualCount = (linksData.manual || []).length;
      if (manualCount > 0) {
        scopeOptions.push({ label: `Linked (${manualCount + 1})`, scope: 'linked', meeting_id: meetingId });
      }
    } catch (e) {}

    // Check for category
    try {
      const tagsResp = await fetch(`${API}/meetings/${meetingId}/tags`);
      const tags = await tagsResp.json();
      if (tags.category && tags.category !== 'other') {
        scopeOptions.push({ label: `Category: ${tags.category}`, scope: 'category', category: tags.category, meeting_id: meetingId });
      }
    } catch (e) {}

    scopeOptions.push({ label: 'All Meetings', scope: 'global' });
  } catch (e) {
    scopeOptions.push({ label: 'All Meetings', scope: 'global' });
  }

  const scopeBarHtml = `<div class="chat-scope-bar">${scopeOptions.map((opt, i) =>
    `<button class="chat-scope-btn${i === 0 ? ' active' : ''}" data-scope-idx="${i}" onclick="setChatScope(${i})">${escHtml(opt.label)}</button>`
  ).join('')}</div>`;

  const chatHtml = `<div class="chat-container">
    ${scopeBarHtml}
    <div class="chat-messages" id="chatMessages">
      <div class="chat-empty">Ask a question about this meeting's content</div>
    </div>
    <div class="chat-input-area">
      <textarea id="chatInput" placeholder="Ask about this meeting..." rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChatMessage();}"></textarea>
      <button class="chat-send-btn" id="chatSendBtn" onclick="sendChatMessage()">Send</button>
    </div>
  </div>`;

  containers.forEach(c => c.innerHTML = chatHtml);

  // Store scope options globally
  window._chatScopeOptions = scopeOptions;
}

function setChatScope(idx) {
  const options = window._chatScopeOptions || [];
  if (!options[idx]) return;
  chatScope = options[idx];
  document.querySelectorAll('.chat-scope-btn').forEach((btn, i) => {
    btn.classList.toggle('active', i === idx);
  });
}

async function sendChatMessage() {
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('chatSendBtn');
  if (!input || !input.value.trim()) return;

  const message = input.value.trim();
  input.value = '';
  input.style.height = 'auto';

  // Add user message
  chatHistory.push({ role: 'user', content: message });
  appendChatMessage('user', message);

  // Clear empty state
  const empty = document.querySelector('.chat-empty');
  if (empty) empty.remove();

  // Disable input during streaming
  if (sendBtn) sendBtn.disabled = true;
  input.disabled = true;

  // Create assistant bubble
  const assistantEl = appendChatMessage('assistant', '');
  const contentEl = assistantEl.querySelector('.chat-msg-text');
  const badgeEl = assistantEl.querySelector('.chat-context-badge');

  // Start streaming
  chatAbortController = new AbortController();
  let fullResponse = '';

  try {
    const resp = await fetch(`${API}/meetings/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        context: chatScope || { scope: 'meeting', meeting_id: currentMeetingId },
        history: chatHistory.slice(0, -1).slice(-20), // last 20 messages, excluding the just-added user msg
      }),
      signal: chatAbortController.signal,
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const event = JSON.parse(line.slice(6));
          if (event.type === 'context') {
            if (badgeEl) badgeEl.textContent = `${event.chunks_used} chunks from ${event.meetings_searched} meeting${event.meetings_searched !== 1 ? 's' : ''}`;
          } else if (event.type === 'token') {
            fullResponse += event.content;
            if (contentEl) contentEl.textContent = fullResponse;
            // Auto-scroll
            const msgs = document.getElementById('chatMessages');
            if (msgs) msgs.scrollTop = msgs.scrollHeight;
          } else if (event.type === 'error') {
            if (contentEl) contentEl.textContent = `Error: ${event.content}`;
          } else if (event.type === 'done') {
            break;
          }
        } catch (e) {}
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      if (contentEl) contentEl.textContent = `Error: ${err.message}`;
    }
  }

  if (fullResponse) {
    chatHistory.push({ role: 'assistant', content: fullResponse });
  }

  chatAbortController = null;
  if (sendBtn) sendBtn.disabled = false;
  input.disabled = false;
  input.focus();
}

function appendChatMessage(role, content) {
  const msgs = document.getElementById('chatMessages');
  if (!msgs) return null;
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  if (role === 'assistant') {
    div.innerHTML = `<div class="chat-msg-text">${escHtml(content)}</div><div class="chat-context-badge"></div>`;
  } else {
    div.innerHTML = `<div class="chat-msg-text">${escHtml(content)}</div>`;
  }
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}


// --- Model dropdown helpers ---
async function populateModelDropdown(selectedModel) {
  const sel = document.getElementById('settingsModel');
  const custom = document.getElementById('settingsModelCustom');
  let models = [];
  try {
    const r = await fetch('/api/models');
    models = (await r.json()).models || [];
  } catch (_) { /* fall back to free text */ }
  while (sel.firstChild) sel.removeChild(sel.firstChild);   // safe clear (no innerHTML)
  for (const m of models) {
    const gb = (m.size / 1e9).toFixed(1);
    const o = document.createElement('option');
    o.value = m.name;
    o.textContent = `${m.name} (${gb} GB${m.parameter_size ? ', ' + m.parameter_size : ''})`;
    sel.appendChild(o);
  }
  const opt = document.createElement('option');
  opt.value = '__custom__'; opt.textContent = 'Custom…';
  sel.appendChild(opt);
  if (selectedModel && models.some(m => m.name === selectedModel)) {
    sel.value = selectedModel; custom.style.display = 'none';
  } else if (selectedModel) {
    sel.value = '__custom__'; custom.style.display = 'block'; custom.value = selectedModel;
  }
  sel.onchange = () => { custom.style.display = sel.value === '__custom__' ? 'block' : 'none'; };
}

function selectedModelValue() {
  const sel = document.getElementById('settingsModel');
  return sel.value === '__custom__'
    ? document.getElementById('settingsModelCustom').value.trim()
    : sel.value;
}

// --- Settings ---
let settingsData = null;   // loaded from server
let settingsDefaults = null;
let settingsDirty = false;

const settingsOverlay = $('settingsOverlay');
const promptFields = {
  cleanup_system: $('promptCleanup'),
  speaker_id: $('promptSpeakerId'),
  analysis_pass_a: $('promptPassA'),
  analysis_pass_b: $('promptPassB'),
  analysis_pass_c: $('promptPassC'),
  analysis_pass_d: $('promptPassD'),
  analysis_pass_e: $('promptPassE'),
  analysis_pass_f: $('promptPassF'),
  analysis_pass_g: $('promptPassG'),
  chunk_summary: $('promptChunkSummary'),
};

$('settingsBtn').addEventListener('click', openSettings);
$('closeSettings').addEventListener('click', closeSettings);
$('settingsCancel').addEventListener('click', closeSettings);
settingsOverlay.addEventListener('click', e => {
  if (e.target === settingsOverlay) closeSettings();
});

// Track unsaved changes
function markSettingsDirty() {
  settingsDirty = true;
  $('settingsUnsaved').classList.add('visible');
}

Object.values(promptFields).forEach(ta => ta.addEventListener('input', markSettingsDirty));
// Client-only capture preference (localStorage) — saved instantly, not via the
// server settings form, so it doesn't participate in the unsaved-changes flow.
$('settingsStreamBackup').addEventListener('change', (e) => {
  localStorage.setItem('captureStreamBackup', e.target.checked ? 'on' : 'off');
});
$('settingsModel').addEventListener('change', markSettingsDirty);
$('settingsModelCustom').addEventListener('input', markSettingsDirty);
$('settingsSttBackend').addEventListener('change', markSettingsDirty);
$('settingsDiarize').addEventListener('change', markSettingsDirty);
$('settingsRemoveFiller').addEventListener('change', markSettingsDirty);
$('settingsTemp').addEventListener('input', () => {
  $('tempDisplay').textContent = parseFloat($('settingsTemp').value).toFixed(2);
  markSettingsDirty();
});

// Chat settings listeners
function toggleChatCustomFields() {
  const custom = $('chatCustomFields');
  if (custom) custom.classList.toggle('visible', $('chatEndpoint').value === 'custom');
}
$('chatEndpoint').addEventListener('change', () => { toggleChatCustomFields(); markSettingsDirty(); });
$('chatModel').addEventListener('input', markSettingsDirty);
$('chatCustomUrl').addEventListener('input', markSettingsDirty);
$('chatCustomKey').addEventListener('input', markSettingsDirty);
$('chatTemp').addEventListener('input', () => {
  $('chatTempDisplay').textContent = parseFloat($('chatTemp').value).toFixed(2);
  markSettingsDirty();
});
$('chatMaxChunks').addEventListener('input', markSettingsDirty);
$('chatSystemPrompt').addEventListener('input', markSettingsDirty);
$('chatResetSystemPrompt').addEventListener('click', () => {
  if (settingsDefaults && settingsDefaults.chat) {
    $('chatSystemPrompt').value = settingsDefaults.chat.system_prompt || '';
    markSettingsDirty();
  }
});

// SMTP / email settings listeners
function toggleSmtpFields() {
  const fields = $('smtpFields');
  if (fields) fields.classList.toggle('visible', $('smtpEnabled').checked);
}
$('smtpEnabled').addEventListener('change', () => { toggleSmtpFields(); markSettingsDirty(); });
['smtpHost', 'smtpPort', 'smtpUsername', 'smtpPassword', 'smtpFromEmail',
 'smtpFromName', 'smtpReplyTo', 'smtpRecipients'].forEach(id => {
  $(id).addEventListener('input', markSettingsDirty);
});
$('smtpSecure').addEventListener('change', markSettingsDirty);
$('smtpTestBtn').addEventListener('click', async () => {
  const status = $('smtpTestStatus');
  if (settingsDirty) {
    status.style.color = 'var(--yellow, #b8860b)';
    status.textContent = 'Save your settings first, then send a test.';
    return;
  }
  status.style.color = 'var(--text-secondary)';
  status.textContent = 'Sending…';
  try {
    const resp = await fetch(`${API}/api/settings/test-email`, { method: 'POST' });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      status.style.color = 'var(--green, #2e7d32)';
      status.textContent = data.detail || 'Test email sent.';
    } else {
      status.style.color = 'var(--red, #c62828)';
      status.textContent = data.detail || 'Failed to send test email.';
    }
  } catch (err) {
    status.style.color = 'var(--red, #c62828)';
    status.textContent = 'Failed: ' + err.message;
  }
});

// Digest / ICS settings listeners
function toggleDigestFields() {
  const fields = $('digestFields');
  if (fields) fields.classList.toggle('visible', $('digestEnabled').checked);
}
$('digestEnabled').addEventListener('change', () => { toggleDigestFields(); markSettingsDirty(); });
['digestTime', 'digestTimezone', 'digestRecipients'].forEach(id => {
  $(id).addEventListener('input', markSettingsDirty);
});
$('digestTestBtn').addEventListener('click', async () => {
  const status = $('digestTestStatus');
  if (settingsDirty) {
    status.style.color = 'var(--yellow, #b8860b)';
    status.textContent = 'Save your settings first, then send a test.';
    return;
  }
  status.style.color = 'var(--text-secondary)';
  status.textContent = 'Sending…';
  try {
    const resp = await fetch(`${API}/api/digest/test`, { method: 'POST' });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      status.style.color = 'var(--green, #2e7d32)';
      status.textContent = data.detail || 'Digest sent.';
    } else {
      status.style.color = 'var(--red, #c62828)';
      status.textContent = data.detail || 'Failed to send digest.';
    }
  } catch (err) {
    status.style.color = 'var(--red, #c62828)';
    status.textContent = 'Failed: ' + err.message;
  }
});

function toggleIcsFields() {
  const fields = $('icsFields');
  if (fields) fields.classList.toggle('visible', $('icsEnabled').checked);
}
function updateIcsUrlHint() {
  const hint = $('icsUrlHint');
  if (!hint) return;
  const token = $('icsToken').value.trim();
  hint.textContent = token
    ? `https://meetings.example.com/api/tasks/calendar.ics?token=${token}`
    : 'https://meetings.example.com/api/tasks/calendar.ics?token=…';
}
$('icsEnabled').addEventListener('change', () => { toggleIcsFields(); markSettingsDirty(); });
$('icsGenerateBtn').addEventListener('click', () => {
  $('icsToken').value = crypto.randomUUID().replace(/-/g, '');
  updateIcsUrlHint();
  markSettingsDirty();
});

// Per-prompt reset buttons
document.querySelectorAll('.settings-reset-prompt-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const key = btn.dataset.prompt;
    if (settingsDefaults && settingsDefaults.prompts[key] !== undefined) {
      promptFields[key].value = settingsDefaults.prompts[key];
      markSettingsDirty();
    }
  });
});

// Reset all
$('settingsResetAll').addEventListener('click', async () => {
  if (!confirm('Reset all LLM settings to defaults?')) return;
  try {
    const resp = await fetch(`${API}/api/settings/reset`, { method: 'POST' });
    if (resp.ok) {
      const data = await resp.json();
      await populateSettingsForm(data.settings, settingsDefaults);
      settingsDirty = false;
      $('settingsUnsaved').classList.remove('visible');
    }
  } catch (err) {
    alert('Reset failed: ' + err.message);
  }
});

// Save
$('settingsSave').addEventListener('click', async () => {
  const body = {
    prompts: {},
    ollama_model: selectedModelValue(),
    temperature: parseFloat($('settingsTemp').value),
    stt_backend: $('settingsSttBackend').value,
    diarize: $('settingsDiarize').checked,
    remove_filler: $('settingsRemoveFiller').checked,
    chat: {
      endpoint: $('chatEndpoint').value,
      model: $('chatModel').value.trim(),
      custom_url: $('chatCustomUrl').value.trim(),
      custom_api_key: $('chatCustomKey').value.trim(),
      temperature: parseFloat($('chatTemp').value),
      max_context_chunks: parseInt($('chatMaxChunks').value) || 15,
      system_prompt: $('chatSystemPrompt').value,
    },
    smtp: {
      enabled: $('smtpEnabled').checked,
      host: $('smtpHost').value.trim(),
      port: parseInt($('smtpPort').value) || 587,
      secure: $('smtpSecure').checked,
      username: $('smtpUsername').value.trim(),
      password: $('smtpPassword').value,
      from_email: $('smtpFromEmail').value.trim(),
      from_name: $('smtpFromName').value.trim(),
      reply_to: $('smtpReplyTo').value.trim(),
      recipients: $('smtpRecipients').value.trim(),
    },
    digest: {
      enabled: $('digestEnabled').checked,
      time: $('digestTime').value.trim(),
      timezone: $('digestTimezone').value.trim(),
      recipients: $('digestRecipients').value.trim(),
    },
    ics: {
      enabled: $('icsEnabled').checked,
      token: $('icsToken').value.trim(),
    },
  };
  for (const [key, ta] of Object.entries(promptFields)) {
    body.prompts[key] = ta.value;
  }

  try {
    const resp = await fetch(`${API}/api/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (resp.ok) {
      settingsDirty = false;
      $('settingsUnsaved').classList.remove('visible');
      closeSettings();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('Save failed: ' + (data.detail || resp.statusText));
    }
  } catch (err) {
    alert('Save failed: ' + err.message);
  }
});

async function openSettings() {
  settingsOverlay.classList.add('visible');
  document.body.style.overflow = 'hidden';
  settingsDirty = false;
  $('settingsUnsaved').classList.remove('visible');

  // Load current settings from server
  try {
    const resp = await fetch(`${API}/api/settings`);
    const data = await resp.json();
    settingsData = data.settings;
    settingsDefaults = data.defaults;
    await populateSettingsForm(settingsData, settingsDefaults);
  } catch (err) {
    $('settingsBody').innerHTML = `<div style="color:var(--red);padding:20px">Failed to load settings: ${escHtml(err.message)}</div>`;
  }
}

async function populateSettingsForm(settings, defaults) {
  $('settingsStreamBackup').checked = streamBackupEnabled();
  await populateModelDropdown(settings.ollama_model);
  $('settingsTemp').value = settings.temperature || 0.3;
  $('tempDisplay').textContent = parseFloat(settings.temperature || 0.3).toFixed(2);
  $('settingsSttBackend').value = settings.stt_backend || 'parakeet';
  $('settingsDiarize').checked = settings.diarize !== false;
  // default-ON semantics: a missing key (pre-feature server) renders checked
  $('settingsRemoveFiller').checked = settings.remove_filler !== false;

  for (const [key, ta] of Object.entries(promptFields)) {
    ta.value = (settings.prompts && settings.prompts[key]) || '';
  }

  // Chat settings
  const chat = settings.chat || {};
  $('chatEndpoint').value = chat.endpoint || 'ollama';
  $('chatModel').value = chat.model || '';
  $('chatCustomUrl').value = chat.custom_url || '';
  $('chatCustomKey').value = chat.custom_api_key || '';
  $('chatTemp').value = chat.temperature || 0.5;
  $('chatTempDisplay').textContent = parseFloat(chat.temperature || 0.5).toFixed(2);
  $('chatMaxChunks').value = chat.max_context_chunks || 15;
  $('chatSystemPrompt').value = chat.system_prompt || '';
  toggleChatCustomFields();

  // SMTP / email settings
  const smtp = settings.smtp || {};
  $('smtpEnabled').checked = smtp.enabled === true;
  $('smtpHost').value = smtp.host || '';
  $('smtpPort').value = smtp.port || 587;
  $('smtpSecure').checked = smtp.secure === true;
  $('smtpUsername').value = smtp.username || '';
  $('smtpPassword').value = smtp.password || '';
  $('smtpFromEmail').value = smtp.from_email || '';
  $('smtpFromName').value = smtp.from_name || 'Meeting Service';
  $('smtpReplyTo').value = smtp.reply_to || '';
  $('smtpRecipients').value = smtp.recipients || '';
  $('smtpTestStatus').textContent = '';
  toggleSmtpFields();

  // Digest / ICS settings
  const digest = settings.digest || {};
  $('digestEnabled').checked = digest.enabled === true;
  $('digestTime').value = digest.time || '07:00';
  $('digestTimezone').value = digest.timezone || 'Europe/London';
  $('digestRecipients').value = digest.recipients || '';
  $('digestTestStatus').textContent = '';
  toggleDigestFields();

  const ics = settings.ics || {};
  $('icsEnabled').checked = ics.enabled === true;
  $('icsToken').value = ics.token || '';
  toggleIcsFields();
  updateIcsUrlHint();
}

function closeSettings() {
  if (settingsDirty) {
    if (!confirm('You have unsaved changes. Discard?')) return;
  }
  settingsOverlay.classList.remove('visible');
  document.body.style.overflow = '';
}

// --- Recovery of autosaved recordings from previous sessions ---
function renderRecoveryList(pendings, serverCaptures) {
  const el = $('captureRecovery');
  if (!el) return;
  el.textContent = '';   // DOM-built (no innerHTML) — content includes derived metadata

  // Server captures whose sid is already covered by a local session are the
  // same recording — prefer the local copy (has the actual bytes + Download).
  const localSids = new Set(pendings.map(p => p.meta.k));
  const serverOnly = (serverCaptures || []).filter(c => !localSids.has(c.sid));

  const total = pendings.length + serverOnly.length;
  if (!total) { el.style.display = 'none'; return; }

  const hideIfEmpty = () => {
    if (!el.querySelector('.cap-recovery-row')) el.style.display = 'none';
  };

  const heading = document.createElement('div');
  heading.className = 'cap-recovery-heading';
  heading.textContent = total === 1
    ? '⚠️ Unsaved recording recovered — restore or discard it:'
    : `⚠️ ${total} unsaved recordings recovered — restore or discard them:`;
  el.appendChild(heading);

  for (const { meta, blob } of pendings) {
    const sid = meta.k;
    const when = meta.startedAt ? new Date(meta.startedAt).toLocaleString() : 'a previous session';
    const dur = meta.durationLabel ? ` (${meta.durationLabel})` : '';
    const sizeKb = Math.round(blob.size / 1024);
    const mt = meta.mimeType || 'audio/webm';
    const ext = mt.includes('ogg') ? '.ogg' : mt.includes('mp4') ? '.m4a' : '.webm';
    const name = meta.fileName || `recovered_${new Date(meta.startedAt || Date.now()).toISOString().slice(0,10)}${ext}`;

    // Queued sessions auto-upload — render them distinctly from never-submitted
    // recovered drafts. The user explicitly hit Upload on these and it failed for
    // a network reason; the flush loop retries them automatically.
    if (meta.queued) {
      const row = document.createElement('div');
      row.className = 'cap-recovery-row';

      const msg = document.createElement('span');
      msg.className = 'cap-recovery-msg';
      msg.textContent = `📤 Queued — uploads automatically when connected — ${when}${dur} — ${sizeKb} KB`;

      const uploadNowBtn = document.createElement('button');
      uploadNowBtn.className = 'cap-recovery-btn';
      uploadNowBtn.textContent = 'Upload now';
      uploadNowBtn.addEventListener('click', () => { flushUploadQueue(); });

      const discardBtn = document.createElement('button');
      discardBtn.className = 'cap-recovery-btn cap-recovery-discard';
      discardBtn.textContent = 'Discard';
      discardBtn.addEventListener('click', () => {
        if (!confirm('Permanently discard this queued recording? It has not been uploaded.')) return;
        capClear(sid);
        capStreamDelete(sid);   // drop the server shadow copy too
        row.remove();
        hideIfEmpty();
      });

      row.appendChild(msg);
      row.appendChild(uploadNowBtn);
      row.appendChild(discardBtn);
      el.appendChild(row);
      continue;
    }

    const row = document.createElement('div');
    row.className = 'cap-recovery-row';

    const msg = document.createElement('span');
    msg.className = 'cap-recovery-msg';
    msg.textContent = `${when}${dur} — ${sizeKb} KB`;

    const recoverBtn = document.createElement('button');
    recoverBtn.className = 'cap-recovery-btn';
    recoverBtn.textContent = 'Recover & upload';
    recoverBtn.addEventListener('click', () => {
      selectFile(new File([blob], name, { type: mt }));
      stagedFromRecording = true;   // keep it protected until uploaded
      stagedSessionId = sid;
      liveTags.roster = Array.isArray(meta.roster) ? meta.roster : [];
      liveTags.markers = Array.isArray(meta.markers) ? meta.markers : [];
      captureNotes.restoreForSession(meta);   // async, best-effort
      row.remove();
      hideIfEmpty();
    });

    // Belt-and-braces: save the audio straight to disk, no server involved.
    const dlBtn = document.createElement('button');
    dlBtn.className = 'cap-recovery-btn';
    dlBtn.textContent = 'Download';
    dlBtn.addEventListener('click', () => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    });

    const discardBtn = document.createElement('button');
    discardBtn.className = 'cap-recovery-btn cap-recovery-discard';
    discardBtn.textContent = 'Discard';
    discardBtn.addEventListener('click', () => {
      if (!confirm('Permanently discard this recovered recording?')) return;
      capClear(sid);
      row.remove();
      hideIfEmpty();
    });

    row.appendChild(msg);
    row.appendChild(recoverBtn);
    row.appendChild(dlBtn);
    row.appendChild(discardBtn);
    el.appendChild(row);
  }

  // Server-only captures: streamed here (possibly from another device that
  // never uploaded) but with no local blob. Recover by adopting on the server.
  for (const c of serverOnly) {
    const when = c.startedAt ? new Date(c.startedAt).toLocaleString() : 'a previous session';
    const dur = c.durationLabel ? ` (${c.durationLabel})` : '';
    const sizeKb = Math.round((c.bytes || 0) / 1024);

    const row = document.createElement('div');
    row.className = 'cap-recovery-row';

    const msg = document.createElement('span');
    msg.className = 'cap-recovery-msg';
    msg.textContent = `☁️ server copy — ${when}${dur} — ${sizeKb} KB`;

    const recoverBtn = document.createElement('button');
    recoverBtn.className = 'cap-recovery-btn';
    recoverBtn.textContent = 'Recover on server';
    recoverBtn.addEventListener('click', async () => {
      recoverBtn.disabled = true;
      try {
        const resp = await fetch(`${API}/captures/${c.sid}/adopt`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 202) {
          row.remove();
          hideIfEmpty();
          startPolling();
          refreshMeetings();
          alert(`Recovered as a new meeting: "${data.title}"`);
        } else {
          recoverBtn.disabled = false;
          alert('Recover failed: ' + (data.detail || resp.statusText));
        }
      } catch (err) {
        recoverBtn.disabled = false;
        alert('Recover failed: ' + err.message);
      }
    });

    const discardBtn = document.createElement('button');
    discardBtn.className = 'cap-recovery-btn cap-recovery-discard';
    discardBtn.textContent = 'Discard';
    discardBtn.addEventListener('click', () => {
      if (!confirm('Permanently discard this server-side recording?')) return;
      capStreamDelete(c.sid);
      row.remove();
      hideIfEmpty();
    });

    row.appendChild(msg);
    row.appendChild(recoverBtn);
    row.appendChild(discardBtn);
    el.appendChild(row);
  }

  el.style.display = 'flex';
}

async function checkPendingRecording() {
  try {
    // Ask the browser to shield this origin's storage from automatic eviction.
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
    await capImportLegacyBackup();
    const local = await capLoadAllPending();
    let server = [];
    try { server = await fetch(`${API}/captures`).then(r => r.ok ? r.json() : []); } catch (_) {}
    renderRecoveryList(local, server);
  } catch (e) { /* recovery is best-effort */ }
  // Auto-flush any session the user already submitted that failed for a network
  // reason. Fire-and-forget; the `flushing` guard makes re-entry a no-op (this
  // is also called from inside the flush loop to refresh the list live).
  flushUploadQueue();
}
checkPendingRecording();
// Retry the offline upload queue when connectivity returns and on a slow timer
// (covers browser-online-but-server-unreachable: box rebooting, VPN/Authelia).
window.addEventListener('online', flushUploadQueue);
setInterval(() => { if (navigator.onLine) flushUploadQueue(); }, 60000);

// ---------------------------------------------------------------------------
// Unsaved-recording guards: warn before leaving / navigating away while a
// recording is in progress or staged-but-not-uploaded. The recording is also
// autosaved (IndexedDB), so it's recoverable even if they proceed.
// ---------------------------------------------------------------------------
function isCapturing() {
  return !!(mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused'));
}

// Returns true if it's OK to navigate away (no risk, or user confirmed).
// Exposed for the pillar nav (notes-tasks.js) and any other in-app navigation.
window.captureGuardConfirm = function () {
  if (isCapturing()) {
    return confirm('A recording is still in progress. It keeps running in the background and is auto-saved — switch anyway?');
  }
  if (stagedFromRecording) {
    return confirm('You have a recording that hasn’t been uploaded yet. It’s saved and can be recovered later — leave it for now?');
  }
  return true;
};
window.isCapturing = isCapturing;

window.addEventListener('beforeunload', (e) => {
  if (isCapturing() || stagedFromRecording) {
    e.preventDefault();
    e.returnValue = '';   // triggers the browser's native "leave site?" prompt
    return '';
  }
});

// --- Init ---
refreshGroupedView();
startPolling();
setTimeout(() => {
  fetch(`${API}/meetings`).then(r => r.json()).then(data => {
    allMeetingsCache = data;
    const inProgress = data.some(m => !['complete', 'error'].includes(m.status));
    if (!inProgress) stopPolling();
  }).catch(() => {});
}, 6000);
wireCollapse($('liveSpeakersToggle'), $('liveSpeakersBody'));
wireCollapse($('uploadFieldsToggle'), $('uploadFieldsBody'));
