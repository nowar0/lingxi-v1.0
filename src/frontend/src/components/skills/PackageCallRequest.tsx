import { useMemo, useRef, useState } from 'react'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { createPluginPackagesClient, approvalProblem, pluginErrorMessage, PLUGIN_PYTHON_RISK } from '@/lib/pluginPackages'
import type { PluginPreview } from '@/lib/pluginPackages'
import { PluginModelPending } from './PluginModelPending'
import { StrictSandboxSnapshot } from './PluginHostSecurity'

type Intent = { status: 'awaiting_user_preview'; authorized: false; executed: false; plugin_id: string; revision: number; digest: string; tool: string; args: Record<string, unknown> }
export function parsePackageIntent(raw: string): Intent | undefined {
  try {
    const value = JSON.parse(raw) as Intent
    if (value.status !== 'awaiting_user_preview' || value.authorized !== false || value.executed !== false
      || !/^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$/.test(value.plugin_id)
      || !Number.isSafeInteger(value.revision) || !/^[a-f0-9]{64}$/.test(value.digest)
      || typeof value.tool !== 'string' || !value.args || typeof value.args !== 'object' || Array.isArray(value.args)) return
    return value
  } catch { return }
}

/** A model may request a preview, but only this explicit human dialog executes. */
export function PackageCallRequest({ result, disabled = false }: { result: string; disabled?: boolean }) {
  const intent = useMemo(() => parsePackageIntent(result), [result])
  const client = useMemo(() => createPluginPackagesClient(), [])
  const [preview, setPreview] = useState<PluginPreview>()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [output, setOutput] = useState<unknown>()
  const lock = useRef(false)
  if (!intent) return null
  const run = async (action: () => Promise<void>) => {
    if (lock.current || disabled) return
    lock.current = true; setBusy(true); setMessage('')
    try { await action() } catch (error) { setMessage(pluginErrorMessage(error)); setPreview(undefined) }
    finally { lock.current = false; setBusy(false) }
  }
  return <div className="mt-3 space-y-3 rounded-lg border border-primary/20 bg-primary/5 p-3 text-xs">
    <p>Agent 仅提出了未授权请求：<strong>{intent.plugin_id} / {intent.tool}</strong>。尚未执行。</p>
    <Button size="sm" variant="outline" disabled={disabled || busy} onClick={() => void run(async () => {
      const live = await api<{revision:number;digest:string}>(`/v1/plugin-packages/${encodeURIComponent(intent.plugin_id)}`)
      if (live.revision !== intent.revision || live.digest !== intent.digest) throw new Error('请求后的插件版本已改变，请让 Agent 重新读取能力目录；未执行。')
      const value = await api<PluginPreview>(`/v1/plugin-packages/${encodeURIComponent(intent.plugin_id)}/calls/preview`, json('POST', {tool:intent.tool,args:intent.args,expected_revision:intent.revision}))
      setPreview(value)
    })}>由我预览准确目标</Button>
    {message && <p role="alert" className="text-destructive">{message}</p>}
    {output !== undefined && <><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(output,null,2)}</pre><PluginModelPending result={output} disabled={disabled||busy}/></>}
    <Dialog open={Boolean(preview)} onOpenChange={open => { if (!open && !busy) setPreview(undefined) }}><DialogContent className="max-w-2xl"><DialogHeader><DialogTitle>确认本次插件请求</DialogTitle><DialogDescription>插件调用会按以下参数执行一次。请核对目标和权限。</DialogDescription></DialogHeader>{preview && <section className="space-y-2 rounded-xl border border-border p-3 text-xs leading-5"><p><strong>插件：</strong>{preview.snapshot.manifest?.name || preview.snapshot.id}</p><p><strong>工具：</strong>{preview.snapshot.tool || intent.tool}</p><p><strong>权限：</strong>{preview.snapshot.permissions.join('、') || '未声明'}</p><div><strong>目标：</strong>{preview.snapshot.targets.length ? <ul className="mt-1 list-inside list-disc break-all">{preview.snapshot.targets.map((target, index) => <li key={index}>{target.kind}：{typeof target.value === 'string' ? target.value : JSON.stringify(target.value)}</li>)}</ul> : '未声明'}</div><p className="break-all"><strong>参数：</strong>{JSON.stringify(preview.snapshot.args || {})}</p>{preview.snapshot.risk && <p><strong>风险：</strong>{preview.snapshot.risk}</p>}</section>}<details className="personal-disclosure"><summary>查看隔离环境与完整参数</summary>{preview&&<StrictSandboxSnapshot snapshot={preview.snapshot}/>}<pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-secondary/40 p-3 text-xs">{JSON.stringify(preview?.snapshot,null,2)}</pre><p className="text-xs text-muted-foreground">{PLUGIN_PYTHON_RISK}</p></details>{preview && approvalProblem(preview) && <p className="text-sm text-destructive">{approvalProblem(preview)}</p>}<DialogFooter><Button variant="outline" disabled={busy} onClick={() => setPreview(undefined)}>不执行</Button><Button disabled={busy || !preview || Boolean(approvalProblem(preview))} onClick={() => void run(async () => { if (!preview) return; const exact = preview; setPreview(undefined); const value = await client.execute(exact); setOutput(value.result); setMessage('本次明确批准的插件调用已完成；结果未自动发送给模型。') })}>我批准本次执行</Button></DialogFooter></DialogContent></Dialog>
  </div>
}
