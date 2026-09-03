import React from 'react'

export function PlusIcon({ size = 16 }) {
  return <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M8 3.5v9M3.5 8h9" /></svg>
}

export function ComposeIcon() {
  return <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M11.2 2.3l2.5 2.5L6 12.5l-3.2.7.7-3.2 7.7-7.7z" /></svg>
}

export function FolderIcon() {
  return <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M1.8 12.5v-9h4.3l1.4 1.8h6.7v7.2a.8.8 0 01-.8.8H2.6a.8.8 0 01-.8-.8z" /></svg>
}

export function CaretIcon({ className = '' }) {
  return <svg className={className} width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M4.5 2.5l3.5 3.5-3.5 3.5" /></svg>
}

export function ChevronIcon({ open }) {
  return <svg className={`absolute transition-transform ${open ? 'rotate-90' : ''}`} width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M4.5 2.5l3.5 3.5-3.5 3.5" /></svg>
}

export function ArchiveIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 4.5h11M3.5 4.5l.8 8h7.4l.8-8M6 7.5h4M2 2h12v2.5H2z" /></svg>
}

export function ThemeIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><circle cx="8" cy="8" r="3.25" /><path d="M8 1v1.5M8 13.5V15M15 8h-1.5M2.5 8H1M12.95 3.05l-1.06 1.06M4.11 11.89l-1.06 1.06M12.95 12.95l-1.06-1.06M4.11 4.11L3.05 3.05" strokeLinecap="round" /></svg>
}

export function CloseIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M4 4l8 8M12 4l-8 8" /></svg>
}

export function BackIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M9.5 3.5L5 8l4.5 4.5" /></svg>
}

export function UpIcon() {
  return <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M8 12.5v-9M4 7.5L8 3.5l4 4" /></svg>
}

export function ImageIcon() {
  return <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2" y="2.5" width="12" height="11" rx="1.5" /><circle cx="5.5" cy="6" r="1.2" /><path d="M3.5 11l3-3 2.2 2.2 1.4-1.4 2.4 2.2" /></svg>
}

export function FileIcon({ className = '' }) {
  return <svg className={className} width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M8 1.5H4a1 1 0 00-1 1v9a1 1 0 001 1h6a1 1 0 001-1V4.5L8 1.5z" /><path d="M8 1.5v3h3" /></svg>
}

export function BashIcon({ className = '' }) {
  return <svg className={className} width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 4l2.5 2.5L3 9M7.5 10h3.5" /></svg>
}

export function MenuIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M2.5 4h11M2.5 8h11M2.5 12h11" /></svg>
}

export function ArrowIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M8 12.5v-9M4 7.5L8 3.5l4 4" /></svg>
}

export function StopIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><rect x="5" y="5" width="6" height="6" rx="1.2" /></svg>
}

export function PlayIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M5 3.5v9l7-4.5z" /></svg>
}
