import {useEffect,useId,useState} from 'react'
import {Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription,DialogFooter} from '@/components/ui/dialog'
import {Button} from '@/components/ui/button'
const owners=new Set<string>()
const syncDesktop=()=>{(window as unknown as {LingxiShell?:{setUnsaved?:(dirty:boolean)=>void}}).LingxiShell?.setUnsaved?.(owners.size>0)}
let ask:((resolve:(value:boolean)=>void)=>void)|undefined
export const hasUnsavedChanges=()=>owners.size>0
let activeQuestion:Promise<boolean>|undefined
export const confirmDiscard=():Promise<boolean>=>!owners.size?Promise.resolve(true):(activeQuestion??=(new Promise<boolean>(resolve=>{if(ask)ask(resolve);else resolve(false)}).finally(()=>{activeQuestion=undefined})))
export function useUnsavedChanges(dirty:boolean){const id=useId();useEffect(()=>{if(dirty)owners.add(id);else owners.delete(id);syncDesktop();return()=>{owners.delete(id);syncDesktop()}},[dirty,id])}
export function UnsavedChanges(){
 const [pending,setPending]=useState<((value:boolean)=>void)>()
 useEffect(()=>{ask=resolve=>setPending(()=>resolve);const unload=(event:BeforeUnloadEvent)=>{if(owners.size){event.preventDefault();event.returnValue=''}}
 window.addEventListener('beforeunload',unload);return()=>{ask=undefined;window.removeEventListener('beforeunload',unload)}},[])
 const finish=(value:boolean)=>{const resolve=pending;setPending(undefined);if(value){owners.clear();syncDesktop()}resolve?.(value)}
 return <Dialog open={!!pending} onOpenChange={open=>{if(!open)finish(false)}}><DialogContent className="max-w-sm"><DialogHeader><DialogTitle>放弃未保存的修改？</DialogTitle><DialogDescription>继续编辑可保留当前内容。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={()=>finish(false)}>继续编辑</Button><Button onClick={()=>finish(true)}>放弃修改</Button></DialogFooter></DialogContent></Dialog>
}
