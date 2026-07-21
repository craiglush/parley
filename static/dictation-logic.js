/* Pure, dependency-free helpers for voice dictation.
   Dual export: browser global (window.DictationLogic) + CommonJS (Node tests).
   Mirrors static/notes-analysis-logic.js packaging. No DOM, no IDB, no network. */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DictationLogic = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function mergeDictationText(existing, transcript) {
    var e = (existing == null ? '' : String(existing)).trim();
    var t = (transcript == null ? '' : String(transcript)).trim();
    if (!e) return t;
    if (!t) return e;
    return e + ' ' + t;
  }

  function findUnchangedSpan(fullText, span) {
    if (!span) return { index: -1, ambiguous: false };
    var first = fullText.indexOf(span);
    if (first === -1) return { index: -1, ambiguous: false };
    var second = fullText.indexOf(span, first + 1);
    if (second !== -1) return { index: -1, ambiguous: true };
    return { index: first, ambiguous: false };
  }

  return { mergeDictationText: mergeDictationText, findUnchangedSpan: findUnchangedSpan };
});
