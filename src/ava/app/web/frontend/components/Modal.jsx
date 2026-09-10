import React, { useEffect, useRef } from 'react'

import { iconButton, modalBackdrop, modalBody, modalCard, modalFooter, modalHeader, modalTitle, pickerRow, primaryButton, secondaryButton } from '../ui'
import { useDialogFocus } from '../useDialogFocus'
import { BackIcon, CloseIcon, FolderIcon, UpIcon } from './Icons'

export function Modal({ modal, projects, onClose, onBack, onBrowse, onUseFolder, onStartChat, onConfirm }) {
  const dialogRef = useDialogFocus(Boolean(modal))
  const inputRef = useRef(null)
  const listRef = useRef(null)
  const picker = modal?.kind === 'projects'
  const browsing = picker && modal.browse !== null
  const addOnly = picker && Boolean(modal.addOnly)
  const loading = picker && Boolean(modal.loading)

  useEffect(() => {
    if (modal?.kind === 'generic' && modal.input) inputRef.current?.focus()
    listRef.current?.scrollTo(0, 0)
  }, [modal])

  if (!modal) return null

  const title = picker
    ? (addOnly ? 'Add a project' : (browsing ? 'Choose a folder' : 'Start a new chat'))
    : modal.title
  const note = modal.error || (picker
    ? (loading
        ? 'Loading folders…'
        : (browsing
            ? 'ava runs its tools inside the folder you choose.'
            : (addOnly ? 'Choose a folder to add it as a project.' : 'Chats are grouped by project directory.')))
    : modal.note || '')

  return <div className={`${modalBackdrop} px-4 py-8`} onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <div className={`${modalCard} max-h-[min(560px,calc(100vh-64px))] w-[min(520px,calc(100vw-32px))]`} ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div className={modalHeader}>
        {browsing && !addOnly && <button className={iconButton} title="Back" aria-label="Back" onClick={onBack}><BackIcon /></button>}
        <span className={modalTitle} id="modal-title">{title}</span>
        <button className={iconButton} title="Close" aria-label="Close" onClick={onClose}><CloseIcon /></button>
      </div>
      {browsing && <div className="shrink-0 overflow-hidden text-ellipsis whitespace-nowrap px-4 pb-2.5 font-mono text-xs text-faint">{modal.browse.path}</div>}
      <div className={`${modalBody} px-2 pb-2`} ref={listRef}>
        {picker && !browsing && !addOnly && projects.length === 0 && <div className="p-2 text-xs text-faint">No projects yet - browse for a folder to add one.</div>}
        {picker && !browsing && !addOnly && projects.map(project => <button className={pickerRow} key={project.id} onClick={() => onStartChat(project.id)}><FolderIcon /><span className="min-w-0 flex-1"><span className="block overflow-hidden text-ellipsis whitespace-nowrap">{project.name}</span><span className="block overflow-hidden text-ellipsis whitespace-nowrap font-mono text-[11px] text-faint">{project.path}</span></span></button>)}
        {browsing && modal.browse.parent && <button className={pickerRow} onClick={() => onBrowse(modal.browse.parent)}><UpIcon /><span className="min-w-0 flex-1">..</span></button>}
        {browsing && modal.browse.entries.map(entry => <button className={pickerRow} key={entry.path} onClick={() => onBrowse(entry.path)}><FolderIcon /><span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{entry.name}</span></button>)}
        {browsing && modal.browse.entries.length === 0 && !modal.browse.parent && <div className="p-2 text-xs text-faint">Nothing to open here.</div>}
        {!picker && modal.text && <div className="p-2 text-[13px] leading-5 whitespace-pre-wrap text-muted">{modal.text}</div>}
        {!picker && modal.input && <input ref={inputRef} className="my-1 mb-2 w-full rounded-lg border border-line-strong bg-panel px-2.5 py-2 font-mono text-ink outline-none transition-[border-color,box-shadow] duration-150 focus:border-accent focus:shadow-[0_0_0_3px_color-mix(in_srgb,var(--ava-accent)_20%,transparent)]" type={modal.input.type || 'text'} placeholder={modal.input.placeholder || ''} autoComplete="off" onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); onConfirm(event.currentTarget.value) } }} />}
        {!picker && (modal.rows || []).map((item, index) => <button className={`${pickerRow} ${item.selected ? 'bg-selected' : ''}`} key={`${item.label}-${index}`} onClick={item.run}><span className="min-w-0 flex-1"><span className="block overflow-hidden text-ellipsis whitespace-nowrap">{item.label}{item.selected && <span className="text-accent"> ✓</span>}</span><span className="block overflow-hidden text-ellipsis whitespace-nowrap font-mono text-[11px] text-faint">{item.detail || ''}</span></span></button>)}
      </div>
      <div className={modalFooter}>
        <span className="min-w-0 flex-1 text-xs text-faint">{note}</span>
        {picker && !browsing && !loading && <button className={secondaryButton} onClick={() => onBrowse('')}>{addOnly ? 'Retry' : 'Browse…'}</button>}
        {browsing && <button className={primaryButton} disabled={loading} onClick={onUseFolder}>Use this folder</button>}
        {!picker && modal.confirm && <button className={primaryButton} onClick={() => onConfirm(inputRef.current?.value || '')}>{modal.confirm.label}</button>}
      </div>
    </div>
  </div>
}
