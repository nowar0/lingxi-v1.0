import { test } from 'node:test';
import assert from 'node:assert/strict';
import { initialWorkTabs, workTabsReducer as reduce, workStartShortcut, workTabTitle } from '../src/lib/workspaceTabs.ts';
import { releaseDock, stepDockSpring, revealDockPane } from '../src/lib/workDockMotion.ts';

test('work session rail uses its own size bounds and collapse threshold', () => {
  assert.deepEqual(releaseDock(90, 0, 236, 320, 200), {width:236, open:false});
  assert.deepEqual(releaseDock(150, 0, 236, 320, 200), {width:200, open:true});
  assert.deepEqual(releaseDock(400, 0, 236, 320, 200), {width:320, open:true});
  assert.deepEqual(releaseDock(110, -400, 236, 320, 200), {width:236, open:false});
});
const open = (state, id, side = 'right', kind = id) => reduce(state, { type: 'open', tab: { id, kind, title: id, side } });
const check = state => {
  assert.equal(new Set(state.tabs.map(t => t.id)).size, state.tabs.length);
  for (const side of ['left', 'right']) {
    const peers = state.tabs.filter(t => t.side === side);
    assert.equal(peers.length ? peers.some(t => t.id === state.active[side]) : state.active[side] === '', true);
  }
};
test('first open shows conversation on the left and Start on the right', () => {
  assert.deepEqual(initialWorkTabs.active, { left: 'chat', right: 'start' }); check(initialWorkTabs);
});
test('reopening selects existing tab without stealing its chosen pane', () => {
  let state = open(initialWorkTabs, 'terminal');
  state = reduce(state, { type: 'move', id: 'terminal', side: 'left' });
  const selected = open(open(state, 'files'), 'terminal', 'right');
  assert.equal(selected.tabs.length, 4); assert.equal(selected.active.left, 'terminal');
  assert.equal(selected.tabs.find(t => t.id === 'terminal').side, 'left'); check(selected);
});
test('move active tab updates both pane selections and retains all identities', () => {
  let state = open(initialWorkTabs, 'files');
  const ids = state.tabs.map(t => t.id);
  state = reduce(state, { type: 'move', id: 'files', side: 'left' });
  assert.deepEqual(state.tabs.map(t => t.id), ids);
  assert.equal(state.active.left, 'files'); assert.equal(state.active.right, 'start'); check(state);
});
test('main chat cannot move right and independent side tabs keep their own session identity', () => {
  assert.equal(reduce(initialWorkTabs, { type: 'move', id: 'chat', side: 'right' }), initialWorkTabs);
  const state = reduce(initialWorkTabs, { type: 'open', tab: { id: 'side:42', kind: 'side-chat', sessionId: 42, title: '独立侧聊', side: 'right' } });
  assert.equal(state.active.left, 'chat'); assert.equal(state.active.right, 'side:42');
  assert.equal(state.tabs.find(t => t.id === 'side:42').sessionId, 42); check(state);
});
test('closing inactive tab preserves both active selections', () => {
  let state = open(open(initialWorkTabs, 'terminal'), 'files');
  state = reduce(state, { type: 'close', id: 'terminal' });
  assert.deepEqual(state.active, { left: 'chat', right: 'files' }); check(state);
});
test('close active middle tab selects its neighbor in the same pane', () => {
  let state = open(open(initialWorkTabs, 'terminal'), 'files');
  state = reduce(state, { type: 'select', id: 'terminal' });
  state = reduce(state, { type: 'close', id: 'terminal' });
  assert.equal(state.active.right, 'start'); assert.equal(state.active.left, 'chat'); check(state);
});
test('closing the last tab in either pane leaves a valid empty pane', () => {
  let state = reduce(initialWorkTabs, { type: 'close', id: 'start' });
  assert.equal(state.active.right, ''); check(state);
  state = reduce(state, { type: 'close', id: 'chat' });
  assert.deepEqual(state, { tabs: [], active: { left: '', right: '' } });
  check(open(state, 'start', 'right'));
});
test('late close/select/move events for removed tabs are harmless', () => {
  for (const type of ['close', 'select', 'move']) assert.equal(reduce(initialWorkTabs, { type, id: 'gone', side: 'left' }), initialWorkTabs);
});
test('same-name files remain independent and rebind removes file tabs on both sides', () => {
  let state = open(open(initialWorkTabs, 'file:a/readme.md', 'left', 'file'), 'file:b/readme.md', 'right', 'file');
  assert.equal(state.tabs.length, 4); check(state);
  state = reduce(state, { type: 'drop-files' });
  assert.deepEqual(state.active, initialWorkTabs.active); assert.deepEqual(state.tabs, initialWorkTabs.tabs); check(state);
});
test('mixed move/select/close/rebind events keep valid active tabs on both sides', () => {
  let state = initialWorkTabs, seed = 1871;
  for (let i = 0; i < 20000; i++) {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    const id = ['chat', 'start', 'files', 'terminal', 'browser', 'file:a', 'file:b'][seed % 7];
    const type = ['open', 'close', 'select', 'drop-files', 'move'][(seed >>> 8) % 5], side = seed & 128 ? 'left' : 'right';
    state = type === 'open' ? open(state, id, side, id.startsWith('file:') ? 'file' : id) : reduce(state, { type, id, side }); check(state);
  }
});
test('Start shortcuts resolve only the exact documented chords', () => {
  const chord = { ctrlKey: true, metaKey: false, shiftKey: false, altKey: false };
  assert.equal(workStartShortcut({ ...chord, key: 'p' }), 'files');
  assert.equal(workStartShortcut({ ...chord, key: 't' }), 'browser');
  assert.equal(workStartShortcut({ ...chord, key: '`', code: 'Backquote' }), 'terminal');
  assert.equal(workStartShortcut({ ...chord, key: 'P', shiftKey: true }), undefined);
  assert.equal(workStartShortcut({ ...chord, key: 't', ctrlKey: false }), undefined);
});
test('drag beyond fold threshold fully collapses while remembering prior width', () => {
  assert.deepEqual(releaseDock(80, 0, 470), { open: false, width: 470 });
  assert.deepEqual(releaseDock(270, 0, 470), { open: true, width: 320 });
  assert.deepEqual(releaseDock(900, 0, 470), { open: true, width: 760 });
});
test('navigation-matched spring settles to zero and saved widths within bounds', () => {
  for (const [start, target] of [[390,0],[0,390],[700,320]]) {
    let position = start, velocity = 0, settled = false;
    for (let i = 0; i < 300; i++) {
      const next = stepDockSpring(position, velocity, target, 1/60);
      assert.ok(next.position >= 0 && next.position <= 760);
      ({ position, velocity, settled } = next); if (settled) break;
    }
    assert.equal(settled, true); assert.equal(position, target);
  }
});

test('reopening a left tab exits expanded right even if selection did not change', () => {
  assert.deepEqual(revealDockPane('left', false, {open:true, expanded:true}), {open:true, expanded:false});
});
test('pane reveal always shows its destination in a narrow window', () => {
  assert.deepEqual(revealDockPane('left', true, {open:true, expanded:true}), {open:false, expanded:false});
  assert.deepEqual(revealDockPane('right', true, {open:false, expanded:false}), {open:true, expanded:false});
});
