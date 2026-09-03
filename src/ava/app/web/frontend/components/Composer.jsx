import React, { useState } from 'react'

import { BUTTON_MODES, BUTTON_TITLES, COMMAND_HINTS, HINTS, PLACEHOLDERS } from '../constants'
import { pendingAttachments, pendingMessages, pendingPreview, pendingText } from '../pending'
import { AttachmentChip } from './AttachmentChip'
import { ArrowIcon, FolderIcon, PlayIcon, PlusIcon, StopIcon } from './Icons'

function SubmitIcon({ mode }) {
  if (mode === 'pause' || mode === 'abort') return <StopIcon />
  if (mode === 'resume') return <PlayIcon />
  return <ArrowIcon />
}

function PendingRow({ message, disabled, onRevise, onDelete, onSend }) {
  const [editing, setEditing] = useState(false)
  const [revision, setRevision] = useState(() => pendingText(message))
  const [working, setWorking] = useState('')
  const steering = message.target === 'next_step'
  const label = steering ? 'Steering' : 'Follow-up'
  const attachments = pendingAttachments(message)
  const canSave = revision.trim() !== '' || attachments.length > 0

  const run = async (action, callback) => {
    if (working) return false
    setWorking(action)
    try {
      return await callback()
    } finally {
      setWorking('')
    }
  }

  if (editing) return <div className="flex min-w-0 items-start gap-2 px-2.5 py-2">
    <span className={`mt-1 shrink-0 rounded-md px-1.5 py-0.5 text-[11px] font-medium ${steering ? 'bg-bubble text-accent' : 'bg-hover text-muted'}`}>{label}</span>
    <textarea
      className="max-h-28 min-h-8 min-w-0 flex-1 resize-y rounded-lg border border-line-strong bg-transparent px-2 py-1 text-[13px] leading-5 text-ink outline-none focus:border-accent"
      rows="2"
      value={revision}
      disabled={disabled || Boolean(working)}
      onChange={event => setRevision(event.target.value)}
      onKeyDown={event => {
        if (event.key === 'Escape') {
          event.preventDefault()
          event.stopPropagation()
          setEditing(false)
          return
        }
        if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && canSave) {
          event.preventDefault()
          run('save', async () => {
            const saved = await onRevise(message.id, revision)
            if (saved) setEditing(false)
            return saved
          })
        }
      }}
      aria-label={`Revise ${label.toLowerCase()} message`}
      autoFocus
    />
    <div className="flex shrink-0 items-center gap-1">
      <button className="rounded-md border-0 bg-transparent px-1.5 py-1 text-xs text-quiet hover:bg-hover hover:text-ink disabled:opacity-40" type="button" disabled={disabled || Boolean(working)} onClick={() => setEditing(false)}>Cancel</button>
      <button className="rounded-md border-0 bg-accent px-2 py-1 text-xs text-white hover:bg-accent-soft disabled:opacity-40" type="button" disabled={disabled || Boolean(working) || !canSave} onClick={() => run('save', async () => {
        const saved = await onRevise(message.id, revision)
        if (saved) setEditing(false)
        return saved
      })}>{working === 'save' ? 'Saving…' : 'Save'}</button>
    </div>
  </div>

  const preview = pendingPreview(message)
  return <div className="flex h-9 min-w-0 items-center gap-2 px-2.5">
    <span className={`shrink-0 rounded-md px-1.5 py-0.5 text-[11px] font-medium ${steering ? 'bg-bubble text-accent' : 'bg-hover text-muted'}`}>{label}</span>
    <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[13px] text-muted" title={preview}>{preview}</span>
    <div className="flex shrink-0 items-center gap-0.5">
      <button className="rounded-md border-0 bg-transparent px-1.5 py-1 text-xs text-quiet hover:bg-hover hover:text-ink disabled:opacity-40" type="button" disabled={disabled || Boolean(working)} onClick={() => setEditing(true)}>Revise</button>
      <button className="rounded-md border-0 bg-transparent px-1.5 py-1 text-xs text-accent hover:bg-hover disabled:opacity-40" type="button" disabled={disabled || Boolean(working)} onClick={() => run('send', () => onSend(message.id))}>{working === 'send' ? 'Sending…' : 'Send now'}</button>
      <button className="rounded-md border-0 bg-transparent px-1.5 py-1 text-xs text-quiet hover:bg-hover hover:text-danger disabled:opacity-40" type="button" disabled={disabled || Boolean(working)} onClick={() => run('delete', () => onDelete(message.id))}>{working === 'delete' ? 'Deleting…' : 'Delete'}</button>
    </div>
  </div>
}

export function Composer({
  archived,
  currentChat,
  currentProject,
  status,
  submitting,
  controlling,
  unavailable,
  dragging,
  staged,
  pending,
  draft,
  draftRef,
  fileInputRef,
  slashOpen,
  slashCandidates,
  slashActive,
  onDraftChange,
  onDraftBlur,
  onDraftKeyDown,
  onPaste,
  onPickSlash,
  onFiles,
  onRemoveFile,
  onRevisePending,
  onDeletePending,
  onSendPending,
  onOpenProjects,
  onUnarchive,
  onControl,
  onEnter,
  onDragEnter,
  onDragLeave,
  onDrop,
}) {
  const mode = BUTTON_MODES[status] || 'send'
  const title = archived ? 'Archived' : submitting ? 'Sending' : BUTTON_TITLES[mode]
  const messages = pendingMessages(pending)

  return <div
    className={`flex w-full max-w-[780px] flex-col gap-3 rounded-[22px] border bg-panel pt-2.5 shadow-card ${dragging ? 'border-accent ring-1 ring-accent' : 'border-line-strong'}`}
    onDragEnter={onDragEnter}
    onDragOver={onDragEnter}
    onDragLeave={onDragLeave}
    onDrop={onDrop}
  >
    {messages.length > 0 && <div className="mx-2.5 max-h-40 overflow-y-auto rounded-xl border border-line bg-canvas" role="list" aria-label="Queued messages">
      {messages.map(message => <div className="border-b border-line last:border-b-0" role="listitem" key={message.id}><PendingRow message={message} disabled={archived || status === 'aborting'} onRevise={onRevisePending} onDelete={onDeletePending} onSend={onSendPending} /></div>)}
    </div>}
    {staged.length > 0 && <div className="flex min-w-0 gap-2 overflow-x-auto px-4.5">{staged.map(entry => <AttachmentChip key={entry.id} name={entry.file.name} kind={entry.file.type.startsWith('image/') ? 'image' : 'file'} size={entry.file.size} preview={entry.preview} onRemove={() => onRemoveFile(entry.id)} />)}</div>}
    <input ref={fileInputRef} type="file" multiple hidden onChange={event => { onFiles(event.target.files); event.target.value = '' }} />
    {slashOpen && slashCandidates.length > 0 && <div className="mx-2.5 rounded-xl border border-line-strong bg-menu p-1 shadow-card">{slashCandidates.map((name, index) => <button className={`flex w-full items-center gap-2.5 rounded-lg border-0 px-2 py-1.5 text-left hover:bg-hover ${index === slashActive ? 'bg-selected' : 'bg-transparent'}`} type="button" key={name} onMouseDown={event => { event.preventDefault(); onPickSlash(name) }}><span className="min-w-0 flex-1"><span className="block font-mono text-[13px] text-ink">/{name}</span><span className="block overflow-hidden text-ellipsis whitespace-nowrap text-xs text-faint">{COMMAND_HINTS[name].replace(/^\/\S+(\s+\[\S+\])?\s+/, '')}</span></span></button>)}</div>}
    <textarea
      className="max-h-84 resize-none overflow-y-auto border-0 bg-transparent px-4.5 font-sans text-base leading-6 text-ink outline-none placeholder:text-faint disabled:cursor-not-allowed disabled:text-quiet"
      ref={draftRef}
      rows="1"
      value={draft}
      disabled={unavailable}
      placeholder={PLACEHOLDERS[status] || PLACEHOLDERS.idle}
      onChange={event => onDraftChange(event.target.value)}
      onBlur={onDraftBlur}
      onKeyDown={onDraftKeyDown}
      onPaste={onPaste}
      aria-label="Message"
    />
    {archived && <div className="flex items-center gap-3 px-2.5 pl-4.5 text-[13px] text-muted"><span className="flex-1">Archived - unarchive to continue</span><button className="rounded-full border border-line-strong bg-transparent px-2.5 py-1 text-ink hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" onClick={() => onUnarchive(currentChat)}>Unarchive</button></div>}
    <div className="flex items-center gap-2 px-2.5 pb-2.5">
      <button className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-quiet hover:bg-hover hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-40" title="Attach files" aria-label="Attach files" disabled={unavailable} onClick={() => fileInputRef.current?.click()}><PlusIcon /></button>
      <button className="inline-flex max-w-80 items-center gap-1.5 rounded-full bg-hover px-2.5 py-1 text-xs text-quiet hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" title="Invocation directory" onClick={onOpenProjects}><FolderIcon /><span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">{currentProject?.name || 'No project'}</span></button>
      <span className={`flex-1 text-right text-xs max-[700px]:hidden ${status !== 'idle' && !submitting ? 'text-quiet' : 'text-faint'}`}>{submitting ? 'Sending…' : HINTS[status] || HINTS.idle}</span>
      <button className={`inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-40 ${mode === 'pause' ? 'bg-muted' : mode === 'abort' ? 'bg-danger' : mode === 'resume' ? 'bg-success' : 'bg-accent'}`} title={title} aria-label={title} disabled={archived || submitting || controlling || status === 'aborting'} onClick={() => mode === 'pause' || mode === 'abort' ? onControl(mode) : onEnter(false)}><SubmitIcon mode={mode} /></button>
    </div>
  </div>
}
