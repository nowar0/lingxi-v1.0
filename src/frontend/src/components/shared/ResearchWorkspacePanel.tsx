import { useEffect, useState } from 'react'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import { settingsPayload } from '@/lib/settingsPayload'
import { ArtifactLink, Confirm, Status, useOperation, type ArtifactResult, type Preview } from './AdvancedWorkspaceTools'

type Provider = 'bing' | 'tavily' | 'searxng'
type Settings = { primary: Provider; fallback: Provider | 'none'; searxng_url: string; cache_seconds: number;
  revision: number; tavily_key_saved?: boolean }
type SearchResult = { title: string; snippet: string; url: string; published_at?: string }
type SearchAnswer = { provider?: string; status: string; cached: boolean; results: SearchResult[];
  attempts: Array<{ provider: string; status: string }> }
type History = { provider?: string; status: string; results: number; latency: number; cached: number; at: string }
const names = { bing: 'Bing RSS', tavily: 'Tavily', searxng: 'SearXNG' }
const statusNames: Record<string, string> = { ok: '成功', empty: '无匹配结果', unavailable: '暂不可用' }

export function SearchPanel({ sessionId, sendToBrowser }: { sessionId: number; sendToBrowser?: (url: string) => void }) {
  const state = useOperation()
  const [query, setQuery] = useState('')
  const [site, setSite] = useState('')
  const [days, setDays] = useState(0)
  const [settings, setSettings] = useState<Settings>()
  const [answer, setAnswer] = useState<SearchAnswer>()
  const [logs, setLogs] = useState<History[]>([])
  const [page, setPage] = useState<{ url: string; text: string; truncated: boolean }>()
  const [key, setKey] = useState('')
  const reload = async () => {
    const [saved, history] = await Promise.all([api<Settings>('/v6/search/settings'),
      api<{ items: History[] }>(`/v6/search/history?session_id=${sessionId}`)])
    setSettings(saved); setLogs(history.items)
  }
  useEffect(() => { void state.run(reload) }, [sessionId])
  const runSearch = (test = false) => void state.run(async () => {
    setPage(undefined)
    const result = await api<SearchAnswer>('/v6/search/query'+(test ? '?check=true' : ''), json('POST',
      { session_id: sessionId, query: test ? (query.trim() || 'IANA example domains') : query.trim(), site, days }))
    setAnswer(result)
    setLogs((await api<{ items: History[] }>(`/v6/search/history?session_id=${sessionId}`)).items)
  })
  const edit = (patch: Partial<Settings>) => setSettings(old => old ? { ...old, ...patch } : old)
  return <section className="advanced-workspace" data-ui="web-search">
    <header className="advanced-header"><div><h2>网页搜索</h2><p>查找来源，按需阅读网页正文。</p></div></header>
    <form className="advanced-form" onSubmit={event => { event.preventDefault(); runSearch() }}>
      <label>搜索词<Input value={query} maxLength={500} onChange={event => setQuery(event.target.value)} /></label>
      <div className="advanced-columns">
        <label>站点（可选）<Input placeholder="example.org" value={site} onChange={event => setSite(event.target.value)} /></label>
        <label>发布时间<select value={days} onChange={event => setDays(Number(event.target.value))}>
          <option value={0}>不限时间</option><option value={1}>最近一天</option><option value={7}>最近一周</option>
          <option value={30}>最近一月</option><option value={365}>最近一年</option>
        </select></label>
      </div>
      <Button size="sm" type="submit" disabled={state.busy || !query.trim()}>搜索</Button>
    </form>
    {answer && <div className="advanced-list">
      <p className="advanced-muted">{answer.provider || '搜索服务不可用'}{answer.cached && ' · 缓存'} · {statusNames[answer.status] || answer.status}
        {answer.attempts.length > 1 && ' · '+answer.attempts.map(entry => entry.provider+' '+statusNames[entry.status]).join(' → ')}</p>
      {answer.results.map(result => <article className="advanced-search-result" key={result.url}>
        <a href={result.url} target="_blank" rel="noreferrer">{result.title || result.url}</a>
        <p className="advanced-path">{result.url}</p><p>{result.snippet}</p>
        <div className="advanced-actions">
          {result.published_at && <time dateTime={result.published_at}>{new Date(result.published_at).toLocaleDateString()}</time>}
          <Button variant="outline" size="xs" disabled={state.busy} onClick={() => void state.run(async () => {
            setPage(await api('/v6/search/read', json('POST', { session_id: sessionId, url: result.url })))
          })}>读取正文</Button>
          {sendToBrowser && <Button variant="ghost" size="xs" onClick={() => sendToBrowser(result.url)}>在工作浏览器打开</Button>}
        </div>
      </article>)}
    </div>}
    {page && <details open><summary>网页正文{page.truncated ? '（已截取）' : ''}</summary><a className="advanced-path" href={page.url}
      target="_blank" rel="noreferrer">{page.url}</a><pre className="advanced-result">{page.text}</pre></details>}
    {settings && <details><summary>搜索源与连接测试</summary><div className="advanced-form">
      <div className="advanced-columns">
        <label>主搜索源<select value={settings.primary} onChange={event => edit({ primary: event.target.value as Provider })}>
          {Object.entries(names).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
        <label>备用搜索源<select value={settings.fallback} onChange={event => edit({ fallback: event.target.value as Settings['fallback'] })}>
          <option value="none">不使用</option>{Object.entries(names).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
      </div>
      {([settings.primary, settings.fallback].includes('searxng') || settings.searxng_url) && <label>SearXNG HTTPS 地址
        <Input value={settings.searxng_url} onChange={event => edit({ searxng_url: event.target.value })} /></label>}
      {([settings.primary, settings.fallback].includes('tavily') || settings.tavily_key_saved) && <>
        <label>Tavily 密钥<Input type="password" autoComplete="off" value={key} onChange={event => setKey(event.target.value)}
          placeholder={settings.tavily_key_saved ? '已保存' : '输入密钥'} /></label>
        <div className="advanced-actions"><Button size="sm" variant="outline" disabled={!key || state.busy} onClick={() => void state.run(async () => {
          await api('/v6/search/tavily-key', json('PUT', { api_key: key })); setKey('')
          edit({ tavily_key_saved: true })
        })}>保存密钥</Button>
          {settings.tavily_key_saved && <Button size="sm" variant="ghost" disabled={state.busy} onClick={() => void state.run(async () => {
            await api('/v6/search/tavily-key', { method: 'DELETE' }); edit({ tavily_key_saved: false })
          })}>移除密钥</Button>}</div>
      </>}
      <label>结果缓存（秒）<Input type="number" min={0} max={3600} value={settings.cache_seconds}
        onChange={event => edit({ cache_seconds: Number(event.target.value) })} /></label>
      <div className="advanced-actions"><Button size="sm" disabled={state.busy} onClick={() => void state.run(async () => {
        const { tavily_key_saved } = settings
        const saved = await api<Settings>('/v6/search/settings', json('PUT', settingsPayload(settings,['primary','fallback','searxng_url','cache_seconds'])))
        setSettings({ ...saved, tavily_key_saved })
      })}>保存搜索配置</Button><Button size="sm" variant="outline" disabled={state.busy} onClick={() => runSearch(true)}>测试已保存配置</Button></div>
    </div></details>}
    <details><summary>本会话调用记录</summary>
      {logs.length ? <ul className="advanced-history">{logs.map((entry, index) => <li key={entry.at+'-'+index}>
        <span>{entry.provider || '未取得结果'} · {statusNames[entry.status] || entry.status}</span>
        <span>{entry.results} 条 · {entry.latency} ms{entry.cached ? ' · 缓存' : ''}</span></li>)}</ul>
        : <p className="advanced-muted">搜索后显示服务、用时和结果数量。</p>}
    </details>
    <Status busy={state.busy} error={state.error} />
  </section>
}

type ElementInfo = { ref: number; tag: string; type: string; text: string; href: string; form: string; method: string }
type BrowserState = { running: boolean; url: string; title?: string; takeover: boolean; text?: string; fingerprint: string;
  tabs: Array<{ id: string; url: string; selected: boolean }>; elements: ElementInfo[] }
type Action = 'open' | 'new_tab' | 'switch_tab' | 'close_tab' | 'close' | 'click' | 'fill' | 'select'
  | 'press' | 'scroll' | 'wait' | 'upload' | 'download' | 'script' | 'take_over' | 'return'
const labels: Record<Action, string> = { open: '打开网址', new_tab: '新标签', switch_tab: '切换标签', close_tab: '关闭标签',
  close: '关闭浏览器', click: '点击', fill: '填写', select: '选择', press: '按键', scroll: '滚动',
  wait: '等待就绪', upload: '上传会话附件', download: '下载到会话产物', script: '运行页面脚本', take_over: '接管', return: '归还控制' }
type ActionBody = { action: Action; session_id: number; url?: string; tab_id?: string; ref?: number; value?: string; attachment_id?: string; delta?: number }
type BrowserPreview = { action: ActionBody; current_url: string; element?: ElementInfo; attachment?: { name: string; sha256: string } }
const base = '/v6/browser/workspace'

function BrowserPanel({ sessionId, initialUrl, openArtifacts }: { sessionId: number; initialUrl: string; openArtifacts: () => void }) {
  const task = useOperation()
  const [state, setState] = useState<BrowserState>()
  const [url, setUrl] = useState(initialUrl)
  const [action, setAction] = useState<Action>('click')
  const [ref, setRef] = useState(0)
  const [value, setValue] = useState('')
  const [tab, setTab] = useState('')
  const [delta, setDelta] = useState(600)
  const [file, setFile] = useState('')
  const [attachments, setAttachments] = useState<ArtifactResult[]>([])
  const [preview, setPreview] = useState<Preview<BrowserPreview>>()
  const [artifact, setArtifact] = useState<ArtifactResult>()
  const [output, setOutput] = useState('')
  const refresh = async () => {
    setState(await api(`${base}?session_id=${sessionId}`))
    setAttachments((await api<{ items: ArtifactResult[] }>(`/v9/artifacts?session_id=${sessionId}`)).items)
  }
  useEffect(() => { void task.run(refresh) }, [sessionId])
  const prepare = (kind: Action, extra: Partial<ActionBody> = {}) => void task.run(async () => {
    setPreview(await api(base+'/preview', json('POST', { session_id: sessionId, action: kind, ...extra })))
  })
  const complete = () => void task.run(async () => {
    if (!preview) return
    const token = preview.token; setPreview(undefined)
    const result = await api<{ browser: BrowserState; artifact?: ArtifactResult; result?: string }>(base+'/commit',
      json('POST', { session_id: sessionId, token }))
    setState(result.browser); setRef(0); setTab(''); setArtifact(result.artifact); setOutput(result.result || '')
  })
  const needsElement = ['click', 'fill', 'select', 'upload', 'download'].includes(action)
  const enabled = state?.running && !state.takeover && !task.busy
  return <section className="advanced-workspace" data-ui="workspace-browser">
    <header className="advanced-header"><div><h2>工作浏览器</h2><p>会话独立 Edge 窗口</p></div>
      <Button variant="outline" size="sm" disabled={task.busy} onClick={() => void task.run(refresh)}>刷新快照</Button></header>
    <form className="advanced-form" onSubmit={event => { event.preventDefault(); prepare('open', { url: url.trim() }) }}>
      <label>网页地址<Input value={url} placeholder="https://…" onChange={event => { setUrl(event.target.value); setPreview(undefined) }} /></label>
      <div className="advanced-actions">
        <Button size="sm" type="submit" disabled={!url.trim() || task.busy || state?.takeover}>预览打开</Button>
        <Button size="sm" type="button" variant="outline" disabled={task.busy || state?.takeover} onClick={() => prepare('new_tab', { url: url.trim() })}>新标签</Button>
      </div>
    </form>
    {state?.running && <>
      <div className="advanced-path">{state.url || '当前无标签'}</div>
      <div className="advanced-actions">
        <Button size="sm" variant="outline" disabled={task.busy} onClick={() => prepare(state.takeover ? 'return' : 'take_over')}>
          {state.takeover ? '归还控制' : '接管浏览器'}</Button>
        <Button size="sm" variant="outline" disabled={!state.url || task.busy} onClick={() => void task.run(async () => {
          setArtifact((await api<{ artifact: ArtifactResult }>(base+'/screenshot', json('POST', { session_id: sessionId }))).artifact)
        })}>保存截图</Button>
        <Button size="sm" variant="ghost" disabled={task.busy} onClick={() => prepare('close')}>关闭浏览器</Button>
      </div>
      {state.takeover && <p className="advanced-muted" role="status">正在手动操作 Edge。归还后可继续从快照操作。</p>}
      {!!state.tabs.length && <div className="advanced-form"><label>标签<select value={tab || state.tabs.find(item => item.selected)?.id || ''}
        onChange={event => { setTab(event.target.value); setPreview(undefined) }}>{state.tabs.map(item => <option key={item.id} value={item.id}>{item.url}</option>)}</select></label>
        <div className="advanced-actions">
          {(['switch_tab', 'close_tab'] as const).map(kind => <Button key={kind} size="xs" variant="outline" disabled={!enabled}
            onClick={() => prepare(kind, { tab_id: tab || state.tabs.find(item => item.selected)?.id || '' })}>{labels[kind]}</Button>)}
        </div></div>}
      <form className="advanced-form" onChange={() => setPreview(undefined)} onSubmit={event => {
        event.preventDefault(); prepare(action, { ref, value: action === 'press' ? value || 'Enter' : value, attachment_id: file, delta })
      }}>
        <label>网页动作<select value={action} onChange={event => { setAction(event.target.value as Action); setValue('') }}>
          {(['click', 'fill', 'select', 'press', 'scroll', 'wait', 'upload', 'download', 'script'] as const).map(kind =>
            <option key={kind} value={kind}>{labels[kind]}</option>)}</select></label>
        {(needsElement || action === 'wait' || action === 'press') && <label>快照中的元素<select value={ref} onChange={event => setRef(Number(event.target.value))}>
          <option value={0}>{needsElement ? '选择目标' : '当前页面'}</option>
          {state.elements.map(element => <option value={element.ref} key={element.ref}>{element.ref} · {element.tag} · {element.text || element.href || element.type}</option>)}
        </select></label>}
        {(['fill', 'select', 'script'] as const).some(kind => kind === action) && <label>{action === 'script' ? '页面脚本' : '内容'}
          <Textarea value={value} onChange={event => setValue(event.target.value)} /></label>}
        {action === 'press' && <label>按键<select value={value || 'Enter'} onChange={event => setValue(event.target.value)}>
          {['Enter', 'Escape', 'Tab', 'Backspace', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', 'Space'].map(key => <option key={key}>{key}</option>)}
        </select></label>}
        {action === 'scroll' && <label>滚动像素<Input type="number" min={-2500} max={2500} value={delta} onChange={event => setDelta(Number(event.target.value))} /></label>}
        {action === 'upload' && <label>会话附件<select value={file} onChange={event => setFile(event.target.value)}>
          <option value="">选择附件</option>{attachments.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>}
        <Button size="sm" type="submit" disabled={!enabled || needsElement && !ref || action === 'upload' && !file}>预览动作</Button>
      </form>
      {state.text && <details><summary>快照正文</summary><pre className="advanced-result">{state.text}</pre></details>}
    </>}
    {preview && <Confirm busy={task.busy} onCancel={() => setPreview(undefined)} onApprove={complete}>
      <p>{labels[preview.details.action.action]}</p>
      <p className="advanced-path">{preview.details.action.url || preview.details.current_url}</p>
      {preview.details.element && <p>{preview.details.element.text || preview.details.element.tag} {preview.details.element.form || preview.details.element.href}</p>}
      {preview.details.attachment && <p>上传 {preview.details.attachment.name}</p>}
      {preview.details.action.value && <pre>{preview.details.action.value}</pre>}
    </Confirm>}
    {artifact && <ArtifactLink item={artifact} onOpen={openArtifacts} />}
    {output && <pre className="advanced-result">{output}</pre>}
    <Status busy={task.busy} error={task.error} />
  </section>
}

export function ResearchWorkspacePanel({ sessionId, openArtifacts, legacy }: { sessionId: number; openArtifacts: () => void; legacy: React.ReactNode }) {
  const [view, setView] = useState<'search' | 'browser' | 'legacy'>('search')
  const [url, setUrl] = useState('')
  const [browserKey, setBrowserKey] = useState(0)
  return <div className="advanced-research" data-ui="research-tools">
    <nav className="advanced-tabs" aria-label="搜索与浏览器工具">
      {(['search', 'browser', 'legacy'] as const).map(kind => <Button key={kind} size="sm" variant={view === kind ? 'secondary' : 'ghost'}
        aria-pressed={view === kind} onClick={() => setView(kind)}>{{ search: '搜索', browser: '网页操作', legacy: '基础浏览器' }[kind]}</Button>)}
    </nav>
    {view === 'search' && <SearchPanel sessionId={sessionId} sendToBrowser={target => {
      setUrl(target); setBrowserKey(value => value+1); setView('browser')
    }} />}
    {view === 'browser' && <BrowserPanel key={browserKey} sessionId={sessionId} initialUrl={url} openArtifacts={openArtifacts} />}
    {view === 'legacy' && legacy}
  </div>
}
