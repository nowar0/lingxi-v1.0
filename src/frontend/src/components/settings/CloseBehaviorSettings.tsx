import { Select } from '@/components/ui/select'
import { useEffect,useState } from 'react'
type Action='ask'|'tray'|'exit'
export function CloseBehaviorSettings(){
 const shell=(window as unknown as {LingxiShell?:{closePreference?:(action?:Action)=>Promise<{action:Action}>}}).LingxiShell
 const [action,setAction]=useState<Action>('ask'),[busy,setBusy]=useState(false),[error,setError]=useState('')
 useEffect(()=>{if(shell?.closePreference)void shell.closePreference().then(v=>setAction(v.action)).catch(e=>setError((e as Error).message))},[])
 if(!shell?.closePreference)return null
 return <div className="close-behavior-settings"><div><h3>关闭窗口时</h3><p>收至托盘后任务继续运行，退出程序会停止任务。</p></div><Select aria-label="关闭窗口时的操作" disabled={busy} value={action} onChange={event=>{const next=event.target.value as Action;setBusy(true);setError('');void shell.closePreference!(next).then(v=>setAction(v.action)).catch(e=>setError((e as Error).message)).finally(()=>setBusy(false))}}><option value="ask">每次询问</option><option value="tray">最小化到托盘</option><option value="exit">退出程序</option></Select>{error&&<p role="alert">{error}</p>}</div>
}
