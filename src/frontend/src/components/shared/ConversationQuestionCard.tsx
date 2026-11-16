import { useEffect, useId, useRef, useState } from 'react'
import { CircleHelp, LoaderCircle, Pencil, X } from 'lucide-react'
import { lingxi } from '@/api/lingxi'
import { ApiError } from '@/api/client'
import { useApp } from '@/context/AppContext'
import type { ConversationQuestion, Mode } from '@/types'
import './conversation-question-card.css'

export function ConversationQuestions({ mode }: { mode: Mode }) {
  const { conversationBatches, activeSessionId, activeWorkSessionId } = useApp()
  const sid = mode === 'work' ? activeWorkSessionId : activeSessionId
  const question = conversationBatches[mode].flatMap(batch => batch.status === 'generating' ? batch.details?.questions || [] : [])
    .find(item => item.valid && item.status === 'waiting' && item.snapshot.session_id === sid && item.snapshot.mode === mode)
  return question ? <QuestionCard key={question.id} question={question} /> : null
}

function QuestionCard({ question }: { question: ConversationQuestion }) {
  const { connection } = useApp()
  const storageKey = `lingxi-question:${question.snapshot.mode}:${question.snapshot.session_id}:${question.id}`
  const [answers, setAnswers] = useState<ConversationQuestion['answers']>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null')
      if (Array.isArray(saved)) return question.questions.map(q => {
        const answer = saved.find(a => a.id === q.id)
        return { id:q.id, selected:Array.isArray(answer?.selected) ? answer.selected.filter((v:unknown) => q.options.some(o => o.label === v)).slice(0,q.multiple?6:1) : [], text:typeof answer?.text === 'string' ? answer.text.slice(0,1000) : '' }
      })
    } catch { /* Server record remains authoritative. */ }
    return question.questions.map(q => ({ id:q.id, selected:[], text:'' }))
  })
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [error, setError] = useState('')
  const [ended, setEnded] = useState(false)
  const [now, setNow] = useState(() => Date.now()/1000)
  const inFlight = useRef(false)
  const requestId = useRef(crypto.randomUUID())
  const titleId = useId()
  useEffect(() => {
    try { sessionStorage.setItem(storageKey,JSON.stringify(answers)) } catch { /* Draft persistence is optional. */ }
  },[answers,storageKey])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()/1000),1000)
    return () => window.clearInterval(timer)
  },[])
  const expired = now > question.expires_at || ended
  const disabled = busy || sent || expired || connection !== 'connected'
  const complete = answers.every(answer => answer.selected.length || answer.text.trim())
  const update = (index:number, patch:Partial<ConversationQuestion['answers'][number]>) => {
    setAnswers(values => values.map((value,i) => i===index ? {...value,...patch} : value));setError('')
  }
  const submit = async (skipped:boolean) => {
    if (inFlight.current || disabled || (!skipped && !complete)) return
    inFlight.current=true;setBusy(true);setError('')
    try {
      await lingxi.answerQuestions(question,skipped ? [] : answers,skipped,requestId.current)
      setSent(true)
      try { sessionStorage.removeItem(storageKey) } catch { /* Optional local draft. */ }
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) setEnded(true)
      setError(failure instanceof Error ? failure.message : '回答未发送，请重试')
    } finally { inFlight.current=false;setBusy(false) }
  }
  return <section className="conversation-question-card" aria-labelledby={titleId} data-question-id={question.id}>
    <div className="conversation-question-heading"><CircleHelp aria-hidden="true"/><span id={titleId}>问题</span><button type="button" aria-label="跳过本次提问" disabled={disabled} onClick={()=>void submit(true)}><X/></button></div>
    <div className="conversation-question-body">{question.questions.map((q,index) => <fieldset key={q.id} disabled={disabled}>
      <legend>{q.question}{q.multiple && <small>可多选</small>}</legend>
      {!!q.options.length && <div className="conversation-question-options">{q.options.map((option,n) => <label key={option.label}>
        <input type={q.multiple?'checkbox':'radio'} name={`${question.id}:${q.id}`} checked={answers[index].selected.includes(option.label)} onChange={()=>update(index,{selected:q.multiple ? answers[index].selected.includes(option.label) ? answers[index].selected.filter(v=>v!==option.label) : [...answers[index].selected,option.label] : [option.label]})}/>
        <span className="conversation-option-number" aria-hidden="true">{n+1}</span><span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span>
      </label>)}</div>}
      <label className="conversation-question-free"><Pencil aria-hidden="true"/><textarea aria-label={`自行填写：${q.question}`} rows={1} maxLength={1000} placeholder={q.options.length?'或自行填写回复':'填写回复'} value={answers[index].text} onChange={event=>update(index,{text:event.target.value})}/></label>
    </fieldset>)}</div>
    {(error || expired || sent) && <p className="conversation-question-state" role="status">{sent?'已提交，继续处理中…':expired?'这次提问已结束':error}</p>}
    <div className="conversation-question-actions"><button type="button" disabled={disabled} onClick={()=>void submit(true)}>跳过</button><button type="button" className="conversation-question-send" disabled={disabled || !complete} onClick={()=>void submit(false)}>{busy?<LoaderCircle className="animate-spin"/>:null}发送</button></div>
  </section>
}

export function QuestionHistory({ questions }: { questions:ConversationQuestion[] }) {
  return <>{questions.filter(q=>q.status!=='waiting').map(q=><details key={q.id} className="conversation-question-history"><summary>{q.status==='answered'?'已回答问题':q.status==='skipped'?'已跳过提问':'提问已结束'}</summary>{q.questions.map(item=>{const answer=q.answers.find(a=>a.id===item.id);return <div key={item.id}><p>{item.question}</p>{answer && <p>{[...answer.selected,answer.text].filter(Boolean).join('；')}</p>}</div>})}</details>)}</>
}
