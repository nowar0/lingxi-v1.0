import {PackageCleanup,RuntimeCleanup} from './ManagementExtras'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api, apiBlob } from '@/api/client'
import { useApp } from '@/context/AppContext'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {LocalSpeechSettings,hasSpeechModelSettings} from '@/components/shared/LocalSpeechSettings'
import { createSpeechPackagesController, emptySpeechSnapshot, type SpeechSnapshot } from './speech-package-controller'

const size = (bytes: number) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`
// Short descriptions are based on the projects' official model documentation.
const features:Record<string,string>={
  'sensevoice-small':'离线语音识别，支持多语言、情绪与声音事件识别。',
  'cosyvoice2':'语音合成，支持参考音频复刻音色与流式生成。',
  'indextts2.5':'语音合成，可用一段参考音频复刻音色并控制情绪。',
  'gpt-sovits-v2':'少量参考音频即可合成相近音色，也支持训练专属音色。',
  'paraformer-zh-offline':'中文离线语音识别，适合本机语音输入。',
}
export function SpeechPackagesPanel({query=''}:{query?:string}) {
  const { modules, refreshModels, connection } = useApp()
  const [state, setState] = useState<SpeechSnapshot>(emptySpeechSnapshot)
  const [dirty,setDirty]=useState(false),[discard,setDiscard]=useState(false)
  const [selectedId,setSelectedId]=useState('')
  const controller = useRef<ReturnType<typeof createSpeechPackagesController> | undefined>(undefined)
  const refreshRef = useRef(refreshModels); refreshRef.current = refreshModels
  useEffect(() => {
    if (connection !== 'connected') { setState(emptySpeechSnapshot()); return }
    const current = createSpeechPackagesController({ api, blob: apiBlob, changed: setState, modelsChanged: () => refreshRef.current(), success: text => toast.success(text) })
    controller.current = current
    void current.start()
    return () => { current.dispose(); if (controller.current === current) controller.current = undefined }
  }, [connection])

  const { packages, engines, busy, error, device } = state
  const visiblePackages=[...packages,...engines.filter(engine=>!packages.some(item=>item.id===engine.engine)).map(engine=>({id:engine.engine,name:engine.name,kind:engine.kind,size:0,installed:engine.installed,runtime_ready:true,version:'',pointer_revision:0,versions:[],license:'见官方许可',device_hint:engine.device,source:''}))] as typeof packages
  const requestClose=()=>{if(dirty)setDiscard(true);else{controller.current?.stopAudio();setSelectedId('')}}
  const disabled = !!busy || connection !== 'connected'
  const chosen = visiblePackages.find(item => item.id === selectedId)
  const engine = engines.find(item => item.engine === chosen?.id)
  const downloading = ['queued','downloading','preparing_runtime'].includes(chosen?.job?.status || '')
  const status = (item: typeof packages[number]) => {
    const assigned = modules.some(module => module.enabled && module.model === item.id)
    return assigned ? '当前使用' : item.installed ? item.runtime_ready ? '已安装' : '待准备环境' : '未安装'
  }
  return <>
    {error && <p className="personal-error" role="alert">{error}</p>}
    {connection === 'connected' && visiblePackages.filter(item=>(item.name+' '+item.id).toLowerCase().includes(query.toLowerCase())).map(item => <article className="local-model-row speech-model-row" key={item.id}>
      <div><h3>{item.name}</h3><p className="speech-model-feature">{features[item.id]||(item.kind==='embedding'?'本地向量模型':item.kind==='image'?'本地图像模型':'本地语音模型')}</p><p className="speech-model-state">{status(item)}</p></div>
      <div className="local-model-row-actions">{item.job&&['queued','downloading','preparing_runtime'].includes(item.job.status)?<Button size="sm" variant="outline" disabled={disabled} onClick={()=>void controller.current?.pause(item)}>暂停</Button>:packages.some(pkg=>pkg.id===item.id)&&(!item.installed||!item.runtime_ready)?<Button size="sm" disabled={disabled} onClick={()=>void controller.current?.download(item)}>{item.job?.status==='paused'?'继续下载':'下载'}</Button>:null}{engines.some(engine=>engine.engine===item.id)&&item.installed&&item.runtime_ready&&<Button size="sm" disabled={disabled} onClick={()=>void controller.current?.activate(item.id)}>{busy===item.id?'启用中…':modules.some(module=>module.enabled&&module.model===item.id)?'重新检查':'启用'}</Button>}{(busy===item.id||engines.some(engine=>engine.engine===item.id&&(engine.ready||engine.loading)))&&<Button size="sm" variant="ghost" disabled={!!busy&&busy!==item.id} onClick={()=>void controller.current?.stop(item.id)}>{busy===item.id?'停止':'释放'}</Button>}<Button size="sm" variant="outline" onClick={() => {setDirty(false);setSelectedId(item.id)}}>详情</Button></div>
    </article>)}
    <Dialog open={Boolean(chosen)} onOpenChange={open => { if (!open) requestClose() }}><DialogContent className="local-model-detail-dialog speech-model-detail-dialog" onCloseRequest={requestClose}>
      <DialogHeader><DialogTitle>{chosen?.name}</DialogTitle><DialogDescription>{chosen ? features[chosen.id]||status(chosen) : ''}</DialogDescription></DialogHeader>
      {chosen && <div className="local-model-detail-body">
        <div className="speech-model-detail-top"><span>{status(chosen)}</span><div className="local-model-detail-actions">{/^https?:\/\//.test(chosen.source)&&<a href={chosen.source} target="_blank" rel="noopener noreferrer">打开官网</a>}{downloading?<Button size="sm" variant="outline" disabled={disabled} onClick={()=>void controller.current?.pause(chosen)}>暂停下载</Button>:packages.some(item=>item.id===chosen.id)&&(!chosen.installed||!chosen.runtime_ready)?<Button size="sm" disabled={disabled} onClick={()=>void controller.current?.download(chosen)}>{chosen.job?.status==='paused'?'继续下载':'下载模型'}</Button>:null}</div></div>
        <details className="speech-model-info"><summary>模型信息</summary><dl>
          <div><dt>来源</dt><dd>{!packages.some(item=>item.id===chosen.id)?chosen.installed?'本机安装':'尚未配置':!chosen.installed ? '可下载' : !chosen.versions.length ? '随包' : '本地添加'}</dd></div>
          <div><dt>用途</dt><dd>{chosen.kind === 'stt' ? '语音输入' : chosen.kind === 'tts' ? '语音输出' : chosen.kind === 'embedding' ? '向量检索' : chosen.kind === 'image' ? '图像处理' : '本地模型'}</dd></div>
          {chosen.size>0&&<div><dt>文件大小</dt><dd>{size(chosen.size)}</dd></div>}
          {chosen.version&&<div><dt>版本</dt><dd>{chosen.active_version || chosen.version}</dd></div>}
          <div><dt>运行设备</dt><dd>{chosen.device_hint}</dd></div>
          <div><dt>许可</dt><dd>{chosen.license}</dd></div>
        </dl></details>
        {engine && <p role="status">{engine.loading ? '正在加载' : engine.ready ? '运行中' : engine.installed ? '尚未运行' : '尚未安装'}{engine.last_error ? ' · ' + engine.last_error : ''}</p>}
        {state.optionErrors?.[chosen.id] && <p className="personal-error" role="alert">{state.optionErrors[chosen.id]}</p>}
        {chosen.job && <div className="evo-download-state"><p>{chosen.job.phase}{chosen.job.error ? '：' + chosen.job.error : ''}</p>{chosen.job.status === 'downloading' && <progress aria-label="下载进度" {...(chosen.job.total_bytes > 0 ? {max:chosen.job.total_bytes,value:Math.min(chosen.job.total_bytes,chosen.job.completed_bytes+chosen.job.current_bytes)} : {})}/>}</div>}
        {hasSpeechModelSettings(chosen.id)&&<LocalSpeechSettings key={chosen.id} engineId={chosen.id} onDirtyChange={setDirty} onSaved={()=>{void controller.current?.refresh()}}/>}
        {(chosen.source||chosen.versions.length>0||packages.some(item=>item.id===chosen.id)&&chosen.kind==='tts')&&<details className="personal-disclosure"><summary>更多管理</summary>{engine&&chosen.kind==='tts'&&<div className="local-model-detail-actions"><Button size="sm" variant="outline" disabled={disabled} onClick={() => void controller.current?.detectDevice()}>检测推理设备</Button><span>{device ? device.cuda ? (device.gpu || 'GPU') + ' · ' + device.vram_mb + ' MiB 显存' : 'CPU 可用' : '尚未检测'}</span></div>}<div className="local-model-detail-actions">{chosen.versions.filter(version=>version!==chosen.active_version).map(version=><Button key={version} size="sm" variant="outline" disabled={disabled} onClick={() => void controller.current?.switchVersion(chosen,version)}>切回 {version}</Button>)}{chosen.versions.map(version=><PackageCleanup key={version} id={chosen.id} version={version} onDone={()=>void controller.current?.refresh()}/>)}</div>{chosen.kind==='tts'&&packages.some(item=>item.id===chosen.id)&&<RuntimeCleanup/>}{/^https?:\/\//.test(chosen.source)&&<a href={chosen.source} target="_blank" rel="noopener noreferrer">官方来源与许可</a>}</details>}
      </div>}
      {error&&<p className="personal-error" role="alert">{error}</p>}
    </DialogContent></Dialog>
    <Dialog open={discard} onOpenChange={setDiscard}><DialogContent className="max-w-sm"><DialogHeader><DialogTitle>放弃未保存的修改？</DialogTitle><DialogDescription>本模型的修改尚未保存。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={()=>setDiscard(false)}>继续编辑</Button><Button onClick={()=>{setDiscard(false);setDirty(false);controller.current?.stopAudio();setSelectedId('')}}>放弃修改</Button></DialogFooter></DialogContent></Dialog>
  </>
}
