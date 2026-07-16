// Pure-logic unit tests for the offline Notes mirror sync.
// Run with:  node --test tests/js/notes_sync_logic.test.mjs   (no npm deps)
import { test } from 'node:test';
import assert from 'node:assert';
import { createHash } from 'node:crypto';

// notes-sync-logic.js is a plain CommonJS script (also a browser global);
// default-import the whole namespace object.
import L from '../../static/notes-sync-logic.js';

// Reference: an independent sha1 of title + NUL + body. The server uses the same
// hashlib.sha1(title + "\x00" + body), so matching this proves cross-compat.
// NOTE: the server hashes the *stripped* body (notes_store._record calls
// content_hash(title, body.strip())) -- callers of refHash must pass an
// already-trimmed body to mirror that.
const refHash = (t, b) => createHash('sha1').update(Buffer.from(String(t) + '\x00' + String(b), 'utf8')).digest('hex');

test('contentHash matches server sha1(title\\0body) on ASCII vectors (no padding, trim is a no-op)', () => {
  for (const [t, b] of [['', ''], ['Hello', 'World'], ['T', 'orig'], ['a', 'multi\nline\nbody']]) {
    assert.strictEqual(L.contentHash(t, b), refHash(t, b));
  }
});

test('contentHash matches server sha1 on unicode / long bodies', () => {
  const t = 'Café ☕ 会議';
  const b = '长文本 '.repeat(500) + '📅 2026-07-14 @alex ⏫';
  assert.strictEqual(L.contentHash(t, b), refHash(t, b));
});

test('contentHash treats null/undefined as empty string', () => {
  assert.strictEqual(L.contentHash(null, undefined), refHash('', ''));
});

test('contentHash trims only leading/trailing body whitespace, matching server .strip()', () => {
  const t = 'Title';
  assert.strictEqual(L.contentHash(t, '  padded  '), refHash(t, 'padded'));
  assert.strictEqual(L.contentHash(t, '\n\tindented\n\t'), refHash(t, 'indented'));
  // internal whitespace is preserved -- only the outer ends are trimmed
  assert.strictEqual(L.contentHash(t, '  multi\n  line  '), refHash(t, 'multi\n  line'));
});

test('contentHash: whitespace-insensitive body is pinned against the reference hash', () => {
  const t = 'T';
  const hashX = L.contentHash(t, 'x');
  assert.strictEqual(L.contentHash(t, ' x \n'), hashX);
  assert.strictEqual(hashX, refHash(t, 'x'));
});

test('contentHash: title whitespace is NEVER trimmed (asymmetric with body)', () => {
  assert.notStrictEqual(L.contentHash('  Title  ', 'body'), L.contentHash('Title', 'body'));
  assert.strictEqual(L.contentHash('  Title  ', 'body'), refHash('  Title  ', 'body'));
});

test('selectDirtyNotes: only dirty+pendingOp, ordered create->edit->delete then oldest-first', () => {
  const recs = [
    { id: 'e2', dirty: true, pendingOp: 'edit', localUpdated: 300 },
    { id: 'c1', dirty: true, pendingOp: 'create', localUpdated: 200 },
    { id: 'clean', dirty: false, pendingOp: null, localUpdated: 1 },
    { id: 'd1', dirty: true, pendingOp: 'delete', localUpdated: 100 },
    { id: 'e1', dirty: true, pendingOp: 'edit', localUpdated: 150 },
    { id: 'nop', dirty: true, pendingOp: null, localUpdated: 5 },
  ];
  assert.deepStrictEqual(L.selectDirtyNotes(recs).map((r) => r.id), ['c1', 'e1', 'e2', 'd1']);
});

test('selectDirtyNotes: empty / non-array -> []', () => {
  assert.deepStrictEqual(L.selectDirtyNotes([]), []);
  assert.deepStrictEqual(L.selectDirtyNotes(undefined), []);
});

test('selectDirtyNotes: does not mutate input', () => {
  const recs = [
    { id: 'a', dirty: true, pendingOp: 'edit', localUpdated: 2 },
    { id: 'b', dirty: true, pendingOp: 'create', localUpdated: 1 },
  ];
  const before = recs.map((r) => r.id);
  L.selectDirtyNotes(recs);
  assert.deepStrictEqual(recs.map((r) => r.id), before);
});

test('resolvePush: create success -> remap temp id to server id', () => {
  const d = L.resolvePush({ id: 'n_local_x', pendingOp: 'create' }, { status: 200, record: { id: 'n_real', content_hash: 'h' } });
  assert.deepStrictEqual(d, { action: 'remap', tempId: 'n_local_x', serverId: 'n_real' });
});

test('resolvePush: edit success -> applied with server record', () => {
  const rec = { id: 'n1', content_hash: 'h2' };
  const d = L.resolvePush({ id: 'n1', pendingOp: 'edit' }, { status: 200, record: rec });
  assert.strictEqual(d.action, 'applied');
  assert.strictEqual(d.server, rec);
});

test('resolvePush: 409 on edit -> conflict carrying server record', () => {
  const server = { id: 'n1', body: 'theirs', content_hash: 'hz' };
  assert.deepStrictEqual(L.resolvePush({ id: 'n1', pendingOp: 'edit' }, { status: 409, record: server }), { action: 'conflict', server });
});

test('resolvePush: delete success -> applied', () => {
  assert.strictEqual(L.resolvePush({ id: 'n1', pendingOp: 'delete' }, { status: 200, record: null }).action, 'applied');
});

test('conflictCopyTitle: deterministic UTC month/day label', () => {
  assert.strictEqual(L.conflictCopyTitle('Roadmap', '2026-07-14T09:00:00Z'), 'Roadmap (conflict copy — Jul 14)');
  assert.strictEqual(L.conflictCopyTitle('', '2026-01-03T00:00:00Z'), 'Untitled (conflict copy — Jan 3)');
  assert.strictEqual(L.conflictCopyTitle('X', null), 'X (conflict copy)');
});

test('resolvePush: server error 500 resolves to retry, never applied', () => {
  const local = { id: 'n1', pendingOp: 'edit' };
  const r = L.resolvePush(local, { status: 500, record: null });
  assert.strictEqual(r.action, 'retry');
});

test('resolvePush: missing/zero status resolves to retry', () => {
  assert.strictEqual(L.resolvePush({ id: 'n1', pendingOp: 'edit' }, { status: 0 }).action, 'retry');
  assert.strictEqual(L.resolvePush({ id: 'n1', pendingOp: 'create' }, {}).action, 'retry');
});

test('mergeServerList: server wins for clean notes (incl newer tags)', () => {
  const local = { n1: { id: 'n1', record: { id: 'n1', title: 'T', body: 'b', tags: ['old'], content_hash: 'h1' }, baseHash: 'h1', dirty: false, pendingOp: null, localUpdated: 0 } };
  const server = [{ id: 'n1', title: 'T', body: 'b', tags: ['old', 'new'], content_hash: 'h1' }];
  const out = L.mergeServerList(local, server);
  assert.deepStrictEqual(out.n1.record.tags, ['old', 'new']);
  assert.strictEqual(out.n1.dirty, false);
  assert.strictEqual(out.n1.baseHash, 'h1');
});

test('mergeServerList: dirty local note keeps its pending edit', () => {
  const dirtyEntry = { id: 'n1', record: { id: 'n1', title: 'T', body: 'MY EDIT', tags: [], content_hash: 'hx' }, baseHash: 'h1', dirty: true, pendingOp: 'edit', localUpdated: 5 };
  const out = L.mergeServerList({ n1: dirtyEntry }, [{ id: 'n1', title: 'T', body: 'server body', content_hash: 'h1' }]);
  assert.strictEqual(out.n1, dirtyEntry);
  assert.strictEqual(out.n1.record.body, 'MY EDIT');
});

test('mergeServerList: pending local create (absent from server) is retained', () => {
  const create = { id: 'n_local_1', record: { id: 'n_local_1', title: 'New', body: 'x' }, baseHash: null, dirty: true, pendingOp: 'create', localUpdated: 9 };
  const out = L.mergeServerList({ n_local_1: create }, []);
  assert.strictEqual(out.n_local_1, create);
});

test('mergeServerList: clean local note absent from server is dropped', () => {
  const clean = { id: 'gone', record: { id: 'gone' }, baseHash: 'h', dirty: false, pendingOp: null, localUpdated: 0 };
  assert.strictEqual(L.mergeServerList({ gone: clean }, []).gone, undefined);
});

test('mergeServerList: orphaned dirty edit survives as pending create (never lose text)', () => {
  const local = { n1: { id: 'n1', dirty: true, pendingOp: 'edit', baseHash: 'old',
                        record: { id: 'n1', title: 'T', body: 'my edit' }, localUpdated: 10 } };
  const out = L.mergeServerList(local, []);   // server no longer has n1
  assert.ok(out.n1, 'entry must survive');
  assert.strictEqual(out.n1.pendingOp, 'create');
  assert.strictEqual(out.n1.baseHash, null);
  assert.strictEqual(out.n1.record.body, 'my edit');
});

test('mergeServerList: orphaned dirty delete drops (both sides agree)', () => {
  const local = { n2: { id: 'n2', dirty: true, pendingOp: 'delete', record: { id: 'n2', title: 'X' }, baseHash: 'h', localUpdated: 5 } };
  const out = L.mergeServerList(local, []);
  assert.strictEqual(out.n2, undefined);
});

test('applyTaskEditToBody: toggle flips checkbox, preserves text/metadata', () => {
  const body = '- [ ] ship it 📅 2026-07-14 @alex ⏫';
  const r = L.applyTaskEditToBody(body, { kind: 'toggle', line: 0, done: true, expectedText: 'ship it' });
  assert.deepStrictEqual(r, { body: '- [x] ship it 📅 2026-07-14 @alex ⏫', ok: true });
});

test('applyTaskEditToBody: add appends a checkbox line joined with one newline', () => {
  const r = L.applyTaskEditToBody('- [ ] first', { kind: 'add', text: 'second', owner: 'sam', due: '2026-08-01', priority: 'high' });
  assert.strictEqual(r.body, '- [ ] first\n- [ ] second @sam 📅 2026-08-01 ⏫');
});

test('applyTaskEditToBody: add into empty body yields just the line', () => {
  assert.strictEqual(L.applyTaskEditToBody('', { kind: 'add', text: 'only' }).body, '- [ ] only');
});

test('applyTaskEditToBody: edit rewrites text+metadata, preserves done/indent/bullet', () => {
  const r = L.applyTaskEditToBody('  * [x] old text @amy', { kind: 'edit', line: 0, expectedText: 'old text', text: 'new text', owner: 'bob', due: '', priority: 'low' });
  assert.deepStrictEqual(r, { body: '  * [x] new text @bob 🔽', ok: true });
});

test('applyTaskEditToBody: delete removes the line', () => {
  const r = L.applyTaskEditToBody('- [ ] a\n- [ ] b\n- [ ] c', { kind: 'delete', line: 1, expectedText: 'b' });
  assert.strictEqual(r.body, '- [ ] a\n- [ ] c');
});

test('applyTaskEditToBody: stale expectedText refuses (ok:false, body unchanged)', () => {
  const body = '- [ ] real';
  assert.deepStrictEqual(L.applyTaskEditToBody(body, { kind: 'toggle', line: 0, done: true, expectedText: 'WRONG' }), { body, ok: false });
});

test('applyTaskEditToBody: non-checkbox / out-of-range refuses', () => {
  assert.strictEqual(L.applyTaskEditToBody('plain text', { kind: 'toggle', line: 0, done: true }).ok, false);
  assert.strictEqual(L.applyTaskEditToBody('- [ ] x', { kind: 'edit', line: 5, text: 'y' }).ok, false);
});
