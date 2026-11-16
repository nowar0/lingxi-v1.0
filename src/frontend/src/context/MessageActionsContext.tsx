import { createContext, useContext, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useApp } from '@/context/AppContext'
import type { Message, Mode } from '@/types'
import { quoteExcerpt } from '@/lib/messageActions'

type Quote = {messageId: Message['id']; name: string; text: string}
type Actions = {quote: (mode:Mode,sid:number,message:Message) => void; currentQuote: (mode:Mode) => Quote|undefined; clearQuote: (mode:Mode,expected?:Quote) => void}
const Context = createContext<Actions|null>(null)
export function MessageActionsProvider({children}: {children:ReactNode}) {
  const {activeSessionId,activeWorkSessionId,activeCharacter} = useApp()
  const navigate = useNavigate(); const location = useLocation()
  const [quotes,setQuotes] = useState<Record<string,Quote|undefined>>({})
  const key = (mode:Mode) => `${mode}:${mode === 'work' ? activeWorkSessionId : activeSessionId}`
  const quote:Actions['quote'] = (mode,sid,message) => {
    const name = message.role === 'user' ? '你' : message.speaker?.name || (mode === 'work' ? '工作助手' : activeCharacter.name)
    setQuotes((saved) => ({...saved,[`${mode}:${sid}`]:{messageId:message.id,name,text:quoteExcerpt(message)}}))
    if (mode === 'work' && location.pathname !== '/work') navigate('/work')
    if (mode === 'companion' && location.pathname !== '/companion') navigate('/companion')
  }
  return <Context.Provider value={{quote,currentQuote:(mode) => quotes[key(mode)],clearQuote:(mode,expected) => setQuotes((saved) => {const id=key(mode);if (expected && saved[id]!==expected) return saved;return {...saved,[id]:undefined}})}}>{children}</Context.Provider>
}
export function useMessageActions() {
  const context = useContext(Context)
  if (!context) throw new Error('消息操作需要 MessageActionsProvider')
  return context
}
