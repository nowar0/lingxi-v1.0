import { useRef } from 'react'
import { useApp } from '@/context/AppContext'
import { createProgramManagerClient } from '@/lib/programManager'
import { ProgramManagerView } from '@/components/settings/ProgramManagerView'

/** Ordinary settings wrapper; the independent manager imports only the pure view. */
export function ProgramManagerPanel(){
  const {connection}=useApp()
  const client=useRef<ReturnType<typeof createProgramManagerClient>|null>(null)
  if(!client.current)client.current=createProgramManagerClient()
  return <ProgramManagerView connected={connection==='connected'} client={client.current}/>
}
