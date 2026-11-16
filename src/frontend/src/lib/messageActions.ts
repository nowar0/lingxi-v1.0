import type { Message } from '@/types'
import { apiBlob } from '@/api/client'

export type MessageImage = { mediaId: string; sessionId: number; url: string; name: string }
export function messageImages(content: string): MessageImage[] {
  return [...content.matchAll(/\[([^\]\n]{1,200})\]\((\/v6\/media\/([a-f0-9]{32})\?session_id=(\d+))\)/g)]
    .filter(([,name]) => name === '生成图片' || /\.(png|jpe?g|webp|gif)$/i.test(name))
    .map(([,name,url,mediaId,sid]) => ({mediaId,sessionId:Number(sid),url,name}))
}
export function quoteExcerpt(message: Message) {
  const text = message.content.replace(/\[([^\]\n]+)\]\(\/v6\/media\/[^)]+\)/g, '[附件：$1]')
  return text.length > 1200 ? text.slice(0,1200) + '…' : text
}
export function withMessageQuote(draft: string, quote?: {name: string; text: string}) {
  if (!quote) return draft
  return `> ${quote.name.replace(/[\r\n]/g, ' ')}\n${quote.text.split('\n').map((line) => `> ${line}`).join('\n')}\n\n${draft}`
}
type ImageResult = {ok: boolean; cancelled?: boolean; error?: string}
type ImageBridge = Record<'copy' | 'save' | 'reveal', (image: MessageImage) => Promise<ImageResult>>
declare global { interface Window { LingxiShell?: {isDesktop?: boolean; messageImages?: ImageBridge} } }
export async function actOnImage(action: 'copy'|'save'|'reveal', image: MessageImage) {
  const bridge = window.LingxiShell?.messageImages
  if (bridge) {
    const result = await bridge[action](image)
    if (!result.ok) throw new Error(result.error || '图片操作未完成')
    return !result.cancelled
  }
  if (action === 'reveal') throw new Error('请重启桌面版零汐后使用打开文件夹')
  const blob = await apiBlob(image.url)
  if (!blob.type.startsWith('image/')) throw new Error('这条消息没有可用图片')
  if (action === 'copy') {
    const bitmap = await createImageBitmap(blob)
    try {
      const canvas = document.createElement('canvas'); canvas.width=bitmap.width; canvas.height=bitmap.height
      const context = canvas.getContext('2d'); if (!context) throw new Error('图片无法复制')
      context.drawImage(bitmap,0,0)
      const png = await new Promise<Blob>((resolve,reject) => canvas.toBlob((value) => value ? resolve(value) : reject(new Error('图片转换失败')), 'image/png'))
      await navigator.clipboard.write([new ClipboardItem({'image/png':png})])
    } finally { bitmap.close() }
  } else {
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a'); link.href=url; link.download=/\.(png|jpe?g|webp|gif)$/i.test(image.name) ? image.name : `零汐图片.${blob.type.split('/')[1]}`
    document.body.append(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url),30000)
  }
  return true
}
