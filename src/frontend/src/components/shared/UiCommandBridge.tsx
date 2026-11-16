import { useEffect, useRef } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api, json } from '@/api/client'
import { useApp } from '@/context/AppContext'
const targets=new Set(['/','/settings','/companion','/work','/tasks','/memory','/skills','/connections','/settings?tab=models','/connections?tab=assignments','/connections?tab=providers','/connections?tab=profiles','/connections?tab=models','/settings?tab=characters','/settings?tab=data&section=diagnostics'])
export function UiCommandBridge(){
 const location=useLocation(),navigate=useNavigate(),{connection,activeSessionId,activeWorkSessionId}=useApp(),running=useRef(false)
 useEffect(()=>{if(connection!=='connected')return;let live=true;const sid=location.pathname==='/companion'?activeSessionId:activeWorkSessionId;const actual=location.pathname+location.search,page=targets.has(actual)?actual:targets.has(location.pathname)?location.pathname:'/'
  const tick=async()=>{if(running.current||!live)return;running.current=true;try{await api('/v6/ui/presence',json('POST',{page,session_id:sid||null}));const commands=await api<{items:Array<{id:string;action:string;target:string;session_id:number}>}>('/v6/ui/commands');for(const item of commands.items){if(!live||item.session_id!==sid||item.action!=='navigate'||!targets.has(item.target))continue;await api(`/v6/ui/commands/${item.id}/ack`,json('POST'));if(live)navigate(item.target);break}}catch{/* A stale or detached UI never gains execution authority. */}finally{running.current=false}}
  void tick();const timer=window.setInterval(()=>void tick(),1500);return()=>{live=false;window.clearInterval(timer)}
 },[connection,location.pathname,location.search,activeSessionId,activeWorkSessionId,navigate])
 return null
}
