import { useEffect, type ReactNode } from 'react'
import { AppContext, useApp } from './AppContext'
import { useConversation } from './useConversation'
import { lingxi } from '@/api/lingxi'
import type { Message } from '@/types'

/** Every work operation in this subtree is bound to this independent session. */
export function WorkConversationScope({ sessionId, children }: { sessionId: number; children: ReactNode }) {
  const parent = useApp()
  const chat = useConversation('work', parent.connection === 'connected', sessionId)
  useEffect(() => () => parent.stopSpeech(), [sessionId, parent.stopSpeech])
  const mutate = async (message: Message, content?: string) => {
    if (chat.generating || chat.sending || chat.pending || typeof message.id !== 'number') throw new Error('请先停止回复并处理未确认输入')
    try {
      const result = content === undefined ? await lingxi.deleteMessage(sessionId, message.id, chat.version) : await lingxi.editMessage(sessionId, message.id, chat.version, content)
      chat.apply(sessionId, result.view)
      void parent.refreshMemories().catch(() => undefined)
      return result
    } catch (error) { await chat.refresh(sessionId).catch(() => undefined); throw error }
  }
  const value = {
    ...parent,
    conversationPaging:{...parent.conversationPaging,work:{hasMore:chat.hasMore,loadingMore:chat.loadingMore,loadMore:chat.loadMore}},
    activeWorkSessionId: sessionId, workMessages: chat.messages,
    conversationBatches:{...parent.conversationBatches,work:chat.batches},
    decideToolApproval:(async(mode,approval,allowed)=>{if(mode!=='work')return parent.decideToolApproval(mode,approval,allowed);if(approval.snapshot.session_id!==sessionId)throw new Error('审批与侧聊不一致');await lingxi.decideToolApproval(sessionId,approval,allowed);await chat.refresh(sessionId)}) as typeof parent.decideToolApproval,
    drafts: { ...parent.drafts, work: chat.draft },
    generating: { ...parent.generating, work: chat.generating },
    sending: { ...parent.sending, work: chat.sending },
    draftConflicts: { ...parent.draftConflicts, work: chat.draftConflict },
    draftBlocked: { ...parent.draftBlocked, work: chat.draftBlocked },
    pendingInputs: { ...parent.pendingInputs, work: chat.pending },
    conversationVersions: { ...parent.conversationVersions, work: chat.version },
    setDraft: ((mode, text) => mode === 'work' ? chat.setDraft(text) : parent.setDraft(mode, text)) as typeof parent.setDraft,
    ensureSession: (async mode => mode === 'work' ? sessionId : parent.ensureSession(mode)) as typeof parent.ensureSession,
    sendMessage: (async (mode, text, draft) => mode === 'work' ? chat.send(sessionId, text, undefined, draft) : parent.sendMessage(mode, text, draft)) as typeof parent.sendMessage,
    stopGenerating: (async mode => mode === 'work' ? chat.stop() : parent.stopGenerating(mode)) as typeof parent.stopGenerating,
    retryMessage: (async (mode, message) => mode === 'work' ? chat.retry(message) : parent.retryMessage(mode, message)) as typeof parent.retryMessage,
    reloadDraftConflict: (async mode => mode === 'work' ? chat.reloadDraftConflict() : parent.reloadDraftConflict(mode)) as typeof parent.reloadDraftConflict,
    resolveDraft: (async (mode, choice) => mode === 'work' ? chat.resolveDraft(choice) : parent.resolveDraft(mode, choice)) as typeof parent.resolveDraft,
    cancelPendingInput: (async mode => mode === 'work' ? chat.cancelPending() : parent.cancelPendingInput(mode)) as typeof parent.cancelPendingInput,
    mutateMessage: (async (mode, message, text) => mode === 'work' ? mutate(message, text) : parent.mutateMessage(mode, message, text)) as typeof parent.mutateMessage,
    undoMessageDeletion: (async (mode, sid, key, version) => {
      if (mode !== 'work') return parent.undoMessageDeletion(mode, sid, key, version)
      if (sid !== sessionId) throw new Error('撤销目标与侧聊不一致')
      chat.apply(sessionId, (await lingxi.undoMessageDeletion(sid, key, version)).view)
      void parent.refreshMemories().catch(() => undefined)
    }) as typeof parent.undoMessageDeletion,
    restoreConversation: (async preview => {
      if (preview.mode !== 'work') return parent.restoreConversation(preview)
      if (preview.session_id !== sessionId) throw new Error('历史目标与侧聊不一致')
      chat.apply(sessionId, (await lingxi.restore(preview)).view)
      void parent.refreshMemories().catch(() => undefined); void parent.refreshTasks().catch(() => undefined)
    }) as typeof parent.restoreConversation,
    speakMessage: (async (mode, message) => parent.speakMessage(mode, message, mode === 'work' ? sessionId : undefined)) as typeof parent.speakMessage,
  }
  return <AppContext.Provider value={value}>{children}</AppContext.Provider>
}
