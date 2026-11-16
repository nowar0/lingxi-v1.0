import { useEffect, useId, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { parseVideoTimestamps, validateVideoPreview, validateVideoProbe, VIDEO_LIMITS, videoPreviewProblem, videoSelection, videoSource } from '@/lib/modelVideo'
import type { VideoConversionPreview, VideoProbe, VideoSelection, VideoSource } from '@/lib/modelVideo'

export type ModelVideoSelectionProps = {
  source: VideoSource
  /** Must be the host report from a real restricted ffprobe, never browser metadata. */
  probe?: VideoProbe
  connected: boolean
  /** No endpoint/default fetch is invented here. The host integration owns authority and bytes. */
  onPreview?: (selection: VideoSelection, signal: AbortSignal) => Promise<VideoConversionPreview>
  /** Receives only a freshly checked host preview plus explicit local-conversion confirmation. */
  onApproveConversion?: (preview: VideoConversionPreview, userConfirmed: true, signal: AbortSignal) => Promise<void>
  onCancelPreview?: (preview: VideoConversionPreview) => Promise<void>
}

/** Independent local selection only: no model route, key, upload URL, send callback or composer mutation. */
export function ModelVideoSelection({ source, probe, connected, onPreview, onApproveConversion, onCancelPreview }: ModelVideoSelectionProps) {
  const id = useId()
  const [timestamps, setTimestamps] = useState('')
  const [streamIndex, setStreamIndex] = useState('')
  const [maxWidth, setMaxWidth] = useState('1280')
  const [maxHeight, setMaxHeight] = useState('720')
  const [preview, setPreview] = useState<VideoConversionPreview>()
  const [ack, setAck] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [, setTick] = useState(0)
  const owner = useRef(0)
  const lock = useRef(false)
  const abort = useRef<AbortController | null>(null)
  const held = useRef<VideoConversionPreview | undefined>(undefined)
  const cancelHost = useRef(onCancelPreview)
  cancelHost.current = onCancelPreview
  held.current = preview

  function dropApproval() {
    const old = held.current
    held.current = undefined
    owner.current++
    abort.current?.abort()
    setPreview(undefined)
    setAck(false)
    setError('')
    if (old && cancelHost.current) void cancelHost.current(old).catch(() => undefined)
  }

  useEffect(() => {
    const old = held.current
    held.current = undefined
    owner.current++
    abort.current?.abort()
    if (old && cancelHost.current) void cancelHost.current(old).catch(() => undefined)
    setPreview(undefined); setAck(false); setError(''); setNotice(''); setBusy(false)
    setTimestamps(''); setStreamIndex(''); setMaxWidth('1280'); setMaxHeight('720')
    return () => {
      owner.current++
      abort.current?.abort()
      const pending = held.current
      held.current = undefined
      if (pending && cancelHost.current) void cancelHost.current(pending).catch(() => undefined)
    }
  }, [source.fid, source.session_id, source.original_version, source.sha256, source.filename, source.mime, source.size, probe?.fingerprint, connected, onPreview, onApproveConversion])
  useEffect(() => {
    if (!preview) return
    const timer = window.setInterval(() => setTick(v => v + 1), 1000)
    return () => window.clearInterval(timer)
  }, [preview])

  let checkedProbe: VideoProbe | undefined
  let sourceProblem = ''
  let selectionProblem = ''
  let selected: VideoSelection | undefined
  try {
    videoSource(source)
    if (!probe) throw Error('尚无真实容器探测报告；依赖审查未通过或探测未接线时不能抽帧')
    checkedProbe = validateVideoProbe(probe, source)
    if (!/^(0|[1-9][0-9]*)$/.test(streamIndex)) throw Error('请明确选择一个视频流，不默认选择全部或第一个流')
    const points = parseVideoTimestamps(timestamps)
    if (!/^[1-9][0-9]*$/.test(maxWidth) || !/^[1-9][0-9]*$/.test(maxHeight)) throw Error('输出尺寸必须为1–2048的整数，不接受空值、NaN或Infinity')
    selected = videoSelection({ timestamps_ms: points, frame_count: points.length, max_width: Number(maxWidth), max_height: Number(maxHeight), stream_index: Number(streamIndex), output_format: 'png' }, checkedProbe)
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : '视频选区无效'
    if (!checkedProbe) sourceProblem = message
    else selectionProblem = message
  }
  let previewProblem = ''
  if (preview) {
    try {
      if (!selected) throw Error('当前选区无效，不能使用旧转换批准')
      validateVideoPreview(preview, source, selected)
      previewProblem = videoPreviewProblem(preview)
    } catch (cause) { previewProblem = cause instanceof Error ? cause.message : '旧视频转换批准已失效' }
  }
  const integrationReady = Boolean(onPreview && onApproveConversion)

  async function act(operation: (signal: AbortSignal, publish: (change: () => void) => void) => Promise<void>) {
    if (lock.current || busy || !connected) return
    lock.current = true; setBusy(true); setError('')
    const generation = owner.current
    const controller = new AbortController()
    abort.current = controller
    const publish = (change: () => void) => { if (generation === owner.current && !controller.signal.aborted) change() }
    try { await operation(controller.signal, publish) }
    catch (cause) { publish(() => setError(cause instanceof Error ? cause.message : '本地视频处理结果未知；未发送模型，不自动重试')) }
    finally { lock.current = false; if (generation === owner.current) setBusy(false) }
  }

  const prepare = () => void act(async (signal, publish) => {
    if (!selected || !onPreview || !checkedProbe || preview) return
    const selection = { ...selected, timestamps_ms: [...selected.timestamps_ms] }
    const raw = await onPreview(selection, signal)
    const value = validateVideoPreview(raw, source, selection)
    let adopted = false
    publish(() => { adopted = true; held.current = value; setPreview(value); setAck(false); setNotice('只完成容器探测和准确抽帧计划；尚未运行FFmpeg抽帧，未发送模型。') })
    if (!adopted && cancelHost.current) void cancelHost.current(value).catch(() => undefined)
  })
  const convert = () => void act(async (signal, publish) => {
    if (!preview || !selected || !ack || !onApproveConversion || previewProblem) return
    const pending = validateVideoPreview(preview, source, selected)
    // Consume locally BEFORE the request. A connection loss never authorizes retry.
    held.current = undefined; setPreview(undefined); setAck(false)
    await onApproveConversion(pending, true, signal)
    publish(() => setNotice('一次本地抽帧请求已完成；实际帧须由宿主持有并另行完整预览，再独立批准模型发送。已按这一次批准登记实际帧，但未批准模型发送。'))
  })
  const cancel = () => {
    dropApproval()
    setNotice('本地批准已丢弃，不能再使用；宿主计划保留到过期或同原件新计划替换，不声称已远端撤销。可取消整轮撤销scope。未删除原件，已启动操作不自动重试。')
  }
  const change = (setter: (value: string) => void, value: string) => { dropApproval(); setter(value); setNotice('选区变化使旧批准失效；未发送模型。') }

  return <section aria-label="视频时间点选择与本地抽帧批准" className="space-y-3 rounded-lg border border-border p-3 text-xs leading-6">
    <h3 className="font-semibold">视频时间点选择 · 本地转换与模型发送分开批准</h3>
    <p>每原件≤15 MiB，整次最多8原件；所有视频合计最多16帧。仅本地已审工具，无自动下载、外部URL或播放列表，不执行附件脚本。</p>
    <p>原生视频保持完整原件，不会因模型不支持而默换模型、转码或改为抽帧。下面的静态帧转换必须单独明确批准。</p>
    {!connected && <p role="status">连接未就绪，不请求探测、转换或模型。</p>}
    {!integrationReady && <p role="status">宿主视频计划／执行接口尚未接线：可查看硬限与填写选区，不会虚构转换成功或发起请求。</p>}
    {sourceProblem && <p role="status">{sourceProblem}</p>}
    {checkedProbe && <div className="space-y-1 rounded border border-border p-2">
      <p>受控ffprobe容器：{checkedProbe.container} · 时长 {checkedProbe.duration_ms} ms · {checkedProbe.video_streams.length} 个视频流</p>
      <p>已探测编码、容器和流，不等于完整视频逐包解码、解码安全证明或原生提供方能力实测。</p>
      <p className="break-all">原件 SHA256：{checkedProbe.original_sha256}</p>
      <p>音频编码：{checkedProbe.audio_codecs.length ? checkedProbe.audio_codecs.join(', ') : '未发现音频流'}；抽帧会全部丢弃。</p>
      <p>scratch处理：{checkedProbe.cleanup_status}；未知归属对象保留，不按进程名或路径猜测清理。</p>
    </div>}
    {error && <p role="alert" className="whitespace-pre-wrap text-destructive">{error}</p>}
    <fieldset disabled={busy || !connected} className="space-y-3">
      <label className="block" htmlFor={`${id}-stream`}>视频流（必须明确选择，不默选全部）</label>
      <select id={`${id}-stream`} className="w-full rounded border border-border bg-background p-2" value={streamIndex} onChange={e => change(setStreamIndex, e.target.value)}>
        <option value="">请选择实际探测的视频流</option>
        {checkedProbe?.video_streams.map(stream => <option key={stream.index} value={stream.index}>流 {stream.index} · {stream.codec} · {stream.width}×{stream.height} · {stream.average_frame_rate} fps</option>)}
      </select>
      <label className="block" htmlFor={`${id}-points`}>时间点 timestamps_ms（0起始整数毫秒，递增且不重复；留空不表示全部）</label>
      <Input id={`${id}-points`} inputMode="numeric" value={timestamps} maxLength={256} placeholder="例如 0,1000,2500；不自动补点或展开范围" onChange={e => change(setTimestamps, e.target.value)} />
      <div className="grid grid-cols-2 gap-2">
        <label htmlFor={`${id}-width`}>PNG最大宽度（1–2048）<Input id={`${id}-width`} inputMode="numeric" value={maxWidth} maxLength={4} onChange={e => change(setMaxWidth, e.target.value)} /></label>
        <label htmlFor={`${id}-height`}>PNG最大高度（1–2048）<Input id={`${id}-height`} inputMode="numeric" value={maxHeight} maxLength={4} onChange={e => change(setMaxHeight, e.target.value)} /></label>
      </div>
      <p>输出为8位RGB PNG，等比缩小、不放大、不自动旋转；准确输出尺寸在宿主计划中展示，不允许自定义滤镜或命令参数。</p>
      <p>本片明确选中 {selected?.frame_count ?? 0} 帧；整次最多 {VIDEO_LIMITS.framesPerRequest} 帧。原件数、其他视频帧数及原子保留由宿主真实scope计算，不发送或信任浏览器自报预算；准确合计在宿主计划中展示。</p>
      {selectionProblem && <p role="status">{selectionProblem}</p>}
      <Button size="sm" variant="outline" disabled={busy || !connected || !selected || !integrationReady || Boolean(preview)} onClick={prepare}>预览准确抽帧计划（不抽帧、不发送）</Button>
    </fieldset>
    {preview && <div className="space-y-2 rounded border border-border p-3">
      <h4 className="font-semibold">准确抽帧计划 · 尚未抽帧</h4>
      <p>{preview.plan.filename} · 原件版本 {preview.plan.original_version} · {preview.plan.byte_length} bytes</p>
      <p>时间点（ms）：{preview.plan.selection.timestamps_ms.join(', ')} · 明确帧数：{preview.plan.frame_count} · 视频流：{preview.plan.selection.stream_index}</p>
      <p>输出尺寸：{preview.plan.output_width}×{preview.plan.output_height} · image/png · 整次共 {preview.plan.request_total_video_frames}/16 帧、{preview.plan.request_original_count}/8 原件</p>
      <p className="break-all">原件 SHA256：{preview.plan.original_sha256}</p>
      <p className="break-all">计划指纹：{preview.plan.fingerprint}</p>
      <p className="break-all">探测器：{preview.plan.probe_tool.version} · SHA256 {preview.plan.probe_tool.sha256}</p>
      <p className="break-all">转换器：{preview.plan.conversion_tool.version} · SHA256 {preview.plan.conversion_tool.sha256} · Pillow {preview.plan.pillow_version}</p>
      <ul className="list-disc pl-5">{preview.plan.loss_notes.map((note, index) => <li key={index}>{note}</li>)}</ul>
      {previewProblem && <p role="alert" className="text-destructive">{previewProblem}</p>}
      <label className="flex items-start gap-2"><input type="checkbox" checked={ack} disabled={busy || Boolean(previewProblem)} onChange={e => setAck(e.target.checked)} />我已核对原件、全部时间点、明确帧数、实际输出尺寸及丢音频／连续运动等损失，仅批准这一次离线本地抽帧，不批准模型发送。</label>
      <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={cancel}>撤销本次计划</Button><Button size="sm" disabled={busy || !connected || !ack || Boolean(previewProblem) || !onApproveConversion} onClick={convert}>明确抽帧一次（不发送模型）</Button></div>
    </div>}
    {busy && <p role="status">正在进行一次宿主本地操作；关闭UI或中断连接不等于确认原生进程已停止，结果未知时不自动重试。</p>}
    {notice && <p role="status">{notice}</p>}
  </section>
}
