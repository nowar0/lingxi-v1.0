import {useState} from 'react'
import {useApp} from '@/context/AppContext'
import {Button} from '@/components/ui/button'
import {toast} from 'sonner'
import type {useComposerMedia} from './useComposerMedia'
export function ComposerLimit({draft,quoted,media,mode}:{draft:string;quoted:string;media:ReturnType<typeof useComposerMedia>;mode:'work'|'companion'}){
 const {setDraft}=useApp(),[busy,setBusy]=useState(false)
 const count=quoted.length+media.items.reduce((total,item)=>total+item.name.length+(item.reference||item.url||'').length+(item.comment?.length||0)+12,0)
 if(count<7000)return null
 return <div className="composer-limit" role="status"><span>{count>8000?'内容超过发送上限':`还可输入约 ${8000-count} 字`}</span>{draft.trim()&&<Button variant="ghost" size="xs" disabled={busy||media.busy} onClick={()=>{setBusy(true);void media.choose(new File([draft],'message.txt',{type:'text/plain'})).then(ok=>{if(ok){setDraft(mode,'');toast.success('正文已转为附件，可选择要读取的范围')}}).finally(()=>setBusy(false))}}>正文转为附件</Button>}</div>
}
