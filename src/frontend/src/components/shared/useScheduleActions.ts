import { useRef, useState } from 'react'
import { useApp } from '@/context/AppContext'
import { lingxi } from '@/api/lingxi'
import type { ScheduleItem } from '@/types'

export function useScheduleActions() {
  const { connection, setSchedule } = useApp()
  const [saving, setSaving] = useState(false), [working, setWorking] = useState<Record<string, boolean>>({})
  const locked = useRef(false), active = useRef(new Set<string>())
  const pending = useRef<{ key: string; id: string } | undefined>(undefined)
  const create = async (title: string, date: string) => {
    if (locked.current) return false
    if (!['connected', 'demo'].includes(connection)) throw new Error('离线时日程只读，请先恢复连接')
    locked.current = true; setSaving(true)
    const key = JSON.stringify([title.trim(), date])
    if (pending.current?.key !== key) pending.current = { key, id: crypto.randomUUID() }
    try {
      if (connection === 'demo') setSchedule(items => [...items, { id: pending.current!.id, title: title.trim(), date, done: false, revision: 1 }])
      else setSchedule((await lingxi.createSchedule(title.trim(), date, pending.current!.id)).items)
      pending.current = undefined
      return true
    } finally { locked.current = false; setSaving(false) }
  }
  const change = async (item: ScheduleItem, remove = false) => {
    const id = String(item.id)
    if (active.current.has(id)) return
    if (!['connected', 'demo'].includes(connection)) throw new Error('离线时日程只读，请先恢复连接')
    active.current.add(id); setWorking(value => ({ ...value, [id]: true }))
    try {
      if (connection === 'demo') setSchedule(items => remove ? items.filter(row => row.id !== item.id) : items.map(row => row.id === item.id ? { ...row, done: !row.done } : row))
      else setSchedule((await (remove ? lingxi.deleteSchedule(item) : lingxi.updateSchedule(item, !item.done))).items)
    } finally { active.current.delete(id); setWorking(value => ({ ...value, [id]: false })) }
  }
  return { create, toggle: (item: ScheduleItem) => change(item), remove: (item: ScheduleItem) => change(item, true), saving, working }
}
