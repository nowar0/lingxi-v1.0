import { createContext, useCallback, useContext, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react'
import { toast } from 'sonner'
import type { ThemeDefinition } from '@/types'
import { downloadBlob } from '@/lib/utils'
import { customizedThemeName } from '@/lib/themeName'
import { validateTheme } from '@/lib/themeValidation'

export const mistBlueTheme: ThemeDefinition = {
  format: 'lingxi-theme', version: 1, name: '零汐 · 黑白玻璃', character: '零汐',
  appearance: { theme: 'light', accent: '#171717', radius: 16, density: 'comfortable', fontScale: 100 },
  colors: { background: '#eeeeee', surface: '#ffffff', sidebar: '#e7e7e7', text: '#171717', muted: '#626262', border: '#d3d3d3', input: '#f5f5f5' },
  scene: 'night',
}

const installedTheme: ThemeDefinition = {
 format:'lingxi-theme', version:1, name:'零汐 · 雾蓝', character:'零汐',
 appearance:{theme:'light',accent:'#4c88d2',radius:14,density:'comfortable',fontScale:100},
 colors:{background:'#f3f4f6',surface:'#ffffff',sidebar:'#eeeff2',text:'#364151',muted:'#858d99',border:'#e0e3e8',input:'#f7f8fa'},scene:'night',
}
const installedDark = {background:'#11161e',surface:'#1b2330',sidebar:'#161d27',text:'#e5edf7',muted:'#8f9bad',border:'#2c3746',input:'#202a38'}
function readMessageTheme(): ThemeDefinition {
 try { const raw=localStorage.getItem('lingxi-message-theme-v1'); if(raw)return validateTheme(JSON.parse(raw)); const saved=JSON.parse(localStorage.getItem('lingxi-next-theme-v1') || 'null'); return saved?.theme ? validateTheme(saved.theme) : installedTheme } catch { return installedTheme }
}
function messageTokens(theme:ThemeDefinition,dark:boolean):CSSProperties {
 const c=dark?darkColors:mistBlueTheme.colors
 return {'--background':c.background,'--foreground':c.text,'--card':c.surface,'--card-foreground':c.text,'--popover':c.surface,'--popover-foreground':c.text,'--primary':c.text,'--primary-foreground':c.surface,'--secondary':c.input,'--secondary-foreground':c.text,'--muted':c.input,'--muted-foreground':c.muted,'--accent':c.input,'--accent-foreground':c.text,'--border':c.border,'--input':c.input,'--ring':c.text,'--radius':`${theme.appearance.radius}px`,'--font-scale':String(theme.appearance.fontScale/100),'--work-muted':`color-mix(in srgb, ${c.text} 72%, ${c.surface})`,fontSize:`${14*theme.appearance.fontScale/100}px`,color:c.text} as CSSProperties
}

type Mode = 'light' | 'dark' | 'system'
interface ThemeContextValue {
  theme: ThemeDefinition
  messageStyle: CSSProperties
  setFontScale: (value: number) => void
  mode: Mode
  glass: number
  reduceMotion: boolean
  setMode: (mode: Mode) => void
  setGlass: (value: number) => void
  setReduceMotion: (value: boolean) => void
  setAccent: (value: string) => void
  setRadius: (value: number) => void
  setDensity: (value: 'comfortable' | 'compact') => void
  importTheme: (file: File) => Promise<void>
  exportTheme: () => void
  resetTheme: () => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)
const STORAGE_KEY = 'lingxi-next-theme-v1'
const darkColors: ThemeDefinition['colors'] = {
  background: '#111111', surface: '#1b1b1b', sidebar: '#151515', text: '#f3f3f1', muted: '#a1a19d', border: '#363634', input: '#242422',
}
function readAppearance(): { theme: ThemeDefinition; mode: Mode; glass: number; reduceMotion: boolean } {
  const defaults = { theme: mistBlueTheme, mode: 'light' as Mode, glass: 72, reduceMotion: typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches }
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return defaults
    const saved = JSON.parse(raw)
    const theme = validateTheme(saved.theme)
    return { theme, mode: ['light', 'dark', 'system'].includes(saved.mode) ? saved.mode : theme.appearance.theme,
      glass: typeof saved.glass === 'number' && Number.isFinite(saved.glass) && saved.glass >= 0 && saved.glass <= 100 ? saved.glass : 72,
      reduceMotion: typeof saved.reduceMotion === 'boolean' ? saved.reduceMotion : defaults.reduceMotion }
  } catch { return defaults }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  // Read before the first persistence effect; StrictMode must not overwrite
  // saved choices with default state during its mount/unmount probe.
  const [messageTheme] = useState(readMessageTheme)
  const [saved] = useState(readAppearance)
  const [theme, setTheme] = useState<ThemeDefinition>(saved.theme.name === '零汐 · 雾蓝' ? mistBlueTheme : saved.theme)
  const [mode, setMode] = useState<Mode>(saved.mode)
  const [glass, setGlass] = useState(saved.glass)
  const [reduceMotion, setReduceMotion] = useState(saved.reduceMotion)
  const [systemDark, setSystemDark] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches)
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const update = () => setSystemDark(media.matches)
    media.addEventListener('change', update)
    update()
    return () => media.removeEventListener('change', update)
  }, [])

  useEffect(() => {
    const dark = mode === 'dark' || (mode === 'system' && systemDark)
    const root = document.documentElement
    const colors = dark ? (theme.appearance.theme === 'dark' ? theme.colors : darkColors) : theme.colors
    root.classList.toggle('dark', dark)
    root.classList.toggle('reduce-motion', reduceMotion)
    root.dataset.density = theme.appearance.density
    const neutral = /^#([0-9a-f]{2})\1\1$/i.test(theme.appearance.accent)
    const accent = dark && neutral ? '#eaeaea' : theme.appearance.accent
    const variables: Record<string, string> = {
      '--chrome-muted':colors.muted,'--background': colors.background, '--foreground': colors.text,
      '--card': colors.surface, '--card-foreground': colors.text,
      '--popover': colors.surface, '--popover-foreground': colors.text,
      '--primary': accent, '--primary-foreground': dark && neutral ? '#171717' : '#ffffff',
      '--secondary': colors.input, '--secondary-foreground': colors.text,
      '--muted': colors.input, '--muted-foreground': colors.muted,
      '--accent': colors.input, '--accent-foreground': colors.text,
      '--border': colors.border, '--input': colors.input, '--ring': accent,
      '--sidebar': colors.sidebar, '--sidebar-foreground': colors.text,
      '--sidebar-primary': accent, '--sidebar-primary-foreground': dark && neutral ? '#171717' : '#ffffff',
      '--sidebar-accent': colors.input, '--sidebar-accent-foreground': colors.text,
      '--sidebar-border': colors.border, '--sidebar-ring': accent,
      '--radius': `${theme.appearance.radius}px`, '--glass-opacity': String(glass / 100),
      '--font-scale': String(theme.appearance.fontScale / 100),
      '--chrome-glass': String(.56+glass*.0035), '--chrome-blur': `${Math.round(8+glass*.2)}px`,
    }
    Object.entries(variables).forEach(([key, value]) => root.style.setProperty(key, value))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ theme, mode, glass, reduceMotion }))
  }, [theme, mode, glass, reduceMotion, systemDark])

  const dark = mode === 'dark' || (mode === 'system' && systemDark)
  const messageStyle = useMemo(() => messageTokens(messageTheme,dark),[messageTheme,dark])
  useEffect(() => {localStorage.setItem('lingxi-message-theme-v1',JSON.stringify(messageTheme))},[messageTheme])

  const importTheme = useCallback(async (file: File) => {
    if (file.size > 100 * 1024) throw new Error('主题 JSON 不能超过 100 KB')
    const parsed = validateTheme(JSON.parse(await file.text()))
    setTheme(parsed)
    setMode(parsed.appearance.theme)
    toast.success(`已应用主题「${parsed.name}」`, { description: '颜色、圆角、密度与字体比例已同步更新' })
  }, [])

  const value = useMemo<ThemeContextValue>(() => ({
    theme, messageStyle, setFontScale: (fontScale) => setTheme(current => ({...current,appearance:{...current.appearance,fontScale}})), mode, glass, reduceMotion, setMode, setGlass, setReduceMotion,
    setAccent: (accent) => setTheme((current) => current.appearance.accent === accent ? current : ({ ...current, name: customizedThemeName(current.name), appearance: { ...current.appearance, accent } })),
    setRadius: (radius) => setTheme((current) => ({ ...current, appearance: { ...current.appearance, radius } })),
    setDensity: (density) => setTheme((current) => ({ ...current, appearance: { ...current.appearance, density } })),
    importTheme,
    exportTheme: () => downloadBlob(new Blob([JSON.stringify(theme, null, 2)], { type: 'application/json' }), `${theme.name.replace(/[\\/:*?"<>|]/g, '-')}.lingxi-theme.json`),
    resetTheme: () => { setTheme(mistBlueTheme); setMode('light'); setGlass(72); setReduceMotion(false); toast.success('已恢复黑白玻璃默认外观') },
  }), [theme, messageStyle, mode, glass, reduceMotion, importTheme])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme 必须在 ThemeProvider 中使用')
  return context
}

export const useOptionalTheme = () => useContext(ThemeContext)
