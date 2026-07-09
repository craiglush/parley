// Pure-logic unit test for the offline upload queue selection.
// Run with:  node --test tests/js/queue_logic.test.mjs   (no npm deps)
import { test } from 'node:test';
import assert from 'node:assert';

// queue-logic.js is a plain CommonJS script (also loaded as a browser global);
// default-import then destructure so the interop works regardless of how Node
// resolves the named export.
import pkg from '../../static/queue-logic.js';
const { selectQueuedSessions } = pkg;

test('selects only queued===true sessions', () => {
  const metas = [
    { k: 'a', queued: true, startedAt: 1 },
    { k: 'b', queued: false, startedAt: 2 },
    { k: 'c', startedAt: 3 },                 // recovered draft, no queued flag
    { k: 'd', queued: true, startedAt: 4 },
  ];
  const out = selectQueuedSessions(metas, null);
  assert.deepStrictEqual(out.map(m => m.k), ['d', 'a']);
});

test('excludes recovered drafts (missing or queued:false)', () => {
  const metas = [
    { k: 'x', startedAt: 10 },
    { k: 'y', queued: false, startedAt: 20 },
  ];
  assert.deepStrictEqual(selectQueuedSessions(metas, null), []);
});

test('empty / non-array input returns []', () => {
  assert.deepStrictEqual(selectQueuedSessions([], null), []);
  assert.deepStrictEqual(selectQueuedSessions(undefined, null), []);
  assert.deepStrictEqual(selectQueuedSessions(null, null), []);
});

test('orders newest-first by startedAt', () => {
  const metas = [
    { k: 'old', queued: true, startedAt: 100 },
    { k: 'new', queued: true, startedAt: 300 },
    { k: 'mid', queued: true, startedAt: 200 },
  ];
  assert.deepStrictEqual(selectQueuedSessions(metas, null).map(m => m.k), ['new', 'mid', 'old']);
});

test('excludes the actively-recording session (k === activeSid)', () => {
  const metas = [
    { k: 'recording-now', queued: true, startedAt: 5 },
    { k: 'queued-past', queued: true, startedAt: 4 },
  ];
  const out = selectQueuedSessions(metas, 'recording-now');
  assert.deepStrictEqual(out.map(m => m.k), ['queued-past']);
});

test('excludes sids already uploaded this session (excludeSids Set or array)', () => {
  const metas = [
    { k: 'sent', queued: true, startedAt: 3 },      // 202'd but local delete may have failed
    { k: 'fresh', queued: true, startedAt: 2 },
  ];
  // Set form
  assert.deepStrictEqual(
    selectQueuedSessions(metas, null, new Set(['sent'])).map(m => m.k), ['fresh']);
  // Array form
  assert.deepStrictEqual(
    selectQueuedSessions(metas, null, ['sent']).map(m => m.k), ['fresh']);
  // Absent/invalid excludeSids is a no-op
  assert.deepStrictEqual(
    selectQueuedSessions(metas, null).map(m => m.k), ['sent', 'fresh']);
});

test('does not mutate the input array', () => {
  const metas = [
    { k: 'a', queued: true, startedAt: 1 },
    { k: 'b', queued: true, startedAt: 2 },
  ];
  const before = metas.map(m => m.k);
  selectQueuedSessions(metas, null);
  assert.deepStrictEqual(metas.map(m => m.k), before);
});
