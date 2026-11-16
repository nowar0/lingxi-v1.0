import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { initialWorkTabs, workTabsReducer as reduce } from '../src/lib/workspaceTabs.ts';

// Server rendering exercises component composition only. No DOM, browser, window
// automation, network calls or effects are executed by these checks.
globalThis.window = { matchMedia: () => ({ matches: false }) };
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const result = await build({ stdin: { contents: "export {WorkDock} from './src/components/shared/WorkDock'; export {WorkStart} from './src/components/shared/WorkStart'; export {ThemeProvider} from './src/context/ThemeContext'; export {TooltipProvider} from './src/components/ui/tooltip'; export {BrowserTools} from './src/components/shared/BrowserTools';", resolveDir: root, loader: 'tsx' }, tsconfig: path.join(root,'tsconfig.json'), bundle: true, packages: 'external', platform: 'node', format: 'cjs', write: false, logLevel: 'silent' });
const module = { exports: {} };
new Function('require','module','exports',result.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports);
const { WorkDock, WorkStart, ThemeProvider, TooltipProvider, BrowserTools } = module.exports;
const noop = () => {};
function render(state, open) {
  return renderToStaticMarkup(React.createElement(ThemeProvider, null, React.createElement(TooltipProvider, null, React.createElement(WorkDock, { state, dispatch: noop, open, onOpenChange: noop, options: [], onOpen: noop, renderTab: tab => React.createElement('div', { 'data-content': tab.id }, tab.title), renderStart: side => React.createElement(WorkStart, { side, onOpen: noop }), projectPath: 'isolated-project' }))));
}
test('Start provides three real button entries and documented shortcuts', () => {
  const html = renderToStaticMarkup(React.createElement(WorkStart, { side: 'right', onOpen: noop, art: './assets/lingxi-stage.png' }));
  for (const text of ['文件', '终端', '浏览器', 'Ctrl + P', 'Ctrl + `', 'Ctrl + T']) assert.ok(html.includes(text), text);
  assert.equal((html.match(/<button/g) || []).length, 3);
  assert.ok(html.includes('aria-hidden="true"'));
});
test('tool launcher is a named region while actual tabs retain accessible panels', () => {
  const html = render(initialWorkTabs, true);
  assert.equal((html.match(/role="tablist"/g) || []).length, 2);
  assert.equal((html.match(/role="tabpanel"/g) || []).length, 1);
  assert.ok(html.includes('role="region" aria-label="添加工具"'));
  assert.ok(!html.includes('title="开始"'));
  assert.ok(html.includes('添加右侧标签页'));
  assert.equal((html.match(/aria-selected="true"/g) || []).length, 1);
  assert.ok(html.includes('aria-controls=')); assert.ok(html.includes('aria-labelledby='));
});
test('collapsed right panels remain mounted but hidden and inert', () => {
  const html = render(initialWorkTabs, false);
  const panels = html.match(/<section[^>]*role="(?:tabpanel|region)"[^>]*>/g);
  const right = panels.find(tag => tag.includes('data-pane-content="right"'));
  assert.ok(right.includes('hidden=""')); assert.ok(right.includes('inert=""'));
  assert.ok(html.includes('data-content="start"'));
});
test('independent side chat renders alongside the main chat with a distinct session', () => {
  const state = reduce(initialWorkTabs, { type: 'open', tab: { id: 'side:42', kind: 'side-chat', sessionId: 42, title: '独立侧聊', side: 'right' } });
  const html = render(state, true);
  assert.match(html, /独立侧聊/); assert.match(html, /工作对话/);
  assert.equal((html.match(/data-content="side:42"/g) || []).length, 1);
});
test('an empty right pane still has a Start surface when expanded', () => {
  const state = reduce(initialWorkTabs, { type: 'move', id: 'start', side: 'left' });
  const html = render(state, true);
  assert.ok(html.includes('文件')); assert.ok(html.includes('终端'));
  assert.ok(html.includes('添加右侧标签页'));
});

test('embedded browser renders its real controls without a modal context', () => {
 const html=renderToStaticMarkup(React.createElement(BrowserTools,{open:true,enabled:false,embedded:true,onOpenChange:noop}));
 for(const text of ['浏览器','专用浏览器地址','打开网页','读取当前页面','连接本地服务后可用'])assert.ok(html.includes(text),text);
 assert.equal(html.includes('role="dialog"'),false);
 assert.ok(html.includes('disabled=""'));
});
