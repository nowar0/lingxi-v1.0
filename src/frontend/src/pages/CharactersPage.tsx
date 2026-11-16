import {MappingEditor} from '@/components/shared/MappingEditor'
import { useEffect, useRef, useState } from 'react'
import { Archive, Box, Check, Download, FileBox, FileJson, Image, LoaderCircle, MoreHorizontal, PackageOpen, Plus, Sparkles, Upload, UserRound, Volume2 } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { lingxi } from '@/api/lingxi'
import { api, json } from '@/api/client'
import { downloadBlob, fileToBase64 } from '@/lib/utils'
import type { Character } from '@/types'
import { PageHeader } from '@/components/shared/PageHeader'
import { DataModeNotice } from '@/components/shared/DataModeNotice'
import { CharacterAvatar } from '@/components/shared/CharacterAvatar'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Input, Textarea } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'

interface ImportPreview { warnings?: string[]; import_id: string; card: Character; included?: string[]; chat_count?: number; preview_assets?: Record<string, string>; conflicts?: Record<string, Array<{ key: string; current: string | null; incoming: string }>> }

export function CharactersPage({ embedded = false }: { embedded?: boolean }) {
  const { characters, activeCharacter, activeSessionId, switchCharacter, connection, refreshCharacters } = useApp()
  const [localCharacters, setLocalCharacters] = useState<Character[]>([])
  const [selectedId, setSelectedId] = useState(activeCharacter.id)
  const [search,setSearch]=useState('')
  const [detailOpen,setDetailOpen]=useState(false)
  const detailRef=useRef<HTMLDivElement>(null)
  useEffect(()=>{if(!detailOpen)return;const key=(e:KeyboardEvent)=>{if(e.key==='Escape'&&!document.querySelector('[role=dialog][data-state=open]'))setDetailOpen(false)};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key)},[detailOpen])
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [persona, setPersona] = useState('')
  const [greeting, setGreeting] = useState('')
  const [importOpen, setImportOpen] = useState(false)
  const [importing, setImporting] = useState(false)
  const [preview, setPreview] = useState<ImportPreview>()
  const [importFile, setImportFile] = useState<File>()
  const [includeCompanion, setIncludeCompanion] = useState(true)
  const [includeShared, setIncludeShared] = useState(false)
  const [includeChat, setIncludeChat] = useState(false)
  const [importCompanion, setImportCompanion] = useState(false)
  const [importShared, setImportShared] = useState(false)
  const [importChat, setImportChat] = useState(false)
  const [conflictChoices, setConflictChoices] = useState<Record<string, boolean>>({})
  const fileInput = useRef<HTMLInputElement>(null)
  const assetInput = useRef<HTMLInputElement>(null)
  const assetTarget = useRef<{ character: Character; kind: 'avatar' | 'art' } | undefined>(undefined)
  const [editCard, setEditCard] = useState<Character>()
  const [assetOpen, setAssetOpen] = useState(false)
  const [mappingOpen, setMappingOpen] = useState(false)
  const [expressions, setExpressions] = useState('{}')
  const [motions, setMotions] = useState('{}')
  const [voiceRef, setVoiceRef] = useState('')
  const [busy, setBusy] = useState(false)
  const run = async (task: () => Promise<void>) => {
    if (busy || connection !== 'connected') return
    setBusy(true)
    try { await task() } catch (error) { toast.error('人物操作未完成', { description: (error as Error).message }) }
    finally { setBusy(false) }
  }
  const allCharacters = [...localCharacters, ...characters]
  const selected = allCharacters.find((item) => item.id === selectedId) || activeCharacter

  const create = async () => {
    if (!name.trim() || !persona.trim()) return
    const payload = { name, description, persona, greeting, tags: ['自定义角色'], expressions: {}, motions: {}, voice_ref: '', theme_ref: '' }
    if (connection === 'demo') {
      const character: Character = { ...payload, id: `demo-${Date.now()}`, revision: 1, avatar: './assets/lingxi-avatar.png', art: './assets/lingxi-stage.png' }
      setLocalCharacters((items) => [character, ...items]); setSelectedId(character.id)
    } else { const character = await lingxi.createCharacter(payload); await refreshCharacters(); setSelectedId(character.id) }
    setCreateOpen(false); setName(''); setDescription(''); setPersona(''); setGreeting(''); toast.success('人物卡已创建')
  }
  const chooseImport = async (file?: File) => {
    if (!file) return
    if (connection !== 'connected') { toast.error('请先连接本地服务'); return }
    if (file.size > 50_000_000) { toast.error('卡包不能超过 50 MB'); return }
    setImportFile(file); setImportOpen(true); setImporting(true); setPreview(undefined)
    setImportCompanion(false); setImportShared(false); setImportChat(false); setConflictChoices({})
    try {
      if (connection === 'connected') setPreview(await lingxi.previewCardImport(await fileToBase64(file)))
      else {
        await new Promise((resolve) => setTimeout(resolve, 650))
        setPreview({ import_id: 'demo-preview', card: { ...activeCharacter, id: 'imported-preview', name: file.name.replace(/\.(zip|json)$/i, '').slice(0, 30) || '导入的人物', description: '从卡包清单读取的演示预览', revision: 1 }, included: [], chat_count: 0 })
      }
    } catch (error) { toast.error('卡包读取失败', { description: (error as Error).message }); setImportOpen(false) } finally { setImporting(false) }
  }
  const commitImport = async () => {
    if (!preview) return
    setImporting(true)
    try {
      if (connection === 'connected') {
        const fields: Record<string, unknown> = { include_companion: importCompanion, include_shared: importShared, include_chat: importChat, companion_events: false, shared_events: false }
        for (const scope of ['companion', 'shared']) {
          const entries = preview.conflicts?.[scope] || []
          fields[`${scope}_choices`] = Object.fromEntries(entries.map((item) => [item.key, item.current === null || conflictChoices[`${scope}:${item.key}`] ? 'import' : 'skip']))
          fields[`${scope}_expected`] = Object.fromEntries(entries.map((item) => [item.key, item.current]))
        }
        await lingxi.commitCardImport(preview.import_id, fields); await refreshCharacters()
      }
      else setLocalCharacters((items) => [{ ...preview.card, id: `imported-${Date.now()}`, avatar: './assets/lingxi-avatar.png', art: './assets/lingxi-stage.png' }, ...items])
      toast.success(`已导入「${preview.card.name}」`, { description: '角色资料与资源已作为一个完整卡包处理。' }); setImportOpen(false)
    } catch (error) { toast.error('导入提交失败', { description: (error as Error).message }) } finally { setImporting(false) }
  }
  const exportPackage = async () => {
    try {
      if (includeChat && !activeSessionId) throw new Error('请先选择要导出的陪伴会话')
      if (connection === 'connected') downloadBlob(await lingxi.exportCharacter(selected.id, includeCompanion, includeShared, includeChat, activeSessionId), `${selected.name}.lingxi-card.zip`)
      else downloadBlob(new Blob([JSON.stringify({ format: 'lingxi-card-demo', card: selected, included: { companion: includeCompanion, shared: includeShared, chat: includeChat } }, null, 2)], { type: 'application/json' }), `${selected.name}-演示卡包.json`)
      toast.success(connection === 'connected' ? '卡包已导出' : '已导出演示卡包', { description: connection === 'demo' ? '演示导出为 JSON；真实 ZIP 导出需连接本地服务。' : '静态资源与所选数据已打包。' })
    } catch (error) { toast.error('导出失败', { description: (error as Error).message }) }
  }
  return <div className={embedded ? undefined : "page-container chrome-page"}>{!embedded && <DataModeNotice />}<PageHeader headingLevel={embedded ? "h2" : "h1"} eyebrow={embedded ? undefined : "CHARACTERS"} title="人物与卡包" description="人物资料、立绘、表情、场景与主题可以独立导入，也可以组合成完整卡包。" actions={<><input ref={fileInput} type="file" accept=".zip,.json" className="hidden" onChange={(event) => { void chooseImport(event.target.files?.[0]); event.target.value = '' }} /><Button variant="outline" onClick={() => fileInput.current?.click()}><PackageOpen />一键导入卡包</Button><Dialog open={createOpen} onOpenChange={setCreateOpen}><DialogTrigger asChild><Button><Plus />新建人物</Button></DialogTrigger><DialogContent className="max-w-xl"><DialogHeader><DialogTitle>创建人物卡</DialogTitle><DialogDescription>先建立角色资料，之后可在素材页继续添加头像、立绘、表情和声音引用。</DialogDescription></DialogHeader><div className="grid gap-3"><div className="grid grid-cols-2 gap-3"><label className="ui-form-field"><span>角色名称 *</span><Input placeholder="角色名称 *" value={name} onChange={(event) => setName(event.target.value)} /></label><label className="ui-form-field"><span>一句话简介</span><Input placeholder="一句话简介" value={description} onChange={(event) => setDescription(event.target.value)} /></label></div><Textarea placeholder="人设与表达边界 *" value={persona} onChange={(event) => setPersona(event.target.value)} /><Textarea className="min-h-20" placeholder="开场白" value={greeting} onChange={(event) => setGreeting(event.target.value)} /></div><DialogFooter><Button disabled={!name.trim() || !persona.trim()} onClick={() => void run(create)}>创建人物卡</Button></DialogFooter></DialogContent></Dialog></>} />
    <Input className="character-search" aria-label="搜索人物" placeholder="搜索人物名称与简介" value={search} onChange={e=>setSearch(e.target.value)}/><div className="character-layout"><section><div className="character-grid">{allCharacters.filter(character=>`${character.name} ${character.description}`.toLowerCase().includes(search.toLowerCase())).map((character) => <button key={character.id} className={cn('character-card', selected.id === character.id && 'is-selected')} onClick={() => {setSelectedId(character.id);setDetailOpen(true)}}><div className="character-card-art"><img src={character.art || './assets/lingxi-stage.png'} alt={character.name} /><div className="absolute left-3 top-3 flex gap-1">{character.id === activeCharacter.id && <Badge className="bg-white/70 backdrop-blur-md"><Check className="size-3" />当前角色</Badge>}</div></div><div className="p-4"><div className="flex items-center gap-2"><CharacterAvatar src={character.avatar} name={character.name} className="size-8" /><div className="min-w-0 text-left"><h3 className="truncate text-sm font-semibold">{character.name}</h3><p className="text-xs text-muted-foreground">rev {character.revision}</p></div><MoreHorizontal className="ml-auto size-4 text-muted-foreground" /></div><p className="mt-3 line-clamp-2 min-h-8 text-left text-xs leading-4 text-muted-foreground">{character.description}</p><div className="mt-3 flex flex-wrap gap-1">{character.tags?.slice(0, 2).map((tag) => <Badge variant="secondary" key={tag}>{tag}</Badge>)}</div></div></button>)}</div></section>
      {detailOpen && <><button className="character-detail-scrim" aria-label="关闭人物详情" onClick={()=>setDetailOpen(false)}/><Card ref={detailRef} className="character-detail"><Button className="character-detail-close" variant="ghost" size="sm" onClick={()=>setDetailOpen(false)}>返回人物库</Button><div className="border-b border-border/60 p-5"><div className="flex items-center gap-3"><CharacterAvatar src={selected.avatar} name={selected.name} className="size-11 rounded-2xl" /><div><h2 className="text-base font-semibold">{selected.name}</h2><p className="mt-1 text-xs text-muted-foreground">人物卡 ID · {selected.id}</p></div>{selected.id !== activeCharacter.id && <Button size="sm" className="ml-auto" onClick={() => void run(() => switchCharacter(selected.id))}>切换为此角色</Button>}</div></div><Tabs defaultValue="profile" className="p-5"><TabsList className="grid w-full grid-cols-3"><TabsTrigger value="profile">角色资料</TabsTrigger><TabsTrigger value="assets">素材映射</TabsTrigger><TabsTrigger value="package">卡包制作</TabsTrigger></TabsList>
        <TabsContent value="profile"><div className="space-y-4 text-xs"><div><label className="detail-label">简介</label><p className="leading-5 text-muted-foreground">{selected.description || '暂无简介'}</p></div><div><label className="detail-label">人设</label><p className="max-h-36 overflow-auto whitespace-pre-wrap leading-5 text-muted-foreground">{selected.persona}</p></div><div><label className="detail-label">开场白</label><p className="leading-5 text-muted-foreground">{selected.greeting || '暂无开场白'}</p></div><Button variant="outline" size="sm" className="w-full" disabled={busy || connection !== 'connected'} onClick={() => setEditCard({ ...selected })}>编辑角色资料</Button></div></TabsContent>
        <TabsContent value="assets"><div className="grid grid-cols-2 gap-3">{[{ icon: Image, label: '头像与立绘', note: 'PNG / JPEG / WebP' }, { icon: Sparkles, label: '表情与动作', note: '未知标签回退默认' }, { icon: Box, label: '人物表现', note: selected.id === 'lingxi' ? '内置正面眨眼与口型' : '静态立绘回退' }, { icon: Volume2, label: '声音引用', note: '需配置语音服务' }].map((item) => <button key={item.label} className="asset-drop" disabled={busy || connection !== 'connected'} onClick={() => { if (item.label === '头像与立绘') setAssetOpen(true); else { setVoiceRef(selected.voice_ref || ''); setExpressions(JSON.stringify(selected.expressions || {}, null, 2)); setMotions(JSON.stringify(selected.motions || {}, null, 2)); setMappingOpen(true) } }}><item.icon /><span>{item.label}</span><small>{item.note}</small></button>)}</div><div className="mt-4 rounded-xl bg-secondary/45 p-3 text-xs leading-5 text-muted-foreground">头像与立绘可真实上传；声音使用已配置的音色编号。默认零汐沿用已有正面 Live2D 演示，其他人物保留静态回退。</div></TabsContent>
        <TabsContent value="package"><div className="space-y-3"><div className="package-option"><div><FileBox /><span>角色资料与静态资源</span></div><Badge variant="success">始终包含</Badge></div><div className="package-option"><div><BookOpenIcon /><span>陪伴记忆</span></div><Switch checked={includeCompanion} onCheckedChange={setIncludeCompanion} /></div><div className="package-option"><div><UserRound /><span>共享画像</span></div><Switch checked={includeShared} onCheckedChange={setIncludeShared} /></div><div className="package-option"><div><FileJson /><span>聊天记录</span></div><Switch checked={includeChat} onCheckedChange={setIncludeChat} /></div><Button className="mt-2 w-full" onClick={() => void exportPackage()}><Download />预览并导出卡包</Button><p className="text-center text-xs leading-4 text-muted-foreground">模型 Key 永不进入卡包。主题与角色包可分别独立使用。</p></div></TabsContent>
      </Tabs></Card></>}</div>
    <Dialog open={importOpen} onOpenChange={setImportOpen}><DialogContent><DialogHeader><DialogTitle>导入人物卡包</DialogTitle><DialogDescription>{importFile?.name || '正在读取卡包'} · 自动校验清单、资源与冲突</DialogDescription></DialogHeader>{importing && !preview ? <div className="flex min-h-44 flex-col items-center justify-center text-sm text-muted-foreground"><LoaderCircle className="mb-3 size-6 animate-spin text-primary" />正在读取并校验卡包…</div> : preview ? <><div className="flex items-center gap-4 rounded-xl border border-border/70 bg-secondary/35 p-4"><CharacterAvatar src={preview.preview_assets?.avatar || preview.card.avatar || './assets/lingxi-avatar.png'} name={preview.card.name} className="size-14 rounded-2xl" /><div><h3 className="text-sm font-semibold">{preview.card.name}</h3><p className="mt-1 text-xs text-muted-foreground">{preview.card.description}</p><div className="mt-2 flex gap-1"><Badge variant="success">清单有效</Badge><Badge variant="secondary">{preview.chat_count || 0} 个聊天会话</Badge></div></div></div>{Boolean(preview.warnings?.length) && <div className="rounded-xl border border-border/70 bg-secondary/35 p-3 text-xs leading-6" role="status">{preview.warnings!.map(warning => <p key={warning}>{warning}</p>)}</div>}<div className="space-y-3 text-xs">
  <div className="package-option"><span>导入陪伴记忆</span><Switch aria-label="导入陪伴记忆" checked={importCompanion} onCheckedChange={setImportCompanion} disabled={!preview.included?.includes('companion-memory.json')} /></div>
  <div className="package-option"><span>导入共享画像</span><Switch aria-label="导入共享画像" checked={importShared} onCheckedChange={setImportShared} disabled={!preview.included?.includes('shared-profile.json')} /></div>
  <div className="package-option"><span>导入聊天记录</span><Switch aria-label="导入聊天记录" checked={importChat} onCheckedChange={setImportChat} disabled={!preview.included?.includes('chat-backup.json')} /></div>
  {(['companion', 'shared'] as const).filter((scope) => scope === 'companion' ? importCompanion : importShared).flatMap((scope) => (preview.conflicts?.[scope] || []).filter((item) => item.current !== null).map((item) => <label className="block rounded-lg border border-border p-3" key={`${scope}:${item.key}`}><span className="flex items-center gap-2"><input type="checkbox" checked={Boolean(conflictChoices[`${scope}:${item.key}`])} onChange={(event) => setConflictChoices((current) => ({ ...current, [`${scope}:${item.key}`]: event.target.checked }))} />覆盖「{item.key}」</span><span className="mt-2 block whitespace-pre-wrap text-muted-foreground">当前：{item.current}<br />导入：{item.incoming}</span></label>))}
</div><div className="rounded-xl border border-border/60 p-3 text-xs leading-6 text-muted-foreground">角色资料与资源始终导入。聊天、记忆和画像只按本次勾选处理；未勾选的冲突项保留当前值。</div><DialogFooter><Button variant="outline" onClick={() => setImportOpen(false)}>取消</Button><Button disabled={importing} onClick={() => void commitImport()}>{importing ? <LoaderCircle className="animate-spin" /> : <Upload />}确认导入</Button></DialogFooter></> : null}</DialogContent></Dialog>
    <input ref={assetInput} type="file" className="hidden" aria-label="人物图片文件" accept="image/png,image/jpeg,image/webp" onChange={(event) => { const file = event.target.files?.[0]; const target = assetTarget.current; event.target.value = ''; if (file && target) void run(async () => { if (file.size > 8000000) throw new Error('人物图片最大 8 MB'); await api(`/v2/characters/${target.character.id}/assets`, json('POST', { kind: target.kind, expected_revision: target.character.revision, data: await fileToBase64(file) })); await refreshCharacters(); toast.success('人物图片已保存') }) }} />
    <Dialog open={assetOpen} onOpenChange={setAssetOpen}><DialogContent><DialogHeader><DialogTitle>头像与立绘</DialogTitle><DialogDescription>PNG / JPEG / WebP，每张最大 8 MB。</DialogDescription></DialogHeader><div className="flex gap-3">{(['avatar', 'art'] as const).map((kind) => <Button key={kind} variant="outline" disabled={busy} onClick={() => { assetTarget.current = { character: selected, kind }; assetInput.current?.click() }}>上传{kind === 'avatar' ? '头像' : '立绘'}</Button>)}</div></DialogContent></Dialog>
    <Dialog open={Boolean(editCard)} onOpenChange={(open) => { if (!open && !busy) setEditCard(undefined) }}><DialogContent><DialogHeader><DialogTitle>编辑人物资料</DialogTitle></DialogHeader>{editCard && <div className="space-y-3"><label className="ui-form-field"><span>编辑人物名称</span><Input aria-label="编辑人物名称" maxLength={80} value={editCard.name} onChange={(event) => setEditCard({ ...editCard, name: event.target.value })} /></label><Textarea aria-label="编辑人物简介" maxLength={300} value={editCard.description} onChange={(event) => setEditCard({ ...editCard, description: event.target.value })} /><Textarea aria-label="编辑人物人设" maxLength={5000} value={editCard.persona} onChange={(event) => setEditCard({ ...editCard, persona: event.target.value })} /><Textarea aria-label="编辑人物开场白" maxLength={500} value={editCard.greeting} onChange={(event) => setEditCard({ ...editCard, greeting: event.target.value })} /></div>}<DialogFooter><Button disabled={busy || !editCard?.name.trim() || !editCard.persona.trim()} onClick={() => void run(async () => { if (!editCard) return; await lingxi.updateCharacter(editCard, { name: editCard.name, persona: editCard.persona, description: editCard.description, greeting: editCard.greeting, tags: editCard.tags }); await refreshCharacters(); setEditCard(undefined); toast.success('人物资料已保存') })}>保存人物资料</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={mappingOpen} onOpenChange={(open) => { if (!busy) setMappingOpen(open) }}><DialogContent><DialogHeader><DialogTitle>表现与声音映射</DialogTitle><DialogDescription>仅保存表现标签映射与音色编号。零汐正面模型的转头功能仍未启用。</DialogDescription></DialogHeader><label className="space-y-1 text-xs"><span>音色编号（留空使用语音模块默认值）</span><Input aria-label="人物音色编号" maxLength={200} placeholder="例如 plana-ja" value={voiceRef} onChange={(event) => setVoiceRef(event.target.value)} /></label><label className="space-y-1 text-xs"><span>表情映射 JSON</span><MappingEditor label="表情" value={expressions} onChange={setExpressions}/></label><label className="space-y-1 text-xs"><span>动作映射 JSON</span><MappingEditor label="动作" value={motions} onChange={setMotions}/></label><DialogFooter><Button disabled={busy} onClick={() => void run(async () => { await lingxi.updateCharacter(selected, { name: selected.name, persona: selected.persona, description: selected.description, greeting: selected.greeting, tags: selected.tags, expressions: JSON.parse(expressions), motions: JSON.parse(motions), voice_ref: voiceRef }); await refreshCharacters(); setMappingOpen(false); toast.success('表现与声音引用已保存') })}>保存映射</Button></DialogFooter></DialogContent></Dialog>
  </div>
}

function BookOpenIcon() { return <Archive /> }
