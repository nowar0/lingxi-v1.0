import { recoveryStorage } from '@/lib/recoveryStorage'
import { useCallback, useEffect, useReducer, useRef } from 'react'
import { toast } from 'sonner'
import { evolution } from '@/lib/evolution'
import { lingxi, type SessionSnapshot } from '@/api/lingxi'
import { acceptsSnapshot } from '@/lib/snapshotOrder'
import { ApiError, apiBlob } from '@/api/client'
import { downloadBlob } from '@/lib/utils'
import type { Character, Message, Mode } from '@/types'

export type DraftConflict = { content: string; revision: number }

type Pending = { stale?: boolean; version?: number; id: string; content: string; draft?: string; character?: Character; accepted?: boolean }
type Entry = {
  draft: string; revision: number; edits: number; loaded: boolean; saving: boolean
  conflict?: DraftConflict; conflictBlocked?: boolean
  dirty: boolean; messages: Message[]; batches:SessionSnapshot['batches']; eventRevision:number; version: number; serial: number
  generating: boolean; sending: boolean; pending?: Pending; timer?: number; expanded?:boolean; hasMore?:boolean; oldest?:number; loadingMore?:boolean
}

// A session owns its draft, outbox and snapshots, including while another session is visible.
export function useConversation(mode: Mode, connected: boolean, id?: number) {
  const entries = useRef(new Map<number, Entry>())
  const active = useRef(id)
  active.current = id
  const [, redraw] = useReducer((n) => n + 1, 0)
  const mounted = useRef(true)
  const notify = useCallback(() => { if (mounted.current) {for(const [sid,value] of entries.current){if(!value.dirty)continue;try{const key=`lingxi-draft:${mode}:${sid}`,text=JSON.stringify({draft:value.draft,revision:value.revision});if(recoveryStorage.getItem(key)!==text)recoveryStorage.setItem(key,text)}catch{toast.error('草稿恢复内容未能保存，请复制当前输入',{id:'lingxi-draft-recovery'})}}redraw()} }, [mode])
  const entry = useCallback((sid = 0) => {
    let value = entries.current.get(sid)
    if (!value) {
      let pending: Pending | undefined
      let recovered: {draft:string;revision:number}|undefined
      try {recovered=JSON.parse(recoveryStorage.getItem(`lingxi-draft:${mode}:${sid}`)||'null')}catch{}
      try { pending = JSON.parse(recoveryStorage.getItem(`lingxi-outbox:${mode}:${sid}`) || 'null') || undefined } catch { /* unusable storage is optional */ }
      value = { draft: recovered?.draft||'', revision: recovered?.revision||0, edits: 0, loaded: false, saving: false, dirty: !!recovered,
        messages: [], batches:[], eventRevision:-1, version: -1, serial: 0, generating: false, sending: false, pending }
      entries.current.set(sid, value)
    }
    return value
  }, [mode])
  const remember = useCallback((sid: number, pending?: Pending) => {
    entry(sid).pending = pending
    try {
      if (pending) recoveryStorage.setItem(`lingxi-outbox:${mode}:${sid}`, JSON.stringify(pending))
      else recoveryStorage.removeItem(`lingxi-outbox:${mode}:${sid}`)
    } catch { /* server persistence remains authoritative */ }
  }, [entry, mode])
  const apply = useCallback((sid: number, snapshot: SessionSnapshot, serial?: number) => {
    const value = entry(sid)
    if (snapshot.session_id !== sid || snapshot.mode !== mode || !acceptsSnapshot(value.version,value.eventRevision,snapshot.session_version,snapshot.event_revision)) return
    if (serial !== undefined && serial !== value.serial) return
    if ((value.version >= 0 && snapshot.session_version > value.version) ||
        (value.pending?.version !== undefined && snapshot.session_version > value.pending.version)) {
      if (value.pending) remember(sid, { ...value.pending, stale: true })
    }
    value.serial++
    const previousVersion=value.version
    value.version = snapshot.session_version
    value.eventRevision = snapshot.event_revision ?? value.eventRevision
    const sameTimeline=previousVersion===snapshot.session_version
    const expanded=value.expanded&&sameTimeline
    value.messages = expanded ? [...value.messages.filter(m=>!snapshot.messages.some(n=>n.id===m.id)),...snapshot.messages] : snapshot.messages
    value.batches = expanded ? [...value.batches.filter(b=>!snapshot.batches.some(n=>n.id===b.id)),...snapshot.batches] : snapshot.batches
    if(!expanded){value.hasMore=!!snapshot.has_more;value.oldest=snapshot.oldest_id;value.expanded=false}
    value.generating = snapshot.batches.some((batch) => batch.status === 'generating')
    notify()
  }, [entry, mode, notify, remember])
  const refresh = useCallback(async (sid: number) => {
    const serial = entry(sid).serial
    apply(sid, await lingxi.snapshot(sid, mode), serial)
  }, [apply, entry, mode])
  const save = useCallback(async (sid: number) => {
    const value = entry(sid)
    if (!connected || !value.loaded || !value.dirty || value.saving || value.conflictBlocked) return
    value.saving = true
    try {
      while (value.dirty && !value.conflictBlocked) {
        const edits = value.edits
        const result = await lingxi.saveDraft(sid, value.draft, value.revision)
        value.revision = result.revision
        value.dirty = value.edits !== edits
        if(!value.dirty)recoveryStorage.removeItem(`lingxi-draft:${mode}:${sid}`)
      }
    } catch (error) {
      // Do not replay an outdated draft over another window's text.
      if (error instanceof ApiError && error.status === 409) {
        value.conflictBlocked = true
        value.conflict = await lingxi.draft(sid).catch(() => value.conflict)
        toast.error('草稿在其他窗口已更新', { description: '本地内容已保留；比较并选择后才会继续保存。' })
      } else toast.error('草稿尚未保存', { description: '当前输入仍保留，恢复连接后编辑可再次保存。' })
    } finally { value.saving = false; notify() }
  }, [connected, entry, notify])
  const setDraft = useCallback((text: string) => {
    const sid = active.current
    const value = entry(sid)
    value.draft = text; value.edits++; value.dirty = true
    try{recoveryStorage.setItem(`lingxi-draft:${mode}:${sid||0}`,JSON.stringify({draft:text,revision:value.revision}))}catch{toast.error('草稿恢复内容未能保存，请先复制当前输入')}
    if (value.timer) window.clearTimeout(value.timer)
    if (sid) value.timer = window.setTimeout(() => { void save(sid) }, 650)
    notify()
  }, [entry, notify, save])
  const adopt = useCallback((sid: number) => {
    const initial = entry()
    const value = entry(sid)
    if (initial.dirty) {
      value.draft = initial.draft; value.dirty = true; value.edits++
      initial.draft = ''; initial.dirty = false
      recoveryStorage.setItem(`lingxi-draft:${mode}:${sid}`,JSON.stringify({draft:value.draft,revision:value.revision}));recoveryStorage.removeItem(`lingxi-draft:${mode}:0`)
    }
    return sid
  }, [entry])
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      for (const [sid, value] of entries.current) {
        if (value.timer) window.clearTimeout(value.timer)
        if (sid) void save(sid)
      }
    }
  }, [save])
  useEffect(() => {
    if (!connected || !id) return
    let disposed = false
    const value = entry(id)
    const edits = value.edits
    void lingxi.draft(id).then((draft) => {
      if (disposed) return
      if (!value.dirty && value.edits === edits) value.draft = draft.content
      if (value.dirty && draft.content !== value.draft && draft.revision !== value.revision) {
        value.conflict = draft; value.conflictBlocked = true
      } else if (!value.conflictBlocked) value.revision = draft.revision
      value.loaded = true
      notify()
      if (value.dirty) void save(id)
    }).catch((error) => { if (!disposed) toast.error('草稿加载失败', { description: (error as Error).message }) })
    void refresh(id).catch((error) => { if (!disposed) toast.error('会话加载失败', { description: (error as Error).message }) })
    const source = new EventSource(lingxi.eventsUrl(id, mode))
    source.onmessage = (event) => {
      if (disposed) return
      try { apply(id, JSON.parse(event.data)) } catch { /* Ignore an incomplete event, then reconnect/read next snapshot. */ }
    }
    return () => { disposed = true; source.close(); void save(id) }
  }, [apply, connected, entry, id, mode, notify, refresh, save])
  const transmit = useCallback(async (sid: number, pending: Pending) => {
    const value = entry(sid)
    if (value.sending) throw new Error('上一条输入正在确认，请稍候')
    if (pending.stale) throw new Error('会话已回退或编辑，请先取消旧输入再修改发送')
    value.sending = true; remember(sid, pending); notify()
    try {
      if (/^\s*\/[a-z]+(?:\s|$)/.test(pending.content)) {
        const result=await evolution.command(sid,pending.content,pending.id)
        if(value.draft===(pending.draft??pending.content)){value.draft='';value.edits++;value.dirty=true;void save(sid)}
        if(result.download) downloadBlob(await apiBlob(result.download),`零汐-会话-${sid}.zip`)
        window.dispatchEvent(new CustomEvent('lingxi-options-changed',{detail:sid}))
        remember(sid); await refresh(sid)
        if(result.session_id!==sid)window.dispatchEvent(new CustomEvent('lingxi-command-session',{detail:{mode,id:result.session_id}}))
        else if(result.message&&!result.proposal)toast.info(result.message)
        return
      }
      if (!pending.accepted) {
        await lingxi.send(sid, mode, pending.content, pending.id, pending.character, pending.version)
        pending = { ...pending, accepted: true }; remember(sid, pending)
      }
      if (value.draft === (pending.draft ?? pending.content)) {
        value.draft = ''; value.edits++; value.dirty = true
        void save(sid)
      }
      const serial = value.serial
      const snapshot = await lingxi.run(sid, mode)
      apply(sid, snapshot, serial)
      remember(sid)
    } catch (error) {
      const rejected = !pending.accepted && error instanceof ApiError && [400, 401, 403, 404, 409, 413, 422].includes(error.status)
      if (rejected) remember(sid)
      const message=rejected ? '输入未发送，可修改后重发' : pending.accepted ? '输入已接收，生成尚未确认' : '输入尚未确认'
      toast.error(message, {
        id:'lingxi-send-'+mode,
        description: rejected ? (error as Error).message : `${(error as Error).message}；重试会沿用原编号，也可核对并取消未执行输入。`,
      })
      Object.assign(error as Error,{notified:true});throw error
    } finally { value.sending = false; notify() }
  }, [apply, entry, mode, notify, remember, save, refresh])
  const send = useCallback(async (sid: number, content: string, character?: Character, draft?: string) => {
    if (!connected) throw new Error('请先连接零汐本地服务')
    if (!content.trim() || content.length > 8000) throw new Error('消息需为 1～8000 字符')
    const value = entry(sid)
    if (value.conflictBlocked) throw new Error('请先比较并处理草稿冲突')
    if (value.pending) throw new Error('此会话还有未确认输入，请先重试或取消，避免重复发送')
    await transmit(sid, { id: crypto.randomUUID(), content, character, draft, version: value.version >= 0 ? value.version : undefined })
  }, [connected, entry, transmit])
  const retry = useCallback(async (message: Message) => {
    const sid = active.current
    if (!connected || !sid) throw new Error('请先选择已连接的会话')
    const value = entry(sid)
    if (value.pending && message.id === `outbox-${value.pending.id}`) return transmit(sid, value.pending)
    if (!message.batch_id) return refresh(sid)
    const serial = value.serial
    apply(sid, await lingxi.retry(sid, mode, message.batch_id), serial)
  }, [apply, connected, entry, mode, refresh, transmit])
  const stop = useCallback(async () => {
    const sid = active.current
    if (!connected || !sid) return
    const serial = entry(sid).serial
    apply(sid, await lingxi.stop(sid, mode), serial)
  }, [apply, connected, entry, mode])
  const reloadDraftConflict = useCallback(async () => {
    const sid = active.current
    if (!sid || !connected) return
    const remote = await lingxi.draft(sid)
    entry(sid).conflict = remote; notify()
  }, [connected, entry, notify])
  const resolveDraft = useCallback(async (choice: 'local' | 'remote') => {
    const sid = active.current
    if (!sid || !connected) return
    const value = entry(sid)
    const remote = value.conflict || await lingxi.draft(sid)
    value.revision = remote.revision
    if (choice === 'remote') { value.draft = remote.content; value.edits++; value.dirty = false; recoveryStorage.removeItem(`lingxi-draft:${mode}:${sid}`) }
    value.conflictBlocked = false; value.conflict = undefined
    notify()
    if (choice === 'local') await save(sid)
  }, [connected, entry, notify, save])
  const cancelPending = useCallback(async () => {
    const sid = active.current
    if (!sid || !connected) return
    const value = entry(sid), pending = value.pending
    if (!pending || value.sending) return
    const result = await lingxi.cancelInput(sid, mode, pending.id)
    remember(sid)
    if (result.cancelled && !value.draft.trim()) { value.draft = pending.draft ?? pending.content; value.edits++; value.dirty = true }
    if (result.already_processed) toast.message('已核对：这条输入已有回复批次，未重复发送。请在消息中查看结果或重试。')
    notify()
    await refresh(sid)
  }, [connected, entry, mode, notify, refresh, remember])
  const loadMore=useCallback(async()=>{
    const sid=active.current;if(!sid||!connected)return;const value=entry(sid)
    if(value.loadingMore||!value.hasMore||!value.oldest)return
    value.loadingMore=true;notify()
    const version=value.version
    try{const snapshot=await lingxi.snapshot(sid,mode,value.oldest);if(value.version!==version||snapshot.session_version!==version)throw new Error('对话已变化，请重试加载历史');value.messages=[...snapshot.messages.filter(m=>!value.messages.some(n=>n.id===m.id)),...value.messages];value.batches=[...snapshot.batches.filter(b=>!value.batches.some(n=>n.id===b.id)),...value.batches];value.expanded=true;value.hasMore=!!snapshot.has_more;value.oldest=snapshot.oldest_id}
    finally{value.loadingMore=false;notify()}
  },[connected,entry,mode,notify])
  const value = entry(id)
  const pendingMessage: Message[] = value.pending && !value.pending.accepted ? [{
    id: `outbox-${value.pending.id}`, role: 'user', content: value.pending.content,
    delivery_status: value.sending ? 'sending' : 'failed',
  }] : []
  // An accepted input can still need an explicit retry of /run after a lost response.
  if (value.pending?.accepted && !value.sending) pendingMessage.push({
    id: `outbox-${value.pending.id}`, role: 'system', content: '输入已保存，点击重试继续生成。', delivery_status: 'failed',
  })
  return { hasMore:!!value.hasMore,loadingMore:!!value.loadingMore,loadMore,batches:value.batches, messages: [...value.messages, ...pendingMessage], draft: value.draft, generating: value.generating,
    draftConflict: value.conflict, draftBlocked: Boolean(value.conflictBlocked), resolveDraft, reloadDraftConflict, cancelPending, pending: value.pending,
    sending: value.sending, version: value.version, setDraft, send, retry, stop, refresh, apply, adopt }
}
