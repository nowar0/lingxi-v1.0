import { ConversationToolScope } from '@/context/ConversationToolContext'
import { ConversationTools } from '@/components/shared/ConversationTools'
import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowRight } from 'lucide-react'
import { useApp } from '@/context/AppContext'
import { ConversationMenu } from '@/components/shared/ConversationMenu'
import { MessageList } from '@/components/shared/MessageList'
import { CompanionComposer } from '@/components/shared/CompanionComposer'
import { CompanionRail } from '@/components/shared/CompanionRail'
import { ConversationHistory } from '@/components/shared/ConversationHistory'
import { DataModeNotice } from '@/components/shared/DataModeNotice'
import { Button } from '@/components/ui/button'
import { Tooltip } from '@/components/ui/tooltip'
import './companion-page.css'

// Route visits share the selected conversation through AppContext and the last panel visibility here.
let openedCompanion = false
let lastContextVisible = true

export function CompanionPage() {
  const { sessions, activeSessionId, setActiveSessionId, messages, activeCharacter, connection } = useApp()
  const [showContext, setShowContext] = useState(lastContextVisible)
  const activeTitle = sessions.find((session) => session.id === activeSessionId)?.title || '新的陪伴对话'
  useEffect(() => {
    if (openedCompanion || connection !== 'connected') return
    openedCompanion = true
    const latest = [...sessions].sort((a, b) => Date.parse(b.updated_at || b.created_at || '') - Date.parse(a.updated_at || a.created_at || '') || b.id - a.id)[0]
    if (latest && !activeSessionId) setActiveSessionId(latest.id)
  }, [activeSessionId, connection, sessions, setActiveSessionId])
  const toggleContext = () => { lastContextVisible = !showContext; setShowContext(lastContextVisible) }
  return <ConversationToolScope identity={activeSessionId}><div className="chat-page companion-message-page">
    <section className="chat-center companion-chat-panel" aria-label="共同陪伴聊天面板">
      <header className="chat-header companion-chat-header">
        <ConversationMenu />
        <div className="companion-chat-heading"><div><h1>{activeCharacter.name} · 共同陪伴</h1><span className="companion-conversation-name" title={activeTitle}>{activeTitle}</span></div><p title={activeCharacter.description}>{activeCharacter.description || '与你一起，慢慢度过每一天。'}</p></div>
        <div className="companion-chat-header-actions"><ConversationHistory mode="companion" />
          <Tooltip content={showContext ? '收起右侧模块' : '展开右侧模块'}><Button variant="ghost" size="icon" className="size-8 companion-context-toggle" aria-label={showContext ? '收起右侧模块' : '展开右侧模块'} aria-expanded={showContext} onClick={toggleContext}>{showContext ? <ArrowRight /> : <ArrowLeft />}</Button></Tooltip>
        </div>
      </header>
      <div className="px-4 pt-2"><DataModeNotice /></div>
      <ConversationTools mode="companion" sessionId={activeSessionId}/><MessageList key={activeSessionId || 'new'} messages={messages} mode="companion" preservePosition emptyText={`和${activeCharacter.name}开始新的对话`} />
      <CompanionComposer />
    </section>
    {showContext && <CompanionRail />}
  </div></ConversationToolScope>
}
