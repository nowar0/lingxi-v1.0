import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Terminal as XTerm } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { api, apiBlob, json } from '@/api/client'
import { downloadBlob } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import './advanced-workspace.css'

export function useOperation() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const running = useRef(false)
  const run = async (action: () => Promise<void>) => {
    if (running.current) return
    running.current = true
    setBusy(true)
    setError('')
    try { await action() } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      running.current = false
      setBusy(false)
    }
  }
  return { busy, error, setError, run }
}

export type Preview<T> = { token: string; details: T }
export type ArtifactResult = { id: string; name: string; url: string; preview_kind?: string }

export function Status({ busy, error }: { busy: boolean; error: string }) {
  return error ? <p className="advanced-error" role="alert">{error}</p>
    : busy ? <p className="advanced-muted" role="status">正在处理…</p> : null
}

export function Confirm({ children, busy, onApprove, onCancel }: {
  children: ReactNode; busy: boolean; onApprove: () => void; onCancel: () => void
}) {
  return <section className="advanced-confirm" aria-label="操作确认">
    <h3>确认这一步</h3>
    {children}
    <div className="advanced-actions">
      <Button variant="outline" size="sm" disabled={busy} onClick={onCancel}>取消</Button>
      <Button size="sm" disabled={busy} onClick={onApprove}>确认执行</Button>
    </div>
  </section>
}

export function ArtifactLink({ item, onOpen }: { item: ArtifactResult; onOpen?: () => void }) {
  const state = useOperation()
  return <div className="advanced-artifact">
    <strong>{item.name}</strong>
    <div className="advanced-actions">
      {onOpen && <Button size="sm" variant="outline" onClick={onOpen}>在产物中查看</Button>}
      <Button size="sm" variant="ghost" disabled={state.busy}
        onClick={() => void state.run(async () => downloadBlob(await apiBlob(item.url), item.name))}>下载</Button>
    </div>
    <Status busy={state.busy} error={state.error} />
  </div>
}

type TerminalState = { available?: boolean; alive: boolean; generation?: string; cwd: string; cursor: number; output: string; reset?: boolean }
type InputPreview = { cwd: string; text: string; enter: boolean; interrupt: boolean; key?: string }
const terminalApi = '/v9/terminal/pty'

export function InteractiveTerminal({ sessionId, active, projectPath }: { sessionId: number; active: boolean; projectPath: string }) {
  const state = useOperation()
  const screen = useRef<HTMLDivElement>(null)
  const display = useRef<XTerm | undefined>(undefined)
  const cursor = useRef(0)
  const generation = useRef('')
  const [connection, setConnection] = useState<TerminalState>()
  const [input, setInput] = useState('')
  const [sendEnter, setSendEnter] = useState(true)
  const [preview, setPreview] = useState<Preview<InputPreview>>()

  useEffect(() => {
    if (!screen.current) return
    const term = new XTerm({ disableStdin: true, convertEol: false, fontSize: 13, cursorBlink: false,
      scrollback: 1600, theme: { background: '#181818', foreground: '#f2f2f1', cursor: '#dedede' } })
    term.open(screen.current)
    display.current = term
    return () => { display.current = undefined; term.dispose() }
  }, [])

  const dimensions = () => ({
    rows: Math.max(5, Math.min(100, Math.floor((screen.current?.clientHeight || 300) / 17))),
    columns: Math.max(20, Math.min(260, Math.floor((screen.current?.clientWidth || 680) / 8.1))),
  })
  useEffect(() => {
    if (!active || !projectPath || !screen.current) return
    let live = true
    let pending = false
    const poll = async () => {
      if (pending) return
      pending = true
      try {
        const value = await api<TerminalState>(`${terminalApi}/status?session_id=${sessionId}&cursor=${cursor.current}`)
        if (!live) return
        if (value.generation !== generation.current || value.reset) {
          display.current?.reset()
          setPreview(undefined)
          if (!value.reset && value.generation && cursor.current) {
            cursor.current = 0
            generation.current = value.generation
            return
          }
        }
        if (value.output) display.current?.write(value.output)
        cursor.current = value.cursor
        generation.current = value.generation || ''
        setConnection(value)
      } catch (error) {
        if (live) state.setError((error as Error).message)
      } finally { pending = false }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 650)
    let last = ''
    const observer = new ResizeObserver(() => {
      const size = dimensions()
      const key = JSON.stringify(size)
      if (key === last) return
      last = key
      display.current?.resize(size.columns, size.rows)
      if (generation.current) void api(terminalApi+'/resize', json('POST', { session_id: sessionId, ...size })).catch(() => {})
    })
    observer.observe(screen.current)
    return () => { live = false; window.clearInterval(timer); observer.disconnect() }
  }, [active, sessionId, projectPath])

  const connect = () => void state.run(async () => {
    cursor.current = 0
    const size = dimensions()
    display.current?.resize(size.columns, size.rows)
    const current = await api<TerminalState>(terminalApi+'/open', json('POST', { session_id: sessionId, ...size }))
    setConnection(current)
  })
  const prepare = (text: string, enter: boolean, interrupt = false, key = '') => void state.run(async () => {
    setPreview(await api(terminalApi+'/preview', json('POST', { session_id: sessionId, text, enter, interrupt, key })))
  })
  return <section className="advanced-workspace" data-ui="interactive-terminal">
    <header className="advanced-header"><div><h2>交互终端</h2><p>PowerShell · Windows ConPTY</p></div>
      <div className="advanced-actions">
        {connection?.alive ? <Button size="sm" variant="outline" disabled={state.busy}
          onClick={() => void state.run(async () => {
            await api(terminalApi+'/close', json('POST', { session_id: sessionId }))
            setConnection(undefined); setPreview(undefined); display.current?.reset()
          })}>关闭终端</Button> : <Button size="sm" onClick={connect} disabled={!projectPath || state.busy || connection?.available === false}>连接终端</Button>}
      </div>
    </header>
    {!projectPath && <p className="advanced-muted">先为工作会话绑定项目目录。</p>}
    <p className="advanced-path">{connection?.cwd || projectPath || '尚未绑定目录'}</p>
    <div ref={screen} className="advanced-terminal" aria-label="交互式 PowerShell 输出" />
    <form className="advanced-form" onSubmit={event => { event.preventDefault(); prepare(input, sendEnter) }}>
      <label>终端命令或交互输入<Textarea value={input} maxLength={8000} disabled={!connection?.alive || state.busy}
        onChange={event => { setInput(event.target.value); setPreview(undefined) }} placeholder="在这里输入或粘贴，确认后才发给终端" /></label>
      <div className="advanced-actions">
        <label className="advanced-inline"><input type="checkbox" checked={sendEnter} onChange={event => { setSendEnter(event.target.checked); setPreview(undefined) }} />附加回车</label>
        <Button size="sm" type="submit" disabled={!connection?.alive || state.busy || !input}>预览输入</Button>
        <Button size="sm" type="button" variant="outline" disabled={!connection?.alive || state.busy}
          onClick={() => prepare('Ctrl+C', false, true)}>中断</Button>
        <select aria-label="发送单键" value="" disabled={!connection?.alive || state.busy}
          onChange={event => { if (event.target.value) prepare(event.target.value, false, false, event.target.value) }}>
          <option value="">发送单键</option>{['Enter', 'Tab', 'Escape', 'Up', 'Down', 'Left', 'Right'].map(key => <option key={key}>{key}</option>)}
        </select>
      </div>
    </form>
    {preview && <Confirm busy={state.busy} onCancel={() => setPreview(undefined)} onApprove={() => void state.run(async () => {
      const token = preview.token; setPreview(undefined)
      await api(terminalApi+'/commit', json('POST', { session_id: sessionId, token }))
      setInput('')
    })}><p>{preview.details.cwd}</p><pre>{preview.details.text}</pre>{preview.details.enter && <p>发送后按回车</p>}</Confirm>}
    <Status busy={state.busy} error={state.error} />
  </section>
}

type Tool = { name: string; description: string; write: boolean; parameters: { properties: Record<string, { type: string; description?: string }>; required: string[] } }
type Step = { id: string; title: string; tool: string; depends_on: string[]; args: string; condition: string }
type SavedWorkflow = { id: string; title: string; revision: number; steps: Array<Omit<Step, 'args' | 'condition'> & { arguments: unknown; when?: unknown }> }
const emptyStep = (id: string): Step => ({ id, title: '', tool: '', depends_on: [], args: '{}', condition: '' })

export function WorkflowPanel({ sessionId }: { sessionId: number }) {
  const state = useOperation()
  const navigate = useNavigate()
  const [saved, setSaved] = useState<SavedWorkflow[]>([])
  const [tools, setTools] = useState<Tool[]>([])
  const [current, setCurrent] = useState<SavedWorkflow>()
  const [title, setTitle] = useState('')
  const [steps, setSteps] = useState<Step[]>([emptyStep('step1')])
  const next = useRef(2)
  const refresh = async () => {
    const [flows, available] = await Promise.all([
      api<{ items: SavedWorkflow[] }>(`/v6/workflows?session_id=${sessionId}`),
      api<{ items: Tool[] }>(`/v6/workflows/tools?session_id=${sessionId}`),
    ])
    setSaved(flows.items); setTools(available.items)
  }
  useEffect(() => { void state.run(refresh) }, [sessionId])
  const edit = (value?: SavedWorkflow) => {
    setCurrent(value); setTitle(value?.title || '')
    setSteps(value ? value.steps.map(step => ({ ...step, args: JSON.stringify(step.arguments, null, 2),
      condition: step.when ? JSON.stringify(step.when, null, 2) : '' })) : [emptyStep('step1')])
    next.current = (value?.steps.length || 1) + 1
  }
  const change = (id: string, patch: Partial<Step>) => setSteps(all => all.map(step => step.id === id ? { ...step, ...patch } : step))
  const save = () => void state.run(async () => {
    const body = { session_id: sessionId, title, expected_revision: current?.revision || 0,
      steps: steps.map(({ args, condition, ...step }) => ({ ...step, arguments: JSON.parse(args), when: condition.trim() ? JSON.parse(condition) : null })) }
    const value = await api<SavedWorkflow>('/v6/workflows' + (current ? '/'+current.id : ''), json(current ? 'PUT' : 'POST', body))
    await refresh(); edit(value)
  })
  return <section className="advanced-workspace" data-ui="workflow-builder">
    <header className="advanced-header"><div><h2>工具流程</h2><p>通过步骤依赖编排串行、并行和条件分支。</p></div>
      <Button variant="outline" size="sm" onClick={() => edit()}>新建</Button></header>
    {saved.length > 0 && <label>已保存流程<select value={current?.id || ''} onChange={event => edit(saved.find(row => row.id === event.target.value))}>
      <option value="">新流程</option>{saved.map(row => <option key={row.id} value={row.id}>{row.title} · 版本 {row.revision}</option>)}
    </select></label>}
    <label>流程名称<Input value={title} maxLength={150} onChange={event => setTitle(event.target.value)} /></label>
    <ol className="advanced-steps">{steps.map((step, index) => {
      const tool = tools.find(item => item.name === step.tool)
      return <li key={step.id}>
        <div className="advanced-step-title"><strong>{step.id}</strong><Button variant="ghost" size="xs" disabled={steps.length <= 1}
          onClick={() => setSteps(all => all.filter(item => item.id !== step.id).map(item => ({ ...item, depends_on: item.depends_on.filter(id => id !== step.id) })))}>移除步骤</Button></div>
        <label>步骤名称<Input value={step.title} maxLength={150} onChange={event => change(step.id, { title: event.target.value })} /></label>
        <label>工具<select value={step.tool} onChange={event => {
          const selected = tools.find(item => item.name === event.target.value)
          const args = Object.fromEntries((selected?.parameters.required || []).map(name => [name, selected?.parameters.properties[name]?.type === 'integer' ? 0 : '']))
          change(step.id, { tool: event.target.value, args: JSON.stringify(args, null, 2) })
        }}><option value="">选择工具</option>{tools.map(item => <option key={item.name} value={item.name}>{item.name}{item.write ? ' · 需审批' : ''}</option>)}</select></label>
        {tool && <p className="advanced-muted">{tool.description}</p>}
        {index > 0 && <fieldset><legend>先完成这些步骤</legend>{steps.slice(0, index).map(parent => <label className="advanced-inline" key={parent.id}>
          <input type="checkbox" checked={step.depends_on.includes(parent.id)}
            onChange={event => change(step.id, { depends_on: event.target.checked ? [...step.depends_on, parent.id] : step.depends_on.filter(id => id !== parent.id) })} />
          {parent.title || parent.id}</label>)}</fieldset>}
        <label>工具参数<Textarea className="advanced-code" value={step.args} onChange={event => change(step.id, { args: event.target.value })} /></label>
        {step.depends_on.length > 0 && <details><summary>条件和输出引用</summary>
          <p className="advanced-muted">可以用 <code>{'{"$from":{"node":"step1","path":["字段"]}}'}</code> 引用依赖的输出。</p>
          <label>执行条件（可选 JSON）<Textarea className="advanced-code" value={step.condition}
            placeholder={'{"source":{"node":"step1","path":[]},"op":"truthy"}'}
            onChange={event => change(step.id, { condition: event.target.value })} /></label></details>}
      </li>
    })}</ol>
    <div className="advanced-actions">
      <Button size="sm" variant="outline" disabled={steps.length >= 20} onClick={() => setSteps(old => {
        let id = 'step'+next.current++
        while (old.some(item => item.id === id)) id = 'step'+next.current++
        return [...old, emptyStep(id)]
      })}>添加步骤</Button>
      <Button size="sm" onClick={save} disabled={state.busy || !title.trim() || steps.some(step => !step.title.trim() || !step.tool)}>保存流程</Button>
      {current && <Button size="sm" variant="outline" disabled={state.busy} onClick={() => void state.run(async () => {
        const job = await api<{ id: string }>(`/v6/workflows/${current.id}/tasks`,
          json('POST', { session_id: sessionId, expected_revision: current.revision }))
        navigate('/tasks?job='+job.id)
      })}>用已保存版本创建任务</Button>}
    </div>
    <p className="advanced-muted">任务创建后等待启动。写操作沿用任务审批。</p>
    <Status busy={state.busy} error={state.error} />
  </section>
}
