import React,{useState,lazy,Suspense} from 'react';
import {createRoot,Root} from 'react-dom/client';
import {applyTheme} from './theme';
import './workbench.css';
const GitPanel=lazy(()=>import('./GitTool'));
const SshTool=lazy(()=>import('./SshTool'));
const ReviewPanel=lazy(()=>import('./review').then(value=>({default:value.ReviewPanel})));
class ToolBoundary extends React.Component<React.PropsWithChildren,{error:boolean}>{
 state={error:false};static getDerivedStateFromError(){return {error:true}}
 render(){return this.state.error?<div className="lx-empty" role="alert">工具加载失败。<button onClick={()=>location.reload()}>重新加载</button></div>:this.props.children}
}
function Workbench({initialTab,sid,jobId,onChat}:any){const [tab,setTab]=useState(initialTab||'git');return <div className="lx-workbench" data-ui="true"><div className="lx-workbar"><button onClick={onChat}>返回工作对话</button><nav aria-label="工作工具">{[['git','Git / Worktree'],['ssh','SSH / SFTP'],['review','预算与验收'],['appearance','主题与壁纸']].map(([id,label])=><button key={id} className={tab===id?'selected':''} aria-current={tab===id?'page':undefined} onClick={()=>id==='appearance'?(window as any).LingxiWorkbench.open('appearance'):setTab(id)}>{label}</button>)}</nav></div><div className="lx-workbody"><ToolBoundary><Suspense fallback={<p className="lx-empty" role="status">正在加载工具…</p>}>{tab==='ssh'?<SshTool/>:!sid?<div className="lx-empty">先选择或新建一个工作会话，再使用项目工具。</div>:tab==='git'?<GitPanel sid={sid}/>:<ReviewPanel sid={sid} jobId={jobId}/>}</Suspense></ToolBoundary></div></div>}
const roots=new Map<Element,Root>();
export function mount(element:Element,options:any){const root=createRoot(element);roots.set(element,root);root.render(<Workbench {...options}/>);applyTheme()}
export function unmountAll(){for(const root of roots.values())root.unmount();roots.clear()}
