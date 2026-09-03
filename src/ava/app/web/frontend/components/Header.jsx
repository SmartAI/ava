import React from 'react'

import { MenuIcon, ThemeIcon } from './Icons'
import { StatusDot } from './Sidebar'

const iconButtonClass = 'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border-0 bg-transparent text-quiet hover:bg-hover hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

export function Header({ title, status, onOpenNavigation, onToggleTheme }) {
  return <header className="flex h-13 shrink-0 items-center gap-2.5 border-b border-line-strong px-3 min-[701px]:px-5">
    <button className={`${iconButtonClass} min-[701px]:hidden`} title="Open navigation" aria-label="Open navigation" onClick={onOpenNavigation}><MenuIcon /></button>
    <StatusDot state={status} />
    <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-sm font-medium">{title}</span>
    <button className={iconButtonClass} title="Toggle theme" aria-label="Toggle theme" onClick={onToggleTheme}><ThemeIcon /></button>
  </header>
}
