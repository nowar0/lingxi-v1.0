import { MemoryV2Panel } from '@/components/memory/MemoryV2Panel'
import { useEffect, useMemo, useRef, useState } from 'react'
import { BookOpen, History, Link2, Pin, Plus, Search, ShieldCheck, Trash2, Undo2 } from 'lucide-react'
import { toast } from 'sonner'
import { useApp } from '@/context/AppContext'
import { lingxi, type MemoryPageResult, type MemorySearchResult } from '@/api/lingxi'
import { api } from '@/api/client'
import type { MemoryItem } from '@/types'
import { PageHeader } from '@/components/shared/PageHeader'
import { DataModeNotice } from '@/components/shared/DataModeNotice'
import { SearchInput } from '@/components/shared/SearchInput'
import { EmptyState } from '@/components/shared/EmptyState'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Input, Textarea } from '@/components/ui/input'
import { formatDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { MEMORY_PAGE_SIZE, deletedMemoryForOwner, lastMemoryPageOffset, loadMemoryHistory, memoryCategories, memoryCategoryNames, memoryOwnerKey, memorySearchNotice, type DeletedMemory, type MemoryHistory, type MemoryScope } from '@/lib/memoryBrowser'

const scopeInfo = {
  companion: { title: '陪伴', note: '', icon: BookOpen },
  work: { title: '工作', note: '', icon: Link2 },
  shared: { title: '共享', note: '', icon: ShieldCheck },
}

export function MemoryPage() {
  const { activeWorkSessionId } = useApp()
  return <MemoryV2Panel workSessionId={activeWorkSessionId} />
}

export function MemoryBrowser({ scope, onScopeChange }: { scope: MemoryScope; onScopeChange: (scope: MemoryScope) => void }) {
  const { memories, connection, activeWorkSessionId, refreshMemories } = useApp()
  const ownerKey = memoryOwnerKey(scope, activeWorkSessionId)
  const sessionId = scope === 'work' ? activeWorkSessionId : undefined
  const workUnavailable = scope === 'work' && sessionId === undefined
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const [query, setQuery] = useState('')
  const searchQuery = query.trim()
  const [offset, setOffset] = useState(0)
  const [reload, setReload] = useState(0)
  const requestKey = JSON.stringify([searchQuery, offset])
  const [remote, setRemote] = useState<{ key: string; page?: MemoryPageResult; search?: MemorySearchResult }>()
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [totals, setTotals] = useState<Partial<Record<MemoryScope, number>>>({ work: activeWorkSessionId === undefined ? 0 : undefined })
  const [selectedId, setSelectedId] = useState<MemoryItem['id']>()
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [category, setCategory] = useState('fact')
  const [editing, setEditing] = useState<MemoryItem>()
  const [busy, setBusy] = useState(false)
  const [history, setHistory] = useState<MemoryHistory>()
  const [historyOpen, setHistoryOpen] = useState(false)
  const [deleted, setDeleted] = useState<DeletedMemory>()
  const deletedItem = deletedMemoryForOwner(deleted, ownerKey)
  const run = async (task: () => Promise<void>) => {
    if (busy || connection !== 'connected') return
    setBusy(true)
    try { await task() } catch (error) { if (alive.current) toast.error('记忆操作未完成', { description: (error as Error).message }) }
    finally { if (alive.current) setBusy(false) }
  }

  useEffect(() => {
    if (connection !== 'connected') return
    let disposed = false
    // A cached first page is not a total; obtain counts from the backend.
    for (const itemScope of Object.keys(scopeInfo) as MemoryScope[]) {
      if (itemScope === 'work' && activeWorkSessionId === undefined) continue
      void lingxi.memories(itemScope, itemScope === 'work' ? activeWorkSessionId : undefined, { offset: 0, limit: 1 }).then((data) => {
        if (!disposed) setTotals((current) => ({ ...current, [itemScope]: data.total }))
      }).catch(() => { if (!disposed) setTotals((current) => ({ ...current, [itemScope]: undefined })) })
    }
    return () => { disposed = true }
  }, [connection, activeWorkSessionId, memories, reload])

  useEffect(() => {
    let disposed = false
    setLoadError('')
    if (connection !== 'connected' || workUnavailable) { setLoading(false); return }
    setLoading(true)
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          if (searchQuery) {
            const result = await lingxi.searchMemories(scope, searchQuery, sessionId)
            if (!disposed) setRemote({ key: requestKey, search: result })
          } else {
            const page = await lingxi.memories(scope, sessionId, { offset, limit: MEMORY_PAGE_SIZE })
            if (disposed) return
            // Deleting the final entry of the final page must not strand it.
            if (offset > 0 && offset >= page.total) { setOffset(lastMemoryPageOffset(page.total)); return }
            setRemote({ key: requestKey, page })
            setTotals((current) => ({ ...current, [scope]: page.total }))
          }
        } catch (error) {
          if (!disposed) { setRemote(undefined); setLoadError((error as Error).message) }
        } finally { if (!disposed) setLoading(false) }
      })()
    }, searchQuery ? 250 : 0)
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [scope, sessionId, searchQuery, offset, requestKey, connection, workUnavailable, memories, reload])

  const currentRemote = remote?.key === requestKey ? remote : undefined
  const cachedItems = useMemo(() => workUnavailable ? [] : memories.filter((item) => item.scope === scope && `${item.key}${item.value}`.toLowerCase().includes(searchQuery.toLowerCase())), [memories, scope, searchQuery, workUnavailable])
  const filtered = connection === 'connected' ? (currentRemote?.search?.items || currentRemote?.page?.items || []).filter((item) => item.scope === scope) : cachedItems.slice(offset, offset + MEMORY_PAGE_SIZE)
  const total = connection === 'connected' ? currentRemote?.page?.total : cachedItems.length
  const selected = (deletedItem?.id === selectedId ? deletedItem : undefined) || filtered.find((item) => item.id === selectedId) || filtered[0]
  const clearSelection = () => { setSelectedId(undefined); setDeleted(undefined); setHistory(undefined); setHistoryOpen(false) }
  const search = (next: string) => { setQuery(next.slice(0, 500)); setOffset(0); clearSelection() }
  const changePage = (next: number) => { setOffset(next); clearSelection() }
  const refresh = async () => {
    if (!alive.current) return
    setReload((current) => current + 1)
    // Refresh shared cache too, but do not label a successful mutation as failed
    // when the separate cache refresh fails.
    try { await refreshMemories() } catch (error) { if (alive.current) toast.error('记忆已更新，但缓存刷新失败', { description: (error as Error).message }) }
  }
  const add = async () => {
    if (!key.trim() || !value.trim()) return
    if (workUnavailable) throw new Error('请先选择工作会话')
    const payload = { scope, key, value, category, pinned: false, ...(scope === 'work' ? { session_id: sessionId } : {}) }
    if (editing) await lingxi.updateMemory(editing, { value, category })
    else await lingxi.createMemory(payload)
    if (!alive.current) return
    setKey(''); setValue(''); setEditing(undefined); setOpen(false); setDeleted(undefined)
    toast.success('记忆已保存')
    await refresh()
  }
  const togglePin = async (item: MemoryItem) => { await lingxi.updateMemory(item, { pinned: !item.pinned }); await refresh() }
  const remove = async (item: MemoryItem) => {
    await lingxi.deleteMemory(item)
    if (!alive.current) return
    setDeleted({ ownerKey, item: { ...item, status: 'deleted', revision: item.revision + 1 } })
    setSelectedId(item.id)
    toast.success('记忆已删除', { description: '历史版本仍保留，可以从详情中恢复。' })
    await refresh()
  }
  const restore = async (item: MemoryItem) => {
    // Deleted state is bound to this exact scope and work session.
    if (deleted?.ownerKey !== ownerKey || deleted.item.id !== item.id) return
    await lingxi.restoreMemory(item)
    if (!alive.current) return
    setDeleted(undefined); setSelectedId(item.id); toast.success('记忆已恢复')
    await refresh()
  }
  const showHistory = async (item: MemoryItem) => {
    const result = await loadMemoryHistory(api, item.id)
    if (!alive.current) return
    setHistory(result); setHistoryOpen(true)
  }
  const pageOffset = currentRemote?.page?.offset ?? offset
  const pageLimit = currentRemote?.page?.limit ?? MEMORY_PAGE_SIZE
  const searchResult = currentRemote?.search
  const searchNotice = searchResult ? memorySearchNotice(searchResult) : ''

  return <div className="page-container chrome-page personal-page customer-page memory-archive-page"><DataModeNotice />
    <PageHeader title="旧记录" actions={<Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button disabled={connection !== 'connected' || busy || workUnavailable} onClick={() => { setEditing(undefined); setKey(''); setValue(''); setCategory('fact') }}><Plus />添加记忆</Button></DialogTrigger>
      <DialogContent><DialogHeader><DialogTitle>{editing ? '编辑' : '添加一条'}{scopeInfo[scope].title}</DialogTitle><DialogDescription>{scopeInfo[scope].note}</DialogDescription></DialogHeader>
        <div className="space-y-3"><Input disabled={Boolean(editing)} maxLength={80} placeholder="简短标题" value={key} onChange={(event) => setKey(event.target.value)} /><Textarea maxLength={2000} placeholder="具体需要记住什么？" value={value} onChange={(event) => setValue(event.target.value)} />
          <Select aria-label="记忆类别" className="h-9 w-full rounded-lg border border-border bg-input px-3 text-sm" value={category} onChange={(event) => setCategory(event.target.value)}>{memoryCategories.map(([id, title]) => <option key={id} value={id}>{title}</option>)}</Select>
        </div><DialogFooter><Button disabled={busy || !key.trim() || !value.trim()} onClick={() => void run(add)}>保存记忆</Button></DialogFooter>
      </DialogContent>
    </Dialog>} />
    <div className="mb-4 grid gap-3 md:grid-cols-3">{(Object.entries(scopeInfo) as Array<[MemoryScope, typeof scopeInfo[MemoryScope]]>).map(([id, info]) => <button key={id} onClick={() => onScopeChange(id)} className={cn('memory-scope-card', scope === id && 'is-active')}><div className="flex items-center gap-2"><info.icon className="size-4" /><span>{info.title}</span><span className="ml-auto text-lg font-semibold">{connection === 'connected' ? totals[id] ?? '—' : memories.filter((item) => item.scope === id).length}</span></div>{info.note&&<p>{info.note}</p>}</button>)}</div>
    <Card className="memory-browser">
      <div className="memory-toolbar"><SearchInput className="ml-auto w-64" placeholder="关键词 / 语义检索" value={query} onChange={(event) => search(event.target.value)} /></div>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3 text-xs text-muted-foreground" aria-live="polite">
        <span>{workUnavailable ? '请先在工作台选择工作会话。' : loading ? '正在读取记忆…' : searchQuery ? searchResult ? `${searchResult.total === undefined ? `已显示 ${filtered.length} 条匹配` : `共 ${searchResult.total} 条匹配，已显示 ${filtered.length} 条`}${searchResult.total_records === undefined ? '' : `；检索范围共 ${searchResult.total_records} 条记忆`}（按相关度返回）` : `当前显示 ${filtered.length} 条匹配` : total === undefined ? '总数暂不可用' : `${connection === 'connected' ? '共' : '缓存共'} ${total} 条${total ? ` · 第 ${pageOffset + 1}–${Math.min(pageOffset + pageLimit, total)} 条` : ''}`}</span>
        {!searchQuery && <div className="flex gap-2"><Button variant="outline" size="sm" disabled={loading || busy || pageOffset === 0 || workUnavailable} onClick={() => changePage(Math.max(0, pageOffset - pageLimit))}>上一页</Button><Button variant="outline" size="sm" disabled={loading || busy || total === undefined || pageOffset + pageLimit >= total || workUnavailable} onClick={() => changePage(pageOffset + pageLimit)}>下一页</Button></div>}
      </div>
      {searchNotice && <p role="status" className={cn('border-b border-border px-4 py-3 text-xs leading-5', searchResult?.semantic_status === 'complete' ? 'text-muted-foreground' : 'text-amber-600 dark:text-amber-400')}>{searchNotice}</p>}
      {loadError && <div role="alert" className="flex items-center justify-between gap-3 border-b border-border px-4 py-3 text-xs text-destructive"><span>记忆读取失败：{loadError}</span><Button variant="outline" size="sm" onClick={() => setReload((current) => current + 1)}>重试</Button></div>}
      <div className="memory-content"><div className="memory-list">{filtered.map((item) => <button key={item.id} className={cn('memory-list-item', selected?.id === item.id && 'is-active')} onClick={() => setSelectedId(item.id)}><div className="flex items-center gap-2">{item.pinned && <Pin className="size-3 fill-primary/20 text-primary" />}<span className="truncate text-xs font-semibold">{item.key}</span><Badge variant="secondary" className="ml-auto">{memoryCategoryNames[item.category] || item.category}</Badge></div><p>{item.value}</p><div className="mt-2 flex items-center justify-between text-[9px] text-muted-foreground"><span>{item.source || '手动添加'}</span><span>{formatDate(item.updated_at)}</span></div></button>)}{!filtered.length && <EmptyState icon={Search} title={loading ? '正在读取记忆' : loadError ? '记忆暂不可用' : '没有找到记忆'} description={workUnavailable ? '请先选择工作会话。' : loading ? '请稍候。' : loadError ? '请重试，不会将读取失败显示为完整空列表。' : '调整检索词，或手动添加一条新记忆。'} />}</div>
        <div className="memory-detail">{selected ? <>
          <div className="flex items-start justify-between gap-4"><div><Badge variant="outline">{memoryCategoryNames[selected.category] || selected.category}</Badge><h2 className="mt-3 text-lg font-semibold">{selected.key}</h2></div><div className="flex gap-1">{selected.status !== 'deleted' && <><Button variant="ghost" size="icon" disabled={busy || connection !== 'connected'} aria-label="切换记忆置顶" onClick={() => void run(() => togglePin(selected))}><Pin className={selected.pinned ? 'fill-primary/20 text-primary' : ''} /></Button><Button variant="ghost" size="icon" className="hover:text-red-500" disabled={busy || connection !== 'connected'} aria-label="删除记忆" onClick={() => void run(() => remove(selected))}><Trash2 /></Button></>}</div></div>
          {selected.status === 'deleted' ? <div className="mt-4 flex items-center gap-2"><Badge variant="danger">已删除</Badge><Button variant="outline" size="sm" disabled={busy || connection !== 'connected'} onClick={() => void run(() => restore(selected))}><Undo2 />恢复记忆</Button></div> : <Button className="mt-4" variant="outline" size="sm" disabled={busy || connection !== 'connected'} onClick={() => { setEditing(selected); setKey(selected.key); setValue(selected.value); setCategory(selected.category); setOpen(true) }}>编辑记忆</Button>}
          <p className="mt-5 whitespace-pre-wrap text-sm leading-7">{selected.value}</p><div className="mt-8 grid grid-cols-2 gap-3"><div className="memory-meta"><span>记忆范围</span><strong>{scopeInfo[selected.scope].title}</strong></div><div className="memory-meta"><span>修订版本</span><strong>rev {selected.revision}</strong></div><div className="memory-meta"><span>来源</span><strong>{selected.source || '手动添加'}</strong></div><div className="memory-meta"><span>最近更新</span><strong>{formatDate(selected.updated_at)}</strong></div></div>
          <div className="mt-6 flex gap-2"><Button variant="outline" size="sm" disabled={busy || connection !== 'connected'} onClick={() => void run(() => showHistory(selected))}><History />来源与历史版本</Button></div>
        </> : <EmptyState icon={BookOpen} title="选择一条记忆" description="在左侧查看记忆内容与来源。" />}</div>
      </div>
    </Card>
    <Dialog open={historyOpen} onOpenChange={setHistoryOpen}><DialogContent className="max-h-[80vh] max-w-2xl overflow-y-auto"><DialogHeader><DialogTitle>来源与历史版本</DialogTitle><DialogDescription>来源与版本分别读取；来源不可用时仍可查看历史版本。</DialogDescription></DialogHeader><div className="space-y-4 text-xs"><h3 className="font-semibold">来源原文</h3>{history?.source.messages?.length ? history.source.messages.map((message) => <div key={message.id} className="rounded-lg bg-secondary/40 p-3 whitespace-pre-wrap">{message.role === 'user' ? '你' : '助手'}：{message.content}</div>) : <p className="text-muted-foreground">{history?.source.note || '该记忆没有可显示的聊天来源。'}</p>}<h3 className="font-semibold">历史版本</h3>{history?.versionsNote && <p role="alert" className="text-destructive">{history.versionsNote}</p>}{history?.versions.map((version, index) => <div key={index} className="rounded-lg border border-border p-3"><div className="mb-2 text-muted-foreground">版本 {version.revision || version.id || index + 1} · {version.status}</div><p className="whitespace-pre-wrap">{version.value}</p></div>)}{history && !history.versionsNote && !history.versions.length && <p className="text-muted-foreground">暂无历史版本。</p>}</div></DialogContent></Dialog>
  </div>
}
