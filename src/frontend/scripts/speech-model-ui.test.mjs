import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

// Run actual form handlers with synthetic requests; never use installed settings.
const clone=value=>JSON.parse(JSON.stringify(value))
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve}}
const nodes=(tree,predicate)=>{const result=[];const walk=node=>{if(Array.isArray(node))return node.forEach(walk);if(!node||typeof node!=='object')return;if(predicate(node))result.push(node);walk(node.props?.children)};walk(tree);return result}
const text=node=>Array.isArray(node)?node.map(text).join(''):node&&typeof node==='object'?text(node.props?.children):node??''
const find=(tree,label)=>nodes(tree,node=>node.props?.['aria-label']===label)[0]
const button=(tree,label)=>nodes(tree,node=>node.type==='button'&&text(node)===label)[0]
function harness(file,exportName,props,imports){
 const slots=[];let cursor=0,dirty=true,tree,effects=[]
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((value,i)=>Object.is(value,b[i]))
 const hooks={
  useState(initial){const i=cursor++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return [slots[i],value=>{const next=typeof value==='function'?value(slots[i]):value;if(!Object.is(next,slots[i])){slots[i]=next;dirty=true}}]},
  useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i]},
  useEffect(fn,deps){const i=cursor++,previous=slots[i];if(!previous||!same(previous.deps,deps)){slots[i]={deps,cleanup:previous?.cleanup};effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=fn()})}},
 }
 const jsx=(type,props)=>({type,props})
 const source=readFileSync(new URL('../src/components/'+file,import.meta.url),'utf8')
 const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText
 const exports={}
 const fixture={react:hooks,'react/jsx-runtime':{jsx,jsxs:jsx,Fragment:'fragment'},...imports}
 vm.runInNewContext(code,{exports,require:name=>{assert.ok(name in fixture,'unexpected dependency '+name);return fixture[name]},AbortController,AbortSignal,URL,JSON,crypto,Error,Audio:class{}},{filename:file})
 return {get tree(){return tree},async flush(){for(let i=0;i<16;i++){if(dirty){cursor=0;dirty=false;tree=exports[exportName](props);const queue=effects;effects=[];queue.forEach(fn=>fn())}await new Promise(resolve=>setImmediate(resolve));if(!dirty&&!effects.length)break}},unmount(){slots.forEach(slot=>slot?.cleanup?.())}}
}
const initial={revision:4,device:'cpu',hotwords:[],voices:[{id:'a',name:'音色甲',prompt_lang:'zh',text_lang:'all_zh',gpt_weights:'',sovits_weights:'',reference_audio:'',reference_text:''}],default_voice:'a',speed:1}
function modelUi(engineId,overrides={}){
 const h={calls:[],dirty:[],saved:0,notices:[]}
 h.ui=harness('shared/LocalSpeechSettings.tsx','LocalSpeechSettings',{engineId,onDirtyChange:value=>h.dirty.push(value),onSaved:()=>h.saved++},{
  'sonner':{toast:{success:value=>h.notices.push(value)}},
  '@/api/client':{json:(method,value)=>({method,body:JSON.stringify(value)}),apiBlob:async()=>({}),api:async(path,options={})=>{h.calls.push({path,...options});if(overrides.api)return overrides.api(path,options);if(options.method==='PUT')return {...JSON.parse(options.body),revision:5};const config=clone(initial);if(engineId==='sensevoice-small'){delete config.voices;delete config.default_voice;delete config.speed}if(engineId==='gpt-sovits-v2'){config.package_dir='models/gpt-sovits';return {settings:config}}return config}},
  '@/lib/utils':{fileToBase64:async()=>''},'./UnsavedChanges':{useUnsavedChanges:()=>{}},
  '@/components/ui/button':{Button:'button'},'@/components/ui/input':{Input:'input',Textarea:'textarea'},'@/components/ui/select':{Select:'select'},
 })
 return h
}
test('speech input exposes hotword correction without synthesis or unsupported GPU controls',async()=>{
 const h=modelUi('sensevoice-small');await h.ui.flush()
 assert.ok(find(h.ui.tree,'热词纠正规则'));assert.equal(find(h.ui.tree,'默认音色'),undefined);assert.equal(find(h.ui.tree,'语音推理设备'),undefined);assert.equal(button(h.ui.tree,'试听'),undefined)
 find(h.ui.tree,'热词纠正规则').props.onChange({target:{value:'零西 => 零汐'}});await h.ui.flush();assert.equal(h.dirty.at(-1),true)
 button(h.ui.tree,'保存设置').props.onClick();await h.ui.flush()
 const saved=h.calls.find(call=>call.method==='PUT');assert.equal(saved.path,'/v6/speech/sensevoice-small/settings');assert.deepEqual(JSON.parse(saved.body).hotwords,['零西 => 零汐']);assert.equal(JSON.parse(saved.body).expected_revision,4);assert.equal(h.dirty.at(-1),false);h.ui.unmount()
})
test('each synthesis model writes only its own endpoint and only GPT exposes GPT weights',async()=>{
 for(const id of ['cosyvoice2','indextts2.5','gpt-sovits-v2']){
  const h=modelUi(id);await h.ui.flush();assert.equal(find(h.ui.tree,'热词纠正规则'),undefined);assert.equal(text(h.ui.tree).includes('GPT 权重路径'),id==='gpt-sovits-v2')
  find(h.ui.tree,'音色名称').props.onChange({target:{value:'独立音色'}});await h.ui.flush();assert.equal(button(h.ui.tree,'试听').props.disabled,true)
  button(h.ui.tree,'保存设置').props.onClick();await h.ui.flush()
  const call=h.calls.find(call=>call.method==='PUT');assert.equal(call.path,id==='gpt-sovits-v2'?'/v6/local-tts':`/v6/speech/${id}/settings`);assert.equal(JSON.parse(call.body).voices[0].name,'独立音色');assert.equal(h.saved,1);h.ui.unmount()
 }
})
test('closing an unread form cancels its read and ignores the late response',async()=>{
 const response=deferred(),h=modelUi('cosyvoice2',{api:()=>response.promise});await h.ui.flush();h.ui.unmount();assert.equal(h.calls[0].signal.aborted,true);response.resolve(clone(initial));await h.ui.flush();assert.equal(h.saved,0);assert.equal(h.notices.length,0)
})
test('model detail X closes clean forms and offers continue/discard for edited forms',async()=>{
 const snapshot={packages:[{id:'sensevoice-small',name:'SenseVoice',kind:'stt',installed:true,runtime_ready:true,versions:[],device_hint:'CPU',license:'官方'}],engines:[{engine:'sensevoice-small',installed:true,ready:true}],error:'',busy:''}
 const controller={start:async()=>{},dispose:()=>{},stopAudio:()=>{},refresh:()=>{}}
 const dialogs=Object.fromEntries(['Dialog','DialogContent','DialogHeader','DialogDescription','DialogFooter','DialogTitle'].map(name=>[name,name]))
 const ui=harness('settings/SpeechPackagesPanel.tsx','SpeechPackagesPanel',{}, {
  './ManagementExtras':{PackageCleanup:'cleanup',RuntimeCleanup:'runtime'},'sonner':{toast:{}},'@/api/client':{},'@/context/AppContext':{useApp:()=>({modules:[],connection:'connected',refreshModels:async()=>{}})},'@/components/ui/button':{Button:'button'},'@/components/ui/dialog':dialogs,
  '@/components/shared/LocalSpeechSettings':{LocalSpeechSettings:'form',hasSpeechModelSettings:()=>true},
  './speech-package-controller':{emptySpeechSnapshot:()=>snapshot,createSpeechPackagesController:()=>controller},
 })
 await ui.flush();button(ui.tree,'详情').props.onClick();await ui.flush()
 let detail=nodes(ui.tree,node=>node.type==='Dialog')[0];assert.equal(detail.props.open,true);detail.props.onOpenChange(false);await ui.flush();assert.equal(nodes(ui.tree,node=>node.type==='Dialog')[0].props.open,false)
 button(ui.tree,'详情').props.onClick();await ui.flush();nodes(ui.tree,node=>node.type==='form')[0].props.onDirtyChange(true);await ui.flush()
 nodes(ui.tree,node=>node.type==='Dialog')[0].props.onOpenChange(false);await ui.flush();assert.equal(nodes(ui.tree,node=>node.type==='Dialog')[1].props.open,true)
 button(ui.tree,'继续编辑').props.onClick();await ui.flush();assert.equal(nodes(ui.tree,node=>node.type==='Dialog')[0].props.open,true)
 nodes(ui.tree,node=>node.type==='Dialog')[0].props.onOpenChange(false);await ui.flush();button(ui.tree,'放弃修改').props.onClick();await ui.flush();assert.equal(nodes(ui.tree,node=>node.type==='Dialog')[0].props.open,false);ui.unmount()
})
