// Pure sync-logic for the offline Notes mirror — shared by static/notes-sync.js
// (loaded as a browser global via a <script> tag) and the Node unit test
// (tests/js/notes_sync_logic.test.mjs). DOM / IndexedDB-free and dependency-free
// (no npm, no import): a self-contained SHA-1 so contentHash() is byte-identical
// to the server's hashlib.sha1(title + "\x00" + body). Mirrors static/queue-logic.js.

// --- self-contained SHA-1 over a Uint8Array -> lowercase hex ----------------
function _sha1Bytes(bytes) {
  const withOne = bytes.length + 1;
  const total = withOne + ((56 - (withOne % 64) + 64) % 64) + 8;
  const buf = new Uint8Array(total);
  buf.set(bytes);
  buf[bytes.length] = 0x80;
  const dv = new DataView(buf.buffer);
  const ml = bytes.length * 8;
  dv.setUint32(total - 8, Math.floor(ml / 0x100000000), false);
  dv.setUint32(total - 4, ml >>> 0, false);
  let h0 = 0x67452301, h1 = 0xEFCDAB89, h2 = 0x98BADCFE, h3 = 0x10325476, h4 = 0xC3D2E1F0;
  const w = new Uint32Array(80);
  for (let i = 0; i < total; i += 64) {
    for (let j = 0; j < 16; j++) w[j] = dv.getUint32(i + j * 4, false);
    for (let j = 16; j < 80; j++) { const n = w[j - 3] ^ w[j - 8] ^ w[j - 14] ^ w[j - 16]; w[j] = (n << 1) | (n >>> 31); }
    let a = h0, b = h1, c = h2, d = h3, e = h4;
    for (let j = 0; j < 80; j++) {
      let f, k;
      if (j < 20) { f = (b & c) | (~b & d); k = 0x5A827999; }
      else if (j < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1; }
      else if (j < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDC; }
      else { f = b ^ c ^ d; k = 0xCA62C1D6; }
      const t = (((a << 5) | (a >>> 27)) + f + e + k + w[j]) >>> 0;
      e = d; d = c; c = (b << 30) | (b >>> 2); b = a; a = t;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0; h4 = (h4 + e) >>> 0;
  }
  const hex = (n) => ('00000000' + (n >>> 0).toString(16)).slice(-8);
  return hex(h0) + hex(h1) + hex(h2) + hex(h3) + hex(h4);
}

const NotesSyncLogic = {};

// contentHash(title, body): the server's version token, sha1(title \0 body).
// The body is trimmed (leading/trailing whitespace only) before hashing to match
// the server's stripped-body hash (notes_store.content_hash, called from _record
// as content_hash(title, body.strip())) -- parse_frontmatter's round-trip mutates
// body whitespace on the server, so an unstripped hash would make every
// whitespace-padded local body look permanently dirty. Do not remove the trim.
// The title is NEVER trimmed -- only body. (JS String.trim() vs Python str.strip()
// differ only on exotic control chars like \x1c-\x1f; acceptable for markdown notes.)
NotesSyncLogic.contentHash = function (title, body) {
  const s = String(title == null ? '' : title) + '\x00' + String(body == null ? '' : body).trim();
  return _sha1Bytes(new TextEncoder().encode(s));
};

// selectDirtyNotes(records): the mirror entries needing a push, ordered
// create -> edit -> delete, oldest-local-edit first. Input is left untouched.
NotesSyncLogic.selectDirtyNotes = function (records) {
  const rank = { create: 0, edit: 1, delete: 2 };
  return (Array.isArray(records) ? records : [])
    .filter((r) => r && r.dirty && r.pendingOp && rank[r.pendingOp] != null)
    .slice()
    .sort((a, b) => {
      const ra = rank[a.pendingOp], rb = rank[b.pendingOp];
      if (ra !== rb) return ra - rb;
      return (a.localUpdated || 0) - (b.localUpdated || 0);
    });
};

// resolvePush(localRecord, serverResult): decide what a flush should do with an
// HTTP response. serverResult = { status:Number, record:Object|null }.
NotesSyncLogic.resolvePush = function (localRecord, serverResult) {
  const op = localRecord && localRecord.pendingOp;
  const rec = serverResult && serverResult.record;
  if (serverResult && serverResult.status === 409) {
    return { action: 'conflict', server: rec || null };
  }
  // Non-2xx status (4xx, 5xx, undefined, 0, etc): retry without clearing dirty — the server never wrote the edit.
  if (!serverResult || serverResult.status == null || serverResult.status < 200 || serverResult.status >= 300) {
    return { action: 'retry' };
  }
  if (op === 'create') {
    return { action: 'remap', tempId: (localRecord && localRecord.id) || null, serverId: (rec && rec.id) || null };
  }
  return { action: 'applied', server: rec || null };
};

// conflictCopyTitle(title, isoDate): "<Title> (conflict copy — Jul 14)".
NotesSyncLogic.conflictCopyTitle = function (title, isoDate) {
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const base = (title == null ? '' : String(title)) || 'Untitled';
  const d = isoDate ? new Date(isoDate) : null;
  if (d && !isNaN(d.getTime())) {
    return base + ' (conflict copy — ' + MON[d.getUTCMonth()] + ' ' + d.getUTCDate() + ')';
  }
  return base + ' (conflict copy)';
};

// mergeServerList(localById, serverRecords): reconcile a freshly pulled server
// list into the mirror. Server wins for clean notes (incl newer tags); dirty
// notes keep their pending local edits; pending local creates survive; clean
// local notes the server no longer has are dropped. Local-only entries:
// (1) pending create survives; (2) orphaned dirty edit converts to create;
// (3) everything else (clean, or dirty delete) drops.
NotesSyncLogic.mergeServerList = function (localById, serverRecords) {
  const local = localById || {};
  const out = {};
  const seen = new Set();
  for (const rec of (Array.isArray(serverRecords) ? serverRecords : [])) {
    if (!rec || !rec.id) continue;
    seen.add(rec.id);
    const prev = local[rec.id];
    if (prev && prev.dirty) {
      out[rec.id] = prev;                       // keep pending local edit; do not clobber
    } else {
      out[rec.id] = {
        id: rec.id, record: rec,
        baseHash: rec.content_hash || null,
        dirty: false, pendingOp: null,
        localUpdated: (prev && prev.localUpdated) || 0,
      };
    }
  }
  for (const id in local) {
    if (seen.has(id)) continue;
    const prev = local[id];
    if (prev && prev.dirty && prev.pendingOp === 'create') {
      out[id] = prev;  // not yet on server
    } else if (prev && prev.dirty && prev.pendingOp === 'edit') {
      // Server-side delete raced an un-pushed local edit: never drop text.
      // Re-create the note on next flush (server's copy is in .trash anyway).
      out[id] = { ...prev, pendingOp: 'create', baseHash: null };
    }
    // a clean local note missing from the server was deleted elsewhere -> drop it
    // a dirty delete on a server-deleted note also drops (both sides agree)
  }
  return out;
};

// --- checkbox-line transforms mirroring tasks_store.py exactly --------------
// (No CWE-78 risk: regex patterns are static, not from untrusted input)
// CRLF parity: use ([^\n]*) not (.*) so \r is captured in rest-group, matching Python behavior
// (Python . matches \r; JS . does not — this preserves \r through rewrites, byte-identical)
const NT_CHECKBOX_RE = /^(\s*)([-*+])\s+\[([ xX/])\]\s+([^\n]*)$/;
const NT_PRIORITY_TO_EMOJI = { high: '⏫', medium: '🔼', low: '🔽' };
const NT_PRIORITY_EMOJIS = ['⏫', '🔼', '🔽'];
// Mirrors tasks_store._STATE_TO_MARK exactly.
const NT_STATE_TO_MARK = { open: ' ', doing: '/', done: 'x' };

// Strip inline metadata (due / priority / @owner) -> clean text, mirroring
// tasks_store.parse_inline_metadata (used only for the expected_text guard).
function _ntCleanText(text) {
  let t = String(text == null ? '' : text);
  t = t.replace(/📅\s*\d{4}-\d{2}-\d{2}/g, '');
  for (const emo of NT_PRIORITY_EMOJIS) { if (t.indexOf(emo) >= 0) { t = t.split(emo).join(''); break; } }
  t = t.replace(/(?:^|\s)@[A-Za-z0-9_-]+/, ' ');   // first owner only, like _OWNER_RE count=1
  return t.replace(/\s+/g, ' ').trim();
}

// Build "text @owner 📅 date <prio>", mirroring tasks_store._task_remainder.
function _ntRemainder(text, owner, due, priority) {
  const parts = [String(text == null ? '' : text).trim()];
  if (owner) parts.push('@' + String(owner).trim().replace(/^@/, '').replace(/\s+/g, '-'));
  const d = String(due == null ? '' : due).trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(d)) parts.push('📅 ' + d);
  const pr = String(priority == null ? '' : priority).trim().toLowerCase();
  if (NT_PRIORITY_TO_EMOJI[pr]) parts.push(NT_PRIORITY_TO_EMOJI[pr]);
  return parts.filter((p) => p).join(' ');
}

// applyTaskEditToBody(body, op): pure note-body transform for an offline task op.
// op.kind: 'toggle' | 'add' | 'edit' | 'delete' | 'state'. Returns { body, ok }.
NotesSyncLogic.applyTaskEditToBody = function (body, op) {
  const src = body == null ? '' : String(body);
  const kind = op && op.kind;
  if (kind === 'add') {
    const line = '- [ ] ' + _ntRemainder(op.text, op.owner, op.due, op.priority);
    const cur = src.replace(/\n+$/, '');
    return { body: cur.trim() ? (cur + '\n' + line) : line, ok: true };
  }
  const lines = src.split('\n');
  const i = op ? op.line : -1;
  if (i == null || i < 0 || i >= lines.length) return { body: src, ok: false };
  const m = NT_CHECKBOX_RE.exec(lines[i]);
  if (!m) return { body: src, ok: false };
  if (op.expectedText != null && _ntCleanText(m[4]) !== op.expectedText) return { body: src, ok: false };
  if (kind === 'toggle') {
    lines[i] = m[1] + m[2] + ' [' + (op.done ? 'x' : ' ') + '] ' + m[4];
    return { body: lines.join('\n'), ok: true };
  }
  if (kind === 'state') {
    if (!NT_STATE_TO_MARK.hasOwnProperty(op.state)) return { body: src, ok: false };
    lines[i] = m[1] + m[2] + ' [' + NT_STATE_TO_MARK[op.state] + '] ' + m[4];
    return { body: lines.join('\n'), ok: true };
  }
  if (kind === 'edit') {
    // Preserve the CURRENT mark verbatim (mirrors tasks_store.update_line's T1 fix):
    // an unrelated text/due/priority edit must never silently clear a 'doing' mark.
    lines[i] = m[1] + m[2] + ' [' + m[3] + '] ' + _ntRemainder(op.text, op.owner, op.due, op.priority);
    return { body: lines.join('\n'), ok: true };
  }
  if (kind === 'delete') {
    lines.splice(i, 1);
    return { body: lines.join('\n'), ok: true };
  }
  return { body: src, ok: false };
};

// Dual export: browser global + CommonJS (Node test). No ES-module syntax so the
// plain <script> load stays valid in the browser.
if (typeof window !== 'undefined') { window.NotesSyncLogic = NotesSyncLogic; }
if (typeof module !== 'undefined' && module.exports) { module.exports = NotesSyncLogic; }
