import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

type Panel = { tab: string; setTab: (tab: string) => void }
const Context = createContext<Panel | undefined>(undefined)
export function ConversationToolScope({ children, identity }: { children: ReactNode; identity?: string | number }) {
  const [tab, setTab] = useState('')
  useEffect(() => setTab(''), [identity])
  return <Context.Provider value={{ tab, setTab }}>{children}</Context.Provider>
}
export const useConversationToolPanel = () => useContext(Context)
