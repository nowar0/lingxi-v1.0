import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
import * as plugins from '../src/lib/pluginPackages.ts'

// In-memory transpilation only. Never mount an application or perform real fetch.
const source = readFileSync(new URL('../src/lib/packageUi.ts', import.meta.url), 'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
const exports={}
vm.runInNewContext(code,{exports,require:name=>name==='./pluginPackages'?plugins:{},Date,JSON,Set,Map,URL,Response,TextEncoder,AbortSignal,Math,crypto:globalThis.crypto,fetch:()=>{throw Error('no network')}},{filename:'packageUi.contract.cjs'})
const {parsePackageUiSchema,parsePackageUiSession,packageUiDefaults,packageUiEditableValues,createPackageUiClient,packageUiApprovalProblem}=exports
const pid='org.test.ui',hash='a'.repeat(64),revision=7,binding={id:pid,revision,digest:hash}
const schema={schema_version:1,title:'本地表单',description:'只显示声明字段',fields:[
 {id:'text',type:'text',label:'文字',source:'config.text',required:true,default:'hello',max_length:20},
 {id:'count',type:'number',label:'数量',source:'state.count',default:2,min:0,max:10},
 {id:'enabled',type:'boolean',label:'选项',source:'config.enabled',default:false},
 {id:'mode',type:'select',label:'模式',source:'config.mode',default:'brief',options:['brief','full']},
 {id:'summary',type:'readonly',label:'只读',source:'state.summary',default:'host value'},
],actions:[{id:'echo',label:'回声',kind:'tool',tool:'echo',arguments:{text:'text'}},{id:'save',label:'保存配置',kind:'save_config'},{id:'state',label:'保存状态',kind:'save_state'}]}
const values={text:'hello',count:2,enabled:false,mode:'brief',summary:'host value'}
const session=(extra={})=>({session_id:'session-12345678901234567890',nonce:'nonce-12345678901234567890',expires_at:Date.now()/1000+200,next_sequence:1,binding,schema,approval_required:true,allowed_methods:['schema','read_values','preview_action'],note:'预览不是执行',...extra})
const preview=(action='call',extra={})=>({token:'approval-single-use-123456789012',fingerprint:hash,expires_at:Date.now()/1000+100,blocked:[],snapshot:{action,id:pid,expected_revision:revision,digest:hash,permissions:['python.execute'],targets:[],...(action==='call'?{tool:'echo',args:{text:'hello'},risk:'不是 OS 沙箱'}:action==='config'?{value:{text:'hello',enabled:false,mode:'brief',untouched:'preserved'}}:{value:{count:2,summary:'host value'}}),...extra}})
const response=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'content-type':'application/json'}})
const clone=value=>JSON.parse(JSON.stringify(value))
const equal=(actual,expected)=>assert.deepEqual(clone(actual),expected)
function harness(custom){const calls=[];const client=createPackageUiClient({fetcher:async(path,init)=>{const body=JSON.parse(init.body);calls.push({path,init,body});if(custom){const out=await custom(path,body,calls);if(out!==undefined)return out}
 if(path.endsWith('/open'))return response(session())
 const common={session_id:body.session_id,sequence:body.sequence,next_sequence:body.sequence+1,request_id:body.request_id,binding}
 if(body.method==='schema')return response({...common,schema})
 if(body.method==='read_values')return response({...common,values})
 return response({...common,preview:preview(body.params.action_id==='save'?'config':body.params.action_id==='state'?'state':'call'),execution:'separate-explicit-user-confirmation-required',execute_path:body.params.action_id==='echo'?'/v1/plugin-packages/calls/execute':'/v1/plugin-packages/commit'})
 }});return {client,calls}}

test('strict native schema accepts the finite field vocabulary, never extra executable UI',()=>{
 equal(parsePackageUiSchema(schema),schema)
 for(const bad of [{...schema,html:'<script>bad</script>'},{...schema,script:'eval()'},{...schema,iframe:'https://example.invalid'},{...schema,fields:[{id:'x',type:'html',label:'x'}]},{...schema,actions:[{id:'bad',label:'bad',kind:'execute'}]}])assert.throws(()=>parsePackageUiSchema(bad),/格式|支持/)
})
test('duplicate IDs, sources, bad ranges, options and excessive counts are refused',()=>{
 for(const bad of [{...schema,fields:[schema.fields[0],schema.fields[0]]},{...schema,fields:[schema.fields[0],{...schema.fields[0],id:'second'}]},{...schema,fields:[{id:'n',type:'number',label:'n',min:5,max:1}]},{...schema,fields:[{id:'s',type:'select',label:'s',options:['x','x']}]},{...schema,fields:Array.from({length:51},(_,i)=>({id:'x'+i,type:'text',label:'x'}))},{...schema,actions:Array.from({length:31},(_,i)=>({id:'a'+i,label:'a',kind:'save_config'}))}])assert.throws(()=>parsePackageUiSchema(bad))
})
test('sensitive or arbitrary file/network/credential sources never render',()=>{
 for(const field of [{id:'api_key',type:'text',label:'Key'},{id:'x',type:'text',label:'x',source:'files.any'},{id:'x',type:'text',label:'x',source:'config.secret'},{id:'x',type:'text',label:'x',default:'sk-abcdefghijklmnopqrst'},{id:'constructor',type:'text',label:'x'}])assert.throws(()=>parsePackageUiSchema({...schema,fields:[field],actions:[]}))
})
test('text content stays plain data and field values are strict, bounded and finite',()=>{
 const text=parsePackageUiSchema({...schema,title:'<b>plain title</b>'});assert.equal(text.title,'<b>plain title</b>')
 equal(packageUiEditableValues(schema,values),{text:'hello',count:2,enabled:false,mode:'brief'})
 for(const patch of [{count:'2'},{count:Infinity},{count:11},{enabled:'false'},{text:' '},{text:'x'.repeat(21)},{mode:'unknown'},{unknown:'field'}])assert.throws(()=>packageUiEditableValues(schema,{...values,...patch}))
 assert.ok(!Object.keys(packageUiEditableValues(schema,values)).includes('summary'))
 equal(packageUiDefaults(schema),values)
 assert.equal(packageUiEditableValues(schema,{...values,count:null}).count,null)
 assert.throws(()=>packageUiEditableValues(schema,{...values,text:null}),/请填写/)
})
test('session binds exact revision digest, explicit allowlist and future TTL',()=>{
 equal(parsePackageUiSession(session(),binding).binding,binding)
 for(const value of [session({approval_required:false}),session({binding:{...binding,revision:8}}),session({expires_at:0}),session({next_sequence:2}),session({allowed_methods:['schema','execute']})])assert.throws(()=>parsePackageUiSession(value,binding))
})
test('client construction and explicit open do not connect tools or auto-preview',async()=>{
 const h=harness();assert.equal(h.calls.length,0)
 await h.client.open(binding);assert.equal(h.calls.length,1);assert.equal(h.calls[0].path,`/v1/plugin-packages/${pid}/ui/open`);equal(h.calls[0].body,{expected_revision:revision});assert.equal(h.calls[0].init.credentials,'same-origin')
 assert.throws(()=>createPackageUiClient({apiBase:'https://evil.invalid/v1/plugin-packages'}),/同源/)
})
test('RPC nonces, strictly increasing sequences and unique request IDs are transmitted',async()=>{
 const h=harness();await h.client.open(binding);await h.client.schema();equal(await h.client.readValues(),values)
 assert.equal(h.calls[1].body.sequence,1);assert.equal(h.calls[2].body.sequence,2);assert.notEqual(h.calls[1].body.request_id,h.calls[2].body.request_id);assert.equal(h.calls[1].body.nonce,session().nonce)
 assert.ok(h.calls.every(call=>!call.path.includes('/execute')&&!call.path.includes('/commit')))
})
test('wrong RPC correlation or new binding invalidates session without retries',async()=>{
 for(const key of ['sequence','request_id','binding','nonce']){
 const h=harness((path,body)=>path.endsWith('/rpc')?response({session_id:body.session_id,sequence:body.sequence,next_sequence:body.sequence+1,request_id:body.request_id,binding,values,...(key==='sequence'?{sequence:55}:key==='request_id'?{request_id:'wrong'}:key==='binding'?{binding:{...binding,revision:9}}:{nonce:'unexpected'})}):undefined)
 await h.client.open(binding);await assert.rejects(h.client.readValues());assert.equal(h.client.getSession(),undefined);assert.equal(h.calls.length,2)
 }
})
test('failed or unknown RPC consumes the local sequence and closes session, never resends',async()=>{
 const h=harness((path)=>{if(path.endsWith('/rpc'))throw Error('connection lost')});await h.client.open(binding);await assert.rejects(h.client.readValues());assert.equal(h.client.getSession(),undefined);await assert.rejects(h.client.readValues());assert.equal(h.calls.length,2)
})
test('late open response after invalidation cannot publish stale schema',async()=>{
 let resolve;const client=createPackageUiClient({fetcher:()=>new Promise(done=>resolve=done)});const pending=client.open(binding);client.invalidate();resolve(response(session()));await assert.rejects(pending,/旧会话/);assert.equal(client.getSession(),undefined)
})
test('late old open cannot invalidate or unlock a newer session',async()=>{
 let resolveFirst;let calls=0
 const client=createPackageUiClient({fetcher:()=>{calls++;return calls===1?new Promise(done=>resolveFirst=done):Promise.resolve(response(session({session_id:'new-session-12345678901234567890'})))}})
 const old=client.open(binding);client.invalidate();const fresh=await client.open(binding)
 resolveFirst(response(session()));await assert.rejects(old,/旧会话/)
 assert.equal(client.getSession().session_id,fresh.session_id)
})
test('101-character dotted tool names use exactly the backend manifest grammar',()=>{
 const tool='e'+'.'.repeat(100);const changed={...schema,actions:[{id:'echo',label:'Echo',kind:'tool',tool,arguments:{text:'text'}}]}
 assert.equal(parsePackageUiSchema(changed).actions[0].tool.length,101)
 assert.throws(()=>parsePackageUiSchema({...changed,actions:[{...changed.actions[0],tool:tool+'a'}]}))
 assert.throws(()=>parsePackageUiSchema({...changed,actions:[{...changed.actions[0],tool:'bad tool'}]}))
})
test('optional explicit null remains null instead of restoring defaults',()=>{
 const parsed=parsePackageUiSchema(schema);const submitted=packageUiEditableValues(parsed,{...values,count:null,mode:null})
 assert.equal(submitted.count,null);assert.equal(submitted.mode,null)
})
test('read-only fields are displayed from host and never submitted in RPC values',async()=>{
 const h=harness();await h.client.open(binding);await h.client.readValues();const result=await h.client.previewAction('echo',{...values,summary:'attempted edit'});assert.equal(result.preview.snapshot.action,'call');equal(h.calls[2].body.params.values,{text:'hello',count:2,enabled:false,mode:'brief'});assert.ok(!('summary'in h.calls[2].body.params.values))
})
test('tool previews preserve exact mapped args and bind manager action version',async()=>{
 const h=harness((path,body)=>path.endsWith('/rpc')&&body.method==='preview_action'?response({session_id:body.session_id,sequence:body.sequence,next_sequence:body.sequence+1,request_id:body.request_id,binding,preview:preview('call',{args:{text:'changed'}}),execution:'separate-explicit-user-confirmation-required',execute_path:'/v1/plugin-packages/calls/execute'}):undefined)
 await h.client.open(binding);await h.client.readValues();await assert.rejects(h.client.previewAction('echo',values),/不一致/);assert.equal(h.client.getSession(),undefined)
 assert.match(packageUiApprovalProblem(preview('state'),session(),schema.actions[0]),/不一致/)
})
test('readonly tool arguments use last host values, never a client edit',async()=>{
 const readonlySchema={...schema,actions:[{id:'echo',label:'echo',kind:'tool',tool:'echo',arguments:{text:'summary'}}]}
 const h=harness((path,body)=>{
  if(path.endsWith('/open'))return response(session({schema:readonlySchema}))
  if(body.method==='preview_action')return response({session_id:body.session_id,sequence:body.sequence,next_sequence:body.sequence+1,request_id:body.request_id,binding,preview:preview('call',{args:{text:'host value'}}),execution:'separate-explicit-user-confirmation-required',execute_path:'/v1/plugin-packages/calls/execute'})
 })
 await h.client.open(binding);await h.client.readValues();const result=await h.client.previewAction('echo',{...values,summary:'changed'});assert.equal(result.preview.snapshot.args.text,'host value');assert.ok(!('summary'in h.calls[2].body.params.values))
})
test('late RPC after disable-style invalidation never publishes or reuses the session',async()=>{
 let resolve;const h=harness((path)=>path.endsWith('/rpc')?new Promise(done=>resolve=done):undefined);await h.client.open(binding)
 const pending=h.client.readValues();h.client.invalidate();const body=h.calls[1].body;resolve(response({session_id:body.session_id,sequence:body.sequence,next_sequence:body.sequence+1,request_id:body.request_id,binding,values}));await assert.rejects(pending,/旧界面/);assert.equal(h.client.getSession(),undefined)
})
test('same-session schema cannot change and expiry blocks approval even if token lives',async()=>{
 const h=harness((path,body)=>path.endsWith('/rpc')?response({session_id:body.session_id,sequence:body.sequence,next_sequence:body.sequence+1,request_id:body.request_id,binding,schema:{...schema,title:'changed schema'}}):undefined)
 await h.client.open(binding);await assert.rejects(h.client.schema(),/已改变/);assert.equal(h.client.getSession(),undefined)
 assert.match(packageUiApprovalProblem(preview(),session({expires_at:0}),schema.actions[0]),/会话已过期/)
})
test('save previews update only declared source fields and keep harmless existing values visible',async()=>{
 const h=harness();await h.client.open(binding);await h.client.readValues();const config=await h.client.previewAction('save',values);assert.equal(config.preview.snapshot.action,'config');assert.equal(config.preview.snapshot.value.untouched,'preserved');assert.equal(h.calls[2].body.method,'preview_action')
})
test('invalid input or undefined actions do not reach RPC transport',async()=>{
 const h=harness();await h.client.open(binding);await assert.rejects(h.client.previewAction('not-declared',values));await assert.rejects(h.client.previewAction('echo',{...values,count:100}));assert.equal(h.calls.length,1)
})
test('schema and RPC body byte limits reject oversized data before transport',async()=>{
 const huge={...schema,fields:Array.from({length:50},(_,i)=>({id:'x'+i,type:'textarea',label:'x',default:'字'.repeat(1000)})),actions:[]}
 assert.throws(()=>parsePackageUiSchema(huge),/32 KiB/)
 const bounded={schema_version:1,title:'large input',fields:Array.from({length:5},(_,i)=>({id:'x'+i,type:'textarea',label:'x',max_length:16000})),actions:[{id:'save',label:'save',kind:'save_config'}]}
 const h=harness(path=>path.endsWith('/open')?response(session({schema:bounded})):undefined)
 await h.client.open(binding)
 await assert.rejects(h.client.previewAction('save',Object.fromEntries(bounded.fields.map(field=>[field.id,'字'.repeat(5000)]))),/64 KiB/)
 assert.equal(h.calls.length,1)
})
test('read values reject missing, unknown, sensitive fields and invalidate session',async()=>{
 for(const valuesPatch of [{text:'hello'},{...values,hidden:'unexpected'},{...values,text:'sk-abcdefghijklmnopqrst'}]){
  const h=harness((path,body)=>path.endsWith('/rpc')?response({session_id:body.session_id,sequence:body.sequence,next_sequence:body.sequence+1,request_id:body.request_id,binding,values:valuesPatch}):undefined)
  await h.client.open(binding);await assert.rejects(h.client.readValues());assert.equal(h.client.getSession(),undefined)
 }
})
test('component uses explicit human dialog, clears sessions after writes and never embeds executable markup',()=>{
 const panel=readFileSync(new URL('../src/components/skills/PackageUiPanel.tsx',import.meta.url),'utf8')
 for(const token of ['dangerouslySetInnerHTML','<iframe','eval(','new Function','window.postMessage'])assert.ok(!panel.includes(token))
 assert.match(panel,/打开声明式界面/);assert.match(panel,/批准本次快照（一次性）/);assert.match(panel,/setAttempted\(true\);setPending\(undefined\);client\.invalidate/);assert.match(panel,/packages\.execute\(value\.preview\)/);assert.match(panel,/packages\.commit\(value\.preview\)/)
 const owner=readFileSync(new URL('../src/components/skills/PluginPackagesPanel.tsx',import.meta.url),'utf8');assert.match(owner,/<PackageUiPanel/);assert.match(owner,/item\.missing \|\| item\.files_mismatch \|\| item\.blocked\.length/)
})
test('wire paths and response envelope match actual backend read-only/preview-only RPC',()=>{
 const backend=readFileSync(new URL('../../app/package_ui.py',import.meta.url),'utf8');const routes=readFileSync(new URL('../../app/plugin_api.py',import.meta.url),'utf8')
 for(const field of ['session_id','request_id','sequence','next_sequence','binding','approval_required','allowed_methods','preview_action'])assert.ok(backend.includes(field))
 assert.ok(routes.includes('/{plugin_id}/ui/open'));assert.ok(routes.includes('/{plugin_id}/ui/rpc'));assert.ok(!source.includes("rpc('execute'"))
})
