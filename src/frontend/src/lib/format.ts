export function formatNumber(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return '—'
  return new Intl.NumberFormat('zh-CN', { notation: value > 99999 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(value)
}

export function formatTime(value?: string | number | Date) {
  if (!value) return '刚刚'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

export function formatDate(value?: string | number | Date) {
  if (!value) return '今天'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(date)
}

export function relativeDay(date?: string) {
  if (!date) return '未定日期'
  const today = new Date()
  const target = new Date(`${date}T00:00:00`)
  const diff = Math.round((target.getTime() - new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) / 86400000)
  if (diff === 0) return '今天'
  if (diff === 1) return '明天'
  if (diff === -1) return '昨天'
  return `${target.getMonth() + 1}月${target.getDate()}日`
}
