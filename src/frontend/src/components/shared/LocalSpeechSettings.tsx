import {useEffect,useRef,useState} from 'react'
import {toast} from 'sonner'
import {api,apiBlob,json} from '@/api/client'
import {fileToBase64} from '@/lib/utils'
import {useUnsavedChanges} from './UnsavedChanges'
import {Button} from '@/components/ui/button'
import {Input,Textarea} from '@/components/ui/input'
import {Select} from '@/components/ui/select'

type Voice={id:string;name:string;prompt_lang:string;text_lang?:string;gpt_weights:string;sovits_weights:string;reference_audio:string;reference_text:string}
type Config={revision:number;device:string;package_dir?:string;hotwords?:string[];voices?:Voice[];default_voice?:string;speed?:number;engine?:string}
const endpoints:Record<string,string>={'gpt-sovits-v2':'/v6/local-tts','paraformer-zh-offline':'/v6/local-stt'}
export const hasSpeechModelSettings=(id:string)=>['sensevoice-small','cosyvoice2','indextts2.5','gpt-sovits-v2','paraformer-zh-offline'].includes(id)

/** The model detail owns closing and the discard prompt. */
export function LocalSpeechSettings({engineId,onDirtyChange,onSaved}:{engineId:string;onDirtyChange:(dirty:boolean)=>void;onSaved:()=>void}){
 const [config,setConfig]=useState<Config>(),[saved,setSaved]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(''),[reload,setReload]=useState(0),[sample,setSample]=useState('你好，我在这里。')
 const live=useRef(false),operation=useRef<AbortController|undefined>(undefined),gate=useRef(false),audio=useRef<HTMLAudioElement|undefined>(undefined),audioUrl=useRef('')
 const tts=['gpt-sovits-v2','cosyvoice2','indextts2.5'].includes(engineId),gpt=engineId==='gpt-sovits-v2',native=!!endpoints[engineId]
 const endpoint=endpoints[engineId]||`/v6/speech/${engineId}/settings`
 const dirty=!!config&&JSON.stringify(config)!==saved
 useUnsavedChanges(dirty)
 useEffect(()=>{onDirtyChange(dirty)},[dirty,onDirtyChange])
 const stopAudio=()=>{audio.current?.pause();audio.current=undefined;if(audioUrl.current)URL.revokeObjectURL(audioUrl.current);audioUrl.current=''}
 useEffect(()=>{
  live.current=true;const controller=new AbortController();setConfig(undefined);setSaved('');setError('')
  void api<Config|{settings:Config}>(endpoint,{signal:controller.signal}).then(value=>{if(!controller.signal.aborted){const next='settings'in value?value.settings:value;setConfig(next);setSaved(JSON.stringify(next))}}).catch(reason=>{if(!controller.signal.aborted)setError(reason.message)})
  return()=>{live.current=false;controller.abort();operation.current?.abort();stopAudio();onDirtyChange(false)}
 },[engineId,reload])
 const run=async(name:string,task:(signal:AbortSignal)=>Promise<void>)=>{
  if(gate.current)return;gate.current=true;setBusy(name);setError('');const controller=new AbortController();operation.current=controller
  try{await task(controller.signal)}catch(reason){if(live.current&&!controller.signal.aborted)setError((reason as Error).message)}finally{gate.current=false;if(live.current)setBusy('')}
 }
 const selected=config?.voices?.find(voice=>voice.id===config.default_voice)
 const change=(patch:Partial<Config>)=>setConfig(current=>current?{...current,...patch}:current)
 const changeVoice=(patch:Partial<Voice>)=>change({voices:config!.voices!.map(voice=>voice.id===selected?.id?{...voice,...patch}:voice)})
 const newVoice=(copy=false)=>{if(!config)return;const id='voice_'+crypto.randomUUID().replaceAll('-','').slice(0,16);const base:Voice={id,name:'新音色',prompt_lang:'zh',text_lang:'all_zh',gpt_weights:'',sovits_weights:'',reference_audio:'',reference_text:''};change({default_voice:id,voices:[...(config.voices||[]),{...base,...(copy?selected:{}),id,name:copy?selected!.name+'（副本）':'新音色'}]})}
 if(!config)return <div role={error?'alert':'status'}>{error||'正在读取设置…'}{error&&<Button size="sm" variant="ghost" onClick={()=>setReload(value=>value+1)}>重试</Button>}</div>
 return <section className="speech-model-settings" aria-label="模型设置">
  {error&&<p className="personal-error" role="alert">{error}</p>}
  <fieldset disabled={!!busy}>
   {config.package_dir!==undefined&&<label>安装目录<Input aria-label="模型安装目录" value={config.package_dir} onChange={event=>change({package_dir:event.target.value})}/></label>}
   {engineId==='sensevoice-small'?<><p className="speech-device-note">推理设备 · CPU</p><label>热词纠正<Textarea aria-label="热词纠正规则" placeholder="原识别词 =&gt; 替换词，每行一条" value={config.hotwords?.join('\n')||''} onChange={event=>change({hotwords:event.target.value.split('\n').filter(value=>value.trim())})}/></label></>:<label>推理设备<Select aria-label="语音推理设备" value={config.device} onChange={event=>change({device:event.target.value})}><option value="cpu">CPU</option>{engineId!=='paraformer-zh-offline'&&<option value={native?'cuda':'cuda:0'}>GPU / CUDA</option>}</Select></label>}
   {tts&&<>
    <label>默认音色<Select aria-label="默认音色" value={config.default_voice||''} onChange={event=>change({default_voice:event.target.value})}>{config.voices?.map(voice=><option key={voice.id} value={voice.id}>{voice.name}</option>)}</Select></label>
    {selected&&<label>音色名称<Input aria-label="音色名称" required maxLength={100} value={selected.name} onChange={event=>changeVoice({name:event.target.value})}/></label>}
    <div className="local-model-detail-actions"><Button size="sm" variant="outline" disabled={(config.voices?.length||0)>=30} onClick={()=>newVoice()}>添加音色</Button><Button size="sm" variant="ghost" disabled={!selected||(config.voices?.length||0)>=30} onClick={()=>newVoice(true)}>复制音色</Button><Button size="sm" variant="ghost" disabled={!selected||(config.voices?.length||0)<=1} onClick={()=>{const voices=config.voices!.filter(voice=>voice.id!==selected!.id);change({voices,default_voice:voices[0].id})}}>移除音色</Button></div>
    {selected&&<>
     {gpt&&<div className="speech-settings-grid"><label>GPT 权重路径<Input value={selected.gpt_weights} onChange={event=>changeVoice({gpt_weights:event.target.value})}/></label><label>SoVITS 权重路径<Input value={selected.sovits_weights} onChange={event=>changeVoice({sovits_weights:event.target.value})}/></label></div>}
     <label>参考录音<Input aria-label="参考录音路径" value={selected.reference_audio} onChange={event=>changeVoice({reference_audio:event.target.value})}/></label>
     <label className="speech-reference-upload">导入 WAV<input aria-label="导入参考录音" type="file" accept=".wav,audio/wav" onChange={event=>{const file=event.target.files?.[0];event.target.value='';if(file)void run('import',async signal=>{if(file.size>8*1024*1024)throw Error('参考录音最大 8 MiB');const result=await api<{path:string}>('/v6/local-tts/reference',{...json('POST',{data:await fileToBase64(file)}),signal});if(live.current)changeVoice({reference_audio:result.path})})}}/><small>3～10 秒，最大 8 MiB</small></label>
     <label>参考录音原文<Textarea aria-label="参考录音原文" maxLength={2000} value={selected.reference_text} onChange={event=>changeVoice({reference_text:event.target.value})}/></label>
     {gpt&&<div className="speech-settings-grid"><label>参考录音语言<Select aria-label="参考录音语言" value={selected.prompt_lang} onChange={event=>changeVoice({prompt_lang:event.target.value})}>{[['zh','中文'],['ja','日语'],['en','英语'],['ko','韩语'],['yue','粤语']].map(([id,label])=><option key={id} value={id}>{label}</option>)}</Select></label><label>输出语言<Select aria-label="合成输出语言" value={selected.text_lang||'auto'} onChange={event=>changeVoice({text_lang:event.target.value})}><option value="auto">自动</option><option value="all_zh">中文</option><option value="all_ja">日语</option><option value="en">英语</option></Select></label></div>}
    </>}
    <label>语速<Input aria-label="语速" type="number" min={0.5} max={2} step={0.1} value={config.speed||1} onChange={event=>change({speed:Number(event.target.value)})}/></label>
   </>}
  </fieldset>
  <div className="local-model-detail-actions"><Button size="sm" disabled={!dirty||!!busy||Boolean(tts&&config.voices?.some(voice=>!voice.name.trim()))} onClick={()=>void run('save',async signal=>{
   const {revision,engine,...values}=config
   const result=await api<Config>(endpoint,{...json('PUT',{...values,expected_revision:revision}),signal})
   if(live.current){setConfig(result);setSaved(JSON.stringify(result));onSaved();toast.success('模型设置已保存')}
  })}>{busy==='save'?'保存中…':'保存设置'}</Button></div>
  {tts&&<div className="speech-settings-preview"><label>试听文字<Input aria-label="试听文字" value={sample} maxLength={500} onChange={event=>setSample(event.target.value)}/></label><Button variant="outline" size="sm" disabled={!!busy||dirty||!sample.trim()} onClick={()=>void run('preview',async signal=>{stopAudio();const blob=await apiBlob(`/v6/speech/${engineId}/synthesize`,{...json('POST',{text:sample}),signal});if(!live.current||signal.aborted)return;audioUrl.current=URL.createObjectURL(blob);audio.current=new Audio(audioUrl.current);audio.current.onended=stopAudio;await audio.current.play()})}>试听</Button><Button variant="ghost" size="sm" onClick={()=>{if(busy==='preview')operation.current?.abort();stopAudio()}}>停止</Button></div>}
 </section>
}
