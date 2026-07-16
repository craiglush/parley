// Pure-logic unit test for note-analysis markdown formatting.
// Run with:  node --test tests/js/notes_analysis_logic.test.mjs   (no npm deps)
import { test } from 'node:test';
import assert from 'node:assert';

import pkg from '../../static/notes-analysis-logic.js';
const { formatAnalysisMarkdown } = pkg;

test('renders all four sections; action items become checkboxes', () => {
  const md = formatAnalysisMarkdown({
    summary: 'It is about X.',
    key_points: ['point one', 'point two'],
    action_items: ['email Bob'],
    insights: ['risk: Y'],
  });
  assert.ok(md.startsWith('## Analysis'));
  assert.ok(md.includes('**Summary**\nIt is about X.'));
  assert.ok(md.includes('- point one'));
  assert.ok(md.includes('- [ ] email Bob'));
  assert.ok(md.includes('- risk: Y'));
});

test('omits empty / missing sections', () => {
  const md = formatAnalysisMarkdown({ summary: 'only summary', key_points: [], action_items: [], insights: [] });
  assert.ok(md.includes('**Summary**'));
  assert.ok(!md.includes('Key points'));
  assert.ok(!md.includes('Action items'));
});

test('returns empty string for empty / invalid input', () => {
  assert.strictEqual(formatAnalysisMarkdown({}), '');
  assert.strictEqual(formatAnalysisMarkdown(null), '');
  assert.strictEqual(formatAnalysisMarkdown({ summary: '   ', key_points: [] }), '');
});

test('coerces + trims list items and drops blanks', () => {
  const md = formatAnalysisMarkdown({ summary: 's', key_points: ['  a  ', '', null, 3], action_items: [], insights: [] });
  assert.ok(md.includes('- a'));
  assert.ok(md.includes('- 3'));
  assert.ok(!/-\s*\n/.test(md));  // no empty bullet lines
});
