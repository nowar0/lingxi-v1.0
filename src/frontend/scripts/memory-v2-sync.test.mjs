import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source=readFileSync(new URL('../src/lib/memoryV2Sync.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
const exports={}
vm.runInNewContext(code,{exports,Date,JSON,Number,Object,Promise,URLSearchParams,AbortController,Error,globalThis:{setTimeout(){throw Error('no implicit real timer')},clearTimeout(){}}})
const {createMemoryV2Sync,parseMemorySyncState,memorySyncPath}=exports
const cursorA='a'.repeat(64),cursorB='b'.repeat(64)
const base={connected:true,visible:true,scope:'companion'}
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}}
const tick=async()=>{await new Promise(resolve=>setImmediate(resolve))}
const response=(cursor=cursorA,changed=true,status=200,extra={})=>({ok:status>=200&&status<300,status,json:async()=>({cursor,changed,read_only:true,...extra})})
function harness({context=base,fetcher,onChange,onStatus}={}){
 let clock=1000,serial=0;const timers=new Map(),calls=[],changes=[],states=[]
 const scheduler={setTimeout(fn,ms){const id=++serial;timers.set(id,{fn,at:clock+ms,ms});return id},clearTimeout(id){timers.delete(id)}}
 const controller=createMemoryV2Sync({context,fetcher:async(path,init)=>{calls.push({path,init});return fetcher?fetcher(path,init,calls):response()},onChange:async change=>{changes.push(change);if(onChange)await onChange(change)},onStatus:value=>{states.push(value);onStatus?.(value)},scheduler,now:()=>clock})
 return {controller,calls,changes,states,timers,advance:async(ms=15000)=>{clock+=ms;const pending=[...timers].filter(([,x])=>x.at<=clock);for(const [id,item]of pending){timers.delete(id);item.fn()}await tick()}}
}

test('construction and context updates do not start network or timers',()=>{
 const h=harness();h.controller.updateContext({...base,category:'preference'});assert.equal(h.calls.length,0);assert.equal(h.timers.size,0);assert.equal(h.controller.status().started,false)
})
test('only connected visible valid work context can poll',async()=>{
 for(const context of [{...base,connected:false},{...base,visible:false},{...base,scope:'work'}]){
  const h=harness({context});h.controller.start();await tick();assert.equal(h.calls.length,0);assert.equal(h.timers.size,0)
 }
 const h=harness({context:{...base,scope:'work',session_id:8}});h.controller.start();await tick();assert.match(h.calls[0].path,/session_id=8/)
})
test('first successful cursor always refreshes even changed false, then serial 15s GET',async()=>{
 const h=harness({fetcher:()=>response(cursorA,false)});h.controller.start();await tick();assert.equal(h.changes.length,1);assert.equal(h.changes[0].initial,true);assert.equal(h.calls[0].init.method,'GET');assert.equal(h.calls[0].init.body,undefined)
 assert.equal(h.controller.status().cursor,cursorA);assert.equal(h.controller.status().last_checked,1000);assert.equal(h.controller.status().stale,false)
 assert.equal([...h.timers.values()][0].ms,15000);await h.advance();assert.equal(h.calls.length,2);assert.equal(h.changes.length,1);assert.match(h.calls[1].path,/cursor=a{64}/)
})
test('changed cursor refreshes once and does not send any write or reinforcement',async()=>{
 const h=harness({fetcher:(_p,_i,calls)=>calls.length===1?response(cursorA,true):response(cursorB,true)});h.controller.start();await tick();await h.advance();assert.equal(h.changes.length,2);assert.equal(h.changes[1].initial,false);assert.equal(h.controller.status().cursor,cursorB)
 assert.ok(h.calls.every(x=>x.init.method==='GET'&&!x.path.includes('reinforce')))
})
test('inflight poll and asynchronous refresh never overlap even on explicit refresh',async()=>{
 const gate=deferred();const h=harness({onChange:()=>gate.promise});h.controller.start();await tick();assert.equal(h.controller.status().pending,true);h.controller.refreshNow();await h.advance(60000);assert.equal(h.calls.length,1);assert.equal(h.controller.status().cursor,undefined)
 gate.resolve();await tick();assert.equal(h.controller.status().pending,false);assert.equal(h.controller.status().cursor,cursorA);assert.equal(h.timers.size,1)
})
test('ignored abort fetch times out, releases admission and late result cannot commit',async()=>{
 const old=deferred();const h=harness({fetcher:(_p,_i,calls)=>calls.length===1?old.promise:response(cursorB,false)})
 h.controller.start();await tick();assert.equal(h.controller.status().inflight,true)
 await h.advance(10000);assert.equal(h.calls[0].init.signal.aborted,true);assert.equal(h.controller.status().inflight,false)
 assert.equal(h.controller.status().stale,true);assert.match(h.controller.status().error,/超时/);assert.equal(h.controller.status().cursor,undefined)
 assert.equal(h.controller.status().manual_restart_required,false);assert.equal([...h.timers.values()][0].ms,15000)
 h.controller.refreshNow();await tick();assert.equal(h.controller.status().cursor,cursorB);assert.equal(h.changes.length,1)
 old.resolve(response(cursorA,false));await tick();assert.equal(h.controller.status().cursor,cursorB);assert.equal(h.changes.length,1)
})
test('body JSON deadline retains prior cursor and readonly retry remains possible',async()=>{
 const json=deferred();const h=harness({fetcher:(_p,_i,calls)=>calls.length===1?response(cursorA,false):calls.length===2?{ok:true,status:200,json:()=>json.promise}:response(cursorB,true)})
 h.controller.start();await tick();await h.advance();assert.equal(h.controller.status().cursor,cursorA)
 await h.advance(10000);assert.equal(h.controller.status().inflight,false);assert.equal(h.controller.status().cursor,cursorA)
 h.controller.refreshNow();await tick();assert.equal(h.controller.status().cursor,cursorB)
 json.resolve({cursor:cursorA,changed:false,read_only:true});await tick();assert.equal(h.controller.status().cursor,cursorB)
})
test('timed-out old read cannot change scope owner error or pending state',async()=>{
 const old=deferred();const h=harness({fetcher:(_p,_i,calls)=>calls.length===1?old.promise:response(cursorB,false)})
 h.controller.start();await tick();await h.advance(10000)
 h.controller.updateContext({...base,scope:'work',session_id:7});await tick()
 old.reject(Error('late ignored abort'));await tick()
 assert.equal(h.controller.status().cursor,cursorB);assert.equal(h.controller.status().stale,false);assert.equal(h.controller.status().error,undefined)
})
test('hidden and offline abort; visible reconnect checks immediately without changing baseline',async()=>{
 const h=harness({fetcher:()=>response(cursorA,false)});h.controller.start();await tick()
 h.controller.updateContext({...base,visible:false});assert.equal(h.timers.size,0);assert.equal(h.controller.status().stale,true)
 h.controller.updateContext(base);await tick();assert.equal(h.calls.length,2);assert.match(h.calls[1].path,/cursor=/)
 h.controller.updateContext({...base,connected:false});assert.equal(h.timers.size,0);h.controller.updateContext(base);await tick();assert.equal(h.calls.length,3)
})
test('hiding or disconnecting an actual inflight request aborts without error publication',async()=>{
 for(const next of [{...base,visible:false},{...base,connected:false}]){
  const gate=deferred();const h=harness({fetcher:()=>gate.promise});h.controller.start();await tick();h.controller.updateContext(next);assert.equal(h.calls[0].init.signal.aborted,true);gate.reject(Error('abort-like transport'));await tick();assert.equal(h.changes.length,0);assert.equal(h.controller.status().error,undefined);assert.equal(h.controller.status().inflight,false);assert.equal(h.timers.size,0)
 }
})
test('filter or session owner change discards old success catch and finally',async()=>{
 const old=deferred(),fresh=deferred();const h=harness({fetcher:(_p,_i,calls)=>calls.length===1?old.promise:fresh.promise});h.controller.start();await tick()
 h.controller.updateContext({...base,scope:'work',session_id:9});await tick();assert.equal(h.calls.length,2);assert.equal(h.calls[0].init.signal.aborted,true)
 old.resolve(response(cursorB));await tick();assert.equal(h.changes.length,0);assert.equal(h.controller.status().inflight,true)
 fresh.resolve(response(cursorA));await tick();assert.equal(h.changes.length,1);assert.equal(h.changes[0].context.scope,'work');assert.equal(h.controller.status().cursor,cursorA)
})
test('late network rejection from old owner cannot clear new state',async()=>{
 const old=deferred();const h=harness({fetcher:(_p,_i,calls)=>calls.length===1?old.promise:response(cursorB)});h.controller.start();await tick();h.controller.updateContext({...base,category:'new'});await tick();old.reject(Error('old failure'));await tick()
 assert.equal(h.controller.status().cursor,cursorB);assert.equal(h.controller.status().stale,false);assert.equal(h.controller.status().error,undefined)
})
test('503 stops until explicit restart, preserves cursor and no invented last checked',async()=>{
 const h=harness({fetcher:(_p,_i,calls)=>calls.length===1?response(cursorA,true,503):response(cursorB)});h.controller.start();await tick();assert.equal(h.controller.status().manual_restart_required,true);assert.equal(h.controller.status().last_checked,undefined);assert.equal(h.controller.status().cursor,undefined);assert.equal(h.timers.size,0)
 await h.advance(60000);assert.equal(h.calls.length,1);h.controller.refreshNow();await tick();assert.equal(h.calls.length,2);assert.equal(h.controller.status().cursor,cursorB)
})
test('network error preserves last cursor and stale indication; readonly retry is bounded by interval',async()=>{
 const h=harness({fetcher:(_p,_i,calls)=>{if(calls.length===2)throw Error('private error never displayed');return calls.length===1?response(cursorA):response(cursorB)}});h.controller.start();await tick();await h.advance();assert.equal(h.controller.status().cursor,cursorA);assert.equal(h.controller.status().stale,true);assert.ok(!h.controller.status().error.includes('private error'))
 await h.advance();assert.equal(h.controller.status().cursor,cursorB);assert.equal(h.changes.length,2)
})
test('malformed or inconsistent response stops for manual restart, never treating errors as no change',async()=>{
 for(const result of [response(cursorA,true,200,{extra:'memory body'}),response('bad'),{ok:true,status:200,json:async()=>{throw Error('json')}}]){
  const h=harness({fetcher:()=>result});h.controller.start();await tick();assert.equal(h.controller.status().manual_restart_required,true);assert.equal(h.changes.length,0);assert.equal(h.timers.size,0)
 }
 assert.throws(()=>parseMemorySyncState({cursor:cursorB,changed:false,read_only:true},cursorA))
 assert.throws(()=>parseMemorySyncState({cursor:cursorA,changed:true,read_only:false}))
})
test('callback failure does not consume change; next read retries refresh, not a mutation',async()=>{
 let attempts=0;const h=harness({onChange:()=>{if(++attempts===1)throw Error('refresh failed')}});h.controller.start();await tick();assert.equal(h.controller.status().cursor,undefined);assert.equal(h.controller.status().stale,true)
 await h.advance();assert.equal(h.changes.length,2);assert.equal(h.changes[1].initial,true);assert.equal(h.controller.status().cursor,cursorA)
})
test('callbacks across context generations are serialized and obsolete results do not commit',async()=>{
 const gate=deferred();let running=0,max=0;const h=harness({onChange:async change=>{running++;max=Math.max(max,running);if(change.context.scope==='companion')await gate.promise;running--}})
 h.controller.start();await tick();h.controller.updateContext({...base,scope:'work',session_id:3});await tick();assert.equal(h.changes.length,1);assert.equal(h.controller.status().pending,true);gate.resolve();await tick();assert.equal(h.changes.length,2);assert.equal(max,1);assert.equal(h.controller.status().cursor,cursorA)
})
test('stop aborts and invalidates late callback or response with no restart',async()=>{
 const gate=deferred();const h=harness({fetcher:()=>gate.promise});h.controller.start();await tick();h.controller.stop();assert.equal(h.calls[0].init.signal.aborted,true);gate.resolve(response());await tick();assert.equal(h.changes.length,0);assert.equal(h.controller.status().started,false);assert.equal(h.timers.size,0)
})
test('reentrant status callback stop prevents transport after publication',async()=>{
 let controller;const h=harness({onStatus:s=>{if(s.inflight)controller.stop()}});controller=h.controller;controller.start();await tick();assert.equal(h.calls.length,0);assert.equal(controller.status().started,false)
})
test('scope path is exact bounded query without body plaintext or arbitrary transport target',()=>{
 const path=memorySyncPath({...base,scope:'work',session_id:4,layer:3,category:'操作习惯',include_deleted:true},cursorA)
 assert.match(path,/^\/v10\/memory-sync\/change-state\?/);const url=new URL(path,'http://127.0.0.1');assert.equal(url.searchParams.get('scope'),'work');assert.equal(url.searchParams.get('category'),'操作习惯');assert.equal(url.searchParams.get('cursor'),cursorA)
 assert.throws(()=>memorySyncPath({...base,scope:'work'}));assert.throws(()=>memorySyncPath(base,'x'))
 assert.throws(()=>memorySyncPath({...base,category:'x'.repeat(81)}));assert.throws(()=>memorySyncPath({...base,session_id:0}))
 assert.ok(!source.includes('localStorage'));assert.ok(!source.includes('BroadcastChannel'));assert.ok(!source.includes("method: 'POST'"))
})
