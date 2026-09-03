import React, { useEffect, useRef } from 'react'

import { BackIcon, CloseIcon, FolderIcon, UpIcon } from './Icons'

const iconButtonClass = 'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-quiet hover:bg-hover hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
const pickerRowClass = 'flex w-full items-center gap-2.5 rounded-lg px-2 py-2.25 text-left text-ink hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent'

export function Modal({ modal, projects, onClose, onBack, onBrowse, onUseFolder, onStartChat, onConfirm }) {
  const inputRef = useRef(null)
  const listRef = useRef(null)
  const picker = modal?.kind === 'projects'
  const browsing = picker && modal.browse !== null

  useEffect(() => {
    if (modal?.kind === 'generic' && modal.input) inputRef.current?.focus()
    listRef.current?.scrollTo(0, 0)
  }, [modal])

  if (!modal) return null

  const title = picker ? (browsing ? 'Choose a folder' : 'Start a new chat') : modal.title
  const note = modal.error || (picker
    ? (browsing ? 'ava runs its tools inside the folder you choose.' : 'Chats are grouped by project directory.')
    : modal.note || '')

  return <div className="fixed inset-0 z-40 flex items-center justify-center bg-mask" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <div className="flex max-h-[min(560px,calc(100vh-64px))] w-[min(520px,calc(100vw-32px))] flex-col overflow-hidden rounded-2xl border border-line-strong bg-menu shadow-float" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div className="flex shrink-0 items-center gap-2.5 px-4 pt-4 pb-3">
        {browsing && <button className={iconButtonClass} title="Back" aria-label="Back" onClick={onBack}><BackIcon /></button>}
        <span className="min-w-0 flex-1 text-[15px] font-semibold" id="modal-title">{title}</span>
        <button className={iconButtonClass} title="Close" aria-label="Close" onClick={onClose}><CloseIcon /></button>
      </div>
      {browsing && <div className="shrink-0 overflow-hidden text-ellipsis whitespace-nowrap px-4 pb-2.5 font-mono text-xs text-faint">{modal.browse.path}</div>}
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2" ref={listRef}>
        {picker && !browsing && projects.length === 0 && <div className="p-2 text-xs text-faint">No projects yet - browse for a folder to add one.</div>}
        {picker && !browsing && projects.map(project => <button className={pickerRowClass} key={project.id} onClick={() => onStartChat(project.id)}><FolderIcon /><span className="min-w-0 flex-1"><span className="block overflow-hidden text-ellipsis whitespace-nowrap">{project.name}</span><span className="block overflow-hidden text-ellipsis whitespace-nowrap font-mono text-[11px] text-faint">{project.path}</span></span></button>)}
        {browsing && modal.browse.parent && <button className={pickerRowClass} onClick={() => onBrowse(modal.browse.parent)}><UpIcon /><span className="min-w-0 flex-1">..</span></button>}
        {browsing && modal.browse.entries.map(entry => <button className={pickerRowClass} key={entry.path} onClick={() => onBrowse(entry.path)}><FolderIcon /><span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{entry.name}</span></button>)}
        {browsing && modal.browse.entries.length === 0 && !modal.browse.parent && <div className="p-2 text-xs text-faint">Nothing to open here.</div>}
        {!picker && modal.text && <div className="p-2 text-[13px] leading-5 whitespace-pre-wrap text-muted">{modal.text}</div>}
        {!picker && modal.input && <input ref={inputRef} className="my-1 mb-2 w-full rounded-lg border border-line-strong bg-panel px-2.5 py-2 font-mono text-ink outline-none focus:border-accent" type={modal.input.type || 'text'} placeholder={modal.input.placeholder || ''} autoComplete="off" onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); onConfirm(event.currentTarget.value) } }} />}
        {!picker && (modal.rows || []).map((item, index) => <button className={`${pickerRowClass} ${item.selected ? 'bg-selected' : ''}`} key={`${item.label}-${index}`} onClick={item.run}><span className="min-w-0 flex-1"><span className="block overflow-hidden text-ellipsis whitespace-nowrap">{item.label}{item.selected && <span className="text-accent"> ✓</span>}</span><span className="block overflow-hidden text-ellipsis whitespace-nowrap font-mono text-[11px] text-faint">{item.detail || ''}</span></span></button>)}
      </div>
      <div className="flex shrink-0 items-center gap-2 border-t border-line-strong px-4 py-3">
        <span className="min-w-0 flex-1 text-xs text-faint">{note}</span>
        {picker && !browsing && <button className="shrink-0 rounded-full border border-line-strong px-3.5 py-1.75 text-[13px] text-ink hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" onClick={() => onBrowse('')}>Browse…</button>}
        {browsing && <button className="shrink-0 rounded-full bg-accent px-3.5 py-1.75 text-[13px] text-white hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" onClick={onUseFolder}>Use this folder</button>}
        {!picker && modal.confirm && <button className="shrink-0 rounded-full bg-accent px-3.5 py-1.75 text-[13px] text-white hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" onClick={() => onConfirm(inputRef.current?.value || '')}>{modal.confirm.label}</button>}
      </div>
    </div>
  </div>
}
