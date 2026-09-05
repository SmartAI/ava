import React, { useEffect, useState } from 'react'

import { activityLabel, groupTranscriptEntries } from '../activity'
import { STATUS_LABELS } from '../constants'
import { renderMarkdown } from '../markdown'
import { summarize } from '../utils'
import { AttachmentChip } from './AttachmentChip'
import { BashIcon, ChevronIcon, FileIcon } from './Icons'

const TOOL_LABELS = {
  bash: 'Run',
  edit: 'Edit',
  read: 'Read',
  write: 'Write',
}

const durationLabel = elapsed => {
  if (!Number.isFinite(elapsed)) return ''
  return elapsed < 1000
    ? `${elapsed}ms`
    : `${(Math.floor(elapsed / 100) / 10).toFixed(1)}s`
}

function ToolResult({ entry }) {
  const summary = summarize(entry.tool, entry.args)
  const duration = durationLabel(entry.elapsed)
  return <div className="overflow-hidden rounded-xl bg-code">
    <div className="flex min-w-0 items-start gap-2 border-b border-line-strong px-3.5 py-2 text-sm text-muted">
      <span className="mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center text-quiet">
        {entry.tool === 'bash' ? <BashIcon /> : <FileIcon />}
      </span>
      <span className="shrink-0 leading-5">{TOOL_LABELS[entry.tool] || entry.tool}</span>
      <span className="min-w-0 flex-1 font-mono text-xs leading-5 break-words whitespace-pre-wrap text-faint">{summary || entry.args}</span>
      {duration && <span className="shrink-0 font-mono text-xs leading-5 text-faint">{duration}</span>}
    </div>
    <pre className={`m-0 max-h-90 overflow-auto px-3.5 py-2.5 font-mono text-xs leading-5 whitespace-pre ${entry.isError ? 'text-danger' : 'text-muted'}`}>{entry.text || '…'}</pre>
  </div>
}

function ActivityDisclosure({ entries }) {
  const hasError = entries.some(entry => entry.isError)
  const [open, setOpen] = useState(hasError)
  useEffect(() => {
    if (hasError) setOpen(true)
  }, [hasError])

  const panelId = `activity-details-${entries[0].id}`
  return <div className="min-w-0">
    <button className="flex h-6 w-full min-w-0 items-center rounded-md border-0 bg-transparent p-0 text-left text-sm text-muted hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent" type="button" onClick={() => setOpen(value => !value)} aria-expanded={open} aria-controls={panelId}>
      <span className="relative mr-1.5 inline-flex h-4 w-4 shrink-0 items-center justify-center text-quiet"><ChevronIcon open={open} /></span>
      <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap leading-6 font-medium">{activityLabel(entries)}</span>
    </button>
    {open && <div className="mt-2 mr-0 mb-1 ml-5.5 flex flex-col gap-2" id={panelId}>
      {entries.map(entry => <ToolResult entry={entry} key={entry.id} />)}
    </div>}
  </div>
}

function ReasoningDisclosure({ entries }) {
  const [open, setOpen] = useState(false)
  const panelId = `reasoning-details-${entries[0].id}`
  const html = entries.map(entry => renderMarkdown(entry.text)).join('')
  return <div className="min-w-0">
    <button className="flex h-6 w-full min-w-0 items-center rounded-md border-0 bg-transparent p-0 text-left text-sm text-muted hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent" type="button" onClick={() => setOpen(value => !value)} aria-expanded={open} aria-controls={panelId}>
      <span className="relative mr-1.5 inline-flex h-4 w-4 shrink-0 items-center justify-center text-quiet"><ChevronIcon open={open} /></span>
      <span className="leading-6 font-medium">Reasoning</span>
    </button>
    {open && <div className="markdown mt-2 mr-0 mb-1 ml-5.5 border-l border-line-strong pl-3 text-sm leading-6 text-muted" id={panelId} dangerouslySetInnerHTML={{ __html: html }} />}
  </div>
}

function MessageRow({ entry }) {
  if (entry.type === 'user') {
    const blocks = entry.blocks || []
    const text = blocks.filter(block => block.kind === 'text').map(block => block.text).join('\n')
    const attachments = blocks.filter(block => block.kind === 'image' || block.kind === 'file_text')
    return <div className="flex justify-end"><div className="max-w-[min(525px,82%)] [overflow-wrap:anywhere] whitespace-pre-wrap rounded-[22px] bg-bubble px-4 py-2.5 text-base leading-6">
      {text}
      {attachments.length > 0 && <span className="mt-2 flex min-w-0 flex-wrap gap-2 overflow-visible">{attachments.map((block, index) => <AttachmentChip key={`${block.display_path}-${index}`} name={block.display_path} kind={block.kind} size={block.byte_size} />)}</span>}
    </div></div>
  }
  if (entry.type === 'assistant') {
    return <div className="markdown text-base leading-6.5 break-words" dangerouslySetInnerHTML={{ __html: renderMarkdown(entry.text) }} />
  }
  if (entry.type === 'reasoning-group') return <ReasoningDisclosure entries={entry.entries} />
  if (entry.type === 'activity') return <ActivityDisclosure entries={entry.entries} />
  if (entry.type === 'error') {
    return <div className="flex items-start gap-2 text-[13px] leading-5"><span className="mt-1.75 h-1.5 w-1.5 shrink-0 rounded-full bg-danger" /><div><span className="mr-1.5 font-semibold text-danger">Error</span><span className="text-muted">{entry.text}</span></div></div>
  }
  return <div className="self-center text-xs text-faint">{entry.text}</div>
}

function WorkingStatus({ status }) {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    setSeconds(0)
    if (status !== 'running') return undefined
    const started = Date.now()
    const timer = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => window.clearInterval(timer)
  }, [status])
  if (!(status in STATUS_LABELS)) return null
  return <div className="working-status inline-flex h-6.5 self-start text-sm font-medium" data-state={status}><span>{STATUS_LABELS[status]}</span><span className="ml-2 font-mono text-[13px] font-normal text-faint [font-variant-numeric:tabular-nums]">{seconds}s</span></div>
}

function EmptyState({ project }) {
  return <div className="mx-auto mt-30 text-center text-sm text-faint">
    <div className="mb-1.5 text-xl font-medium text-ink">{project ? project.name : 'New chat'}</div>
    <div>{project ? `ava will work in ${project.path}` : 'Pick a project directory to start.'}</div>
  </div>
}

export function Transcript({ entries, empty, project, status }) {
  const rows = groupTranscriptEntries(entries)
  return <div className="flex-1 shrink-0 px-4 py-4 min-[701px]:px-8">
    <div className="mx-auto flex w-full max-w-[748px] flex-col gap-4">
      {rows.map(entry => <MessageRow entry={entry} key={entry.id} />)}
      {empty && <EmptyState project={project} />}
      <WorkingStatus status={status} />
    </div>
  </div>
}
