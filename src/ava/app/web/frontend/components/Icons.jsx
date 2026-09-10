import React from 'react'

function Icon({ size = 16, className = '', filled = false, children }) {
  return <svg
    width={size}
    height={size}
    viewBox="0 0 16 16"
    fill={filled ? 'currentColor' : 'none'}
    stroke={filled ? 'none' : 'currentColor'}
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    className={className}
    aria-hidden="true"
    focusable="false"
  >{children}</svg>
}

export function PlusIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M8 3.5v9M3.5 8h9" /></Icon>
}

export function ComposeIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M11.2 2.3l2.5 2.5L6 12.5l-3.2.7.7-3.2 7.7-7.7z" /></Icon>
}

export function FolderIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M1.8 12.5v-9h4.3l1.4 1.8h6.7v7.2a.8.8 0 01-.8.8H2.6a.8.8 0 01-.8-.8z" /></Icon>
}

export function CaretIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M6 3.5l4 4.5-4 4.5" /></Icon>
}

export function ChevronIcon({ open, size = 16, className = '' }) {
  return <Icon size={size} className={`${className} transition-transform duration-150 ${open ? 'rotate-90' : ''}`}><path d="M6 3.5l4 4.5-4 4.5" /></Icon>
}

export function ArchiveIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M2.5 4.5h11M3.5 4.5l.8 8h7.4l.8-8M6 7.5h4M2 2h12v2.5H2z" /></Icon>
}

export function ThemeIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><circle cx="8" cy="8" r="3.25" /><path d="M8 1v1.5M8 13.5V15M15 8h-1.5M2.5 8H1M12.95 3.05l-1.06 1.06M4.11 11.89l-1.06 1.06M12.95 12.95l-1.06-1.06M4.11 4.11L3.05 3.05" /></Icon>
}

export function SettingsIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><circle cx="8" cy="8" r="2.25" /><path d="M6.9 1.8h2.2l.4 1.65c.35.14.68.33.98.56l1.62-.5 1.1 1.9-1.22 1.15c.03.2.05.4.05.61s-.02.42-.05.62l1.22 1.14-1.1 1.9-1.62-.5c-.3.24-.63.43-.98.57l-.4 1.64H6.9l-.4-1.64a4.8 4.8 0 01-.98-.57l-1.62.5-1.1-1.9 1.22-1.14A4.24 4.24 0 014 7.17c0-.21.02-.41.05-.61L2.8 5.41l1.1-1.9 1.62.5c.3-.23.63-.42.98-.56l.4-1.65z" /></Icon>
}

export function CloseIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M4 4l8 8M12 4l-8 8" /></Icon>
}

export function BackIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M9.5 3.5L5 8l4.5 4.5" /></Icon>
}

export function UpIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M8 12.5v-9M4 7.5L8 3.5l4 4" /></Icon>
}

export function ImageIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><rect x="2" y="2.5" width="12" height="11" rx="1.5" /><circle cx="5.5" cy="6" r="1.2" /><path d="M3.5 11l3-3 2.2 2.2 1.4-1.4 2.4 2.2" /></Icon>
}

export function FileIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M8 1.5H4a1 1 0 00-1 1v11a1 1 0 001 1h8a1 1 0 001-1V5.5L8 1.5z" /><path d="M8 1.5v4h4" /></Icon>
}

export function BashIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M3 4l2.5 2.5L3 9M7.5 10h5" /></Icon>
}

export function MenuIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M2.5 4h11M2.5 8h11M2.5 12h11" /></Icon>
}

export function ArrowIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className}><path d="M8 12.5v-9M4 7.5L8 3.5l4 4" /></Icon>
}

export function StopIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className} filled><rect x="5" y="5" width="6" height="6" rx="1.2" /></Icon>
}

export function PlayIcon({ size = 16, className = '' }) {
  return <Icon size={size} className={className} filled><path d="M5 3.5v9l7-4.5z" /></Icon>
}
