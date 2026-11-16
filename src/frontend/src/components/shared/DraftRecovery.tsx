import { protectMessageUI } from '@/context/SurfaceContext'
import { useState } from 'react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import type { Mode } from '@/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

function DraftRecoveryBody({ mode }: { mode: Mode }) {
  const { draftConflicts, draftBlocked, drafts, resolveDraft, reloadDraftConflict, pendingInputs, cancelPendingInput, sending } = useApp()
  const conflict = draftConflicts[mode], pending = pendingInputs[mode]
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false)
  const run = async (task: () => Promise<void>) => {
    if (busy) return
    setBusy(true)
    try { await task(); setOpen(false) } catch (error) { toast.error('恢复操作未完成', { description: (error as Error).message }) }
    finally { setBusy(false) }
  }
  return <>
    {draftBlocked[mode] && <div className="draft-recovery" role="status"><span>草稿在其他窗口已更新，本地编辑仍保留。</span><Button size="xs" variant="outline" onClick={() => setOpen(true)}>比较草稿</Button></div>}
    {pending && !sending[mode] && <div className="draft-recovery" role="status"><span>{pending.stale ? '会话进度已改变，旧输入不会自动重发。' : pending.accepted ? '输入已保存，生成结果尚未确认。' : '发送结果尚未确认。重试沿用原编号。'}</span><Button size="xs" variant="outline" disabled={busy} onClick={() => void run(() => cancelPendingInput(mode))}>取消未执行输入并编辑</Button></div>}
    <Dialog open={open} onOpenChange={setOpen}><DialogContent className="workspace-file-compare"><DialogHeader><DialogTitle>比较草稿</DialogTitle><DialogDescription>明确选择后才继续保存；另一窗口的新版本仍受检查保护。</DialogDescription></DialogHeader><div className="workspace-file-compare-grid"><label>服务端草稿<textarea readOnly value={conflict?.content || ''} /></label><label>本地编辑<textarea readOnly value={drafts[mode]} /></label></div><DialogFooter>{!conflict && <Button variant="outline" disabled={busy} onClick={() => void run(() => reloadDraftConflict(mode))}>重试读取草稿</Button>}<Button variant="outline" disabled={busy || !conflict} onClick={() => void run(() => resolveDraft(mode, 'remote'))}>采用服务端草稿</Button><Button disabled={busy || !conflict} onClick={() => void run(() => resolveDraft(mode, 'local'))}>保存本地草稿</Button></DialogFooter></DialogContent></Dialog>
  </>
}

export const DraftRecovery = protectMessageUI(DraftRecoveryBody)
