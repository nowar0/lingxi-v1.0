import { createPortal } from 'react-dom'
import { useEffect, useLayoutEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { Copy, Minus, Square, X } from 'lucide-react'
import { toast } from 'sonner'
import './desktop-window-chrome.css'

type State = { maximized:boolean; fullscreen:boolean }
type Bridge = { state:()=>Promise<State>; action:(action:'minimize'|'toggle-maximize'|'close')=>Promise<State|null>; onChange:(listener:(state:State)=>void)=>()=>void }
const bridge = () => (window as unknown as {LingxiShell?:{windowChrome?:Bridge}}).LingxiShell?.windowChrome

export function DesktopWindowChrome(){
  const location=useLocation(),api=bridge()
  const [host,setHost]=useState<HTMLElement|null>(null),[state,setState]=useState<State>({maximized:false,fullscreen:false})
  useEffect(()=>{
    if(!api)return
    let alive=true
    document.documentElement.classList.add('has-desktop-window-chrome')
    void api.state().then(value=>{if(alive)setState(value)}).catch(()=>{})
    const stop=api.onChange(value=>{if(alive)setState(value)})
    return()=>{alive=false;stop();document.documentElement.classList.remove('has-desktop-window-chrome')}
  },[api])
  useLayoutEffect(()=>{
    if(!api)return
    const main=document.querySelector('.app-stage');if(!main)return
    const find=()=>{
      const found=main.querySelector<HTMLElement>('.app-topbar')
      setHost(current=>current===found?current:found)
    }
    find();const observer=new MutationObserver(find);observer.observe(main,{childList:true,subtree:true});window.addEventListener('resize',find)
    return()=>{observer.disconnect();window.removeEventListener('resize',find)}
  },[api,location.pathname])
  useLayoutEffect(()=>{
    if(!api||!host)return
    host.classList.add('desktop-caption-host')
    return()=>host.classList.remove('desktop-caption-host')
  },[api,host,location.pathname])
  if(!api)return null
  const act=(action:'minimize'|'toggle-maximize'|'close')=>{void api.action(action).then(value=>{if(value)setState(value)}).catch(error=>toast.error('窗口操作未完成',{description:(error as Error).message}))}
  const restored=state.maximized||state.fullscreen
  return <>{host&&createPortal(<div className="desktop-window-controls" role="group" aria-label="窗口控制"><button type="button" aria-label="最小化窗口" title="最小化" onClick={()=>act('minimize')}><Minus/></button><button type="button" aria-label={restored?'还原窗口':'最大化窗口'} title={restored?'还原':'最大化'} onClick={()=>act('toggle-maximize')}>{restored?<Copy/>:<Square/>}</button><button type="button" className="desktop-window-close" aria-label="关闭窗口" title="关闭" onClick={()=>act('close')}><X/></button></div>,host)}<div className="desktop-window-drag-edge" aria-hidden="true"/></>
}
