import { useEffect,useState } from 'react'
import { BookOpen,Search } from 'lucide-react'
import { api,json } from '@/api/client'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
type Skill={id:string;name:string;description:string;enabled:boolean;available?:boolean}
export function WorkSkillsPicker({onSelected}:{onSelected:()=>void}){
 const {activeWorkSessionId:sid,generating,sending}=useApp(),[busy,setBusy]=useState(false),[items,setItems]=useState<Skill[]>([]),[query,setQuery]=useState(''),[error,setError]=useState(''),[loading,setLoading]=useState(true)
 useEffect(()=>{const c=new AbortController();void api<{items:Skill[]}>('/v6/skills',{signal:c.signal}).then(v=>{if(!c.signal.aborted)setItems(v.items.filter(s=>s.enabled&&s.available!==false))}).catch(e=>{if(!c.signal.aborted)setError(e.message)}).finally(()=>{if(!c.signal.aborted)setLoading(false)});return()=>c.abort()},[])
 return <div className="composer-skills-picker"><label><Search size={15}/><input aria-label="搜索可用技能" placeholder="搜索技能…" value={query} onChange={e=>setQuery(e.target.value)}/></label>{loading&&<p role="status">正在读取技能…</p>}{error&&<p role="alert">{error}</p>}{items.filter(s=>(s.name+s.description).toLowerCase().includes(query.toLowerCase())).map(s=><button type="button" className="composer-menu-row" key={s.id} disabled={!sid||busy||generating.work||sending.work} onClick={()=>{if(!sid||busy)return;setBusy(true);setError('');void api(`/v6/sessions/${sid}/commands`,json('POST',{text:'/skill '+s.id,request_id:crypto.randomUUID()})).then(()=>{toast.success('技能已应用到当前会话');onSelected()}).catch(e=>setError(e.message)).finally(()=>setBusy(false))}}><BookOpen/><span>{s.name}<small>{s.description}</small></span></button>)}{!loading&&!error&&!items.length&&<p>暂无可用技能，请在技能中心启用或导入。</p>}<p className="composer-menu-note">选择后用于当前会话参考，不执行技能中的命令；输入草稿保留。</p></div>
}
