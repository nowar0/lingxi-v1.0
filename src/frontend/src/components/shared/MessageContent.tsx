import { Children, isValidElement, memo, useState, type ReactElement, type ReactNode } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, ChevronDown, ChevronUp, Copy } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { ArtifactPreview } from './ArtifactPreview'
import { ImageEditor } from './ImageEditor'

const localMedia = /^\/v6\/media\/[a-f0-9]{32}\?session_id=[1-9]\d*(?:#(?:L\d+-L\d+|zip=[^\s()<>]{1,2000};L\d+-L\d+))?$/

function MessageImage({url,name}:{url:string;name:string}){const [open,setOpen]=useState(false);return <><button type="button" className="message-image-button" aria-label={`预览图片：${name}`} onClick={()=>setOpen(true)}><img src={url} alt={name} loading="lazy" decoding="async"/></button>{open&&<ImageEditor src={url.split('#')[0]} name={name} open onOpenChange={setOpen}/>}</>}

export function safeMessageUrl(url: string) {
  if (!url || /[\u0000-\u0020\u007f]/.test(url)) return undefined
  if (localMedia.test(url) || /^#[\w\p{L}\p{N}-]+$/u.test(url)) return url
  try {
    const parsed = new URL(url)
    if (['https:', 'http:', 'mailto:'].includes(parsed.protocol) && !parsed.username && !parsed.password) return url
  } catch { /* Relative file paths need the workspace's explicit resolution flow. */ }
  return undefined
}

function ArtifactLink({url,children}:{url:string;children:ReactNode}) {
  const [open,setOpen]=useState(false)
  const match=url.match(/^\/v6\/media\/([a-f0-9]{32})\?session_id=(\d+)/)!
  return <><a href={url} className="composer-artifact-link" onClick={event=>{event.preventDefault();setOpen(true)}}>{children}</a>{open&&<ArtifactPreview id={match[1]} sessionId={Number(match[2])} initialReference={url} open onOpenChange={setOpen}/>}</>
}

function plain(node: ReactNode): string {
  return Children.toArray(node).map(item => typeof item === 'string' || typeof item === 'number' ? String(item) : isValidElement<{children?: ReactNode}>(item) ? plain(item.props.children) : '').join('')
}

const CodeBlock = memo(function CodeBlock({ code, language }: { code: string; language: string }) {
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  const long = code.split('\n').length > 20 || code.length > 3000
  const copy = async () => {
    try { await navigator.clipboard.writeText(code); setCopied(true); window.setTimeout(() => setCopied(false), 1500) }
    catch { toast.error('代码未复制，请选择代码后复制') }
  }
  return <div className="message-code-block"><div className="message-code-toolbar"><span>{language || '代码'}</span><Button variant="ghost" size="xs" aria-label="复制代码" onClick={() => void copy()}>{copied ? <Check /> : <Copy />}{copied ? '已复制' : '复制代码'}</Button>{long && <Button variant="ghost" size="xs" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? <ChevronUp /> : <ChevronDown />}{expanded ? '折叠代码' : '展开代码'}</Button>}</div><pre tabIndex={0} aria-label={`${language || '文本'}代码`} className={long && !expanded ? 'is-collapsed' : undefined}><code className={language ? `language-${language}` : undefined}>{code}</code></pre></div>
})

const components: Components = {
  pre({ children }) {
    const element = Children.toArray(children).find(item => isValidElement(item)) as ReactElement<{className?: string; children?: ReactNode}> | undefined
    const language = element?.props.className?.match(/(?:^|\s)language-([\w#+.-]{1,40})/)?.[1] || ''
    return <CodeBlock code={plain(element?.props.children ?? children)} language={language} />
  },
  table({ children }) { return <div className="message-table-scroll" tabIndex={0} role="region" aria-label="消息表格，可横向滚动"><table>{children}</table></div> },
  a({ href, children }) {
    const url = href && safeMessageUrl(href)
    if (!url) return <span>{children}</span>
    const label = plain(children)
    if (localMedia.test(url) && (label === '生成图片' || /\.(png|jpe?g|webp)$/i.test(label))) return <MessageImage url={url} name={label}/>
    if(localMedia.test(url))return <ArtifactLink url={url}>{children}</ArtifactLink>
    return <a href={url} target={url.startsWith('#') ? undefined : '_blank'} rel="noopener noreferrer">{children}</a>
  },
  img({ src, alt }) {
    const url = typeof src === 'string' && safeMessageUrl(src)
    if (!url) return <span>{alt || '图片链接不可用'}</span>
    if (localMedia.test(url)) return <MessageImage url={url} name={alt || '消息图片'}/>
    // External images are opened explicitly, rather than contacted on render.
    return <a href={url} target="_blank" rel="noopener noreferrer">远程图片：{alt || '打开查看'}</a>
  },
}

export const MessageContent = memo(function MessageContent({ text }: { text: string }) {
  return <div className="message-markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={url => safeMessageUrl(url)} components={components}>{text}</Markdown></div>
})
