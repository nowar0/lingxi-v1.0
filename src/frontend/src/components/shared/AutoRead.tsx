import { useEffect, useRef } from 'react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { evolution } from '@/lib/evolution'
import type { Mode } from '@/types'

/** Mounted by each visible conversation, including an independent side chat. */
export function AutoRead({mode}:{mode:Mode}){
 const {activeSessionId,activeWorkSessionId,messages,workMessages,generating,speakMessage,stopSpeech,connection}=useApp()
 const sid=mode==='work'?activeWorkSessionId:activeSessionId,values=mode==='work'?workMessages:messages
 const enabled=useRef(false),seen=useRef(new Set<string>()),initialized=useRef(false),epoch=useRef(0)
 useEffect(()=>{initialized.current=false;enabled.current=false;seen.current.clear();const token=++epoch.current;const load=()=>{if(sid&&connection==='connected')void evolution.options(sid).then(v=>{if(epoch.current===token)enabled.current=v.settings.auto_read}).catch(()=>{})};load();window.addEventListener('lingxi-options-changed',load);return()=>{epoch.current++;window.removeEventListener('lingxi-options-changed',load);stopSpeech()}},[sid,connection,stopSpeech])
 useEffect(()=>{
   const complete=values.filter(m=>m.role==='assistant'&&m.delivery_status==='complete'&&m.content.trim())
   if(!initialized.current){if(values.length){complete.forEach(m=>seen.current.add(String(m.id)));initialized.current=true}return}
   if(generating[mode]){stopSpeech();return}
   const latest=complete.at(-1);if(!latest||seen.current.has(String(latest.id)))return
   seen.current.add(String(latest.id))
   if(enabled.current)void speakMessage(mode,latest,sid).catch(e=>toast.error('自动朗读未完成',{description:(e as Error).message}))
 },[values,generating[mode],mode,sid,speakMessage,stopSpeech])
 return null
}
