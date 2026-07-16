// Pure-logic unit tests for the in-meeting capture-notes helpers.
// Run with:  node --test tests/js/capture_notes_logic.test.mjs
// (explicit FILE path — Node-on-Windows directory runs don't discover .mjs)
import { test } from 'node:test';
import assert from 'node:assert';

// capture-notes-logic.js is a plain CommonJS script (also a browser global);
// default-import the module.exports namespace, like notes_sync_logic.test.mjs.
import L from '../../static/capture-notes-logic.js';

test('captureNoteTitle: meeting title wins over context', () => {
  assert.strictEqual(
    L.captureNoteTitle('Sprint Planning', 'AWS review', '2026-07-15T10:30:00Z'),
    'Sprint Planning — notes (2026-07-15)');
});

test('captureNoteTitle: context is the fallback when title is blank/whitespace', () => {
  assert.strictEqual(
    L.captureNoteTitle('', 'AWS review', '2026-07-15T10:30:00Z'),
    'AWS review — notes (2026-07-15)');
  assert.strictEqual(
    L.captureNoteTitle('   ', 'AWS review', '2026-07-15T10:30:00Z'),
    'AWS review — notes (2026-07-15)');
});

test('captureNoteTitle: bare fallback is "Meeting — notes (YYYY-MM-DD)" (null/undefined safe)', () => {
  assert.strictEqual(
    L.captureNoteTitle('', '', '2026-07-15T10:30:00Z'),
    'Meeting — notes (2026-07-15)');
  assert.strictEqual(
    L.captureNoteTitle(null, undefined, '2026-01-02T00:00:00Z'),
    'Meeting — notes (2026-01-02)');
});

test('captureNoteTitle: trims title/context before use', () => {
  assert.strictEqual(
    L.captureNoteTitle('  Sprint Planning  ', '', '2026-07-15T10:30:00Z'),
    'Sprint Planning — notes (2026-07-15)');
});

test('captureNoteTitle: date is the first 10 chars of the ISO string', () => {
  assert.strictEqual(
    L.captureNoteTitle('T', '', '2026-12-31T23:59:59.999Z'),
    'T — notes (2026-12-31)');
});

test('isTempNoteId: temp true; real/empty/null/undefined false', () => {
  assert.strictEqual(L.isTempNoteId('n_local_ab12cd34ef'), true);
  assert.strictEqual(L.isTempNoteId('n_1a2b3c4d5e6f'), false);
  assert.strictEqual(L.isTempNoteId(''), false);
  assert.strictEqual(L.isTempNoteId(null), false);
  assert.strictEqual(L.isTempNoteId(undefined), false);
});
