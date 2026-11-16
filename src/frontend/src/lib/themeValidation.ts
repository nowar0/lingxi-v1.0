import type { ThemeDefinition } from '@/types'
import { normalizeThemeName } from './themeName'

const colorPattern = /^#[0-9a-fA-F]{6}$/
export function validateTheme(value: unknown): ThemeDefinition {
  const theme = value as ThemeDefinition
  if (!theme || theme.format !== 'lingxi-theme' || theme.version !== 1) throw new Error('仅支持 lingxi-theme v1 主题包')
  if (typeof theme.name !== 'string' || !theme.name.trim() || theme.name.length > 100 || !theme.appearance || !theme.colors) throw new Error('主题包缺少有效名称、外观或颜色字段')
  if (!colorPattern.test(theme.appearance.accent)) throw new Error('主色必须是六位十六进制颜色')
  const colors = ['background', 'surface', 'sidebar', 'text', 'muted', 'border', 'input'] as const
  if (colors.some(key => typeof theme.colors[key] !== 'string' || !colorPattern.test(theme.colors[key])) || Object.keys(theme.colors).some(key => !colors.includes(key as typeof colors[number]))) throw new Error('颜色字段需完整且为六位十六进制颜色')
  if (!['light', 'dark'].includes(theme.appearance.theme)) throw new Error('明暗模式不受支持')
  if (!['comfortable', 'compact'].includes(theme.appearance.density)) throw new Error('界面密度不受支持')
  if (!Number.isFinite(theme.appearance.radius) || theme.appearance.radius < 0 || theme.appearance.radius > 32) throw new Error('主题圆角需为 0～32 的有限数字')
  if (!Number.isFinite(theme.appearance.fontScale) || theme.appearance.fontScale < 80 || theme.appearance.fontScale > 140) throw new Error('字体比例需为 80～140 的有限数字')
  if (theme.scene !== undefined && !['night', 'day', 'minimal'].includes(theme.scene)) throw new Error('主题场景不受支持')
  return { ...theme, name: normalizeThemeName(theme.name) }
}
