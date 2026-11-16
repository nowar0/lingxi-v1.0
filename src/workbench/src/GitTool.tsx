import React,{useState,useEffect,useCallback,useRef} from 'react';
import {GraphDialog} from '../vendor/dsh-git-graph/src/client/graph/GraphDialog';
import {WorktreeManager} from '../vendor/dsh-git-graph/src/client/worktrees/WorktreeManager';
import {CreateBranchDialog} from '../vendor/dsh-git-graph/src/client/chips/CreateBranchDialog';
import {CreateWorktreeDialog} from '../vendor/dsh-git-graph/src/client/chips/CreateWorktreeDialog';
import {zh} from '../vendor/dsh-git-graph/src/client/locales';
import {confirmAction} from './confirm';
import {api,requestError} from './api';
import {createRequestOwner} from './requestOwner';
import {GitChanges} from './GitChanges';
const t=(key:string,values?:any)=>{let s=(zh as any)[key]||key;for(const [k,v]of Object.entries(values||{}))s=s.replaceAll('{'+k+'}',String(v));return s};
export default function GitPanel({sid}:{sid:number}){
 const [tab,setTab]=useState('graph'),[view,setView]=useState<any>(null),[message,setMessage]=useState(''),[dialog,setDialog]=useState(''),[commit,setCommit]=useState<any>(null);
 const [loadError,setLoadError]=useState('');
 const reads=useRef(createRequestOwner()),commitReads=useRef(createRequestOwner()),fetching=useRef<AbortSignal>();
 const load=useCallback(async()=>{
  if(fetching.current&&!fetching.current.aborted)return;
  const read=reads.current.begin();fetching.current=read.signal;
  try{const next=await api('/v8/workbench/git/branches?session_id='+sid,'GET',undefined,read.signal);if(read.isCurrent()){setView(next);setLoadError('')}}
  catch(error){if(read.isCurrent()){setView(null);setLoadError(requestError(error))}}
  finally{if(fetching.current===read.signal)fetching.current=undefined}
 },[sid]);
 useEffect(()=>{void load();const timer=window.setInterval(()=>{if(document.visibilityState==='visible')void load()},2500);return()=>{window.clearInterval(timer);reads.current.invalidate();commitReads.current.invalidate()}},[load]);
 const graph=useCallback((limit?:number)=>api(`/v8/workbench/git/graph?session_id=${sid}&limit=${limit||200}`),[sid]);
 const trees=useCallback(()=>api('/v8/workbench/git/worktrees?session_id='+sid),[sid]);
 async function change(action:string,name:string){if(!await confirmAction(`${action==='switch'?'切换到':'删除已合并的本地'}分支 ${name}？`))return;try{const r=await api('/v8/workbench/git/'+action,'POST',{session_id:sid,name,expected_root:view?.root||'',expected_head:view?.head||''});if(!r.ok)throw Error(r.error.message);await load();setTab('branches')}catch(e:any){setMessage(e.message)}}
 const select=async(oid:string)=>{const read=commitReads.current.begin();setCommit(null);try{const result=await api(`/v8/workbench/git/commit?session_id=${sid}&oid=${oid}`,'GET',undefined,read.signal);if(read.isCurrent())setCommit(result)}catch(error){if(read.isCurrent())setMessage(requestError(error))}};
 return <div className="lx-git-panel"><div className="lx-section-head"><div><h2>Git 项目</h2><p>{view?view.root:'当前工作会话的项目目录'}</p></div><div className="lx-actions"><button onClick={()=>void load()}>刷新</button><button disabled={!view?.canSwitch} title={view?.switchReason||'从当前提交创建并切换新分支'} onClick={()=>setDialog('branch')}>创建分支</button><button disabled={!view} title={!view?'先在工作记录中绑定 Git 项目目录':'创建 Worktree'} onClick={()=>setDialog('worktree')}>新建 worktree</button></div></div>
 {(message||loadError)&&<div role="alert" className="lx-error">{message||loadError}</div>}
 {!view?<p className="lx-empty">先在工作记录中选择一个 Git 项目目录。</p>:<><div className="lx-subtabs" role="tablist">{[['changes','本地更改'],['graph','提交图谱'],['branches','分支'],['worktrees','Worktree 管理']].map(([id,label])=><button key={id} role="tab" aria-selected={tab===id} onClick={()=>{setTab(id);setCommit(null)}}>{label}</button>)}<span className="lx-muted">{view.branch||'分离 HEAD'} · {view.dirtyFiles} 个修改 · {view.untrackedFiles} 个未跟踪文件</span></div>
 {tab==='changes'&&<GitChanges sid={sid} onChange={load}/>}
 {tab==='graph'&&<div className="lx-inline-graph"><GraphDialog key={view.branch} graph={graph} t={t as any} onClose={()=>setTab('branches')} onSelectCommit={oid=>void select(oid)}/></div>}
 {tab==='branches'&&<section className="lx-panel">{view.switchReason&&<p className="lx-muted" role="status">{view.switchReason}</p>}<div className="lx-list">{view.branches.map((b:any)=><div key={b.name} className="lx-list-row"><strong>{b.name}</strong><span>{b.current?'当前分支':''}</span><button disabled={b.current||!view.canSwitch} title={view.switchReason||'切换分支'} onClick={()=>void change('switch',b.name)}>切换</button><button disabled={b.current} onClick={()=>void change('branch-delete',b.name)}>删除</button></div>)}</div></section>}
 {tab==='worktrees'&&<div className="lx-inline-graph"><WorktreeManager key={view.branch} fetchWorktrees={trees} onRemove={async(path,options)=>{const r=await api('/v8/workbench/git/worktree-remove','POST',{session_id:sid,path,...options});if(r.warning)setMessage(r.warning);return r}} onClose={()=>setTab('graph')} t={t as any}/><WorktreeBindings sid={sid} trees={trees} onChange={load}/></div>}
 </>}
 {commit&&<section className="lx-panel lx-diff"><div className="lx-section-head"><h3>提交 {commit.oid.slice(0,12)}</h3><button onClick={()=>{commitReads.current.invalidate();setCommit(null)}}>收起差异</button></div><pre>{commit.diff}</pre></section>}
 {dialog==='branch'&&<CreateBranchDialog t={t as any} onClose={()=>setDialog('')} onCreate={async name=>{const r=await api('/v8/workbench/git/branch','POST',{session_id:sid,name,expected_root:view?.root||'',expected_head:view?.head||''});if(r.ok)await load();return r}}/>}
 {dialog==='worktree'&&view&&<CreateWorktreeDialog branches={view.branches} currentBranch={view.branch} t={t as any} onClose={()=>setDialog('')} onCreate={async(name,base)=>{const r=await api('/v8/workbench/git/worktree-add','POST',{session_id:sid,name,base:base||'HEAD'});if(r.ok){setMessage('已创建 '+r.path+'；可在 Worktree 管理中绑定');setTab('worktrees');await load()}return r}}/>}
 </div>
}
function WorktreeBindings({sid,trees,onChange}:any){const [rows,setRows]=useState<any[]>([]),[error,setError]=useState('');useEffect(()=>{trees().then((v:any)=>setRows(v.worktrees)).catch((e:any)=>setError(e.message))},[trees]);async function bind(path:string){if(!await confirmAction(`将当前工作会话的项目目录绑定到：\n${path}\n现有聊天历史保留。确认切换？`))return;try{const s=await api('/v6/work-settings/'+sid);await api('/v6/work-settings/'+sid,'PUT',{path,acceptance:s.acceptance||'',expected_revision:s.revision||0});await onChange()}catch(e:any){setError(e.message)}}return <div className="lx-panel"><h3>会话项目目录</h3>{error&&<p role="alert">{error}</p>}{rows.map(r=><div className="lx-list-row" key={r.path}><span>{r.branch||'分离 HEAD'}</span><code>{r.path}</code><button onClick={()=>void bind(r.path)}>绑定当前会话</button></div>)}</div>}

