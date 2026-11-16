import { protectMessageUI } from '@/context/SurfaceContext'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '../ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog'
import { createMemorySourceClient, MEMORY_SOURCE_PAGE_SIZE } from '../../lib/memorySource'
import type { MemorySourcePage } from '../../lib/memorySource'
import type { MemoryV2Item } from '../../lib/memoryV2'

/** Explicitly opened source viewer; never fetches on mount or guesses a chat route. */
function MemorySourceMessagesBody({item,mode,sessionId}:{item:MemoryV2Item;mode:'companion'|'work';sessionId?:number}) {
  const client=useMemo(()=>createMemorySourceClient(),[])
  const [open,setOpen]=useState(false),[loading,setLoading]=useState(false),[error,setError]=useState('')
  const [page,setPage]=useState<MemorySourcePage>()
  const epoch=useRef(0)
  const unavailable=item.scope==='shared'?'共享记忆不等于整条聊天授权，不能显示原消息。':!item.is_current?'此处是历史内容；请回到当前有效记忆核对原消息。':item.status!=='active'||!item.eligible||item.redacted?'当前来源已失效、删除或被彻底忘记，不能显示原消息。':!item.source?.known||!['message','memory'].includes(item.source.kind)||!item.source.message_ids?.length?'此记忆为手动录入或没有可验证消息，不制造原文。':item.scope!==mode||mode==='work'&&(!sessionId||item.owner_id!==String(sessionId))?'当前模式或工作会话不能读取此来源。':''
  useEffect(()=>{epoch.current++;client.cancel();setOpen(false);setPage(undefined);setError('');setLoading(false)
    return()=>{epoch.current++;client.cancel()}
  },[client,item.id,item.revision,item.current_revision,item.version_id,item.status,item.eligible,item.scope,mode,sessionId])
  function close(){epoch.current++;client.cancel();setOpen(false);setPage(undefined);setError('');setLoading(false)}
  async function load(offset:number){
    if(unavailable)return
    const owner=++epoch.current;setOpen(true);setLoading(true);setError('');setPage(undefined)
    try{const next=await client.load({entityId:item.id,expectedRevision:item.current_revision,mode,sessionId},offset)
      if(owner===epoch.current)setPage(next)
    }catch(cause){if(owner===epoch.current)setError(cause instanceof Error?cause.message:'原消息未读取；请刷新当前来源后再试')}
    finally{if(owner===epoch.current)setLoading(false)}
  }
  return <div className="mt-3 space-y-2 border-t border-border pt-3">
    <Button type="button" size="sm" variant="outline" disabled={Boolean(unavailable)||loading} onClick={()=>void load(0)}>查看已验证原消息</Button>
    <p className="text-[11px] leading-5 text-muted-foreground">{unavailable||'点击后仅读取此当前记忆实际绑定的来源消息；不调用模型，不强化，不打开其他聊天。'}</p>
    <Dialog open={open} onOpenChange={value=>{if(!value)close()}}><DialogContent className="max-w-3xl"><DialogHeader><DialogTitle>已验证的来源原消息</DialogTitle><DialogDescription>{item.key} · 当前修订 r{item.current_revision}。只读实际消息，不代表历史内容是当前事实；来源变化后本页会拒绝继续读取。</DialogDescription></DialogHeader>
      {loading&&<p role="status" className="text-sm text-muted-foreground">正在核对来源与读取原消息…</p>}
      {error&&<p role="alert" className="whitespace-pre-wrap text-sm text-destructive">{error}</p>}
      {page&&<div className="space-y-3"><p className="text-xs text-muted-foreground">来源已验证 · 共 {page.total} 条 · {page.items.length?`${page.offset+1}–${page.offset+page.items.length}`:'无本页记录'}</p><div className="max-h-[55vh] space-y-3 overflow-auto">{page.items.map(message=><article key={message.id} className="space-y-2 rounded-lg border border-border p-3"><header className="flex flex-wrap gap-3 text-xs text-muted-foreground"><span>消息 #{message.id}</span><span>会话 #{message.session_id} · {message.session_mode}</span><span>真实角色：{message.role}</span><time>{message.at||'未记录时间'}</time></header>{message.speaker&&<details className="text-xs"><summary>当时人物快照</summary><pre className="mt-2 whitespace-pre-wrap break-all">{JSON.stringify(message.speaker,null,2)}</pre></details>}<p className="whitespace-pre-wrap break-words text-sm leading-7">{message.content}</p></article>)}</div><nav className="flex items-center justify-between gap-2 text-xs" aria-label="来源原消息分页"><Button type="button" variant="outline" size="sm" disabled={loading||page.offset===0} onClick={()=>void load(Math.max(0,page.offset-MEMORY_SOURCE_PAGE_SIZE))}>上一页</Button><span>{Math.floor(page.offset/page.limit)+1} / {Math.max(1,Math.ceil(page.total/page.limit))}</span><Button type="button" variant="outline" size="sm" disabled={loading||page.offset+page.limit>=page.total} onClick={()=>void load(page.offset+page.limit)}>下一页</Button></nav></div>}
      <DialogFooter><Button type="button" variant="outline" onClick={close}>关闭原消息</Button></DialogFooter></DialogContent></Dialog>
  </div>
}

export const MemorySourceMessages = protectMessageUI(MemorySourceMessagesBody)
