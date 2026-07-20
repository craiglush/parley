// Pure lane-assignment + drag-drop mutation logic for the Tasks board view.
// Shared by static/notes-tasks.js (loaded as a browser global via a <script>
// tag) and the Node unit test (tests/js/kanban_logic.test.mjs). DOM-free and
// dependency-free (no npm, no import). Mirrors the due-date bucketing already
// used server-side (tasks_store.filter_tasks / build_digest_snapshot) and
// client-side (notes-tasks.js dueBucket()) so the board agrees with the list
// view and the digest email on what counts as overdue/today/this-week.

const KanbanLogic = {};

// _plusDaysISO('2026-07-14', 6) -> '2026-07-20'. UTC-based so it never drifts
// across a local-timezone midnight boundary.
function _plusDaysISO(iso, days) {
  const [y, m, d] = String(iso).split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + days));
  return dt.getUTCFullYear() + '-' + String(dt.getUTCMonth() + 1).padStart(2, '0')
    + '-' + String(dt.getUTCDate()).padStart(2, '0');
}
KanbanLogic._plusDaysISO = _plusDaysISO;

// laneForTask(task, todayISO) -> 'doing'|'overdue'|'today'|'week'|'later'|'done'.
// A 'doing' task is pinned to the Doing lane regardless of its due date (it only
// falls out of Doing when its state changes). Done is state=='done' (or the
// legacy done:true boolean, for defensiveness). Everything else buckets by due
// date exactly like tasks_store.filter_tasks' due=overdue/today/week semantics.
KanbanLogic.laneForTask = function (task, todayISO) {
  if (!task) return 'later';
  const state = task.state || (task.done ? 'done' : 'open');
  if (state === 'done') return 'done';
  if (state === 'doing') return 'doing';
  const due = task.due;
  if (!due) return 'later';
  if (due < todayISO) return 'overdue';
  if (due === todayISO) return 'today';
  if (due <= _plusDaysISO(todayISO, 6)) return 'week';
  return 'later';
};

// dropActionFor(task, lane, todayISO) -> the mutation descriptor for dragging
// `task` onto `lane`, or null when the drop is rejected (Overdue is never a
// drop target -- you can't make something overdue by dragging).
//
// Descriptor shape (only the relevant keys are present):
//   { state: 'doing' }                  Doing lane      -> POST .../tasks/state
//   { done: true }                      Done lane       -> POST .../tasks/toggle
//                                                           (the EXISTING toggle
//                                                           path, not the state
//                                                           endpoint -- toggle
//                                                           already lands cleanly
//                                                           on done from any state)
//   { due: <ISO>|null, state?: 'open' } Today/Week/Later -> PATCH edit endpoint
//                                                           for `due` (null means
//                                                           "clear it"), PLUS a
//                                                           state-endpoint call to
//                                                           reset state to 'open'
//                                                           IF the task's current
//                                                           state isn't already
//                                                           'open' (covers both a
//                                                           Doing->due-lane drag
//                                                           AND a Done->due-lane
//                                                           drag -- dragging a
//                                                           finished task back
//                                                           into an active lane
//                                                           un-completes it too).
KanbanLogic.dropActionFor = function (task, lane, todayISO) {
  if (lane === 'overdue') return null;
  if (lane === 'doing') return { state: 'doing' };
  if (lane === 'done') return { done: true };
  const state = task && (task.state || (task.done ? 'done' : 'open'));
  const needsOpen = state != null && state !== 'open';
  if (lane === 'today') {
    const out = { due: todayISO };
    if (needsOpen) out.state = 'open';
    return out;
  }
  if (lane === 'week') {
    const out = { due: _plusDaysISO(todayISO, 6) };
    if (needsOpen) out.state = 'open';
    return out;
  }
  if (lane === 'later') {
    const out = { due: null };
    if (needsOpen) out.state = 'open';
    return out;
  }
  return null;
};

// Dual export: browser global + CommonJS (Node test). No ES-module syntax so
// the plain <script> load stays valid in the browser.
if (typeof window !== 'undefined') { window.KanbanLogic = KanbanLogic; }
if (typeof module !== 'undefined' && module.exports) { module.exports = KanbanLogic; }
