import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import './management-surface.css'

export function ManagementSurface({ title, navigation, footer, actions, children, className = '' }: { title: string; navigation: ReactNode; footer?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return <div className={`page-container chrome-page personal-page customer-page management-page ${className}`}>
    <header className="personal-heading management-page-heading"><h1>{title}</h1>{actions && <div className="management-page-actions">{actions}</div>}</header>
    <div className="management-workspace"><aside className="management-navigation">{navigation}{footer && <div className="management-navigation-footer">{footer}</div>}</aside><div className="management-content">{children}</div></div>
  </div>
}

export function ManagementNav({ label, items, value, onChange }: { label: string; items: Array<{ id: string; label: string; icon: LucideIcon }>; value: string; onChange: (value: string) => void }) {
  return <nav className="management-nav" aria-label={label}>{items.map(({ id, label, icon: Icon }) => <button key={id} type="button" aria-current={value === id ? 'page' : undefined} onClick={() => onChange(id)}><Icon /><span>{label}</span></button>)}</nav>
}
