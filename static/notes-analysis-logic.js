/* Pure, dependency-free formatter for note-analysis results.
   Dual export: browser global (window.NotesAnalysis) + CommonJS (Node tests).
   Mirrors static/queue-logic.js packaging. No DOM, no IDB. The caller inserts the
   returned Markdown into the CodeMirror editor as plain text. */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NotesAnalysis = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function _section(title, items, bullet) {
    if (!Array.isArray(items)) return '';
    var lines = items
      .filter(function (x) { return x != null && String(x).trim() !== ''; })
      .map(function (x) { return bullet + String(x).trim(); });
    if (lines.length === 0) return '';
    return '**' + title + '**\n' + lines.join('\n');
  }

  function formatAnalysisMarkdown(result) {
    if (!result || typeof result !== 'object') return '';
    var blocks = [];
    var summary = (result.summary == null ? '' : String(result.summary)).trim();
    if (summary) blocks.push('**Summary**\n' + summary);
    var kp = _section('Key points', result.key_points, '- ');
    if (kp) blocks.push(kp);
    var ai = _section('Action items', result.action_items, '- [ ] ');
    if (ai) blocks.push(ai);
    var ins = _section('Insights', result.insights, '- ');
    if (ins) blocks.push(ins);
    if (blocks.length === 0) return '';
    return '## Analysis\n\n' + blocks.join('\n\n') + '\n';
  }

  return { formatAnalysisMarkdown: formatAnalysisMarkdown };
});
