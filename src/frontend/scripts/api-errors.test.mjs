import{test}from'node:test';import assert from'node:assert/strict';import{readFileSync}from'node:fs';import vm from'node:vm';import ts from'typescript';
const code=ts.transpileModule(readFileSync(new URL('../src/api/client.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
function harness(response){const exports={};vm.runInNewContext(code,{exports,Headers,FormData,AbortSignal,URLSearchParams,fetch:async()=>response,window:{location:{hash:''}}});return exports}
test('audio JSON error body displays the useful message without serialized metadata',async()=>{
 const api=harness(new Response(JSON.stringify({detail:{code:'engine_unavailable',message:'请先安装本地语音模型',details:{internal:'private'}}}),{status:409,headers:{'Content-Type':'application/json'}}));
 await assert.rejects(api.apiBlob('/test'),e=>e.message==='请先安装本地语音模型'&&!e.message.includes('private'))
});
test('audio plain-text JSON error and HTML response remain readable',async()=>{
 await assert.rejects(harness(new Response('{"detail":"音色未配置"}',{status:422})).apiBlob('/test'),e=>e.message==='音色未配置');
 await assert.rejects(harness(new Response('<html>traceback</html>',{status:500})).apiBlob('/test'),e=>e.message==='请求失败（HTTP 500）');
});
test('malformed JSON error falls back to HTTP status without raw exception data',async()=>{
 await assert.rejects(harness(new Response('{bad',{status:502,headers:{'Content-Type':'application/json'}})).apiBlob('/test'),e=>e.message==='请求失败（HTTP 502）');
});
