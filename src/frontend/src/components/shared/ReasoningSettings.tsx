import { Select } from '@/components/ui/select'
import { toast } from 'sonner'
import type { ModelModule } from '@/types'
import { api, json } from '@/api/client'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { SettingRow } from './SettingRow'
import { ReasoningControl, defaultEffortPreferences, saveEffortPreferences, useEffortPreferences, type EffortPreferences } from './ReasoningControl'
import { compatibleEffort } from '@/lib/workComposerSettings'
import { useRef, useState } from 'react'

export function ModuleReasoningDialog({ module, onClose, onSaved }: { module?: ModelModule; onClose: () => void; onSaved: (module: ModelModule) => Promise<void> }) {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const writeLock = useRef(false)
  return <Dialog open={Boolean(module)} onOpenChange={(open) => { if (!open && !saving) onClose() }}><DialogContent className="module-reasoning-dialog"><DialogHeader><DialogTitle>模型思考强度</DialogTitle><DialogDescription>{module?.model || '未配置模型'} · 保存为此模块的默认档位，会话单独选择时优先使用会话设置。</DialogDescription></DialogHeader>
    {module && <ReasoningControl key={`${module.id}:${module.revision}`} capability={module.reasoning} value={compatibleEffort(module.reasoning_effort, module.reasoning)} disabled={saving} onCommit={(effort) => {
      if (writeLock.current) return
      writeLock.current = true
      setSaving(true); setError('')
      void api<ModelModule>(`/v6/modules/${module.id}/reasoning`, json('PUT', { effort, expected_revision: module.revision })).then(onSaved).catch((failure) => { setError((failure as Error).message); toast.error('思考强度未保存', { description: (failure as Error).message }) }).finally(() => { writeLock.current = false; setSaving(false) })
    }} />}
    <p className="work-model-save-state" role="status">{saving ? '正在保存…' : '调整后自动保存；正在进行的请求不改变。'}</p>{error && <p role="alert" className="work-model-error">{error}</p>}
  </DialogContent></Dialog>
}

export function ReasoningSliderSettings() {
  const preferences = useEffortPreferences()
  const save = (patch: Partial<EffortPreferences>) => { try { saveEffortPreferences({ ...preferences, ...patch }) } catch { toast.error('滑条外观设置未能保存，请重试') } }
  return <Card className="settings-section"><div className="settings-section-heading"><div><h2>思考滑条设置</h2><p>仅改变滑条外观，实际档位由模型决定。</p></div><Button variant="ghost" size="sm" onClick={() => { try { saveEffortPreferences(defaultEffortPreferences) } catch { toast.error('恢复失败，请重试') } }}>恢复默认</Button></div><div className="p-5">
    <SettingRow title="动画效果" description="减弱动态设置与系统偏好优先"><Select aria-label="思考滑条动画效果" className="reasoning-setting-select" value={preferences.motion} onChange={(event) => save({ motion: event.target.value as EffortPreferences['motion'] })}><option value="fast">快速动画</option><option value="quick">简洁动画</option><option value="full">完整动画</option></Select></SettingRow>
    <SettingRow title="已到达档位的刻度" description="只改变刻度显示，不影响实际模型参数"><Select aria-label="思考滑条刻度显示" className="reasoning-setting-select" value={preferences.reachedTicks} onChange={(event) => save({ reachedTicks: event.target.value as EffortPreferences['reachedTicks'] })}><option value="highlight">亮白显示</option><option value="dim">与未到达同色</option><option value="hidden">隐藏</option></Select></SettingRow>
  </div></Card>
}
