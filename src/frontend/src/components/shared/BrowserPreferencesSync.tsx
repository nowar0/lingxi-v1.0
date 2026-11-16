import {useEffect,useRef} from 'react'
import {toast} from 'sonner'
import {useApp} from '@/context/AppContext'
import {api,json} from '@/api/client'
import {collectBrowserPreferences,preferenceRevision,setPreferenceRevision} from '@/lib/browserPreferences'
export function BrowserPreferencesSync(){
 const {connection}=useApp();const last=useRef('')
 useEffect(()=>{if(connection!=='connected')return;let live=true,busy=false;const controller=new AbortController();const save=async()=>{if(busy||document.hidden)return;const browser=collectBrowserPreferences(),text=JSON.stringify(browser);if(text===last.current)return;busy=true;try{const result=await api<{revision:string}>('/v6/manage/browser-preferences',{...json('PUT',{browser,expected_revision:preferenceRevision}),signal:controller.signal});if(live){setPreferenceRevision(result.revision);last.current=text;toast.dismiss('lingxi-preferences-save')}}catch(e){if(live){toast.error('界面偏好尚未同步',{id:'lingxi-preferences-save',description:(e as Error).message,action:{label:'重试当前设置',onClick:()=>{void api<{revision:string}>('/v6/data/preferences').then(v=>{if(live){setPreferenceRevision(v.revision);last.current='';void save()}}).catch(()=>{})}}});last.current=text}}finally{busy=false}};void save();const timer=setInterval(()=>void save(),2500);return()=>{live=false;controller.abort();clearInterval(timer)}},[connection]);return null
}
