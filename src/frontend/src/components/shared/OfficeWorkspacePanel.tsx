import { useEffect, useState } from 'react'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import { ArtifactLink, Confirm, Status, useOperation, type ArtifactResult, type Preview } from './AdvancedWorkspaceTools'

type Kind = 'word' | 'excel' | 'powerpoint' | 'ocr'
type Action = 'create' | 'edit' | 'render' | 'recognize'
type Cell = { address: string; value: string; type: 'text' | 'number' | 'formula' }
type Slide = { title: string; body: string }
type Capability = { office: Record<Exclude<Kind, 'ocr'>, boolean>; ocr_languages: string[] }
type OfficeResult = { artifact: ArtifactResult; text?: string; workspace_path?: string; workspace_error?: string;
  result?: { formula_results?: Array<{ cell: string; text: string; value: unknown }> } }
type Confirmation = { operation: Record<string, unknown>; workspace: string; source_name?: string;
  source_sha256?: string; output_sha256: string }
const kindName = { word: 'Word', excel: 'Excel', powerpoint: 'PowerPoint', ocr: 'OCR' }
const actionName = { create: '生成文档', edit: '编辑副本', render: '生成 PDF 预览', recognize: '识别图片或扫描 PDF' }

export function OfficeWorkspacePanel({ sessionId, projectPath, openArtifacts }: {
  sessionId: number; projectPath: string; openArtifacts: () => void
}) {
  const request = useOperation()
  const [capability, setCapability] = useState<Capability>()
  const [attachments, setAttachments] = useState<ArtifactResult[]>([])
  const [kind, setKind] = useState<Kind>('word')
  const [action, setAction] = useState<Action>('create')
  const [source, setSource] = useState('')
  const [attachment, setAttachment] = useState('')
  const [output, setOutput] = useState('')
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [find, setFind] = useState('')
  const [replace, setReplace] = useState('')
  const [sheet, setSheet] = useState('')
  const [slideIndex, setSlideIndex] = useState(1)
  const [slides, setSlides] = useState<Slide[]>([{ title: '', body: '' }])
  const [cells, setCells] = useState<Cell[]>([{ address: 'A1', value: '', type: 'text' }])
  const [language, setLanguage] = useState('zh-Hans-CN')
  const [pages, setPages] = useState('1')
  const [prepared, setPrepared] = useState<Preview<Confirmation>>()
  const [result, setResult] = useState<OfficeResult>()
  const reload = async () => {
    const [available, files] = await Promise.all([api<Capability>('/v6/office/status'),
      api<{ items: ArtifactResult[] }>(`/v9/artifacts?session_id=${sessionId}`)])
    setCapability(available); setAttachments(files.items)
    if (available.ocr_languages.length) setLanguage(available.ocr_languages[0])
  }
  useEffect(() => { void request.run(reload) }, [sessionId])
  const editFields = action === 'edit' || action === 'create'
  const available = kind === 'ocr' ? !!capability?.ocr_languages.length : !!capability?.office[kind]
  const prepare = () => void request.run(async () => {
    setResult(undefined)
    const pageNumbers = pages.split(/[，,\s]+/).filter(Boolean).map(Number)
    if (kind === 'ocr' && pageNumbers.some(page => !Number.isInteger(page) || page < 1)) throw new Error('请输入以逗号分隔的页码')
    const values = kind === 'excel' && editFields ? cells.map(cell => {
      const value = cell.type === 'number' ? Number(cell.value) : cell.value
      if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('请为 '+cell.address+' 填写有效数字')
      return { address: cell.address.trim().toUpperCase(), value, formula: cell.type === 'formula' }
    }) : []
    setPrepared(await api('/v6/office/preview', json('POST', {
      session_id: sessionId, kind, action, source: action !== 'create' && !attachment ? source.trim() : '',
      attachment_id: action === 'create' ? '' : attachment, output: editFields ? output.trim() : '',
      fields: { title, paragraphs: body.split('\n'), find, replace, sheet, cells: values, slides, slide: slideIndex },
      language, pages: kind === 'ocr' ? pageNumbers : [1],
    })))
  })
  return <section className="advanced-workspace" data-ui="office-tools">
    <header className="advanced-header"><div><h2>Office 与文字识别</h2><p>使用本机 Office 和 Windows OCR 处理项目文件或会话附件。</p></div>
      <Button size="sm" variant="outline" disabled={request.busy} onClick={() => void request.run(reload)}>刷新</Button></header>
    <form className="advanced-form" onChange={() => setPrepared(undefined)} onSubmit={event => { event.preventDefault(); prepare() }}>
      <div className="advanced-columns">
        <label>类型<select value={kind} onChange={event => {
          const value = event.target.value as Kind; setKind(value); setAction(value === 'ocr' ? 'recognize' : 'create')
        }}>{Object.entries(kindName).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>操作<select value={action} onChange={event => setAction(event.target.value as Action)}>
          {(kind === 'ocr' ? ['recognize'] : ['create', 'edit', 'render']).map(value =>
            <option key={value} value={value}>{actionName[value as Action]}</option>)}</select></label>
      </div>
      {capability && !available && <p className="advanced-muted">{kind === 'ocr' ? 'Windows 尚未安装 OCR 语言包。' : '本机尚未检测到 '+kindName[kind]+'。'}</p>}
      {action !== 'create' && <div className="advanced-columns">
        <label>项目相对路径<Input value={source} disabled={!!attachment} onChange={event => setSource(event.target.value)}
          placeholder={kind === 'ocr' ? '扫描件.pdf' : '项目资料中的 Office 文件'} /></label>
        <label>或使用会话附件<select value={attachment} onChange={event => setAttachment(event.target.value)}>
          <option value="">使用项目文件</option>{attachments.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
      </div>}
      {editFields && <label>保存到项目（可选）<Input value={output} onChange={event => setOutput(event.target.value)}
        placeholder="留空时只添加到会话产物" /></label>}
      {kind === 'word' && action === 'create' && <>
        <label>标题<Input value={title} onChange={event => setTitle(event.target.value)} /></label>
        <label>正文<Textarea value={body} onChange={event => setBody(event.target.value)} /></label>
      </>}
      {kind === 'word' && action === 'edit' && <div className="advanced-columns">
        <label>查找<Input value={find} onChange={event => setFind(event.target.value)} /></label>
        <label>替换为<Input value={replace} onChange={event => setReplace(event.target.value)} /></label>
      </div>}
      {kind === 'excel' && editFields && <>
        <label>工作表名称（可选）<Input value={sheet} placeholder="默认第一个工作表" onChange={event => setSheet(event.target.value)} /></label>
        <div className="advanced-list">{cells.map((cell, index) => {
          const edit = (patch: Partial<Cell>) => setCells(old => old.map((item, i) => i === index ? { ...item, ...patch } : item))
          return <div className="advanced-cell" key={index}>
            <Input aria-label={`单元格地址 ${index+1}`} value={cell.address} onChange={event => edit({ address: event.target.value })} />
            <select aria-label={`值类型 ${index+1}`} value={cell.type} onChange={event => edit({ type: event.target.value as Cell['type'] })}>
              <option value="text">文本</option><option value="number">数字</option><option value="formula">公式</option>
            </select>
            <Input aria-label={`单元格值 ${index+1}`} value={cell.value} onChange={event => edit({ value: event.target.value })} />
            <Button variant="ghost" size="xs" type="button" disabled={cells.length === 1}
              onClick={() => { setCells(old => old.filter((_, i) => i !== index)); setPrepared(undefined) }}>移除</Button>
          </div>
        })}</div>
        <Button variant="outline" size="sm" type="button" disabled={cells.length >= 500}
          onClick={() => { setCells(old => [...old, { address: 'A'+(old.length+1), value: '', type: 'text' }]); setPrepared(undefined) }}>添加单元格</Button>
      </>}
      {kind === 'powerpoint' && action === 'create' && <>
        <ol className="advanced-steps">{slides.map((slide, index) => <li key={index}>
          <div className="advanced-step-title"><strong>第 {index+1} 张</strong><Button type="button" variant="ghost" size="xs" disabled={slides.length === 1}
            onClick={() => { setSlides(old => old.filter((_, i) => i !== index)); setPrepared(undefined) }}>移除</Button></div>
          <label>标题<Input value={slide.title} onChange={event => setSlides(old => old.map((item, i) => i === index ? { ...item, title: event.target.value } : item))} /></label>
          <label>内容<Textarea value={slide.body} onChange={event => setSlides(old => old.map((item, i) => i === index ? { ...item, body: event.target.value } : item))} /></label>
        </li>)}</ol>
        <Button variant="outline" size="sm" type="button" disabled={slides.length >= 30}
          onClick={() => { setSlides(old => [...old, { title: '', body: '' }]); setPrepared(undefined) }}>添加幻灯片</Button>
      </>}
      {kind === 'powerpoint' && action === 'edit' && <div className="advanced-columns">
        <label>幻灯片编号<Input type="number" min={1} value={slideIndex} onChange={event => setSlideIndex(Number(event.target.value))} /></label>
        <label>新标题<Input value={title} onChange={event => setTitle(event.target.value)} /></label>
      </div>}
      {kind === 'ocr' && <div className="advanced-columns">
        <label>识别语言<select value={language} onChange={event => setLanguage(event.target.value)}>
          {capability?.ocr_languages.map(item => <option key={item}>{item}</option>)}</select></label>
        <label>PDF 页码<Input value={pages} onChange={event => setPages(event.target.value)} placeholder="1, 2, 3" /></label>
      </div>}
      <Button size="sm" type="submit" disabled={!projectPath || !available || request.busy}>预览操作</Button>
      {!projectPath && <p className="advanced-muted">先绑定工作目录，便于记录源文件与产物位置。</p>}
    </form>
    {prepared && <Confirm busy={request.busy} onCancel={() => setPrepared(undefined)} onApprove={() => void request.run(async () => {
      const token = prepared.token; setPrepared(undefined)
      setResult(await api('/v6/office/commit', json('POST', { session_id: sessionId, token })))
    })}>
      <p>{kindName[kind]} · {actionName[action]} · {prepared.details.workspace}</p>
      {prepared.details.source_name && <p>来源 {prepared.details.source_name}</p>}
      {prepared.details.output_sha256 !== 'new' && <p className="advanced-error">目标已存在，确认后替换该文件。</p>}
      <details><summary>查看完整输入</summary><pre>{JSON.stringify(prepared.details.operation, null, 2)}</pre></details>
    </Confirm>}
    {result && <>
      <ArtifactLink item={result.artifact} onOpen={openArtifacts} />
      {result.workspace_path && <p className="advanced-path">{result.workspace_path}</p>}
      {result.workspace_error && <p className="advanced-error" role="alert">{result.workspace_error}</p>}
      {result.result?.formula_results?.map(cell => <p key={cell.cell}><code>{cell.cell}</code> = {cell.text}</p>)}
      {result.text && <pre className="advanced-result">{result.text}</pre>}
    </>}
    <Status busy={request.busy} error={request.error} />
  </section>
}
