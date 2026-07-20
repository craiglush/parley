// Pure-logic unit tests for the kanban board's lane assignment + drop mapping.
// Run with: node --test tests/js/kanban_logic.test.mjs (no npm deps)
import { test } from 'node:test';
import assert from 'node:assert';
import K from '../../static/kanban-logic.js';

const TODAY = '2026-07-17';

test('laneForTask: doing state pins to doing regardless of due date', () => {
  assert.strictEqual(K.laneForTask({ state: 'doing', due: '2020-01-01' }, TODAY), 'doing');
  assert.strictEqual(K.laneForTask({ state: 'doing', due: null }, TODAY), 'doing');
});

test('laneForTask: done state (or legacy done:true) -> done', () => {
  assert.strictEqual(K.laneForTask({ state: 'done', due: TODAY }, TODAY), 'done');
  assert.strictEqual(K.laneForTask({ done: true, due: TODAY }, TODAY), 'done');
});

test('laneForTask: overdue / today / week / later boundaries', () => {
  assert.strictEqual(K.laneForTask({ state: 'open', due: '2026-07-16' }, TODAY), 'overdue');
  assert.strictEqual(K.laneForTask({ state: 'open', due: '2026-07-17' }, TODAY), 'today');
  assert.strictEqual(K.laneForTask({ state: 'open', due: '2026-07-23' }, TODAY), 'week');   // today+6
  assert.strictEqual(K.laneForTask({ state: 'open', due: '2026-07-24' }, TODAY), 'later');  // today+7
});

test('laneForTask: no due date -> later; missing task -> later', () => {
  assert.strictEqual(K.laneForTask({ state: 'open', due: null }, TODAY), 'later');
  assert.strictEqual(K.laneForTask(null, TODAY), 'later');
});

test('laneForTask: state absent falls back via done bool (backward compat)', () => {
  assert.strictEqual(K.laneForTask({ due: '2026-07-10' }, TODAY), 'overdue');
});

test('dropActionFor: Overdue is never a valid drop target', () => {
  assert.strictEqual(K.dropActionFor({ state: 'open' }, 'overdue', TODAY), null);
});

test('dropActionFor: Doing lane -> state:doing, regardless of current state', () => {
  assert.deepStrictEqual(K.dropActionFor({ state: 'open' }, 'doing', TODAY), { state: 'doing' });
  assert.deepStrictEqual(K.dropActionFor({ state: 'done' }, 'doing', TODAY), { state: 'doing' });
});

test('dropActionFor: Done lane -> done:true (existing toggle path)', () => {
  assert.deepStrictEqual(K.dropActionFor({ state: 'doing' }, 'done', TODAY), { done: true });
});

test('dropActionFor: Today from an open task -> due only, no state change', () => {
  assert.deepStrictEqual(K.dropActionFor({ state: 'open' }, 'today', TODAY), { due: TODAY });
});

test('dropActionFor: Today from a doing task -> due + state:open', () => {
  assert.deepStrictEqual(K.dropActionFor({ state: 'doing' }, 'today', TODAY), { due: TODAY, state: 'open' });
});

test('dropActionFor: This Week -> due = today+6', () => {
  assert.deepStrictEqual(K.dropActionFor({ state: 'open' }, 'week', TODAY), { due: '2026-07-23' });
  assert.deepStrictEqual(K.dropActionFor({ state: 'doing' }, 'week', TODAY), { due: '2026-07-23', state: 'open' });
});

test('dropActionFor: Later clears due, resets state if not already open', () => {
  assert.deepStrictEqual(K.dropActionFor({ state: 'open' }, 'later', TODAY), { due: null });
  assert.deepStrictEqual(K.dropActionFor({ state: 'doing' }, 'later', TODAY), { due: null, state: 'open' });
  assert.deepStrictEqual(K.dropActionFor({ state: 'done' }, 'later', TODAY), { due: null, state: 'open' });
});

test('_plusDaysISO: crosses a month boundary correctly', () => {
  assert.strictEqual(K._plusDaysISO('2026-07-28', 6), '2026-08-03');
});
