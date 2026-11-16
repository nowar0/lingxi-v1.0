const customSuffix = /(?:\s*·\s*自定义)+\s*$/u

export function customizedThemeName(name: string): string {
  return `${name.replace(customSuffix, '').trimEnd()} · 自定义`
}

export function normalizeThemeName(name: string): string {
  return customSuffix.test(name) ? customizedThemeName(name) : name
}
