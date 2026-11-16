import React,{useState,useEffect,useCallback,useRef} from 'react';
import {VerificationReport} from '../vendor/dsh-task-board/src/client/board/VerificationReport';
import css from '../vendor/dsh-task-board/src/client/board.module.css';
import {api,requestError} from './api';
import {createRequestOwner} from './requestOwner';

function verification(report:any){
 const dest=report.destination||{model:report.reviewer||'',base_url:''};
 const route={provider:dest.base_url?new URL(dest.base_url).host:'',model:dest.model,reasoningEffort:''};
 const attempts=(report.attempts||[]).map((a:any,index:number)=>({index:index+1,at:a.at*1000,stage:a.stage,passed:!!a.passed,score:a.total||0,baseline:a.baselineTotal||0,
  criteria:(a.scores||[]).map((s:any)=>({...s,baseline:s.baselineScore,threshold:.65,passed:s.score>=.65})),
  findings:(a.findings||[]).map((f:any)=>f.gap),criterionFindings:(a.findings||[]).map((f:any)=>({...f,location:f.location==='TASK'?'task':f.location==='CONTEXT'?'workspace':'trajectory'})),
  usage:a.usage||{calls:a.rounds?.length||0,inputTokens:null,outputTokens:null,reasoningTokens:null,usageIncomplete:true},
  evidence:{hash:a.evidenceHash||'',chars:a.evidenceChars||0,entries:0,omittedCharacters:0,workspaceFiles:report.workspace?.changes?.length||0},
  route,channel:'explicit-tag',rounds:2,error:a.error,invalidReason:a.stage==='invalid'?'missing-finding':undefined}));
 return {contract:{enabled:true,modelSource:'configured',route,preset:'coding',threshold:.65},attempts,applicability:'enforced'} as any;
}

export function ReviewPanel({sid,jobId}:any){
 const [settings,setSettings]=useState<any>(null),[budget,setBudget]=useState<any>(null),[models,setModels]=useState<any>({}),[tasks,setTasks]=useState<any[]>([]);
 const [selected,setSelected]=useState(jobId||''),[snapshot,setSnapshot]=useState<any>(null),[taskLoading,setTaskLoading]=useState(false);
 const [paths,setPaths]=useState(''),[localConsent,setLocalConsent]=useState(false),[sendConsent,setSendConsent]=useState(false),[preview,setPreview]=useState<any>(null);
 const [busy,setBusy]=useState(''),[message,setMessage]=useState('');
 const alive=useRef(true),locked=useRef(false),settingsDirty=useRef(false),initialized=useRef(false),selectedRef=useRef(selected);
 selectedRef.current=selected;
 const reads=useRef(createRequestOwner()),taskReads=useRef(createRequestOwner());
 const detail=snapshot?.id===selected?snapshot.detail:null,disk=snapshot?.id===selected?snapshot.disk:null,report=snapshot?.id===selected?snapshot.report:null;
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;reads.current.invalidate();taskReads.current.invalidate()}},[]);
 const editSettings=(value:any)=>{settingsDirty.current=true;setSettings(value)};
 const load=useCallback(async()=>{
  const read=reads.current.begin();
  try{
   const [s,b,m,t]=await Promise.all([api('/v7/tasks/settings','GET',undefined,read.signal),api('/v7/tasks/budget','GET',undefined,read.signal),api('/v1/models','GET',undefined,read.signal),api('/v7/tasks?session_id='+sid,'GET',undefined,read.signal)]);
   if(!read.isCurrent()||!alive.current)return;
   if(!settingsDirty.current)setSettings(s);
   setBudget(b);setModels({...Object.fromEntries((m.models||[]).map((id:string)=>[id,id])),...(m.roles||{})});setTasks(t.items||[]);
   if(!initialized.current){initialized.current=true;if(!jobId&&t.items?.length)setSelected(t.items[0].id)}
  }catch(error){if(read.isCurrent()&&alive.current)setMessage(requestError(error))}
 },[sid,jobId]);
 useEffect(()=>{void load();return()=>reads.current.invalidate()},[load]);
 const loadTask=useCallback(async()=>{
  const read=taskReads.current.begin(),id=selected;
  setSnapshot(null);setPreview(null);setSendConsent(false);setTaskLoading(Boolean(id));
  if(!id)return;
  try{
   const [detail,disk,report]=await Promise.all([api('/v6/jobs/'+id,'GET',undefined,read.signal),api(`/v8/workbench/review/${id}/evidence`,'GET',undefined,read.signal),api(`/v7/tasks/${id}/review`,'GET',undefined,read.signal)]);
   if(read.isCurrent()&&alive.current&&selectedRef.current===id)setSnapshot({id,detail,disk,report});
  }catch(error){if(read.isCurrent()&&alive.current&&selectedRef.current===id)setMessage(requestError(error))}
  finally{if(read.isCurrent()&&alive.current)setTaskLoading(false)}
 },[selected]);
 useEffect(()=>{void loadTask();return()=>taskReads.current.invalidate()},[loadTask]);
 const selectTask=(id:string)=>{if(locked.current)return;taskReads.current.invalidate();selectedRef.current=id;setSnapshot(null);setPreview(null);setSendConsent(false);setLocalConsent(false);setPaths('');setMessage('');setSelected(id)};
 async function perform(label:string,fn:()=>Promise<any>){if(locked.current)return;locked.current=true;setBusy(label);setMessage('');try{await fn()}catch(error){if(alive.current)setMessage(requestError(error))}finally{locked.current=false;if(alive.current)setBusy('')}}
 async function saveSettings(){const r=await api('/v7/tasks/settings','PUT',{daily_tokens:Number(settings.daily_tokens),daily_media_calls:Number(settings.daily_media_calls||0),reply_tokens:Number(settings.reply_tokens),reviewer:settings.reviewer,auto_archive_days:Number(settings.auto_archive_days),expected_revision:settings.revision||0});if(alive.current){settingsDirty.current=false;setSettings(r);setMessage('预算与验收设置已保存')}}
 async function baseline(){if(!detail||taskLoading)return;const names=paths.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);await api(`/v8/workbench/review/${selected}/baseline`,'POST',{paths:names,confirm_local_capture:localConsent});await loadTask();setMessage('所选文件基线已仅在本机保存；尚未发送到模型')}
 async function prepare(){if(!detail||taskLoading)return;const data=await api(`/v8/workbench/review/${selected}/preview`,'POST');if(alive.current&&selectedRef.current===selected){setPreview(data);setSendConsent(false)}}
 async function run(){if(!detail||taskLoading||!preview||!sendConsent)return;await api(`/v7/tasks/${selected}/review`,'POST',{preview_token:preview.preview_token,payload_hash:preview.payload_hash,confirm_send:true});await loadTask();await load()}
 async function accept(){if(taskLoading||!detail||!report?.current||!report.report?.passed)return;await api(`/v6/jobs/${selected}/action`,'POST',{action:'accept',expected_revision:detail.revision});await loadTask();await load()}
 return <div className="lx-review-panel">
  <div className="lx-section-head"><div><h2>任务预算与独立验收</h2><p>三项编码判据 · A/B 换位评分 · 阈值 65% · 所选文件的宿主磁盘差异</p></div><button disabled={!!busy} onClick={()=>void perform('刷新',async()=>{await load();await loadTask()})}>刷新</button></div>
  {message&&<p role="status" className={message.includes('保存')?'lx-muted':'lx-error'}>{message}</p>}
  {busy&&<p role="status">{busy}…{busy==='独立评分'?'六次模型请求进行中，请保持页面打开。':''}</p>}
  <section className="lx-review-conclusion" aria-label="验收结论"><h3>{report?.report?(report.current?'已有当前验收报告':'证据变化，报告已过期'):'尚未完成独立验收'}</h3><p>{report?.report?'详细结论与证据见下方报告；配置状态不代表成果通过。':'选择任务、核对证据后发起验收。预算与裁判配置保留在设置中。'}</p></section><div className="lx-review-grid">
   <details className="lx-review-settings"><summary>预算与裁判设置</summary><section className="lx-panel"><h3>预算与裁判设置</h3>{settings&&<fieldset disabled={!!busy} className="lx-fields lx-stack" style={{border:0,padding:0,margin:0}}><label>任务每日 Token 上限<input type="number" min="0" value={settings.daily_tokens} onChange={e=>editSettings({...settings,daily_tokens:e.target.value})}/><small>0 不限制文字 Token；覆盖任务执行器和独立评分。</small></label><label>任务每日媒体调用次数上限<input type="number" min="0" max="100000" value={settings.daily_media_calls||0} onChange={e=>editSettings({...settings,daily_media_calls:e.target.value})}/><small>图像、语音等按次数单独控制。0 不限；开启文字预算时须另设媒体上限。</small></label><label>每次回复预留 Token<input type="number" min="128" max="32768" value={settings.reply_tokens} onChange={e=>editSettings({...settings,reply_tokens:e.target.value})}/></label><label>独立验收模型<select value={settings.reviewer} onChange={e=>editSettings({...settings,reviewer:e.target.value})}><option value="">关闭独立验收</option>{settings.reviewer&&!Object.prototype.hasOwnProperty.call(models,settings.reviewer)&&<option value={settings.reviewer}>当前配置 · {settings.reviewer}</option>}{Object.entries(models).map(([id,name])=><option key={id} value={id}>{id} · {String(name)}</option>)}</select></label><label>闲置工作会话自动归档天数<input type="number" min="0" value={settings.auto_archive_days} onChange={e=>editSettings({...settings,auto_archive_days:e.target.value})}/></label><button disabled={!!busy} onClick={()=>void perform('保存设置',saveSettings)}>保存设置</button></fieldset>}{budget&&<p className="lx-muted">今日已记账/保守预留：{budget.charged?.toLocaleString()} Token · {budget.unknown} 次用量未上报。未知用量保留预留，不按 0 处理。媒体 {budget.media_calls||0} 次，未知状态 {budget.media_unknown||0} 次，上限 {budget.media_limit||'未限制'} 次。</p>}</section></details>
   <section className="lx-panel"><h3>任务与证据</h3><label className="lx-field">工作任务<select disabled={!!busy} value={selected} onChange={e=>selectTask(e.target.value)}><option value="">选择任务</option>{tasks.map(j=><option key={j.id} value={j.id}>{j.document?.goal||j.goal||j.id} · {j.status}</option>)}</select></label>{!detail?<p className="lx-empty">{taskLoading?'正在读取任务…':selected?'任务读取未完成，请刷新重试。':'先选择一个任务。'}</p>:<><p className="lx-muted">状态：{detail.status} · {detail.nodes?.length} 个节点</p>{detail.status==='pending'&&<><label className="lx-field">本次要核对的文件（项目内相对路径，每行一个）<textarea value={paths} onChange={e=>setPaths(e.target.value)} rows={4}/></label><label><input type="checkbox" checked={localConsent} onChange={e=>setLocalConsent(e.target.checked)}/> 仅在本机采集以上文件作为执行前基线</label><button disabled={!localConsent||!paths.trim()||!!busy} onClick={()=>void perform('采集本机基线',baseline)}>记录执行前基线</button></>}{disk?.available?<p>{disk.selectedPaths.length} 个选定文件，当前 {disk.changes.length} 个有变化</p>:<p className="lx-muted">{disk?.reason}</p>}<div className="lx-actions"><button disabled={!disk?.available||detail.status!=='review'||!!busy} onClick={()=>void perform('准备证据预览',prepare)}>预览差异与发送目标</button><button disabled={!report?.current||!report?.report?.passed||detail.status!=='review'||!!busy} onClick={()=>void perform('确认任务完成',accept)}>确认任务完成</button></div></>}</section>
  </div>
  {disk?.available&&<section className="lx-panel lx-diff"><h3>本机磁盘差异</h3><p className="lx-muted">{disk.scope}</p>{disk.changes.length?disk.changes.map((c:any)=><details key={c.path}><summary>{c.status} · {c.path} {c.added!==undefined?`+${c.added} / −${c.removed}`:''}</summary><pre>{c.diff}</pre></details>):<p>选定文件尚无变化。</p>}</section>}
  {detail&&preview&&<section className="lx-panel lx-evidence-preview"><h3>确认将发送的证据</h3><p><strong>模型：</strong>{preview.destination.model} <strong>服务地址：</strong><code>{preview.destination.base_url}</code></p><p>三项判据分别交换 A/B 位置两轮，共六次调用；会产生该模型的实际费用并计入任务预算。</p><details open><summary>本次完整证据</summary><pre>{JSON.stringify(preview.payload,null,2)}</pre></details><label><input type="checkbox" checked={sendConsent} onChange={e=>setSendConsent(e.target.checked)}/> 我已检查以上内容，同意发送给这里显示的模型与服务地址</label><button disabled={!sendConsent||!!busy} onClick={()=>void perform('独立评分',run)}>确认发送并开始双盲评分</button></section>}
  {report?.report&&<section className={`lx-panel ${css.detailSection}`}><h3>独立验收报告</h3>{!report.current&&<p className="lx-error">证据已变化，旧报告不能用于确认完成。</p>}<VerificationReport verification={verification(report.report)}/><p className="lx-muted">{report.report.limitations}</p></section>}
 </div>
}
