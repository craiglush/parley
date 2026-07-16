// static/notes-sync.js — offline mirror + sync engine for Notes & Tasks.
// DOM / IndexedDB-bound. Reuses the withFlushLock pattern from app.js with a
// DISTINCT lock name ('notes-sync-flush') and the pure decisions in
// notes-sync-logic.js. The dedicated IndexedDB DB ('notes-mirror' v1) is the
// offline source of truth for note bodies; the note LIST and every BODY are
// pulled via GET /api/notes/export. Loaded as a plain <script> AFTER
// notes-sync-logic.js and BEFORE notes-tasks.js's boot() (see index.html order).
(function () {
  'use strict';
  const L = (typeof window !== 'undefined' && window.NotesSyncLogic) || {};
  const API = '';

  // ---- IndexedDB (separate DB so its version lifecycle is decoupled from CAP_DB) ----
  const DB_NAME = 'notes-mirror';
  const DB_VERSION = 1;
  const STORE = 'notes';
  let dbPromise = null;

  function openDB() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      let req;
      try { req = indexedDB.open(DB_NAME, DB_VERSION); }
      catch (e) { reject(e); return; }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'id' });
      };
      req.onsuccess = () => {
        const db = req.result;
        db.onversionchange = () => { try { db.close(); } catch (_) {} dbPromise = null; };
        resolve(db);
      };
      req.onerror = () => reject(req.error);
    });
    return dbPromise;
  }

  function reqP(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }
  function store(mode) { return openDB().then((db) => db.transaction([STORE], mode).objectStore(STORE)); }
  function mGet(id) { return store('readonly').then((s) => reqP(s.get(id))); }
  function mAll() { return store('readonly').then((s) => reqP(s.getAll())).then((r) => r || []); }
  function mPut(entry) { return store('readwrite').then((s) => reqP(s.put(entry))); }
  function mDel(id) { return store('readwrite').then((s) => reqP(s.delete(id))); }
  function mRemap(oldId, newEntry) {
    return openDB().then((db) => new Promise((resolve, reject) => {
      const tx = db.transaction([STORE], 'readwrite');
      const st = tx.objectStore(STORE);
      st.delete(oldId);
      st.put(newEntry);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error('tx aborted'));
    }));
  }

  // ---- helpers ----
  const nowMs = () => Date.now();
  const tempId = () => 'n_local_' + Math.random().toString(36).slice(2, 12);
  const isoNow = () => new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  function netError() { const e = new Error('network'); e._net = true; return e; }

  // Recompute dirty/pendingOp from record vs baseHash. 'create'/'delete' are sticky.
  function reseal(entry) {
    if (entry.pendingOp === 'create' || entry.pendingOp === 'delete') return entry;
    const h = L.contentHash(entry.record.title || '', entry.record.body || '');
    entry.dirty = (h !== entry.baseHash);
    entry.pendingOp = entry.dirty ? 'edit' : null;
    return entry;
  }

  let hooks = {};

  // ---- local writes ----
  function createNote(payload) {
    payload = payload || {};
    const id = tempId();
    const ts = isoNow();
    const record = {
      id, title: payload.title || 'Untitled', body: payload.body || '',
      tags: [], type: payload.type || 'note', folder: payload.folder || '',
      linked_meetings: [], created: ts, updated: ts, path: '',
      content_hash: L.contentHash(payload.title || 'Untitled', payload.body || ''),
    };
    const entry = { id, record, baseHash: null, dirty: true, pendingOp: 'create', localUpdated: nowMs() };
    return mPut(entry).then(() => { scheduleFlush(); return record; });
  }

  function updateNote(id, patch) {
    patch = patch || {};
    return mGet(id).then((entry) => {
      if (!entry) throw new Error('note not in mirror: ' + id);
      if (patch.title != null) entry.record.title = patch.title;
      if (patch.body != null) entry.record.body = patch.body;
      if (patch.tags != null) entry.record.tags = patch.tags.slice();  // local-only; never pushed
      entry.record.updated = isoNow();
      entry.record.content_hash = L.contentHash(entry.record.title || '', entry.record.body || '');
      entry.localUpdated = nowMs();
      reseal(entry);
      return mPut(entry).then(() => { if (entry.dirty) scheduleFlush(); return entry.record; });
    });
  }

  function applyTask(noteId, op) {
    return mGet(noteId).then((entry) => {
      if (!entry) throw new Error('note not in mirror: ' + noteId);
      const r = L.applyTaskEditToBody(entry.record.body || '', op);
      if (!r.ok) { const err = new Error('Task line changed or not a checkbox; refresh'); err.status = 409; throw err; }
      return updateNote(noteId, { body: r.body });
    });
  }

  function deleteNote(id) {
    return mGet(id).then((entry) => {
      if (!entry) return undefined;
      if (entry.pendingOp === 'create') return mDel(id);   // never synced -> just forget it
      entry.pendingOp = 'delete'; entry.dirty = true; entry.localUpdated = nowMs();
      return mPut(entry).then(() => { scheduleFlush(); });
    });
  }

  const TASKS_INBOX_TITLE = 'tasks';
  function findOrCreateInbox() {
    return mAll().then((all) => {
      const hit = all.find((e) => e.pendingOp !== 'delete' && !e.record.folder
        && (e.record.title || '').trim().toLowerCase() === TASKS_INBOX_TITLE);
      if (hit) return hit.record.id;
      return createNote({ title: 'Tasks', folder: '', type: 'note', body: '' }).then((rec) => rec.id);
    });
  }
  function addTask(payload) {
    payload = payload || {};
    const doAdd = (noteId) => applyTask(noteId, {
      kind: 'add', text: payload.text, owner: payload.owner, due: payload.due, priority: payload.priority,
    }).then(() => noteId);
    return (payload.note_id ? Promise.resolve(payload.note_id) : findOrCreateInbox()).then(doAdd);
  }

  // ---- reads (from the mirror) ----
  function listNotes() {
    return mAll().then((all) => all
      .filter((e) => e.pendingOp !== 'delete')
      .map((e) => {
        const r = e.record;
        return {
          id: r.id, title: r.title, type: r.type, folder: r.folder, path: r.path || '',
          tags: r.tags || [], linked_meetings: r.linked_meetings || [],
          created: r.created || '', updated: r.updated || '',
        };
      }));
  }
  function readNote(id) {
    return mGet(id).then((e) => (e && e.pendingOp !== 'delete') ? e.record : null);
  }

  // ---- pull ----
  function pull() {
    return fetch(API + '/api/notes/export', { headers: { Accept: 'application/json' } })
      .then((resp) => { if (!resp.ok) throw new Error('export ' + resp.status); return resp.json(); })
      .then((data) => {
        const server = (data && data.notes) || [];
        return withNotesFlushLock(() => {
          return mAll().then((all) => {
            const localById = {};
            all.forEach((e) => { localById[e.id] = e; });
            const merged = L.mergeServerList(localById, server);
            const keep = {};
            const puts = Object.keys(merged).map((id) => { keep[id] = 1; return mPut(merged[id]); });
            const dels = all.filter((e) => !keep[e.id]).map((e) => mDel(e.id));
            return Promise.all(puts.concat(dels));
          });
        }).then((result) => {
          // Skip refresh if lock was unavailable; next interval will retry
          if (result !== undefined) return refreshUI();
        });
      });
  }

  // ---- flush (under a distinct Web Lock, mirroring app.js withFlushLock) ----
  let _flushing = false;
  function withNotesFlushLock(fn) {
    if (navigator.locks && navigator.locks.request) {
      return navigator.locks.request('notes-sync-flush', { ifAvailable: true }, (lock) => {
        if (!lock) return undefined;
        return fn();
      });
    }
    if (_flushing) return Promise.resolve();
    _flushing = true;
    return Promise.resolve().then(fn).finally(() => { _flushing = false; });
  }

  let _flushTimer = null;
  function scheduleFlush() { if (_flushTimer) return; _flushTimer = setTimeout(() => { _flushTimer = null; flush(); }, 150); }

  function jsonFetch(path, method, bodyObj) {
    return fetch(API + path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: bodyObj == null ? undefined : JSON.stringify(bodyObj),
    });
  }

  function makeConflictCopy(entry, serverRec) {
    const isoToday = isoNow();
    const copyTitle = L.conflictCopyTitle(entry.record.title || '', isoToday);
    const localBody = entry.record.body || '';
    let keepServer = Promise.resolve();
    if (serverRec && serverRec.id) {
      serverRec.content_hash = serverRec.content_hash || L.contentHash(serverRec.title || '', serverRec.body || '');
      keepServer = mPut({ id: serverRec.id, record: serverRec, baseHash: serverRec.content_hash, dirty: false, pendingOp: null, localUpdated: 0 });
    }
    const cid = tempId();
    const crec = {
      id: cid, title: copyTitle, body: localBody, tags: [], type: entry.record.type || 'note',
      folder: entry.record.folder || '', linked_meetings: [], created: isoToday, updated: isoToday, path: '',
      content_hash: L.contentHash(copyTitle, localBody),
    };
    return keepServer
      .then(() => mPut({ id: cid, record: crec, baseHash: null, dirty: true, pendingOp: 'create', localUpdated: nowMs() }))
      .then(() => { if (hooks.toast) hooks.toast('Note changed elsewhere — saved your copy as “' + copyTitle + '”', 'error'); });
  }

  function flush() {
    return withNotesFlushLock(() => mAll().then((all) => {
      const dirty = L.selectDirtyNotes(all);
      let i = 0;
      function step() {
        if (i >= dirty.length) return refreshUI();
        const entry = dirty[i++];
        const op = entry.pendingOp;
        let p;
        if (op === 'create') {
          p = jsonFetch('/api/notes', 'POST', {
            title: entry.record.title, folder: entry.record.folder || '',
            type: entry.record.type || 'note', body: entry.record.body || '',
          }).then((resp) => resp.json().then((rec) => {
            const d = L.resolvePush(entry, { status: resp.status, record: rec });
            if (d.action !== 'remap' || !d.serverId) throw netError();
            rec.content_hash = rec.content_hash || L.contentHash(rec.title || '', rec.body || '');
            return mRemap(entry.id, { id: rec.id, record: rec, baseHash: rec.content_hash, dirty: false, pendingOp: null, localUpdated: 0 })
              .then(() => { if (hooks.onRemap) hooks.onRemap(entry.id, rec.id); });
          }));
        } else if (op === 'edit') {
          p = jsonFetch('/api/notes/' + encodeURIComponent(entry.id), 'PUT', {
            title: entry.record.title, body: entry.record.body, expected_body_hash: entry.baseHash,
          }).then((resp) => resp.json().then((rec) => {
            const d = L.resolvePush(entry, { status: resp.status, record: rec });
            if (d.action === 'conflict') return makeConflictCopy(entry, d.server);
            if (d.action !== 'applied') throw netError();
            rec.content_hash = rec.content_hash || L.contentHash(rec.title || '', rec.body || '');
            return mPut({ id: rec.id, record: rec, baseHash: rec.content_hash, dirty: false, pendingOp: null, localUpdated: 0 });
          }));
        } else {   // delete
          p = jsonFetch('/api/notes/' + encodeURIComponent(entry.id), 'DELETE', null)
            .then((resp) => { if (!resp.ok && resp.status !== 404) throw netError(); return mDel(entry.id); });
        }
        return p.then(step);   // any network throw rejects the chain -> caught below (stop the pass)
      }
      return Promise.resolve().then(step).catch(() => { /* network error: stop, retry next trigger */ });
    }));
  }

  // ---- UI refresh + init ----
  function refreshUI() {
    if (!hooks.onNotes) return Promise.resolve();
    return listNotes().then((list) => { try { hooks.onNotes(list); } catch (_) {} });
  }

  function init(h) {
    hooks = h || {};
    pull().then(flush).catch(() => {});
    window.addEventListener('online', () => { pull().then(flush).catch(() => {}); });
    setInterval(() => { if (navigator.onLine) pull().then(flush).catch(() => {}); }, 60000);
  }

  window.NotesSync = {
    init, pull, flush,
    listNotes, readNote,
    createNote, updateNote, deleteNote, applyTask, addTask,
  };
})();
