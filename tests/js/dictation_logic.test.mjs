// Pure-logic unit test for dictation text-merge and swap-safety helpers.
// Run with:  node --test tests/js/dictation_logic.test.mjs   (no npm deps)
import { test } from 'node:test';
import assert from 'node:assert';

import pkg from '../../static/dictation-logic.js';
const { mergeDictationText, findUnchangedSpan } = pkg;

test('mergeDictationText: empty existing returns the transcript alone', () => {
  assert.strictEqual(mergeDictationText('', 'call Dave tomorrow'), 'call Dave tomorrow');
});

test('mergeDictationText: whitespace-only existing treated as empty', () => {
  assert.strictEqual(mergeDictationText('   ', 'call Dave tomorrow'), 'call Dave tomorrow');
});

test('mergeDictationText: non-empty existing joins with a single space', () => {
  assert.strictEqual(mergeDictationText('chase John', 'about the invoice'), 'chase John about the invoice');
});

test('mergeDictationText: trims both sides before joining', () => {
  assert.strictEqual(mergeDictationText('  chase John  ', '  about the invoice  '), 'chase John about the invoice');
});

test('findUnchangedSpan: single unchanged occurrence returns its index', () => {
  const r = findUnchangedSpan('Meeting notes.\n\nraw dictated text\n\nMore.', 'raw dictated text');
  assert.strictEqual(r.index, 'Meeting notes.\n\n'.length);
  assert.strictEqual(r.ambiguous, false);
});

test('findUnchangedSpan: zero occurrences (hand-edited away) returns -1', () => {
  const r = findUnchangedSpan('Meeting notes.\n\nsomething the user typed instead\n\nMore.', 'raw dictated text');
  assert.strictEqual(r.index, -1);
  assert.strictEqual(r.ambiguous, false);
});

test('findUnchangedSpan: two or more occurrences is ambiguous', () => {
  const r = findUnchangedSpan('raw dictated text and later raw dictated text again', 'raw dictated text');
  assert.strictEqual(r.index, -1);
  assert.strictEqual(r.ambiguous, true);
});
