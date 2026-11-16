import { useEffect } from 'react'
import { Quote, X } from 'lucide-react'
import type { Mode } from '@/types'
import { Button } from '@/components/ui/button'
import { useMessageActions } from '@/context/MessageActionsContext'

export function QuotePreview({mode}: {mode:Mode}) {
  const {currentQuote,clearQuote} = useMessageActions()
  const quote = currentQuote(mode)
  useEffect(() => {if (quote) document.querySelector<HTMLTextAreaElement>(`[data-composer-mode="${mode}"]`)?.focus({preventScroll:true})},[quote,mode])
  if (!quote) return null
  return <div className="composer-quote"><Quote aria-hidden="true" /><div><strong>{quote.name}</strong><p>{quote.text}</p></div><Button type="button" variant="ghost" size="icon" aria-label="取消引用" onClick={() => clearQuote(mode)}><X /></Button></div>
}
