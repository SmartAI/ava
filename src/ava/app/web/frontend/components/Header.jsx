import React from 'react'

import { iconButton, statusDot } from '../ui'
import { MenuIcon, ThemeIcon } from './Icons'

export function Header({ title, status, onOpenNavigation, onToggleTheme }) {
  return <header className="flex h-13 shrink-0 items-center gap-2.5 border-b border-line-strong px-3 min-[701px]:px-5">
    <button className={`${iconButton} min-[701px]:hidden`} title="Open navigation" aria-label="Open navigation" onClick={onOpenNavigation}><MenuIcon /></button>
    <span className={statusDot(status)} aria-hidden="true" />
    <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-sm font-medium">{title}</span>
    <button className={iconButton} title="Toggle theme" aria-label="Toggle theme" onClick={onToggleTheme}><ThemeIcon /></button>
  </header>
}
