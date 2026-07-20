/* ============================================================================
   Notes & Tasks UI — Phase 4. Loaded (deferred) AFTER the inline app script,
   so app globals (escHtml, closeDetail, isMobile) already exist.

   SECURITY (XSS): every interpolation into an `.innerHTML =` below is wrapped in
   esc() (the app's HTML-escaper). Static template text is author-controlled. The
   one place raw markdown becomes HTML — the note preview — is rendered by
   MoonbaseEditor.renderMarkdown(), which runs the output through DOMPurify in the
   vendored bundle. No eval / child_process anywhere.
   ========================================================================== */
(function () {
  'use strict';

  // ---- shared helpers ------------------------------------------------------
  const API = '';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => (typeof window.escHtml === 'function')
    ? window.escHtml(s == null ? '' : String(s))
    : String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  async function api(path, opts) {
    const resp = await fetch(API + path, opts);
    if (!resp.ok) {
      let detail = 'HTTP ' + resp.status;
      try { const j = await resp.json(); if (j && j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch (e) {}
      const err = new Error(detail); err.status = resp.status; throw err;
    }
    if (resp.status === 204) return null;
    const ct = resp.headers.get('content-type') || '';
    return ct.indexOf('application/json') >= 0 ? resp.json() : resp.text();
  }
  function debounce(fn, ms) {
    let t;
    const d = function (...a) { clearTimeout(t); t = setTimeout(() => fn.apply(null, a), ms); };
    d.cancel = () => clearTimeout(t);
    d.flush = function (...a) { clearTimeout(t); fn.apply(null, a); };
    return d;
  }
  function todayStr() { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return '';
    const now = new Date();
    const sameYear = d.getFullYear() === now.getFullYear();
    return d.toLocaleDateString(undefined, sameYear ? { month: 'short', day: 'numeric' } : { year: 'numeric', month: 'short', day: 'numeric' });
  }
  function relTime(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return '';
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + 'd ago';
    return fmtDate(iso);
  }

  // ---- toast ---------------------------------------------------------------
  let toastWrap;
  function toast(msg, kind) {
    if (!toastWrap) { toastWrap = document.createElement('div'); toastWrap.className = 'nt-toast-wrap'; document.body.appendChild(toastWrap); }
    const el = document.createElement('div');
    el.className = 'nt-toast' + (kind ? ' ' + kind : '');
    el.textContent = msg;                       // textContent: safe, no escaping needed
    toastWrap.appendChild(el);
    setTimeout(() => { el.style.transition = 'opacity .25s ease'; el.style.opacity = '0'; setTimeout(() => el.remove(), 260); }, 2600);
  }

  // ---- generic modal -------------------------------------------------------
  function modal(html) {
    const ov = document.createElement('div'); ov.className = 'nt-modal-overlay';
    ov.innerHTML = '<div class="nt-modal" role="dialog" aria-modal="true">' + html + '</div>';
    document.body.appendChild(ov);
    requestAnimationFrame(() => ov.classList.add('visible'));
    const close = () => { ov.classList.remove('visible'); setTimeout(() => ov.remove(), 180); };
    ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
    document.addEventListener('keydown', function onEsc(e) { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onEsc); } });
    return { el: ov, close, q: (sel) => ov.querySelector(sel) };
  }

  /* ==========================================================================
     PILLAR NAV
     ======================================================================== */
  let currentPillar = 'meetings';
  let notesInit = false;

  function setActivePillar(p) {
    currentPillar = p;
    document.body.dataset.pillar = p;
    document.querySelectorAll('#pillarNav .pillar-btn').forEach((b) => {
      const on = b.dataset.pillar === p;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (p !== 'meetings' && typeof window.closeDetail === 'function') { try { window.closeDetail(); } catch (e) {} }
    if (p === 'tasks') loadTasks();
    if (p === 'notes') {
      if (!notesInit) { notesInit = true; loadNotesTree(); }
      else loadNotesTree();
      if (!currentNoteId) {
        if (isMobileWidth()) notesViewEl().classList.add('tree-open');
        notesViewEl().classList.remove('tree-collapsed');
      }
    }
  }
  function isMobileWidth() { return (typeof window.isMobile === 'function') ? window.isMobile() : window.innerWidth < 768; }
  function notesViewEl() { return $('notesView'); }

  function initPillarNav() {
    const nav = $('pillarNav');
    if (!nav) return;
    nav.addEventListener('click', (e) => {
      const btn = e.target.closest('.pillar-btn'); if (!btn) return;
      const target = btn.dataset.pillar;
      // Guard leaving the Meetings pillar while a recording is in progress or
      // staged-but-unsaved (the capture UI lives on the Meetings pillar).
      if (target !== 'meetings' && currentPillar === 'meetings'
          && typeof window.captureGuardConfirm === 'function'
          && !window.captureGuardConfirm()) {
        return;
      }
      setActivePillar(target);
    });
    document.body.dataset.pillar = 'meetings';
  }

  /* ==========================================================================
     TASKS DASHBOARD
     ======================================================================== */
  let allTasks = [];
  const taskFilters = { status: 'open', due: '', source: '', owner: '' };
  let taskGroupBy = 'due';

  async function loadTasks() {
    const list = $('tasksList');
    if (!list) return;
    list.innerHTML = '<div class="tasks-empty">Loading…</div>';
    try {
      const data = await api('/api/tasks');          // fetch ALL; filter/group client-side
      allTasks = (data && data.tasks) || [];
      populateOwnerFilter();
      renderTasks();
      updateTaskBadge();
      if (tasksView === 'board') renderBoard();
    } catch (e) {
      list.innerHTML = '<div class="tasks-empty">Failed to load tasks: ' + esc(e.message) + '</div>';
    }
  }

  function dueBucket(t) {
    if (!t.due) return 'none';
    const today = todayStr();
    if (t.due < today) return 'overdue';
    if (t.due === today) return 'today';
    const d = new Date(t.due + 'T00:00:00'); const now = new Date(todayStr() + 'T00:00:00');
    const days = (d - now) / 86400000;
    if (days <= 7) return 'week';
    return 'later';
  }
  function passesFilters(t) {
    if (taskFilters.status === 'open' && t.done) return false;
    if (taskFilters.status === 'done' && !t.done) return false;
    if (taskFilters.source && t.source !== taskFilters.source) return false;
    if (taskFilters.owner && (t.owner || '') !== taskFilters.owner) return false;
    if (taskFilters.due) {
      const b = dueBucket(t);
      if (taskFilters.due === 'overdue' && b !== 'overdue') return false;
      if (taskFilters.due === 'today' && b !== 'today') return false;
      if (taskFilters.due === 'week' && !(b === 'today' || b === 'week' || b === 'overdue')) return false;
    }
    return true;
  }
  const PRIO_RANK = { high: 0, medium: 1, low: 2 };
  function sortTasks(a, b) {
    if (a.done !== b.done) return a.done ? 1 : -1;
    const ad = a.due || '9999', bd = b.due || '9999';
    if (ad !== bd) return ad < bd ? -1 : 1;
    const ap = PRIO_RANK[a.priority] ?? 3, bp = PRIO_RANK[b.priority] ?? 3;
    if (ap !== bp) return ap - bp;
    return (a.text || '').localeCompare(b.text || '');
  }

  function groupTasks(tasks) {
    const groups = new Map();
    const add = (key, label, t) => { if (!groups.has(key)) groups.set(key, { label, items: [] }); groups.get(key).items.push(t); };
    for (const t of tasks) {
      if (taskGroupBy === 'none') { add('all', 'All tasks', t); continue; }
      if (taskGroupBy === 'source') { add(t.source, t.source === 'note' ? 'Notes' : 'Meetings', t); continue; }
      if (taskGroupBy === 'owner') { const o = t.owner || '~'; add(o, t.owner || 'Unassigned', t); continue; }
      if (taskGroupBy === 'priority') { const p = t.priority || 'none'; add(p, ({ high: 'High priority', medium: 'Medium priority', low: 'Low priority', none: 'No priority' })[p], t); continue; }
      if (t.done) { add('zdone', 'Completed', t); continue; }
      const b = dueBucket(t);
      add(b, ({ overdue: 'Overdue', today: 'Today', week: 'This week', later: 'Later', none: 'No due date' })[b], t);
    }
    const order = taskGroupBy === 'due'
      ? ['overdue', 'today', 'week', 'later', 'none', 'zdone']
      : (taskGroupBy === 'priority' ? ['high', 'medium', 'low', 'none'] : null);
    const entries = [...groups.entries()];
    if (order) entries.sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
    else entries.sort((a, b) => a[1].label.localeCompare(b[1].label));
    return entries;
  }

  function taskRowHtml(t, idx) {
    const isNote = t.source === 'note';
    const due = t.due ? '<span class="task-chip due ' + dueBucket(t) + '">📅 ' + esc(t.due) + '</span>' : '';
    const prio = t.priority ? '<span class="task-chip prio-' + esc(t.priority) + '">' + esc(t.priority) + '</span>' : '';
    const owner = t.owner ? '<span class="task-chip owner">@' + esc(t.owner) + '</span>' : '';
    const srcIcon = isNote ? '📝' : '🎙';
    const src = '<span class="task-chip source" data-open-source="' + idx + '" title="Open ' + esc(t.source) + '">' + srcIcon + ' ' + esc(t.source_title || t.source) + '</span>';
    const editDel = '<button class="task-mini-btn" data-edit="' + idx + '" title="Edit task">✎</button>'
      + '<button class="task-mini-btn danger" data-del="' + idx + '" title="' + (isNote ? 'Delete task' : 'Dismiss task') + '">🗑</button>';
    const actions = isNote
      ? '<div class="task-actions">' + editDel + '</div>'
      : '<div class="task-actions">' + editDel + '<button class="task-push-btn" data-push="' + idx + '" title="Copy into a note">→ Note</button></div>';
    return '<div class="task-row' + (t.done ? ' done' : '') + '" data-idx="' + idx + '">'
      + '<input type="checkbox" class="task-check"' + (t.done ? ' checked' : '') + ' data-toggle="' + idx + '">'
      + '<div class="task-main"><div class="task-text">' + esc(t.text) + '</div>'
      + '<div class="task-meta">' + due + prio + owner + src + '</div></div>'
      + actions + '</div>';
  }

  function renderTasks() {
    const list = $('tasksList'); if (!list) return;
    const filtered = allTasks.map((t, i) => ({ t, i })).filter((o) => passesFilters(o.t));
    const counts = { open: 0, overdue: 0, today: 0, done: 0 };
    for (const t of allTasks) {
      if (t.done) counts.done++; else { counts.open++; const b = dueBucket(t); if (b === 'overdue') counts.overdue++; if (b === 'today') counts.today++; }
    }
    const cEl = $('tasksCounts');
    if (cEl) cEl.innerHTML = '<span><b>' + counts.open + '</b> open</span>'
      + (counts.overdue ? '<span style="color:var(--red)"><b style="color:var(--red)">' + counts.overdue + '</b> overdue</span>' : '')
      + (counts.today ? '<span><b>' + counts.today + '</b> today</span>' : '')
      + '<span><b>' + counts.done + '</b> done</span>';

    if (!filtered.length) { list.innerHTML = '<div class="tasks-empty">No tasks match these filters.</div>'; return; }
    filtered.sort((a, b) => sortTasks(a.t, b.t));
    const groups = groupTasks(filtered.map((o) => Object.assign({ __idx: o.i }, o.t)));
    let html = '';
    for (const [, g] of groups) {
      html += '<div class="task-group-head"><span class="task-group-title">' + esc(g.label) + '</span><span class="task-group-line"></span><span class="task-group-count">' + g.items.length + '</span></div>';
      for (const t of g.items) html += taskRowHtml(t, t.__idx);
    }
    list.innerHTML = html;
  }

  function populateOwnerFilter() {
    const sel = $('taskOwnerFilter'); if (!sel) return;
    const owners = [...new Set(allTasks.map((t) => t.owner).filter(Boolean))].sort();
    const cur = taskFilters.owner;
    sel.innerHTML = '<option value="">Everyone</option>' + owners.map((o) => '<option value="' + esc(o) + '"' + (o === cur ? ' selected' : '') + '>@' + esc(o) + '</option>').join('');
  }

  function updateTaskBadge() {
    const badge = $('tasksOpenBadge'); if (!badge) return;
    const today = todayStr();
    let open = 0, overdue = 0;
    for (const t of allTasks) {
      if (t.done) continue;
      open++;
      if (t.due && t.due < today) overdue++;
    }
    if (open > 0) { badge.textContent = open > 99 ? '99+' : String(open); badge.hidden = false; }
    else badge.hidden = true;
    badge.classList.toggle('overdue', overdue > 0);
    badge.title = overdue > 0 ? overdue + ' overdue' : '';
  }

  async function toggleTaskByIndex(idx, checkboxEl) {
    const t = allTasks[idx]; if (!t) return;
    const target = !t.done;
    try {
      if (t.source === 'note') {
        await window.NotesSync.applyTask(t.source_id, { kind: 'toggle', line: t.line, done: target, expectedText: t.text });
      } else {
        await api('/api/meetings/' + encodeURIComponent(t.source_id) + '/tasks/toggle', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ index: t.index, done: target }) });
      }
      t.done = target;
      renderTasks(); updateTaskBadge();
      if (t.source === 'note' && currentNoteId === t.source_id && noteEditor) reloadCurrentNoteBody();
    } catch (e) {
      if (checkboxEl) checkboxEl.checked = t.done;
      if (e.status === 409) { toast('That task moved — refreshing', 'error'); loadTasks(); }
      else toast('Toggle failed: ' + e.message, 'error');
    }
  }

  async function pushMeetingToNote(idx) {
    const t = allTasks[idx]; if (!t || t.source !== 'meeting') return;
    if (!allNotes.length) { try { allNotes = (await api('/api/notes')).notes || []; } catch (e) {} }
    const items = allNotes.map((n) => '<div class="nt-modal-list-item" data-note="' + esc(n.id) + '">' + esc(n.title) + '<div class="sub">' + esc(n.folder || 'root') + '</div></div>').join('');
    const m = modal('<h3>Push “' + esc(t.source_title) + '” action items to…</h3>'
      + '<div class="nt-modal-list">'
      + '<div class="nt-modal-list-item" data-note="__new__" style="color:var(--accent)">+ New note from this meeting</div>'
      + items + '</div>'
      + '<div class="nt-modal-actions"><button class="nt-modal-btn" data-cancel>Cancel</button></div>');
    m.q('[data-cancel]').onclick = m.close;
    m.el.querySelectorAll('[data-note]').forEach((el) => {
      el.onclick = async () => {
        try {
          let noteId = el.dataset.note;
          if (noteId === '__new__') {
            const created = await api('/api/notes', { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ title: t.source_title || 'Meeting action items', folder: '', type: 'note', body: '' }) });
            noteId = created.id;
            await api('/api/notes/' + noteId + '/link-meeting', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ meeting_id: t.source_id, add: true }) });
          }
          await api('/api/notes/' + noteId + '/push-action-items', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ meeting_id: t.source_id }) });
          m.close(); toast('Action items pushed to note', 'success');
          allNotes = []; await loadTasks();
        } catch (e) { toast('Push failed: ' + e.message, 'error'); }
      };
    });
  }

  function openTaskSource(idx) {
    const t = allTasks[idx]; if (!t) return;
    if (t.source === 'note') { setActivePillar('notes'); openNote(t.source_id); }
    else { setActivePillar('meetings'); if (typeof window.openMeeting === 'function') { try { window.openMeeting(t.source_id); } catch (e) {} } }
  }

  // ---- task create / edit / delete ----------------------------------------
  function taskFormModal(opts) {
    const t = opts.task || {};
    const prios = [['', 'None'], ['low', 'Low'], ['medium', 'Medium'], ['high', 'High']];
    const m = modal(
      '<h3>' + esc(opts.heading) + '</h3>'
      + '<label>Task<input type="text" id="tfText" placeholder="What needs doing?" value="' + esc(t.text || '') + '"></label>'
      + '<label>Owner<input type="text" id="tfOwner" placeholder="name (optional)" value="' + esc(t.owner || '') + '"></label>'
      + '<label>Due<input type="date" id="tfDue" value="' + esc(t.due || '') + '"></label>'
      + '<label>Priority<select id="tfPrio">'
      + prios.map((p) => '<option value="' + p[0] + '"' + (p[0] === (t.priority || '') ? ' selected' : '') + '>' + p[1] + '</option>').join('')
      + '</select></label>'
      + '<div class="nt-modal-actions"><button class="nt-modal-btn" data-cancel>Cancel</button>'
      + '<button class="nt-modal-btn primary" data-save>' + esc(opts.saveLabel || 'Save') + '</button></div>'
    );
    m.q('[data-cancel]').onclick = m.close;
    const textEl = m.q('#tfText');
    const submit = () => {
      const val = { text: textEl.value.trim(), owner: m.q('#tfOwner').value.trim(), due: m.q('#tfDue').value, priority: m.q('#tfPrio').value };
      if (!val.text) { textEl.focus(); return; }
      opts.onSave(val, m);
    };
    m.q('[data-save]').onclick = submit;
    textEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
    setTimeout(() => textEl.focus(), 30);
  }

  function newTask() {
    taskFormModal({ heading: 'New task', saveLabel: 'Add', task: null, onSave: async (val, m) => {
      try {
        await window.NotesSync.addTask(val);   // val = {text, owner, due, priority}; no note_id -> Tasks inbox
        m.close(); toast('Task added', 'success');
        allNotes = [];
        if (navigator.onLine && window.NotesSync) { try { await window.NotesSync.flush(); } catch (e) {} }
        await loadTasks();
        if (currentNoteId && noteEditor) reloadCurrentNoteBody();
      } catch (e) { toast('Add failed: ' + e.message, 'error'); }
    } });
  }

  function editTask(idx) {
    const t = allTasks[idx]; if (!t) return;
    taskFormModal({ heading: 'Edit task', saveLabel: 'Save', task: { text: t.text, owner: t.owner, due: t.due, priority: t.priority },
      onSave: async (val, m) => {
        try {
          if (t.source === 'note') {
            await window.NotesSync.applyTask(t.source_id, { kind: 'edit', line: t.line, expectedText: t.text, text: val.text, owner: val.owner, due: val.due, priority: val.priority });
          } else {
            await api('/api/meetings/' + encodeURIComponent(t.source_id) + '/tasks', { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ index: t.index, text: val.text, owner: val.owner, due: val.due, priority: val.priority }) });
          }
          m.close(); toast('Task updated', 'success');
          if (navigator.onLine && window.NotesSync) { try { await window.NotesSync.flush(); } catch (e) {} }
          await loadTasks();
          if (t.source === 'note' && currentNoteId === t.source_id && noteEditor) reloadCurrentNoteBody();
        } catch (e) {
          if (e.status === 409) { m.close(); toast('That task moved — refreshing', 'error'); loadTasks(); }
          else toast('Update failed: ' + e.message, 'error');
        }
      } });
  }

  function deleteTask(idx) {
    const t = allTasks[idx]; if (!t) return;
    const isNote = t.source === 'note';
    const m = modal('<h3>' + (isNote ? 'Delete task?' : 'Dismiss task?') + '</h3><p class="nt-modal-text">' + esc(t.text) + '</p>'
      + (isNote ? '' : '<p class="nt-modal-text" style="opacity:.7">This hides the action item from the meeting; it stays in the meeting summary.</p>')
      + '<div class="nt-modal-actions"><button class="nt-modal-btn" data-cancel>Cancel</button>'
      + '<button class="nt-modal-btn danger" data-yes>' + (isNote ? 'Delete' : 'Dismiss') + '</button></div>');
    m.q('[data-cancel]').onclick = m.close;
    m.q('[data-yes]').onclick = async () => {
      try {
        if (isNote) {
          await window.NotesSync.applyTask(t.source_id, { kind: 'delete', line: t.line, expectedText: t.text });
        } else {
          await api('/api/meetings/' + encodeURIComponent(t.source_id) + '/tasks', { method: 'DELETE', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: t.index }) });
        }
        m.close(); toast(isNote ? 'Task deleted' : 'Task dismissed', 'success');
        if (navigator.onLine && window.NotesSync) { try { await window.NotesSync.flush(); } catch (e) {} }
        await loadTasks();
        if (isNote && currentNoteId === t.source_id && noteEditor) reloadCurrentNoteBody();
      } catch (e) {
        m.close();
        if (e.status === 409) { toast('That task moved — refreshing', 'error'); loadTasks(); }
        else toast('Delete failed: ' + e.message, 'error');
      }
    };
  }

  function initTasksView() {
    const filters = $('tasksFilters');
    if (filters) filters.addEventListener('click', (e) => {
      const btn = e.target.closest('.task-filter-btn'); if (!btn) return;
      const group = btn.closest('.task-filter-group'); const key = group.dataset.filter;
      group.querySelectorAll('.task-filter-btn').forEach((b) => b.classList.toggle('active', b === btn));
      taskFilters[key] = btn.dataset.value;
      renderTasks();
      if (tasksView === 'board') renderBoard();
    });
    const owner = $('taskOwnerFilter');
    if (owner) owner.addEventListener('change', () => { taskFilters.owner = owner.value; renderTasks(); if (tasksView === 'board') renderBoard(); });
    const gb = $('taskGroupBy');
    if (gb) gb.addEventListener('change', () => { taskGroupBy = gb.value; renderTasks(); });
    const newBtn = $('taskNewBtn');
    if (newBtn) newBtn.addEventListener('click', newTask);
    const list = $('tasksList');
    if (list) list.addEventListener('click', (e) => {
      const cb = e.target.closest('[data-toggle]'); if (cb) { toggleTaskByIndex(+cb.dataset.toggle, cb); return; }
      const ed = e.target.closest('[data-edit]'); if (ed) { editTask(+ed.dataset.edit); return; }
      const del = e.target.closest('[data-del]'); if (del) { deleteTask(+del.dataset.del); return; }
      const push = e.target.closest('[data-push]'); if (push) { pushMeetingToNote(+push.dataset.push); return; }
      const src = e.target.closest('[data-open-source]'); if (src) { openTaskSource(+src.dataset.openSource); return; }
    });
    initTasksBoard();
  }

  /* ==========================================================================
     TASKS BOARD (kanban view)
     ======================================================================== */
  let tasksView = localStorage.getItem('tasks-view') === 'board' ? 'board' : 'list';
  // Lane icons: constant 24x24 SVG strings only -- never interpolate task/user
  // data into these. They inherit lane color via stroke/fill="currentColor".
  const LANE_ICONS = {
    doing: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><path d="M10 8.5v7l6-3.5z" fill="currentColor" stroke="none"></path></svg>',
    overdue: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13" r="8"></circle><path d="M12 9v4.5l3 2"></path><path d="M9 3h6"></path></svg>',
    today: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"></path></svg>',
    week: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="16" rx="2"></rect><path d="M3 9.5h18M8 2.5v4M16 2.5v4"></path></svg>',
    later: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"></path></svg>',
    done: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5.5 5.5L20 7"></path></svg>',
  };
  const LANE_DEFS = [
    { key: 'doing', label: 'Doing', icon: LANE_ICONS.doing },
    { key: 'overdue', label: 'Overdue', icon: LANE_ICONS.overdue },
    { key: 'today', label: 'Today', icon: LANE_ICONS.today },
    { key: 'week', label: 'This Week', icon: LANE_ICONS.week },
    { key: 'later', label: 'Later', icon: LANE_ICONS.later },
    { key: 'done', label: 'Done', icon: LANE_ICONS.done },
  ];
  const DONE_LANE_CAP = 30;
  let boardFocusRefs = [];       // this-session focus refs from the last triage run
  let boardDismissedNudges = new Set();

  function refMatches(task, ref) {
    if (!task || !ref || task.source !== ref.source || task.source_id !== ref.source_id) return false;
    return ref.source === 'note' ? task.line === ref.line : task.index === ref.index;
  }

  function setTasksView(view) {
    tasksView = view === 'board' ? 'board' : 'list';
    localStorage.setItem('tasks-view', tasksView);
    const toggle = $('tasksViewToggle');
    if (toggle) toggle.querySelectorAll('[data-view]').forEach((b) => b.classList.toggle('active', b.dataset.view === tasksView));
    const listEl = $('tasksList'), boardEl = $('tasksBoard'), filtersEl = $('tasksFilters');
    if (listEl) listEl.style.display = tasksView === 'list' ? '' : 'none';
    if (boardEl) boardEl.style.display = tasksView === 'board' ? '' : 'none';
    // Lanes ARE the status/time buckets, so both the status and due filter groups are
    // inert in board mode -- hide both. mine-only/owner/source filters still apply to
    // the board (via passesBoardFilters below), so their controls stay visible.
    if (filtersEl) filtersEl.querySelectorAll('.task-filter-group[data-filter="status"], .task-filter-group[data-filter="due"]').forEach((g) => { g.style.display = tasksView === 'board' ? 'none' : ''; });
    if (tasksView === 'board') renderBoard();
  }

  // passesBoardFilters(t): the board's analogue of passesFilters (list view) --
  // applies ONLY source/owner (status/due don't apply: lanes already ARE the
  // status/time buckets, and their filter controls are hidden in board mode above).
  function passesBoardFilters(t) {
    if (taskFilters.source && t.source !== taskFilters.source) return false;
    if (taskFilters.owner && (t.owner || '') !== taskFilters.owner) return false;
    return true;
  }

  function laneTasksFor(key) {
    const today = todayStr();
    const K = window.KanbanLogic;
    const out = allTasks.map((t, i) => ({ t, i })).filter((o) => passesBoardFilters(o.t) && K.laneForTask(o.t, today) === key);
    if (key === 'done') return out.slice(0, DONE_LANE_CAP);
    return out;
  }

  function boardCardHtml(t, idx) {
    const isNote = t.source === 'note';
    const today = todayStr();
    const overdue = t.due && t.due < today;
    const due = t.due ? '<span class="task-chip due' + (overdue ? ' overdue' : '') + '">📅 ' + esc(t.due) + '</span>' : '';
    // Word chip (not emoji) for priority -- deliberate consistency with the list
    // view's taskRowHtml, which renders the same "high"/"medium"/"low" word chip.
    const prio = t.priority ? '<span class="task-chip prio-' + esc(t.priority) + '">' + esc(t.priority) + '</span>' : '';
    const owner = t.owner ? '<span class="task-chip owner">@' + esc(t.owner) + '</span>' : '';
    const srcIcon = isNote ? '📝' : '🎙';
    const focus = boardFocusRefs.some((r) => refMatches(t, r)) ? ' focus-pulse' : '';
    const pushBtn = isNote ? '' : '<button class="task-mini-btn" data-push="' + idx + '" title="Copy into a note">→</button>';
    return '<div class="board-card' + focus + '" draggable="true" data-card="' + idx + '">'
      + '<div class="board-card-text" data-open-source="' + idx + '">' + esc(t.text) + '</div>'
      + '<div class="task-meta">' + due + prio + owner + '<span class="task-chip source">' + srcIcon + ' ' + esc(t.source_title || t.source) + '</span></div>'
      + '<div class="board-card-actions"><button class="task-mini-btn board-move-btn" data-move="' + idx + '" title="Move to…">⋮</button>' + pushBtn + '</div>'
      + '</div>';
  }

  function renderBoard() {
    const wrap = $('boardLanes'); if (!wrap) return;
    const today = todayStr();
    const K = window.KanbanLogic;
    let html = '';
    for (const lane of LANE_DEFS) {
      const items = laneTasksFor(lane.key);
      const total = lane.key === 'done' ? allTasks.filter((t) => passesBoardFilters(t) && K.laneForTask(t, today) === 'done').length : items.length;
      const wip = lane.key === 'doing' && items.length > 5 ? ' <span class="board-lane-wip">WIP</span>' : '';
      html += '<div class="board-lane" data-lane="' + lane.key + '">'
        + '<div class="board-lane-head"><span class="board-lane-icon">' + lane.icon + '</span><span class="board-lane-label">' + esc(lane.label) + '</span><span class="board-lane-count">' + total + '</span>' + wip + '</div>'
        + '<div class="board-lane-body" data-lane-drop="' + lane.key + '">'
        + (items.length ? items.map((o) => boardCardHtml(o.t, o.i)).join('') : '<div class="board-lane-empty">—</div>')
        + '</div></div>';
    }
    wrap.innerHTML = html;
    renderNudges();
  }

  function renderNudges() {
    const el = $('boardNudges'); if (!el) return;
    const today = todayStr();
    const K = window.KanbanLogic;
    let overdue = 0, dueToday = 0, doing = 0;
    for (const t of allTasks) {
      if (!passesBoardFilters(t)) continue;
      const lane = K.laneForTask(t, today);
      if (lane === 'overdue') overdue++;
      if (lane === 'today') dueToday++;
      if (lane === 'doing') doing++;
    }
    const msgs = [];
    if (overdue > 0) msgs.push({ id: 'overdue', text: overdue + ' overdue' });
    if (dueToday > 0) msgs.push({ id: 'today', text: dueToday + ' due today' });
    if (doing > 5) msgs.push({ id: 'doing', text: 'Doing has ' + doing + ' tasks — consider finishing one before starting another' });
    const visible = msgs.filter((m) => !boardDismissedNudges.has(m.id));
    el.innerHTML = visible.map((m) =>
      '<div class="board-nudge" data-nudge="' + m.id + '">' + esc(m.text) + '<button class="board-nudge-x" data-dismiss-nudge="' + m.id + '">×</button></div>').join('');
  }

  // ---- drag & drop + move menu ----
  async function applyKanbanDrop(idx, lane) {
    const t = allTasks[idx]; if (!t) return;
    const today = todayStr();
    if (window.KanbanLogic.laneForTask(t, today) === lane) return;
    const action = window.KanbanLogic.dropActionFor(t, lane, today);
    if (!action) return;
    try {
      if ('done' in action) {
        if (t.source === 'note') {
          await window.NotesSync.applyTask(t.source_id, { kind: 'toggle', line: t.line, done: action.done, expectedText: t.text });
        } else {
          await api('/api/meetings/' + encodeURIComponent(t.source_id) + '/tasks/toggle', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: t.index, done: action.done }) });
        }
        t.done = action.done; t.state = action.done ? 'done' : t.state;
      }
      if ('due' in action) {
        const due = action.due == null ? '' : action.due;
        if (t.source === 'note') {
          await window.NotesSync.applyTask(t.source_id, { kind: 'edit', line: t.line, expectedText: t.text, text: t.text, owner: t.owner, due, priority: t.priority });
        } else {
          await api('/api/meetings/' + encodeURIComponent(t.source_id) + '/tasks', { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: t.index, text: t.text, owner: t.owner, due, priority: t.priority }) });
        }
        t.due = action.due;
      }
      if (action.state && !('done' in action)) {
        if (t.source === 'note') {
          // Note-task state drags route through NotesSync.applyTask (kind:'state'),
          // exactly like the toggle/edit branches above -- NOT the direct /api/tasks/
          // state endpoint. That endpoint is note RMW+409's API-parity sibling of
          // /api/tasks/toggle; going around NotesSync here would skip the offline
          // queue AND desync the notes-sync mirror's baseHash from the server.
          await window.NotesSync.applyTask(t.source_id, { kind: 'state', line: t.line, state: action.state, expectedText: t.text });
        } else {
          await api('/api/meetings/' + encodeURIComponent(t.source_id) + '/tasks/state', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: t.index, state: action.state }) });
        }
        t.state = action.state;
      }
      renderBoard(); updateTaskBadge();
      if (t.source === 'note' && currentNoteId === t.source_id && noteEditor) reloadCurrentNoteBody();
    } catch (e) {
      if (e.status === 409) { toast('That task moved — refreshing', 'error'); loadTasks(); }
      else toast('Move failed: ' + e.message, 'error');
    }
  }

  function openMoveMenu(idx) {
    const m = modal('<h3>Move to…</h3><div class="nt-modal-list">'
      + LANE_DEFS.filter((l) => l.key !== 'overdue').map((l) => '<div class="nt-modal-list-item" data-lane="' + l.key + '">' + esc(l.label) + '</div>').join('')
      + '</div><div class="nt-modal-actions"><button class="nt-modal-btn" data-cancel>Cancel</button></div>');
    m.q('[data-cancel]').onclick = m.close;
    m.el.querySelectorAll('[data-lane]').forEach((el) => { el.onclick = () => { m.close(); applyKanbanDrop(idx, el.dataset.lane); }; });
  }

  function initBoardDnD() {
    const wrap = $('boardLanes'); if (!wrap) return;
    let dragIdx = null;
    wrap.addEventListener('dragstart', (e) => {
      const card = e.target.closest('[data-card]'); if (!card) return;
      dragIdx = +card.dataset.card;
      e.dataTransfer.effectAllowed = 'move';
    });
    wrap.addEventListener('dragover', (e) => {
      const body = e.target.closest('[data-lane-drop]'); if (!body) return;
      if (body.dataset.laneDrop === 'overdue') return;   // not a valid drop target
      e.preventDefault();
      body.classList.add('drag-over');
    });
    wrap.addEventListener('dragleave', (e) => { const body = e.target.closest('[data-lane-drop]'); if (body) body.classList.remove('drag-over'); });
    wrap.addEventListener('drop', (e) => {
      const body = e.target.closest('[data-lane-drop]'); if (!body || dragIdx == null) return;
      e.preventDefault();
      body.classList.remove('drag-over');
      const lane = body.dataset.laneDrop;
      const idx = dragIdx; dragIdx = null;
      if (lane !== 'overdue') applyKanbanDrop(idx, lane);
    });
    wrap.addEventListener('click', (e) => {
      const mv = e.target.closest('[data-move]'); if (mv) { openMoveMenu(+mv.dataset.move); return; }
      const push = e.target.closest('[data-push]'); if (push) { pushMeetingToNote(+push.dataset.push); return; }
      const src = e.target.closest('[data-open-source]'); if (src) { openTaskSource(+src.dataset.openSource); return; }
    });
  }

  // ---- AI quick-add ----
  function renderQuickAddPreview(parsed) {
    const el = $('boardQuickAddPreview'); if (!el) return;
    const prios = [['', 'None'], ['low', 'Low'], ['medium', 'Medium'], ['high', 'High']];
    el.innerHTML = '<span class="board-preview-text">' + esc(parsed.text) + '</span>'
      + '<input type="date" id="qaDue" value="' + esc(parsed.due || '') + '">'
      + '<select id="qaPrio">' + prios.map((p) => '<option value="' + p[0] + '"' + (p[0] === (parsed.priority || '') ? ' selected' : '') + '>' + p[1] + '</option>').join('') + '</select>'
      + '<input type="text" id="qaOwner" placeholder="owner" value="' + esc(parsed.owner || '') + '">'
      + '<button class="nt-modal-btn primary" id="qaConfirm">Add</button>'
      + '<button class="nt-modal-btn" id="qaCancel">Cancel</button>';
    el.style.display = 'flex';
    $('qaConfirm').onclick = async () => {
      try {
        await window.NotesSync.addTask({ text: parsed.text, owner: $('qaOwner').value.trim(), due: $('qaDue').value, priority: $('qaPrio').value });
        el.style.display = 'none'; $('boardQuickAddInput').value = '';
        if (navigator.onLine && window.NotesSync) { try { await window.NotesSync.flush(); } catch (e) {} }
        toast('Task added', 'success');
        await loadTasks();
      } catch (e) { toast('Add failed: ' + e.message, 'error'); }
    };
    $('qaCancel').onclick = () => { el.style.display = 'none'; };
  }

  function initBoardQuickAdd() {
    const form = $('boardQuickAdd'); if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = $('boardQuickAddInput');
      const text = input.value.trim(); if (!text) return;
      try {
        const parsed = await api('/api/tasks/ai/parse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
        renderQuickAddPreview(parsed);
      } catch (e) { toast('Parse failed: ' + e.message, 'error'); }
    });
  }

  // ---- AI triage ----
  function renderTriagePanel(result) {
    const el = $('boardTriagePanel'); if (!el) return;
    const rawSuggestions = (result && result.suggestions) || [];
    // Resolve each suggestion's ref to its current task via refMatches (same helper
    // boardCardHtml's focus-pulse uses). A suggestion whose ref no longer resolves
    // (task edited/removed since the triage snapshot was built server-side) is
    // dropped rather than rendered as an unidentifiable row.
    const resolved = rawSuggestions
      .map((s) => ({ sug: s, t: allTasks.find((x) => refMatches(x, s.ref)) }))
      .filter((r) => r.t);
    if (!resolved.length) { el.style.display = 'none'; el.innerHTML = ''; boardFocusRefs = []; return; }
    el.innerHTML = '<div class="board-triage-head"><span>Suggestions</span><button class="nt-modal-btn" id="triageCloseBtn">Close</button></div>'
      + resolved.map((r, i) => '<div class="board-triage-row" data-sug="' + i + '">'
        + '<span class="board-triage-text">' + esc(r.t.text) + ' — ' + esc(r.t.priority || 'none') + ' → <b>' + esc(r.sug.priority) + '</b>: ' + esc(r.sug.reason) + '</span>'
        + '<button class="nt-modal-btn primary" data-apply-sug="' + i + '">Apply</button>'
        + '<button class="nt-modal-btn" data-dismiss-sug="' + i + '">Dismiss</button></div>').join('');
    el.style.display = 'block';
    el._suggestions = resolved.map((r) => r.sug);
    boardFocusRefs = (result.focus || []);
    renderBoard();
  }

  async function applyTriageSuggestion(sug) {
    const idx = allTasks.findIndex((t) => refMatches(t, sug.ref));
    if (idx < 0) { toast('Task no longer found — refreshing', 'error'); loadTasks(); return; }
    const t = allTasks[idx];
    try {
      if (t.source === 'note') {
        await window.NotesSync.applyTask(t.source_id, { kind: 'edit', line: t.line, expectedText: t.text, text: t.text, owner: t.owner, due: t.due, priority: sug.priority });
      } else {
        await api('/api/meetings/' + encodeURIComponent(t.source_id) + '/tasks', { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ index: t.index, text: t.text, owner: t.owner, due: t.due, priority: sug.priority }) });
      }
      t.priority = sug.priority;
      toast('Priority updated', 'success');
      if (tasksView === 'board') renderBoard(); else renderTasks();
    } catch (e) {
      if (e.status === 409) { toast('That task moved — refreshing', 'error'); loadTasks(); }
      else toast('Apply failed: ' + e.message, 'error');
    }
  }

  function initBoardTriage() {
    const btn = $('boardTriageBtn'); if (btn) btn.addEventListener('click', async () => {
      try {
        const result = await api('/api/tasks/ai/triage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        renderTriagePanel(result);
      } catch (e) { toast('Triage failed: ' + e.message, 'error'); }
    });
    const panel = $('boardTriagePanel'); if (panel) panel.addEventListener('click', (e) => {
      if (e.target.closest('#triageCloseBtn')) { panel.style.display = 'none'; panel.innerHTML = ''; boardFocusRefs = []; renderBoard(); return; }
      const apply = e.target.closest('[data-apply-sug]');
      if (apply) { const sug = panel._suggestions[+apply.dataset.applySug]; if (sug) applyTriageSuggestion(sug); return; }
      const dismiss = e.target.closest('[data-dismiss-sug]');
      if (dismiss) { const row = dismiss.closest('.board-triage-row'); if (row) row.remove(); return; }
    });
    const nudges = $('boardNudges'); if (nudges) nudges.addEventListener('click', (e) => {
      const x = e.target.closest('[data-dismiss-nudge]'); if (x) { boardDismissedNudges.add(x.dataset.dismissNudge); renderNudges(); }
    });
  }

  function initTasksBoard() {
    const toggle = $('tasksViewToggle');
    if (toggle) toggle.addEventListener('click', (e) => { const b = e.target.closest('[data-view]'); if (b) setTasksView(b.dataset.view); });
    initBoardDnD();
    initBoardQuickAdd();
    initBoardTriage();
    setTasksView(tasksView);
  }

  /* ==========================================================================
     NOTES WORKSPACE
     ======================================================================== */
  let allNotes = [];                 // list records (no body)
  let allFolders = [];
  let collapsedFolders = {};
  let currentNoteId = null;
  let currentNote = null;            // with body
  let noteEditor = null;             // MoonbaseEditor handle
  let previewOn = false;
  let semanticMode = false;
  let searchQuery = '';

  function noteTitleSet() { return new Set(allNotes.map((n) => (n.title || '').toLowerCase())); }
  function wikiTargets() { return allNotes.map((n) => ({ label: n.title, detail: n.folder || '' })); }

  async function loadNotesTree() {
    const body = $('notesTreeBody'); if (!body) return;
    try {
      const [nd, fd] = await Promise.all([
        window.NotesSync.listNotes().then((notes) => ({ notes })),   // read from the offline mirror
        api('/api/notes/folders').catch(() => ({ folders: allFolders })),  // folders stay online, best-effort
      ]);
      allNotes = (nd && nd.notes) || [];
      allFolders = (fd && fd.folders) || [];
      renderTree();
    } catch (e) { body.innerHTML = '<div class="notes-tree-empty">Failed: ' + esc(e.message) + '</div>'; }
  }

  function renderTree() {
    const body = $('notesTreeBody'); if (!body) return;
    let notes = allNotes;
    if (searchQuery && !semanticMode) {
      const q = searchQuery.toLowerCase();
      notes = allNotes.filter((n) => (n.title || '').toLowerCase().includes(q) || (n.folder || '').toLowerCase().includes(q));
    }
    if (!notes.length) { body.innerHTML = '<div class="notes-tree-empty">' + (searchQuery ? 'No matches.' : 'No notes yet.<br>Create one to begin.') + '</div>'; return; }

    const byFolder = new Map();
    for (const n of notes) { const f = n.folder || ''; if (!byFolder.has(f)) byFolder.set(f, []); byFolder.get(f).push(n); }
    const folders = [...byFolder.keys()].sort((a, b) => (a === '' ? -1 : b === '' ? 1 : a.localeCompare(b)));
    let html = '';
    for (const f of folders) {
      const items = byFolder.get(f).sort((a, b) => (b.updated || '').localeCompare(a.updated || ''));
      if (f === '') {
        html += '<div class="nt-folder-items">' + items.map(itemHtml).join('') + '</div>';
      } else {
        const collapsed = collapsedFolders[f];
        html += '<div class="nt-folder' + (collapsed ? ' collapsed' : '') + '" data-folder="' + esc(f) + '">'
          + '<div class="nt-folder-head"><span class="nt-folder-caret">▾</span><span>' + esc(f) + '</span><span class="nt-folder-count">' + items.length + '</span></div>'
          + '<div class="nt-folder-items">' + items.map(itemHtml).join('') + '</div></div>';
      }
    }
    body.innerHTML = html;
  }

  function itemHtml(n) {
    const active = n.id === currentNoteId ? ' active' : '';
    const typeBadge = (n.type && n.type !== 'note') ? '<span class="nt-item-type">' + esc(n.type) + '</span>' : '';
    return '<div class="nt-item' + active + '" data-note="' + esc(n.id) + '">'
      + '<div class="nt-item-title">' + esc(n.title || 'Untitled') + '</div>'
      + '<div class="nt-item-meta">' + typeBadge + '<span>' + esc(relTime(n.updated)) + '</span>'
      + (n.tags && n.tags.length ? '<span>#' + esc(n.tags[0]) + (n.tags.length > 1 ? '+' + (n.tags.length - 1) : '') + '</span>' : '')
      + '</div></div>';
  }

  async function runSemanticSearch(q) {
    const body = $('notesTreeBody');
    body.innerHTML = '<div class="notes-tree-empty">Searching…</div>';
    try {
      const data = await api('/api/notes/search?q=' + encodeURIComponent(q) + '&limit=20');
      const hits = (data && data.results) || [];
      const seen = new Set(); const rows = [];
      for (const h of hits) { if (seen.has(h.note_id)) continue; seen.add(h.note_id); rows.push(h); }
      if (!rows.length) { body.innerHTML = '<div class="notes-tree-empty">No semantic matches.</div>'; return; }
      body.innerHTML = '<div class="nt-section-label">Semantic results</div>' + rows.map((h) =>
        '<div class="nt-item' + (h.note_id === currentNoteId ? ' active' : '') + '" data-note="' + esc(h.note_id) + '">'
        + '<div class="nt-item-title">' + esc(h.title || 'Untitled') + '</div>'
        + '<div class="nt-item-snippet">' + esc((h.text || '').slice(0, 80)) + '</div></div>').join('');
    } catch (e) { body.innerHTML = '<div class="notes-tree-empty">Search failed: ' + esc(e.message) + '</div>'; }
  }

  // ---- editor lifecycle ----
  function ensureEditor() {
    if (noteEditor) return noteEditor;
    if (!window.MoonbaseEditor) { toast('Editor failed to load', 'error'); return null; }
    noteEditor = window.MoonbaseEditor.create({
      parent: $('noteEditorHost'),
      doc: '',
      onChange: onEditorChange,
      getWikiTargets: wikiTargets,
      knownTargets: noteTitleSet,
      onWikiLink: openByTitle,
      onTag: (tag) => { setActivePillar('notes'); const v = tag.replace(/^#/, ''); $('ntSearch').value = v; searchQuery = v; semanticMode = false; renderTree(); },
    });
    return noteEditor;
  }

  const autosave = debounce(saveBody, 800);
  function onEditorChange() {
    setSaveState('saving');
    autosave();
    if (previewOn) updatePreview();
  }

  async function openNote(id) {
    try {
      const note = await window.NotesSync.readNote(id);
      if (!note) throw new Error('Note not found');
      currentNote = note; currentNoteId = note.id;
      $('notesWelcome').style.display = 'none';
      $('notesDoc').style.display = 'flex';
      $('noteTitleInput').value = note.title || '';
      ensureEditor();
      if (noteEditor) noteEditor.setValue(note.body || '');
      renderMeta(); renderTags(); setSaveState('saved', 'Saved');
      if (previewOn) updatePreview();
      renderTree();
      loadBacklinks();
      loadRelated();
      loadAnalysis();
      if (isMobileWidth()) notesViewEl().classList.remove('tree-open');
    } catch (e) { toast('Open failed: ' + e.message, 'error'); }
  }

  async function reloadCurrentNoteBody() {
    if (!currentNoteId) return;
    try { const note = await window.NotesSync.readNote(currentNoteId); if (note) { currentNote = note; if (noteEditor) noteEditor.setValue(note.body || ''); } } catch (e) {}
  }

  function openByTitle(title) {
    const t = (title || '').toLowerCase();
    const hit = allNotes.find((n) => (n.title || '').toLowerCase() === t);
    if (hit) { openNote(hit.id); return; }
    if (confirm('No note titled “' + title + '”. Create it?')) createNote(title, currentNote ? currentNote.folder : '');
  }

  function renderMeta() {
    const bar = $('noteMetaBar'); if (!bar || !currentNote) return;
    const parts = [];
    parts.push('<span>' + esc(currentNote.folder || 'root') + '</span>');
    parts.push('<span>edited ' + esc(relTime(currentNote.updated)) + '</span>');
    const lm = currentNote.linked_meetings || [];
    if (lm.length) parts.push('<span class="nm-meeting" data-meeting="' + esc(lm[0]) + '">🔗 ' + lm.length + ' meeting' + (lm.length > 1 ? 's' : '') + '</span>');
    bar.innerHTML = parts.join('');
  }

  function renderTags() {
    const row = $('noteTagsRow'); if (!row || !currentNote) return;
    const tags = currentNote.tags || [];
    row.innerHTML = tags.map((t) => '<span class="note-tag-chip">#' + esc(t) + '<span class="x" data-rmtag="' + esc(t) + '">×</span></span>').join('')
      + '<button class="note-tag-add" id="noteTagAdd">+ tag</button>';
  }

  function setSaveState(cls, text) {
    const el = $('noteSaveState'); if (!el) return;
    el.className = 'note-save-state ' + (cls || '');
    el.textContent = text || (cls === 'saving' ? 'Saving…' : cls === 'saved' ? 'Saved' : cls === 'error' ? 'Error' : '');
  }

  async function saveBody() {
    if (!currentNoteId || !noteEditor) return;
    const body = noteEditor.getValue();
    try {
      const updated = await window.NotesSync.updateNote(currentNoteId, { body });
      currentNote = updated;
      const li = allNotes.find((n) => n.id === currentNoteId); if (li) li.updated = updated.updated;
      setSaveState('saved', 'Saved'); renderMeta();
    } catch (e) { setSaveState('error', 'Unsaved'); toast('Autosave failed: ' + e.message, 'error'); }
  }

  const saveTitle = debounce(async () => {
    if (!currentNoteId) return;
    const title = $('noteTitleInput').value.trim() || 'Untitled';
    try {
      const updated = await window.NotesSync.updateNote(currentNoteId, { title });
      currentNote = updated;
      const li = allNotes.find((n) => n.id === currentNoteId); if (li) li.title = updated.title;
      renderTree();
    } catch (e) { toast('Title save failed: ' + e.message, 'error'); }
  }, 700);

  async function setTags(tags) {
    if (!currentNoteId) return;
    try {
      // Direct online write — server owns tags (the auto-tagger overwrites them
      // within 60s otherwise), and the offline mirror never pushes tags, so a
      // mirror-only edit would silently revert on the next pull(). Offline this
      // throws a normal network error (caught below) — acceptable per spec.
      const updated = await api('/api/notes/' + currentNoteId, { method: 'PUT',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tags }) });
      currentNote = updated; renderTags();
      const li = allNotes.find((n) => n.id === currentNoteId); if (li) li.tags = updated.tags;
      renderTree();
      // Absorb the new server record into the offline mirror so it doesn't look
      // stale/dirty on the next pull; NotesSync has no "absorb one record" helper,
      // so fall back to a full pull().
      if (window.NotesSync && window.NotesSync.pull) { try { await window.NotesSync.pull(); } catch (e) {} }
    } catch (e) { toast('Tag save failed: ' + e.message, 'error'); }
  }

  async function createNote(title, folder) {
    try {
      const note = await window.NotesSync.createNote({ title: title || 'Untitled note', folder: folder || '', type: 'note', body: '' });
      allNotes.unshift({ id: note.id, title: note.title, type: note.type, folder: note.folder, path: note.path, tags: note.tags, linked_meetings: note.linked_meetings, created: note.created, updated: note.updated });
      await openNote(note.id);
      const ti = $('noteTitleInput'); ti.focus(); ti.select();
    } catch (e) { toast('Create failed: ' + e.message, 'error'); }
  }

  async function createOrOpenJournal() {
    const t = todayStr();
    const existing = allNotes.find((n) => n.type === 'journal' && n.title === t) || allNotes.find((n) => n.title === t);
    if (existing) { openNote(existing.id); return; }
    try {
      const note = await window.NotesSync.createNote({ title: t, folder: 'Journal', type: 'journal', body: '# ' + t + '\n\n' });
      allNotes.unshift({ id: note.id, title: note.title, type: note.type, folder: note.folder, path: note.path, tags: [], linked_meetings: [], created: note.created, updated: note.updated });
      if (allFolders.indexOf('Journal') < 0) allFolders.push('Journal');
      await openNote(note.id);
    } catch (e) { toast('Journal failed: ' + e.message, 'error'); }
  }

  async function deleteCurrentNote() {
    if (!currentNoteId) return;
    if (!confirm('Move “' + (currentNote.title || 'this note') + '” to trash?')) return;
    const id = currentNoteId;
    try {
      await window.NotesSync.deleteNote(id);
      allNotes = allNotes.filter((n) => n.id !== id);
      currentNoteId = null; currentNote = null;
      $('notesDoc').style.display = 'none'; $('notesWelcome').style.display = 'flex';
      notesViewEl().classList.remove('tree-collapsed');
      renderTree(); toast('Moved to trash', 'success');
    } catch (e) { toast('Delete failed: ' + e.message, 'error'); }
  }

  function openMoveModal() {
    if (!currentNote) return;
    const folderOpts = allFolders.map((f) => '<option value="' + esc(f) + '">').join('');
    const m = modal('<h3>Rename / move note</h3>'
      + '<label>Title</label><input type="text" id="mvTitle" value="' + esc(currentNote.title || '') + '">'
      + '<label>Folder (blank = root)</label><input type="text" id="mvFolder" list="mvFolderList" value="' + esc(currentNote.folder || '') + '"><datalist id="mvFolderList">' + folderOpts + '</datalist>'
      + '<div class="nt-modal-actions"><button class="nt-modal-btn" data-cancel>Cancel</button><button class="nt-modal-btn primary" data-save>Save</button></div>');
    m.q('[data-cancel]').onclick = m.close;
    m.q('[data-save]').onclick = async () => {
      const title = m.q('#mvTitle').value.trim() || 'Untitled';
      const folder = m.q('#mvFolder').value.trim();
      try {
        const updated = await api('/api/notes/' + currentNoteId + '/rename', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title, folder }) });
        currentNote = updated;
        const li = allNotes.find((n) => n.id === currentNoteId);
        if (li) { li.title = updated.title; li.folder = updated.folder; li.path = updated.path; li.updated = updated.updated; }
        if (folder && allFolders.indexOf(folder) < 0) allFolders.push(folder);
        $('noteTitleInput').value = updated.title; renderMeta(); renderTree(); m.close(); toast('Saved', 'success');
      } catch (e) { toast('Rename failed: ' + e.message, 'error'); }
    };
  }

  async function openLinkMeetingModal() {
    if (!currentNote) return;
    let meetings = [];
    try { const d = await api('/meetings'); meetings = (d && (d.meetings || d)) || []; } catch (e) {}
    const linked = new Set(currentNote.linked_meetings || []);
    const rows = meetings.map((mt) => {
      const id = mt.id || mt.meeting_id; const title = mt.title || mt.name || id;
      return '<div class="nt-modal-list-item" data-mt="' + esc(id) + '">' + (linked.has(id) ? '✓ ' : '') + esc(title) + '<div class="sub">' + esc(mt.date || mt.created || '') + '</div></div>';
    }).join('');
    const m = modal('<h3>Link a meeting</h3><div class="nt-modal-list">' + (rows || '<div class="nt-modal-list-item">No meetings found</div>') + '</div>'
      + '<div class="nt-modal-actions"><button class="nt-modal-btn" data-cancel>Close</button></div>');
    m.q('[data-cancel]').onclick = m.close;
    m.el.querySelectorAll('[data-mt]').forEach((el) => {
      el.onclick = async () => {
        const id = el.dataset.mt; const add = !linked.has(id);
        try {
          const updated = await api('/api/notes/' + currentNoteId + '/link-meeting', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ meeting_id: id, add }) });
          currentNote = updated; renderMeta(); m.close(); toast(add ? 'Meeting linked' : 'Meeting unlinked', 'success');
        } catch (e) { toast('Link failed: ' + e.message, 'error'); }
      };
    });
  }

  async function loadBacklinks() {
    const el = $('noteBacklinks'); if (!el || !currentNoteId) return;
    el.innerHTML = '';
    try {
      const data = await api('/api/notes/' + currentNoteId + '/links');
      const bl = (data && data.backlinks) || [];
      if (!bl.length) { el.innerHTML = ''; return; }
      el.innerHTML = '<span class="bl-label">Linked from</span>' + bl.map((n) => '<span class="bl-chip" data-note="' + esc(n.id) + '">' + esc(n.title) + '</span>').join('');
    } catch (e) { el.innerHTML = ''; }
  }

  async function loadRelated() {
    const el = $('noteRelated'); if (!el || !currentNoteId) return;
    el.innerHTML = '';
    try {
      const data = await api('/api/notes/' + currentNoteId + '/related');
      const rel = (data && data.related) || [];
      if (!rel.length) return;
      el.innerHTML = '<span class="bl-label">Related meetings</span>' + rel.map((r) =>
        '<span class="bl-chip" data-meeting="' + esc(r.meeting_id) + '">' + esc(r.title || r.meeting_id) +
        '<span class="pin" data-pin="' + esc(r.meeting_id) + '" title="Pin as a link">📌</span></span>').join('');
    } catch (e) {}
  }

  // ---- AI analysis ----
  function renderAnalysisPanel(result) {
    const el = $('noteAnalysis'); if (!el) return;
    if (!result || typeof result !== 'object') { el.style.display = 'none'; el.innerHTML = ''; return; }
    const list = (title, items, bullet) => {
      const arr = Array.isArray(items) ? items.filter((x) => x != null && String(x).trim()) : [];
      if (!arr.length) return '';
      return '<h4 style="margin:8px 0 4px;font-size:12px;opacity:.8">' + esc(title) + '</h4><ul style="margin:4px 0 4px 18px">'
        + arr.map((x) => '<li>' + esc(bullet + String(x).trim()) + '</li>').join('') + '</ul>';
    };
    const summary = (result.summary == null ? '' : String(result.summary)).trim();
    let html = '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px">'
      + '<span style="font-weight:600">AI analysis</span>'
      + '<div style="display:flex;gap:8px"><button class="nt-modal-btn primary" data-na-insert>Insert into note</button>'
      + '<button class="nt-modal-btn" data-na-close>Close</button></div></div>';
    if (summary) html += '<p style="margin:8px 0">' + esc(summary) + '</p>';
    html += list('Key points', result.key_points, '');
    html += list('Action items', result.action_items, '☐ ');
    html += list('Insights', result.insights, '');
    el.innerHTML = html;      // all interpolations above are esc()-wrapped
    el.style.display = 'block';
    el._result = result;
  }

  async function loadAnalysis() {
    const el = $('noteAnalysis'); if (!el || !currentNoteId) return;
    el.style.display = 'none'; el.innerHTML = '';
    try {
      const data = await api('/api/notes/' + currentNoteId + '/analysis');
      if (data && data.status === 'done' && data.result) renderAnalysisPanel(data.result);
    } catch (e) {}
  }

  function pollAnalysis(noteId, tries) {
    if (currentNoteId !== noteId) return;                 // navigated away
    if (tries <= 0) { toast('Analysis is taking a while — reopen the note to check', 'error'); return; }
    setTimeout(async () => {
      if (currentNoteId !== noteId) return;
      try {
        const data = await api('/api/notes/' + noteId + '/analysis');
        if (data && data.status === 'done' && data.result) { renderAnalysisPanel(data.result); toast('Analysis ready', 'success'); return; }
        if (data && data.status === 'error') { toast('Analysis failed', 'error'); return; }
      } catch (e) {}
      pollAnalysis(noteId, tries - 1);
    }, 3000);
  }

  async function runAnalyze() {
    if (!currentNoteId) return;
    const noteId = currentNoteId;
    try {
      await api('/api/notes/' + noteId + '/analyze', { method: 'POST' });
      toast('Analyzing note — runs when the GPU is free…', 'success');
      pollAnalysis(noteId, 40);
    } catch (e) { toast('Analyze failed: ' + e.message, 'error'); }
  }

  // ---- preview ----
  function updatePreview() {
    const pv = $('notePreview'); if (!pv || !noteEditor || !window.MoonbaseEditor) return;
    pv.innerHTML = window.MoonbaseEditor.renderMarkdown(noteEditor.getValue());   // DOMPurify-sanitized inside the bundle
  }
  function togglePreview() {
    previewOn = !previewOn;
    $('notePreviewToggle').classList.toggle('active', previewOn);
    $('notePreview').style.display = previewOn ? 'block' : 'none';
    $('notesDoc').querySelector('.notes-doc-body').classList.toggle('split', previewOn);
    if (previewOn) updatePreview();
  }

  // ---- toolbar ----
  const TOOLBAR = [
    { cmd: 'h1', html: 'H1', t: 'Heading 1' }, { cmd: 'h2', html: 'H2', t: 'Heading 2' }, { cmd: 'h3', html: 'H3', t: 'Heading 3' }, { sep: 1 },
    { cmd: 'bold', html: '<b>B</b>', t: 'Bold (Ctrl/Cmd-B)' }, { cmd: 'italic', html: '<i>I</i>', t: 'Italic (Ctrl/Cmd-I)' }, { cmd: 'strike', html: '<s>S</s>', t: 'Strikethrough' }, { cmd: 'code', html: '&lt;/&gt;', t: 'Inline code' }, { sep: 1 },
    { cmd: 'ul', html: '•', t: 'Bullet list' }, { cmd: 'ol', html: '1.', t: 'Numbered list' }, { cmd: 'checkbox', html: '☑', t: 'Task' }, { cmd: 'quote', html: '❝', t: 'Quote' }, { sep: 1 },
    { cmd: 'link', html: '🔗', t: 'Link' }, { cmd: 'wikilink', html: '[[', t: 'Wiki-link to a note' }, { cmd: 'codeblock', html: '{ }', t: 'Code block' }, { cmd: 'hr', html: '―', t: 'Divider' },
  ];
  function renderToolbar() {
    const tb = $('noteToolbar'); if (!tb) return;
    tb.innerHTML = TOOLBAR.map((b) => b.sep ? '<span class="tb-sep"></span>' : '<button class="tb-btn" data-cmd="' + b.cmd + '" title="' + esc(b.t) + '">' + b.html + '</button>').join('');
    tb.addEventListener('click', (e) => { const btn = e.target.closest('[data-cmd]'); if (btn && noteEditor) noteEditor.applyFormat(btn.dataset.cmd); });
  }

  function applyToolbarHidden(hidden) {
    $('notesDoc').classList.toggle('toolbar-hidden', hidden);
    $('noteToolbarToggle').classList.toggle('active', hidden);
  }

  async function uploadAttachment(fileObj) {
    if (!currentNoteId || !fileObj) return;
    const fd = new FormData(); fd.append('file', fileObj);
    try {
      const r = await api('/api/notes/' + currentNoteId + '/attachments', { method: 'POST', body: fd });
      if (noteEditor && noteEditor.insertAtCursor) noteEditor.insertAtCursor('\n' + r.embed + '\n');
      if (previewOn) updatePreview();
      toast('Attached ' + r.filename, 'success');
    } catch (e) { toast('Attach failed: ' + e.message, 'error'); }
  }

  function uploadSvg(svgString, filename) {
    if (!currentNoteId) { toast('No note open', 'error'); return Promise.reject(new Error('no note')); }
    const fd = new FormData();
    fd.append('file', new File([svgString], filename, { type: 'image/svg+xml' }));
    return api('/api/notes/' + currentNoteId + '/attachments', { method: 'POST', body: fd });
  }
  function tsName() {
    const d = new Date(); const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '-sketch.svg';
  }

  function initNotesView() {
    renderToolbar();
    $('ntNewNote').onclick = () => createNote('Untitled note', '');
    $('ntWelcomeNew').onclick = () => createNote('Untitled note', '');
    $('ntNewJournal').onclick = createOrOpenJournal;
    $('ntWelcomeJournal').onclick = createOrOpenJournal;
    $('ntRescan').onclick = async () => { try { const r = await api('/api/notes/rescan', { method: 'POST' }); await loadNotesTree(); toast('Re-scanned ' + (r && r.count != null ? r.count + ' notes' : ''), 'success'); } catch (e) { toast('Rescan failed: ' + e.message, 'error'); } };

    let toolbarHidden = localStorage.getItem('notes-toolbar-hidden') === '1';
    applyToolbarHidden(toolbarHidden);
    $('noteToolbarToggle').onclick = () => {
      toolbarHidden = !toolbarHidden;
      localStorage.setItem('notes-toolbar-hidden', toolbarHidden ? '1' : '0');
      applyToolbarHidden(toolbarHidden);
    };

    const search = $('ntSearch');
    const onSearch = debounce(() => { searchQuery = search.value.trim(); if (!semanticMode) renderTree(); }, 200);
    search.addEventListener('input', onSearch);
    search.addEventListener('keydown', (e) => { if (e.key === 'Enter' && semanticMode && search.value.trim()) runSemanticSearch(search.value.trim()); });
    $('ntSemanticBtn').onclick = () => {
      semanticMode = !semanticMode;
      $('ntSemanticBtn').classList.toggle('active', semanticMode);
      search.placeholder = semanticMode ? 'Semantic search — press Enter…' : 'Search notes…';
      if (semanticMode && search.value.trim()) runSemanticSearch(search.value.trim());
      else { searchQuery = search.value.trim(); renderTree(); }
    };

    $('notesTreeBody').addEventListener('click', (e) => {
      const fh = e.target.closest('.nt-folder-head');
      if (fh) { const f = fh.parentElement.dataset.folder; collapsedFolders[f] = !collapsedFolders[f]; fh.parentElement.classList.toggle('collapsed'); return; }
      const item = e.target.closest('.nt-item'); if (item) openNote(item.dataset.note);
    });

    $('noteTitleInput').addEventListener('input', saveTitle);
    $('notePreviewToggle').onclick = togglePreview;
    $('noteDeleteBtn').onclick = deleteCurrentNote;
    $('noteMoveBtn').onclick = openMoveModal;
    $('noteLinkMeetingBtn').onclick = openLinkMeetingModal;
    $('noteRetagBtn').onclick = async () => {
      if (!currentNoteId) return;
      try {
        await api('/api/notes/' + currentNoteId + '/retag', { method: 'POST' });
        toast('Tagging queued — tags update shortly', 'success');
        setTimeout(() => { if (currentNoteId) openNote(currentNoteId); }, 8000);
      } catch (e) { toast('Retag failed: ' + e.message, 'error'); }
    };

    $('noteAnalyzeBtn').onclick = runAnalyze;
    $('noteAnalysis').addEventListener('click', (e) => {
      const panel = $('noteAnalysis');
      if (e.target.closest('[data-na-close]')) { panel.style.display = 'none'; panel.innerHTML = ''; return; }
      if (e.target.closest('[data-na-insert]')) {
        const md = window.NotesAnalysis ? window.NotesAnalysis.formatAnalysisMarkdown(panel._result) : '';
        if (md && noteEditor && noteEditor.insertAtCursor) {
          noteEditor.insertAtCursor('\n' + md + '\n');
          if (previewOn) updatePreview();
          toast('Inserted', 'success');
        }
      }
    });

    $('noteTagsRow').addEventListener('click', (e) => {
      const rm = e.target.closest('[data-rmtag]');
      if (rm) { setTags((currentNote.tags || []).filter((t) => t !== rm.dataset.rmtag)); return; }
      if (e.target.id === 'noteTagAdd') { const v = prompt('Add tag (no #):'); if (v && v.trim()) { const tg = v.trim().replace(/^#/, ''); if (!(currentNote.tags || []).includes(tg)) setTags([...(currentNote.tags || []), tg]); } }
    });
    $('noteMetaBar').addEventListener('click', (e) => { const mt = e.target.closest('[data-meeting]'); if (mt) { setActivePillar('meetings'); if (typeof window.openMeeting === 'function') { try { window.openMeeting(mt.dataset.meeting); } catch (err) {} } } });
    $('noteBacklinks').addEventListener('click', (e) => { const c = e.target.closest('[data-note]'); if (c) openNote(c.dataset.note); });
    $('noteRelated').addEventListener('click', async (e) => {
      const pin = e.target.closest('[data-pin]');
      if (pin) { e.stopPropagation();
        try { const u = await api('/api/notes/' + currentNoteId + '/link-meeting',
                { method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ meeting_id: pin.dataset.pin, add: true }) });
          currentNote = u; renderMeta(); toast('Pinned', 'success'); } catch (err) { toast(err.message, 'error'); }
        return; }
      const chip = e.target.closest('[data-meeting]');
      if (chip) { setActivePillar('meetings'); if (window.openMeeting) { try { window.openMeeting(chip.dataset.meeting); } catch (x) {} } }
    });
    // expose for the meeting side:
    window.openNoteFromMeeting = (id) => { setActivePillar('notes'); openNote(id); };

    $('notePreview').addEventListener('click', async (e) => {
      const img = e.target.closest('img.note-embed');
      if (img && e.altKey && /\.svg(\?|$)/.test(img.getAttribute('src') || '')) {
        e.preventDefault();
        if (!window.MoonbaseDraw) { toast('Drawing canvas not loaded', 'error'); return; }
        const url = img.getAttribute('src');
        const fname = decodeURIComponent(url.split('/').pop());
        try {
          const svg = await (await fetch(url)).text();
          window.MoonbaseDraw.open({ svg, onSave: async (newSvg) => {
            try {
              const r = await uploadSvg(newSvg, fname.replace(/\.svg$/, '') + '-edit.svg');
              if (noteEditor && noteEditor.insertAtCursor) noteEditor.insertAtCursor('\n' + r.embed + '\n');
              if (previewOn) updatePreview();
            } catch (err) { toast(err.message, 'error'); }
          } });
        } catch (err) { toast('Could not load drawing: ' + err.message, 'error'); }
        return;
      }
      const wl = e.target.closest('.note-wikilink'); if (wl) { e.preventDefault(); openByTitle(wl.dataset.wikilink); return; }
      const a = e.target.closest('a[href]'); if (a && a.getAttribute('href') && a.getAttribute('href')[0] !== '#') { e.preventDefault(); window.open(a.href, '_blank', 'noopener'); }
    });

    $('noteAttachBtn').onclick = () => $('noteAttachInput').click();
    $('noteAttachInput').onchange = (e) => { if (e.target.files[0]) uploadAttachment(e.target.files[0]); e.target.value = ''; };
    $('noteDrawBtn').onclick = () => {
      if (!currentNoteId || !window.MoonbaseDraw) return;
      window.MoonbaseDraw.open({ onSave: async (svg) => {
        try {
          const r = await uploadSvg(svg, tsName());
          if (noteEditor && noteEditor.insertAtCursor) noteEditor.insertAtCursor('\n' + r.embed + '\n');
          if (previewOn) updatePreview();
          toast('Drawing saved — Alt-click the sketch in preview to edit it', 'success');
        } catch (e) { toast('Save drawing failed: ' + e.message, 'error'); }
      } });
    };
    const host = $('noteEditorHost');
    host.addEventListener('dragover', (e) => { e.preventDefault(); host.classList.add('drag-over'); });
    host.addEventListener('dragleave', (e) => { if (!host.contains(e.relatedTarget)) host.classList.remove('drag-over'); });
    host.addEventListener('drop', (e) => { e.preventDefault(); host.classList.remove('drag-over');
      if (e.dataTransfer.files[0]) uploadAttachment(e.dataTransfer.files[0]); });
    host.addEventListener('paste', (e) => { const f = [...(e.clipboardData.files || [])][0]; if (f) { e.preventDefault(); uploadAttachment(f); } });

    const toggle = document.createElement('button');
    toggle.className = 'nt-tree-toggle'; toggle.innerHTML = '☰'; toggle.title = 'Notes list';
    toggle.onclick = () => {
      const cls = isMobileWidth() ? 'tree-open' : 'tree-collapsed';
      notesViewEl().classList.toggle(cls);
    };
    $('notesDoc').querySelector('.notes-doc-head').prepend(toggle);
  }

  /* ==========================================================================
     BOOT
     ======================================================================== */
  function boot() {
    initPillarNav();
    initTasksView();
    initNotesView();
    if (window.NotesSync) {
      window.NotesSync.init({
        onNotes: (notes) => { allNotes = notes; if (currentPillar === 'notes') renderTree(); },
        onRemap: (tempIdVal, serverId) => {
          if (currentNoteId === tempIdVal) currentNoteId = serverId;
          if (currentNote && currentNote.id === tempIdVal) currentNote.id = serverId;
          const li = allNotes.find((n) => n.id === tempIdVal); if (li) li.id = serverId;
        },
        toast,
      });
    }
    api('/api/tasks').then((d) => { allTasks = (d && d.tasks) || []; updateTaskBadge(); }).catch(() => {});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
