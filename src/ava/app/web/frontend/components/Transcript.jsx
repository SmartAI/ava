import React, { useEffect, useState } from 'react'

import { STATUS_LABELS } from '../constants'
import { renderMarkdown } from '../markdown'
import { summarize } from '../utils'
import { AttachmentChip } from './AttachmentChip'
import { BashIcon, ChevronIcon, FileIcon } from './Icons'

function ToolDisclosure({ entry }) {
  const [open, setOpen] = useState(Boolean(entry.isError))
  useEffect(() => {
    if (entry.isError) setOpen(true)
  }, [entry.isError])

  const summary = summarize(entry.tool, entry.args)
  let duration = ''
  if (Number.isFinite(entry.elapsed)) {
    duration = entry.elapsed < 1000
      ? `${entry.elapsed}ms`
      : `${(Math.floor(entry.elapsed / 100) / 10).toFixed(1)}s`
  }

  return <div className="min-w-0">
    <button className="flex h-6 w-full min-w-0 items-center rounded-md border-0 bg-transparent p-0 text-left text-sm text-muted hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent" onClick={() => setOpen(value => !value)} aria-expanded={open}>
      <span className="group relative mr-1.5 inline-flex h-4 w-4 shrink-0 items-center justify-center text-quiet">
        {entry.tool === 'bash' ? <BashIcon className="transition-opacity group-hover:opacity-0" /> : <FileIcon className="transition-opacity group-hover:opacity-0" />}
        <span className="opacity-0 transition-opacity group-hover:opacity-100"><ChevronIcon open={open} /></span>
      </span>
      <span className="shrink-0 leading-6">{entry.tool}</span>
      <span className="ml-2 min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap font-mono text-xs text-faint">{summary}</span>
      {duration && <span className="ml-2 shrink-0 font-mono text-xs text-faint">{duration}</span>}
    </button>
    {open && <div className="mt-2 mr-0 mb-1 ml-5.5 overflow-hidden rounded-xl bg-code">
      <div className="border-b border-line-strong px-3.5 py-2 font-mono text-xs leading-5 break-words whitespace-pre-wrap text-muted">{summary || entry.args}</div>
      <pre className={`m-0 max-h-90 overflow-auto px-3.5 py-2.5 font-mono text-xs leading-5 whitespace-pre ${entry.isError ? 'text-danger' : 'text-muted'}`}>{entry.text || '…'}</pre>
    </div>}
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
  if (entry.type === 'tool') return <ToolDisclosure entry={entry} />
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
  return <div className="flex-1 shrink-0 px-4 py-4 min-[701px]:px-8">
    <div className="mx-auto flex w-full max-w-[748px] flex-col gap-4">
      {entries.map(entry => <MessageRow entry={entry} key={entry.id} />)}
      {empty && <EmptyState project={project} />}
      <WorkingStatus status={status} />
    </div>
  </div>
}
