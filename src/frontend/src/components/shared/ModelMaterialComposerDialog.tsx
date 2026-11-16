import { useEffect, useRef, useState } from 'react'
import { FileText, LoaderCircle } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Tooltip } from '@/components/ui/tooltip'
import { artifactPath, type Artifact } from '@/lib/artifacts'
import { modelMaterialInputProblem, validateModelMaterialAttachments, type ModelMaterialAttachment } from '@/lib/model-material-send'
import { cn } from '@/lib/utils'
import type { Mode } from '@/types'
import { ModelMaterialSend } from './ModelMaterialSend'
import type { Attachment } from './useComposerMedia'

export type ModelMaterialComposerDialogProps = {
  sessionId?: number
  mode: Mode
  text: string
  attachments: readonly Attachment[]
  connected: boolean
  busy: boolean
  className?: string
}
type ComposerSource = Omit<ModelMaterialComposerDialogProps, 'className'>
type OpenMaterials = { identity: string; sessionId: number; attachments: ModelMaterialAttachment[] }

// Include queue identity and selected references even though references are never
// expanded or sent here. An in-place queue mutation must invalidate an old read.
function sourceIdentity(source: ComposerSource) {
  return JSON.stringify([source.mode, source.sessionId, source.connected, source.busy, source.text,
    source.attachments.map(item => [item.localId, item.id, item.sessionId, item.status, item.name, item.mime, item.size, item.url, item.reference])])
}
function attachmentMetadata(attachments: readonly Attachment[]): ModelMaterialAttachment[] {
  return attachments.map(item => ({ fid: item.id || '', name: item.name, mime: item.mime, size: item.size }))
}
function sourceProblem(source: ComposerSource) {
  if (!source.sessionId || !Number.isSafeInteger(source.sessionId) || source.sessionId < 1) return '请先选择已有的真实会话；此入口不会新建会话。'
  if (!source.connected) return '服务未连接、离线或正在核验连接；不能打开独立资料发送。'
  if (source.busy) return '请等待当前发送、生成、录音或媒体处理结束。'
  if (source.attachments.some(item => item.sessionId !== source.sessionId)) return '附件不属于当前会话；不能采用跨会话原件。'
  if (source.attachments.some(item => item.status !== 'ready')) return '请等待全部附件上传完成，并重试或移除排队、上传中及失败的附件。'
  try { validateModelMaterialAttachments(source.sessionId, attachmentMetadata(source.attachments)) }
  catch (cause) { return cause instanceof Error ? cause.message : '当前原件身份无效；拒绝，不截断。' }
  const problem = modelMaterialInputProblem(source.text, [])
  // An empty draft is valid when there are actual ready originals to approve.
  return problem && (source.text !== '' || !source.attachments.length) ? problem : ''
}
function verifiedArtifact(value: Artifact, expected: ModelMaterialAttachment, sessionId: number): ModelMaterialAttachment {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.id !== expected.fid || value.session_id !== sessionId || value.name !== expected.name || value.mime !== expected.mime || value.size !== expected.size || typeof value.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(value.sha256)) {
    throw new Error('宿主原件的 fid、会话、名称、MIME、大小或真实 SHA256 与当前附件不一致；没有打开或发送。')
  }
  return { fid: value.id, name: value.name, mime: value.mime, size: value.size, sha256: value.sha256 }
}

/** One explicit entry for all composers; it never creates a session or consumes chat state. */
export function ModelMaterialComposerDialog({ sessionId, mode, text, attachments, connected, busy, className }: ModelMaterialComposerDialogProps) {
  const source: ComposerSource = { sessionId, mode, text, attachments, connected, busy }
  const identity = sourceIdentity(source)
  const current = useRef(source)
  current.current = source
  const live = useRef(false), generation = useRef(0), locked = useRef(false)
  const controller = useRef<AbortController | undefined>(undefined)
  const trigger = useRef<HTMLButtonElement>(null)
  const [checking, setChecking] = useState(false)
  const [opened, setOpened] = useState<OpenMaterials>()
  const problem = sourceProblem(source)

  useEffect(() => {
    live.current = true
    return () => { live.current = false; generation.current++; controller.current?.abort(); controller.current = undefined; locked.current = false }
  }, [])
  useEffect(() => {
    generation.current++; controller.current?.abort(); controller.current = undefined; locked.current = false
    setChecking(false); setOpened(undefined)
    // Synchronous identity gating below unmounts Send immediately, before this
    // effect, so its revokeScope / handed-off-source cleanup cannot be skipped.
  }, [identity])

  const close = () => {
    generation.current++; controller.current?.abort(); controller.current = undefined; locked.current = false
    setChecking(false); setOpened(undefined)
  }
  const openMaterials = async () => {
    if (locked.current || !live.current) return
    const started = current.current, startedIdentity = sourceIdentity(started)
    const blocked = sourceProblem(started)
    if (blocked) { toast.error('无法打开资料预览发送', { description: blocked }); return }
    const target = started.sessionId!
    // Copy exact metadata before awaiting: useComposerMedia can mutate row objects.
    const expected = validateModelMaterialAttachments(target, attachmentMetadata(started.attachments))
    const ticket = ++generation.current, owner = new AbortController()
    const ownsRead = () => live.current && generation.current === ticket && sourceIdentity(current.current) === startedIdentity
    const canPublish = () => ownsRead() && !owner.signal.aborted && !sourceProblem(current.current)
    if (!canPublish()) return
    locked.current = true; controller.current = owner; setChecking(true)
    const timer = window.setTimeout(() => owner.abort(), 15000)
    try {
      // Only this user click reads same-origin host metadata. No file bytes,
      // placeholder digest, URL parsing, historical references or chat API.
      const artifacts = await Promise.all(expected.map(item => api<Artifact>(artifactPath(item.fid, target), { method: 'GET', signal: owner.signal })))
      if (!canPublish()) return
      const verified = validateModelMaterialAttachments(target, artifacts.map((artifact, index) => verifiedArtifact(artifact, expected[index], target)))
      if (!canPublish()) return
      setOpened({ identity: startedIdentity, sessionId: target, attachments: verified })
    } catch (cause) {
      if (ownsRead()) toast.error('原件身份核验未完成', { description: owner.signal.aborted ? '元数据核验已超时；没有打开、发送或自动重试。' : cause instanceof Error ? cause.message : '未取得可信原件元数据；没有打开或发送。' })
    } finally {
      window.clearTimeout(timer)
      if (generation.current === ticket) { locked.current = false; controller.current = undefined; if (live.current) setChecking(false) }
    }
  }
  // Closing, changing the draft/queue/session or disconnecting must actually
  // unmount ModelMaterialSend, not leave a hidden live scope in a closed Modal.
  const active = opened && opened.identity === identity && !problem ? opened : undefined
  const label = checking ? '正在核验当前原件…' : '资料预览发送'
  return <>
    <Tooltip content={problem || label}><span className="inline-flex shrink-0" title={problem || label}>
      <Button ref={trigger} type="button" variant="ghost" size="icon" className={cn('size-7', className)} aria-label="资料预览发送" aria-haspopup="dialog" aria-expanded={Boolean(active)} disabled={checking || Boolean(problem)} onClick={event => { event.preventDefault(); event.stopPropagation(); void openMaterials() }}>
        {checking ? <LoaderCircle className="animate-spin" /> : <FileText />}
      </Button>
    </span></Tooltip>
    {active && <Dialog open onOpenChange={next => { if (!next) close() }}>
      <DialogContent className="max-w-5xl" onCloseAutoFocus={event => { event.preventDefault(); trigger.current?.focus() }} onKeyDown={event => event.stopPropagation()} onClick={event => event.stopPropagation()} onPaste={event => event.stopPropagation()} onDragOver={event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault(); event.stopPropagation() }} onDrop={event => { event.preventDefault(); event.stopPropagation() }}>
        <DialogHeader><DialogTitle>独立模型资料发送</DialogTitle><DialogDescription>仅采用当前{mode === 'companion' ? '陪伴' : '工作'}会话输入区的原样正文与已核验原件，不展开历史引用。转换、最终外发与插件交接分别确认；草稿、附件和引用保持不变。</DialogDescription></DialogHeader>
        <ModelMaterialSend sessionId={active.sessionId} connected={connected && !busy} text={text} attachments={active.attachments} onCancel={close} onHandoff={() => close()} />
      </DialogContent>
    </Dialog>}
  </>
}
