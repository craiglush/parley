// CodeMirror 6 + Obsidian-style markdown editor glue, bundled offline for the Meeting/Notes PWA.
// Exposes window.MoonbaseEditor with:
//   create(opts) -> handle { getValue, setValue, applyFormat, focus, destroy, dom }
//   renderMarkdown(md) -> HTML string (via marked) for the read preview pane.
//
// Colors/fonts come from the app's existing (UNPREFIXED) theme tokens — --text-primary, --accent,
// --bg-card, --border, --font-ui, --font-mono, --radius, plus two editor tokens (--editor-selection,
// --editor-active) defined in index.html — so the editor follows the page light/dark theme automatically.
//
// SECURITY NOTE: every `.exec(...)` / `.test(...)` call below is RegExp.prototype.exec on a string —
// there is NO child_process / shell execution anywhere in this file.

import './draw.mjs';
import { RangeSetBuilder, Compartment } from '@codemirror/state';
import {
  EditorView, keymap, drawSelection, highlightActiveLine,
  highlightSpecialChars, placeholder, ViewPlugin, Decoration, WidgetType,
} from '@codemirror/view';
import {
  defaultKeymap, history, historyKeymap, indentWithTab,
} from '@codemirror/commands';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import {
  syntaxHighlighting, HighlightStyle, defaultHighlightStyle, indentOnInput, bracketMatching,
} from '@codemirror/language';
import { tags as t } from '@lezer/highlight';
import {
  autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
} from '@codemirror/autocomplete';
import { marked } from 'marked';
import DOMPurify from 'dompurify';

// ---------------------------------------------------------------------------
// Syntax highlighting: heading sizes + emphasis, mapped to Moonbase tokens.
// ---------------------------------------------------------------------------
const mbHighlight = HighlightStyle.define([
  { tag: t.heading1, fontSize: '1.65em', fontWeight: '700', color: 'var(--text-primary)', lineHeight: '1.3' },
  { tag: t.heading2, fontSize: '1.38em', fontWeight: '700', color: 'var(--text-primary)' },
  { tag: t.heading3, fontSize: '1.18em', fontWeight: '600', color: 'var(--text-primary)' },
  { tag: t.heading4, fontSize: '1.06em', fontWeight: '600', color: 'var(--text-primary)' },
  { tag: [t.heading5, t.heading6], fontWeight: '600', color: 'var(--text-secondary)' },
  { tag: t.strong, fontWeight: '700', color: 'var(--text-primary)' },
  { tag: t.emphasis, fontStyle: 'italic' },
  { tag: t.strikethrough, textDecoration: 'line-through', opacity: '0.65' },
  { tag: t.monospace, fontFamily: 'var(--font-mono)', color: 'var(--accent)', fontSize: '0.92em' },
  { tag: [t.link, t.url], color: 'var(--accent)', textDecoration: 'underline' },
  { tag: t.quote, color: 'var(--text-secondary)', fontStyle: 'italic' },
  { tag: t.list, color: 'var(--accent)' },
  { tag: [t.processingInstruction, t.meta], color: 'var(--text-muted)' },
  { tag: t.contentSeparator, color: 'var(--border)' },
]);

// ---------------------------------------------------------------------------
// Editor chrome theme (fonts, spacing, scrollbars) via CSS vars.
// ---------------------------------------------------------------------------
const mbTheme = EditorView.theme({
  '&': {
    color: 'var(--text-primary)',
    backgroundColor: 'transparent',
    height: '100%',
    fontSize: 'var(--editor-font-size, 15.5px)',
  },
  '.cm-scroller': {
    fontFamily: 'var(--font-ui)',
    lineHeight: '1.75',
    overflow: 'auto',
    padding: '4px 0',
  },
  '.cm-content': {
    caretColor: 'var(--accent)',
    maxWidth: 'var(--editor-measure, 780px)',
    margin: '0 auto',
    padding: '10px 30px 45vh',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--accent)', borderLeftWidth: '2px' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection': {
    backgroundColor: 'var(--editor-selection, rgba(212,160,57,0.22))',
  },
  '.cm-activeLine': { backgroundColor: 'var(--editor-active, rgba(255,255,255,0.03))' },
  '.cm-line': { padding: '0 2px' },
  '.cm-task-checkbox': {
    appearance: 'none', WebkitAppearance: 'none',
    width: '1.05em', height: '1.05em', margin: '0 0.5em 0 0',
    verticalAlign: '-0.18em', cursor: 'pointer',
    border: '2px solid var(--accent)', borderRadius: '4px',
    background: 'transparent', position: 'relative', flex: '0 0 auto',
    transition: 'background 0.12s ease',
  },
  '.cm-task-checkbox:checked': { background: 'var(--accent)' },
  '.cm-task-checkbox:checked::after': {
    content: '""', position: 'absolute', left: '3px', top: '0px',
    width: '4px', height: '8px', border: 'solid var(--bg-primary)',
    borderWidth: '0 2px 2px 0', transform: 'rotate(45deg)',
  },
  '.cm-task-done': { color: 'var(--text-muted)', textDecoration: 'line-through' },
  '.cm-wikilink': {
    color: 'var(--accent)', cursor: 'pointer',
    textDecoration: 'underline', textDecorationStyle: 'dotted', textUnderlineOffset: '2px',
  },
  '.cm-wikilink-missing': { color: 'var(--red)' },
  '.cm-extlink': { color: 'var(--accent)', cursor: 'pointer' },
  '.cm-tag-pill': {
    color: 'var(--accent)', background: 'var(--accent-dim)',
    borderRadius: '5px', padding: '0 4px',
  },
  '.cm-tooltip': {
    background: 'var(--bg-card)', border: '1px solid var(--border)',
    borderRadius: 'var(--radius-sm, 6px)', color: 'var(--text-primary)',
    boxShadow: 'var(--shadow, 0 8px 24px rgba(0,0,0,0.4))', overflow: 'hidden',
  },
  '.cm-tooltip.cm-tooltip-autocomplete > ul': { fontFamily: 'var(--font-ui)', maxHeight: '14em' },
  '.cm-tooltip.cm-tooltip-autocomplete > ul > li': { padding: '4px 10px' },
  '.cm-tooltip-autocomplete ul li[aria-selected]': {
    background: 'var(--accent)', color: 'var(--bg-primary)',
  },
  '.cm-completionDetail': { color: 'var(--text-muted)', fontStyle: 'italic', marginLeft: '0.5em' },
});

// ---------------------------------------------------------------------------
// Checkbox widget: replaces `[ ]` / `[x]` with a real checkbox; click toggles source.
// ---------------------------------------------------------------------------
class CheckboxWidget extends WidgetType {
  constructor(checked, innerPos) { super(); this.checked = checked; this.innerPos = innerPos; }
  eq(o) { return o.checked === this.checked && o.innerPos === this.innerPos; }
  toDOM(view) {
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = this.checked;
    box.className = 'cm-task-checkbox';
    box.setAttribute('aria-label', 'Toggle task');
    box.addEventListener('mousedown', (e) => e.preventDefault());
    box.addEventListener('click', (e) => {
      e.preventDefault();
      const ch = this.checked ? ' ' : 'x';
      view.dispatch({ changes: { from: this.innerPos, to: this.innerPos + 1, insert: ch } });
    });
    return box;
  }
  ignoreEvent() { return false; }
}

const CHECKBOX_RE = /^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\])/;
const WIKILINK_RE = /\[\[([^\]\n]+?)\]\]/g;
const URL_RE = /\bhttps?:\/\/[^\s<>)\]]+/g;
const TAG_RE = /(^|\s)(#[A-Za-z0-9_][A-Za-z0-9_\/-]*)/g;

// Combined decoration plugin: checkboxes, done-line styling, wiki-links, urls, tags.
function decorationPlugin(opts) {
  const knownTargets = opts.knownTargets || (() => new Set());
  return ViewPlugin.fromClass(class {
    constructor(view) { this.decorations = this.build(view); }
    update(u) { if (u.docChanged || u.viewportChanged || u.selectionSet) this.decorations = this.build(u.view); }
    build(view) {
      const widgets = [];
      const known = knownTargets();
      for (const { from, to } of view.visibleRanges) {
        let pos = from;
        while (pos <= to) {
          const line = view.state.doc.lineAt(pos);
          const text = line.text;
          const cb = CHECKBOX_RE.exec(text);
          if (cb) {
            const bracketFrom = line.from + cb[1].length - 1; // index of '['
            const innerPos = bracketFrom + 1;
            const checked = cb[2].toLowerCase() === 'x';
            widgets.push({ from: bracketFrom, to: bracketFrom + 3, side: -1,
              deco: Decoration.replace({ widget: new CheckboxWidget(checked, innerPos) }) });
            if (checked) {
              const restFrom = bracketFrom + 3;
              if (line.to > restFrom) {
                widgets.push({ from: restFrom, to: line.to, side: 1,
                  deco: Decoration.mark({ class: 'cm-task-done' }) });
              }
            }
          }
          let m;
          WIKILINK_RE.lastIndex = 0;
          while ((m = WIKILINK_RE.exec(text))) {
            const s = line.from + m.index;
            const e = s + m[0].length;
            const target = m[1].split('|')[0].split('#')[0].trim();
            const missing = known.size > 0 && !known.has(target.toLowerCase());
            widgets.push({ from: s, to: e, side: 2,
              deco: Decoration.mark({ class: 'cm-wikilink' + (missing ? ' cm-wikilink-missing' : ''),
                attributes: { 'data-wikilink': target } }) });
          }
          URL_RE.lastIndex = 0;
          while ((m = URL_RE.exec(text))) {
            const s = line.from + m.index;
            const e = s + m[0].length;
            widgets.push({ from: s, to: e, side: 3,
              deco: Decoration.mark({ class: 'cm-extlink', attributes: { 'data-href': m[0] } }) });
          }
          TAG_RE.lastIndex = 0;
          while ((m = TAG_RE.exec(text))) {
            const s = line.from + m.index + m[1].length;
            const e = s + m[2].length;
            widgets.push({ from: s, to: e, side: 4,
              deco: Decoration.mark({ class: 'cm-tag-pill', attributes: { 'data-tag': m[2] } }) });
          }
          pos = line.to + 1;
        }
      }
      widgets.sort((a, b) => a.from - b.from || a.side - b.side);
      const builder = new RangeSetBuilder();
      for (const w of widgets) builder.add(w.from, w.to, w.deco);
      return builder.finish();
    }
  }, {
    decorations: (v) => v.decorations,
  });
}

// Ctrl/Cmd+click follows links; plain click on a checkbox is handled by the widget.
function linkClickHandlers(opts) {
  return EditorView.domEventHandlers({
    mousedown(e) {
      const el = e.target;
      if (!el || !el.closest) return false;
      const follow = e.metaKey || e.ctrlKey;
      if (!follow) return false;
      const wl = el.closest('.cm-wikilink');
      if (wl) { e.preventDefault(); opts.onWikiLink && opts.onWikiLink(wl.getAttribute('data-wikilink')); return true; }
      const ext = el.closest('.cm-extlink');
      if (ext) {
        e.preventDefault();
        const href = ext.getAttribute('data-href');
        if (opts.onExtLink) opts.onExtLink(href); else window.open(href, '_blank', 'noopener');
        return true;
      }
      const tg = el.closest('.cm-tag-pill');
      if (tg) { e.preventDefault(); opts.onTag && opts.onTag(tg.getAttribute('data-tag')); return true; }
      return false;
    },
  });
}

// [[wiki-link]] autocomplete sourcing note titles from the app.
function wikiCompletions(getTargets) {
  return (ctx) => {
    const before = ctx.matchBefore(/\[\[[^\]\n]*/);
    if (!before) return null;
    if (before.from + 2 > ctx.pos) return null;
    const targets = getTargets ? getTargets() : [];
    const options = (targets || []).map((tt) => {
      const label = (tt && tt.label) ? tt.label : tt;
      return {
        label,
        detail: (tt && tt.detail) ? tt.detail : '',
        apply: (view, completion, from, to) => {
          const insert = completion.label + ']]';
          view.dispatch({ changes: { from, to, insert }, selection: { anchor: from + insert.length } });
        },
      };
    });
    return { from: before.from + 2, options, validFor: /[^\]\n]*/ };
  };
}

// Markdown-aware Enter: continue lists / checkboxes; empty item terminates the list.
function continueListKeymap() {
  return keymap.of([{
    key: 'Enter',
    run: (view) => {
      const { state } = view;
      const sel = state.selection.main;
      if (!sel.empty) return false;
      const line = state.doc.lineAt(sel.head);
      const m = /^(\s*)([-*+]|\d+[.)])(\s+)(\[[ xX]\]\s+)?/.exec(line.text);
      if (!m) return false;
      const contentStart = line.from + m[0].length;
      if (sel.head < contentStart) return false;
      if (line.text.trim() === m[0].trim()) {
        view.dispatch({ changes: { from: line.from, to: line.to, insert: '' }, selection: { anchor: line.from } });
        return true;
      }
      let marker = m[2];
      if (/\d+[.)]/.test(marker)) {
        const n = parseInt(marker, 10) + 1;
        marker = String(n) + marker.replace(/\d+/, '');
      }
      const cont = '\n' + m[1] + marker + m[3] + (m[4] ? '[ ] ' : '');
      view.dispatch({ changes: { from: sel.head, insert: cont }, selection: { anchor: sel.head + cont.length } });
      return true;
    },
  }]);
}

// ---------------------------------------------------------------------------
// Toolbar formatting commands.
// ---------------------------------------------------------------------------
function applyFormat(view, cmd, arg) {
  if (!view) return;
  const { state } = view;
  const sel = state.selection.main;
  const sliced = state.sliceDoc(sel.from, sel.to);

  const wrap = (before, after, ph) => {
    const inner = sliced || ph || '';
    const insert = before + inner + after;
    view.dispatch({
      changes: { from: sel.from, to: sel.to, insert },
      selection: { anchor: sel.from + before.length, head: sel.from + before.length + inner.length },
    });
    view.focus();
  };

  const eachLinePrefix = (prefix, toggle = true) => {
    const fromLine = state.doc.lineAt(sel.from);
    const toLine = state.doc.lineAt(sel.to);
    const changes = [];
    for (let n = fromLine.number; n <= toLine.number; n++) {
      const ln = state.doc.line(n);
      const has = ln.text.startsWith(prefix);
      if (toggle && has) changes.push({ from: ln.from, to: ln.from + prefix.length, insert: '' });
      else if (!has) changes.push({ from: ln.from, to: ln.from, insert: prefix });
    }
    view.dispatch({ changes });
    view.focus();
  };

  switch (cmd) {
    case 'bold': wrap('**', '**', 'bold text'); break;
    case 'italic': wrap('*', '*', 'italic text'); break;
    case 'strike': wrap('~~', '~~', 'text'); break;
    case 'code': wrap('`', '`', 'code'); break;
    case 'codeblock': wrap('```\n', '\n```', 'code'); break;
    case 'h1': eachLinePrefix('# '); break;
    case 'h2': eachLinePrefix('## '); break;
    case 'h3': eachLinePrefix('### '); break;
    case 'quote': eachLinePrefix('> '); break;
    case 'ul': eachLinePrefix('- '); break;
    case 'ol': eachLinePrefix('1. '); break;
    case 'checkbox': eachLinePrefix('- [ ] '); break;
    case 'link': {
      const url = arg || 'https://';
      const label = sliced || 'link';
      const insert = `[${label}](${url})`;
      view.dispatch({ changes: { from: sel.from, to: sel.to, insert }, selection: { anchor: sel.from + 1, head: sel.from + 1 + label.length } });
      view.focus();
      break;
    }
    case 'wikilink': {
      const insert = `[[${sliced}`;
      view.dispatch({ changes: { from: sel.from, to: sel.to, insert: insert + ']]' }, selection: { anchor: sel.from + 2, head: sel.from + 2 + sliced.length } });
      view.focus();
      break;
    }
    case 'hr': {
      const ln = state.doc.lineAt(sel.from);
      view.dispatch({ changes: { from: ln.to, insert: '\n\n---\n' } });
      view.focus();
      break;
    }
    default: break;
  }
}

// ---------------------------------------------------------------------------
// marked config for the read preview pane.
// ---------------------------------------------------------------------------
marked.setOptions({ gfm: true, breaks: false });

const IMG_EXT = /\.(png|jpe?g|gif|webp|svg)$/i;

function renderMarkdown(md) {
  // ![[name.ext]] -> <img> (image) or <a> (other), BEFORE wiki-link step.
  let src = String(md || '');
  src = src.replace(/!\[\[([^\]\n|]+)\]\]/g, (whole, name) => {
    const f = name.trim();
    const url = '/api/notes/attachments/' + encodeURIComponent(f);
    return IMG_EXT.test(f)
      ? `<img class="note-embed" src="${url}" alt="${f.replace(/"/g, '&quot;')}">`
      : `<a href="${url}" target="_blank" rel="noopener">${f.replace(/[<>]/g, '')}</a>`;
  });
  // Rewrite relative attachment hrefs so they resolve to /api/notes/attachments/<name>.
  src = src.replace(/\]\(attachments\/([^)\s]+)\)/g, '](/api/notes/attachments/$1)');
  // [[wiki-links]] -> clickable anchors, BEFORE markdown parse so marked escapes the rest.
  const withWiki = src.replace(/\[\[([^\]\n|]+)(\|[^\]\n]+)?\]\]/g, (whole, target, alias) => {
    const label = alias ? alias.slice(1) : target;
    const safe = String(target).replace(/"/g, '&quot;').trim();
    const safeLabel = String(label).replace(/[<>]/g, (c) => (c === '<' ? '&lt;' : '&gt;'));
    return `<a href="#" class="note-wikilink" data-wikilink="${safe}">${safeLabel}</a>`;
  });
  const html = marked.parse(withWiki);
  // Sanitize: notes/AI summaries are user-trusted but marked passes raw HTML through,
  // so DOMPurify removes any <script>/onerror/etc. Allow data-wikilink + class for nav.
  // ADD_TAGS: ['img'] so embed <img class="note-embed"> survives sanitization.
  return DOMPurify.sanitize(html, { ADD_ATTR: ['data-wikilink', 'target'], ADD_TAGS: ['img'] });
}

// ---------------------------------------------------------------------------
// Public factory.
// ---------------------------------------------------------------------------
function create(opts = {}) {
  const parent = opts.parent;
  const onChange = opts.onChange || (() => {});

  const extensions = [
    history(),
    drawSelection(),
    highlightActiveLine(),
    highlightSpecialChars(),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    EditorView.lineWrapping,
    markdown({ base: markdownLanguage, addKeymap: true }),
    syntaxHighlighting(mbHighlight),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    decorationPlugin(opts),
    linkClickHandlers(opts),
    autocompletion({ override: opts.getWikiTargets ? [wikiCompletions(opts.getWikiTargets)] : undefined }),
    continueListKeymap(),
    keymap.of([
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...historyKeymap,
      ...completionKeymap,
      indentWithTab,
      { key: 'Mod-b', run: (v) => (applyFormat(v, 'bold'), true) },
      { key: 'Mod-i', run: (v) => (applyFormat(v, 'italic'), true) },
      { key: 'Mod-k', run: (v) => { opts.onLinkShortcut ? opts.onLinkShortcut(v) : applyFormat(v, 'link'); return true; } },
    ]),
    placeholder(opts.placeholder || 'Start writing…   [[ links · - [ ] tasks · # headings'),
    mbTheme,
    EditorView.updateListener.of((u) => { if (u.docChanged) onChange(u.state.doc.toString()); }),
  ];

  const view = new EditorView({ doc: opts.doc || '', extensions, parent });

  return {
    view,
    dom: view.dom,
    getValue: () => view.state.doc.toString(),
    setValue: (text) => view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text || '' },
      selection: { anchor: 0 },
    }),
    replaceRange: (from, to, text) => view.dispatch({
      changes: { from, to, insert: text || '' },
    }),
    applyFormat: (cmd, arg) => applyFormat(view, cmd, arg),
    insertAtCursor: (text) => {
      const sel = view.state.selection.main;
      view.dispatch({ changes: { from: sel.from, to: sel.to, insert: text },
                      selection: { anchor: sel.from + text.length } });
      view.focus();
    },
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}

const MoonbaseEditor = { create, renderMarkdown, applyFormat };
if (typeof window !== 'undefined') window.MoonbaseEditor = MoonbaseEditor;
export default MoonbaseEditor;
export { create, renderMarkdown };
