import { test } from 'node:test';
import assert from 'node:assert/strict';
import { initialWorkTabs, workTabsReducer as reduce, parseWorkLayout, saveWorkLayout } from '../src/lib/workspaceTabs.ts';

test('same-pane reorder preserves active identities and cross-pane reorder is rejected', () => {
  let state=reduce(initialWorkTabs,{type:'open',tab:{id:'files',kind:'files',title:'文件',side:'right'}});
  state=reduce(state,{type:'reorder',id:'files',beforeId:'start'});
  assert.deepEqual(state.tabs.map(t=>t.id),['chat','files','start']);
  assert.equal(state.active.right,'files');
  assert.equal(reduce(state,{type:'reorder',id:'files',beforeId:'chat'}),state);
});

test('layout serialization saves only stable navigation fields', () => {
  let raw;
  globalThis.localStorage={setItem:(_,value)=>raw=value};
  const state={...initialWorkTabs,tabs:[...initialWorkTabs.tabs,{id:'side:55',kind:'side-chat',sessionId:55,title:'独立侧聊',side:'right',token:'old-approval',command:'never-repeat',key:'secret'}]};
  saveWorkLayout(8,state,'C:/project',false);
  assert.ok(!raw.includes('secret')&&!raw.includes('old-approval')&&!raw.includes('never-repeat'));
  const parsed=parseWorkLayout(raw);
  assert.equal(parsed.open,false);assert.equal(parsed.root,'C:/project');
  assert.equal(parsed.state.tabs.at(-1).sessionId,55);
});

test('invalid targets and duplicate identities fall back, legacy moved main stays left', () => {
  const data={version:1,root:'C:/project',tabs:[{id:'chat',kind:'chat',title:'工作对话',side:'right'}],active:{right:'chat',left:''}};
  assert.equal(parseWorkLayout(JSON.stringify(data)).state.active.left,'chat');
  for(const tabs of [[{id:'shell',kind:'execute',side:'right'}],[{id:'side:5',kind:'side-chat',sessionId:6,side:'right'}],[...initialWorkTabs.tabs,initialWorkTabs.tabs[0]]]) {
    assert.deepEqual(parseWorkLayout(JSON.stringify({...data,tabs})).state,initialWorkTabs);
  }
});
