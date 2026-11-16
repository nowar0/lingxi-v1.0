import {useEffect,useMemo,useRef,useState,type ReactNode} from 'react'
import {BookOpen,CalendarDays,Download,Focus,HelpCircle,List,MoreHorizontal,Pin,Plus,Search,Trash2,X} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
import {Dialog,DialogContent,DialogHeader,DialogTitle} from '@/components/ui/dialog'
import {MemoryToolsDrawer} from './MemoryToolsDrawer'
import {MemoryNebula} from './MemoryNebula'
import {memoryV2,type MemoryV2Catalog,type MemoryV2Graph,type MemoryV2Item,type MemoryV2Scope} from '@/lib/memoryV2'
import {LAYER_COLORS,LAYER_NAMES,type NebulaLayer} from '@/lib/nebula'
import {memoryV2NebulaData} from '@/lib/memoryV2'
import './memory-explorer.css'

type Props={scope:MemoryV2Scope;sessionId?:number;catalog?:MemoryV2Catalog;graph?:MemoryV2Graph;loading:boolean;error:string;selected?:MemoryV2Item;detailOpen:boolean;disabled:boolean;busy:boolean;connected:boolean;onViewList:()=>void;directoryRequest?:number;
 query:string;onQuery:(value:string)=>void;pinned:boolean;onPinned:(value:boolean)=>void;layer?:number;onLayer:(value:number|undefined)=>void;before:string;onBefore:(value:string)=>void;
 records:MemoryV2Item[];total:number;offset:number;includeDeleted:boolean;onIncludeDeleted:(value:boolean)=>void;onPage:(offset:number)=>void;onSelect:(id:string)=>void;onCloseDetail:()=>void;onNew:()=>void;onAccess:()=>void;onEdit:()=>void;onPin:()=>void;onPinRecord:(item:MemoryV2Item)=>void;onDelete:()=>void;onRestore:()=>void;onAdvanced:()=>void;onConnect:()=>void;curve:ReactNode}
const day=(date?:string|null)=>date?date.slice(0,10).replaceAll('-','.'):''
const layerKey=(layer:number)=>`L${layer}` as NebulaLayer

export function MemoryExplorer(p:Props){
 const [directoryOpen,setDirectoryOpen]=useState(false),[recordsOpen,setRecordsOpen]=useState(false),[helpOpen,setHelpOpen]=useState(false)
 const [mode,setMode]=useState<'nebula'|'graph'>('nebula'),[exporting,setExporting]=useState(false),[error,setError]=useState('')
 const [focusRequest,setFocusRequest]=useState(0)
 const input=useRef<HTMLInputElement>(null),data=useMemo(()=>memoryV2NebulaData(p.graph??{nodes:[],edges:[],items:[],total:0,offset:0,limit:200}),[p.graph])
 useEffect(()=>{const shortcut=(event:KeyboardEvent)=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'&&(!document.querySelector('[role="dialog"]')||!!document.querySelector('.memory-directory-drawer'))){event.preventDefault();event.stopImmediatePropagation();setDirectoryOpen(true);setRecordsOpen(false)}};document.addEventListener('keydown',shortcut,true);return()=>document.removeEventListener('keydown',shortcut,true)},[])
 useEffect(()=>{if(directoryOpen){const id=setTimeout(()=>input.current?.focus(),100);return()=>clearTimeout(id)}},[directoryOpen])
 useEffect(()=>{if(p.directoryRequest)setDirectoryOpen(true)},[p.directoryRequest])
 const all=()=>{p.onLayer(undefined);p.onPinned(false);p.onQuery('');p.onBefore('');p.onIncludeDeleted(false);setDirectoryOpen(false)}
 const exportData=async()=>{if(exporting)return;setExporting(true);setError('');try{const body=await memoryV2.exportData(p.scope,p.sessionId),blob=new Blob([JSON.stringify(body,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`lingxi-memory-${p.scope}-${new Date().toISOString().slice(0,10)}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),3000)}catch(reason){setError(reason instanceof Error?reason.message:String(reason))}finally{setExporting(false)}}
 const first=p.catalog?.first_date,last=p.catalog?.last_date,range=first&&last?Date.parse(last)-Date.parse(first):0
 const progress=!p.before||!first||!range?100:Math.max(0,Math.min(100,(Date.parse(p.before)-Date.parse(first))/range*100))
 const time=(value:number)=>{if(value>=100||!first||!last){p.onBefore('');return}p.onBefore(new Date(Date.parse(first)+range*value/100).toISOString().slice(0,10)+'T23:59:59.999Z')}
 const pick=(id:string)=>{if(p.graph&&!p.graph.nodes.some(node=>node.id===id)){p.onLayer(undefined);p.onPinned(false);p.onQuery('');p.onBefore('')}p.onSelect(id);setDirectoryOpen(false)}
 const item=p.selected,color=item?LAYER_COLORS[layerKey(item.layer)]:undefined
 const neighbors=new Map((p.graph?.nodes??[]).map(node=>[node.id,node]))
 const emotion=item?.valence!=null&&item?.arousal!=null
 return <div className={`memory-explorer-frame${p.detailOpen&&item?' has-inspector':''}`}>
  <div className="memory-explorer-main">
   <section className="memory-explorer-stage" aria-label="记忆宇宙">
    <MemoryNebula nodes={data.nodes} edges={data.edges} selectedId={p.detailOpen?item?.id:undefined} onClearSelection={p.onCloseDetail} onSelect={id=>pick(String(id))} onOpenList={p.onViewList} loading={p.loading} variant={mode} focusRequest={focusRequest}/>
    <div className="memory-explorer-heading"><h2>记忆有引力，<br/>我们因此相连。</h2><p>在星光之间，重新发现那些重要的瞬间。</p><div className="memory-explorer-stats" aria-live="polite"><span>{p.graph?.total??0} 条记忆</span><span>{p.graph?.edges.length??0} 条关联</span></div></div>
    <div className="memory-cosmos-mode" role="group" aria-label="星空显示方式"><button type="button" aria-pressed={mode==='nebula'} onClick={()=>setMode('nebula')}>星云</button><button type="button" aria-pressed={mode==='graph'} onClick={()=>setMode('graph')}>星图</button></div>
    {(p.layer||p.pinned||p.query||p.before)&&<div className="memory-explorer-filter">{[p.layer&&`L${p.layer}`,p.pinned&&'钉选',p.query&&`“${p.query}”`,p.before&&`${day(p.before)} 前`].filter(Boolean).join(' · ')}<button type="button" onClick={all}>清除</button></div>}
    {p.error&&<p role="alert" className="memory-explorer-error">{p.error}</p>}
    <p className="memory-cosmos-hint">拖动旋转 · 滚轮缩放 · 点击探索</p>
   </section>
   {p.detailOpen&&item&&<aside className="memory-inline-inspector" aria-label="记忆切片">
    <header><span>记忆切片</span><div><button type="button" aria-label={item.pinned?'取消钉选':'钉选记忆'} aria-pressed={item.pinned} disabled={p.disabled||p.busy||item.status!=='active'} title={item.pinned?'取消钉选':'钉选记忆'} onClick={p.onPin}><Pin/><span>{item.pinned?'已钉选':'钉选'}</span></button><button type="button" aria-label="详细记忆信息" onClick={p.onAdvanced}><MoreHorizontal/></button><button type="button" aria-label="关闭记忆详情" onClick={p.onCloseDetail}><X/></button></div></header>
    {p.disabled&&<p role="status" className="memory-inspector-notice">记忆正在校核，或已有更新。可关闭详情后在设置中检查更新。</p>}
    <div className="memory-inspector-type"><i style={{background:color}}/>{LAYER_NAMES[layerKey(item.layer)]}</div>{item.status==='deleted'&&<p className="memory-inspector-muted">已删除 · 可恢复</p>}<h3>{item.key}</h3>
    <p className="memory-inspector-date"><CalendarDays/>{day(item.occurred_at||item.created_at)}<span>{item.source?.kind==='manual'?'手动记录':item.source?.kind==='message'?'来自对话':'已记录'}</span></p>
    <blockquote>{item.value}</blockquote>{!!item.tags?.length&&<div className="memory-inspector-tags">{item.tags.map(tag=><span key={tag}>#{tag}</span>)}</div>}
    <section><div className="memory-inspector-section"><h4>情绪坐标</h4>{!emotion&&<button type="button" disabled={p.disabled||p.busy||item.layer===4||item.status!=='active'||(item.scope==='shared'&&item.source.kind==='memory')} onClick={p.onEdit}>补充</button>}</div>{emotion?<><div className="memory-emotion-map" role="img" aria-label={`感受 ${item.valence}，强度 ${item.arousal}`}><span className="top">强烈</span><span className="bottom">平静</span><span className="left">低落</span><span className="right">愉悦</span><i style={{left:`${8+item.valence!*84}%`,bottom:`${8+item.arousal!*84}%`,background:color}}/></div><div className="memory-emotion-values"><span>感受 {item.valence!.toFixed(2)}</span><span>强度 {item.arousal!.toFixed(2)}</span></div></>:<p className="memory-inspector-muted">尚未记录</p>}</section>
    <section><div className="memory-inspector-section"><h4>记忆留存</h4><strong>{Math.round(item.activation*100)}%</strong></div><p className="memory-inspector-muted">{item.half_life_days===null?'长期保留':`半衰期 ${item.half_life_days} 天`}{item.pinned?' · 已钉选':''}</p><div className="memory-retention-bar"><i style={{width:`${Math.round(item.activation*100)}%`}}/></div><details className="memory-retention-details"><summary>活跃度变化</summary>{p.curve}</details></section>
    <section><div className="memory-inspector-section"><h4>星光的连接</h4><button type="button" disabled={p.disabled||p.busy||item.status!=='active'||!item.eligible} onClick={p.onConnect}><Plus/>连接</button></div><div className="memory-inspector-relations">{item.relations?.map(edge=>{const target=edge.from_id===item.id?edge.to_id:edge.from_id,node=neighbors.get(target);return <button type="button" key={edge.id} disabled={p.disabled||p.busy} onClick={()=>pick(target)}><i style={{background:node?LAYER_COLORS[layerKey(node.layer)]:color}}/><span>{node?.key||'关联记忆'}<small>{edge.kind==='confirmed'?'已确认连接':'相似候选'} · {edge.label}</small></span></button>})}{!item.relations?.length&&<p className="memory-inspector-muted">暂无连接</p>}</div></section>
    <footer><button type="button" disabled={p.disabled||p.busy} onClick={()=>setFocusRequest(value=>value+1)}><Focus/>聚焦星点</button>{item.status==='deleted'?<button type="button" disabled={p.disabled||p.busy} onClick={p.onRestore}>恢复</button>:<><button type="button" disabled={p.disabled||p.busy||(item.scope==='shared'&&item.source.kind==='memory')} onClick={p.onEdit}>改一下</button><button type="button" disabled={p.disabled||p.busy} onClick={p.onAccess}>谁能使用</button><button type="button" aria-label="不再使用这条记忆（可恢复）" disabled={p.disabled||p.busy} onClick={p.onDelete}><Trash2/>不再使用</button></>}</footer>
   </aside>}
  </div>
  <footer className="memory-time-travel"><div><CalendarDays/><span>时间回溯</span></div><div className="memory-time-track"><div><span>{day(first)}</span><span>{day(last)}</span></div><input aria-label="记忆时间范围" type="range" min="0" max="100" value={progress} disabled={!first||!last||range<=0} onChange={event=>time(Number(event.target.value))}/></div><div><span>{p.before?`${day(p.before)} 前`:'全部时间'}</span><button type="button" disabled={!p.before} onClick={()=>p.onBefore('')}>重置</button></div></footer>
  <MemoryToolsDrawer open={directoryOpen} onOpenChange={setDirectoryOpen} title="记忆目录" className="memory-directory-drawer" side="right">
   {error&&<p role="alert">{error}</p>}<div className="memory-directory-identity"><BookOpen/><div><strong>我的记忆宇宙</strong><small>{({companion:'陪伴',work:'工作',shared:'共享'})[p.scope]}</small></div><i className={p.connected?'connected':''}/></div>
   <label className="memory-directory-search"><Search/><Input ref={input} aria-label="搜索记忆目录" value={p.query} onChange={event=>{p.onQuery(event.target.value);if(event.target.value)setRecordsOpen(true)}} placeholder="搜索记忆、标签…"/></label>
   <nav className="memory-directory-navigation" aria-label="记忆探索"><button type="button" onClick={all}><BookOpen/>全部记忆<span>{p.catalog?.total??0}</span></button><button type="button" aria-pressed={p.pinned} onClick={()=>{p.onPinned(!p.pinned);p.onLayer(undefined);setDirectoryOpen(false)}}><Pin/>钉选记忆<span>{p.catalog?.pinned??0}</span></button><button type="button" aria-pressed={recordsOpen} onClick={()=>setRecordsOpen(value=>!value)}><List/>{recordsOpen?'收起记录':'记忆目录'}</button></nav>
   {!recordsOpen?<section className="memory-directory-layers"><h3>四层记忆</h3>{([1,2,3,4]as const).map(layer=><button type="button" key={layer} aria-pressed={p.layer===layer} onClick={()=>{p.onLayer(p.layer===layer?undefined:layer);setDirectoryOpen(false)}}><i style={{background:LAYER_COLORS[layerKey(layer)]}}/>L{layer} · {LAYER_NAMES[layerKey(layer)]}<span>{p.catalog?.layers[String(layer)]??0}</span></button>)}</section>:<section className="memory-directory-records" aria-label="分页记忆列表">{p.records.map(record=><div className="memory-directory-row" key={record.id}><button type="button" disabled={p.loading} onClick={()=>pick(record.id)}><strong>{record.key}</strong><span>L{record.layer} · {day(record.occurred_at||record.created_at)}{record.pinned?' · 已钉选':''}{record.status==='deleted'?' · 已删除':!record.eligible?' · 已失效':''}</span><small>{record.value}</small></button><button className="memory-record-pin" type="button" aria-label={`${record.pinned?'取消钉选':'钉选记忆'}：${record.key}`} aria-pressed={record.pinned} disabled={p.disabled||p.busy||record.status!=='active'} onClick={()=>p.onPinRecord(record)}><Pin/></button></div>)}{!p.records.length&&<p className="memory-inspector-muted">暂无匹配记忆</p>}{p.total>40&&<div className="memory-directory-pages"><button type="button" disabled={p.offset===0} onClick={()=>p.onPage(Math.max(0,p.offset-40))}>上一页</button><span>{Math.floor(p.offset/40)+1} / {Math.ceil(p.total/40)}</span><button type="button" disabled={p.offset+40>=p.total} onClick={()=>p.onPage(p.offset+40)}>下一页</button></div>}</section>}
   <label className="memory-directory-deleted"><input type="checkbox" checked={p.includeDeleted} onChange={event=>p.onIncludeDeleted(event.target.checked)}/>包含已删除 / 失效</label><div className="memory-directory-actions"><Button size="sm" disabled={p.disabled} onClick={()=>{setDirectoryOpen(false);p.onNew()}}><Plus/>记住一件事</Button><button type="button" disabled={exporting} onClick={()=>void exportData()}><Download/>导出</button><button type="button" aria-label="记忆操作说明" onClick={()=>setHelpOpen(true)}><HelpCircle/></button></div>
  </MemoryToolsDrawer>
  <Dialog open={helpOpen} onOpenChange={setHelpOpen}><DialogContent><DialogHeader><DialogTitle>探索记忆</DialogTitle></DialogHeader><p>拖动旋转，滚轮缩放，点击星点查看右侧详情。Ctrl + K 搜索，Home 回到全景。</p><p>目录可按四层或钉选筛选，时间回溯只筛选日期。情绪坐标来自你填写的内容，记忆留存使用真实活跃度。</p></DialogContent></Dialog>
 </div>
}
