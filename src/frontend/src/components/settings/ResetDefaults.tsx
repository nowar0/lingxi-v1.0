import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'

export function ResetDefaults({title,current,defaults,labels={},onApply,disabled=false}:{title:string;current:Record<string,unknown>;defaults:Record<string,unknown>;labels?:Record<string,string>;onApply:()=>void;disabled?:boolean}){
 const [open,setOpen]=useState(false)
 const changed=Object.keys(defaults).filter(key=>JSON.stringify(current[key])!==JSON.stringify(defaults[key]))
 const show=(value:unknown)=>typeof value==='boolean'?(value?'开启':'关闭'):value===''?'跟随默认':value===null?'自动':Array.isArray(value)?value.join('、')||'无':String(value)
 return <><Button type="button" variant="ghost" size="sm" disabled={disabled||!changed.length} onClick={()=>setOpen(true)}>恢复{title}默认值</Button><Dialog open={open} onOpenChange={setOpen}><DialogContent><DialogHeader><DialogTitle>恢复{title}默认值</DialogTitle><DialogDescription>只修改下列参数。确认后填入当前表单，点击该组的保存按钮后生效。Key、聊天、人物卡、工作区和其他设置均保留。</DialogDescription></DialogHeader><dl className="space-y-3 text-sm">{changed.map(key=><div key={key}><dt>{labels[key]||key}</dt><dd className="text-muted-foreground break-words">{show(current[key])} → {show(defaults[key])}</dd></div>)}</dl><DialogFooter><Button variant="outline" onClick={()=>setOpen(false)}>取消</Button><Button disabled={disabled} onClick={()=>{onApply();setOpen(false)}}>确认填入默认值</Button></DialogFooter></DialogContent></Dialog></>
}
