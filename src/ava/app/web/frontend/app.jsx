import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'

const call = async (method, path, body) => {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = await response.json().then(payload => payload.error).catch(() => '')
    throw new Error(detail || `${method} ${path} returned ${response.status}`)
  }
  return response.status === 204 ? null : response.json()
}

const api = {
  projects: () => call('GET', '/api/projects').then(payload => payload.projects),
  addProject: path => call('POST', '/api/projects', { path }),
  browse: path => call('GET', `/api/fs?path=${encodeURIComponent(path || '')}`),
  createChat: project => call('POST', '/api/chats', { project_id: project }),
  open: id => call('GET', `/api/chats/${id}`),
  message: (id, text, attachments, delivery) =>
    call('POST', `/api/chats/${id}/messages`, { text, attachments, delivery }),
  archive: (id, archived) => call('POST', `/api/chats/${id}/archive`, { archived }),
  cancel: (id, cause) => call('POST', `/api/chats/${id}/cancel`, { cause }),
  resume: id => call('POST', `/api/chats/${id}/resume`),
  models: id => call('GET', `/api/chats/${id}/models`),
  selectModel: (id, body) => call('POST', `/api/chats/${id}/model`, body),
  compact: id => call('POST', `/api/chats/${id}/compact`),
  context: id => call('GET', `/api/chats/${id}/context`),
  skills: id => call('GET', `/api/chats/${id}/skills`).then(payload => payload.skills),
  login: (provider, key) => call('POST', '/api/credentials', { provider, key }),
  logout: provider => call('DELETE', `/api/credentials/${encodeURIComponent(provider)}`),
}

const escapeHtml = text =>
  text.replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[character])

const separatorRow = /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$/

const splitRow = line => {
  const cells = line.split('|')
  if (cells.length && cells[0].trim() === '') cells.shift()
  if (cells.length && cells[cells.length - 1].trim() === '') cells.pop()
  return cells.map(cell => cell.trim())
}

const cellAlign = separator => {
  if (!separator.endsWith(':')) return ''
  return separator.startsWith(':') ? ' style="text-align:center"' : ' style="text-align:right"'
}

const renderTable = lines => {
  const aligns = splitRow(lines[1]).map(cellAlign)
  const row = (cells, tag) =>
    `<tr>${cells.map((cell, index) => `<${tag}${aligns[index] || ''}>${cell}</${tag}>`).join('')}</tr>`
  const head = row(splitRow(lines[0]), 'th')
  const body = lines.slice(2).map(line => row(splitRow(line), 'td')).join('')
  return `<table><thead>${head}</thead><tbody>${body}</tbody></table>`
}

const isTable = lines =>
  lines.length >= 2 && lines.every(line => line.includes('|')) &&
  separatorRow.test(lines[1].trim()) && splitRow(lines[0]).length >= 2

const mathHtml = (raw, tex, display) => {
  if (!window.katex) return escapeHtml(raw)
  return window.katex.renderToString(tex, { displayMode: display, throwOnError: false })
}

const renderMarkdown = source => source
  .split(/```/)
  .map((part, index) => {
    if (index % 2 === 1) {
      const newline = part.indexOf('\n')
      const body = newline === -1 ? part : part.slice(newline + 1)
      return `<pre><code>${escapeHtml(body.replace(/\n$/, ''))}</code></pre>`
    }
    const stash = []
    const hold = html => `\u0000${stash.push(html) - 1}\u0000`
    const guarded = part
      .replace(/`([^`\n]+)`/g, (_, code) => hold(`<code>${escapeHtml(code)}</code>`))
      .replace(/\$\$([^$]+?)\$\$/g, (raw, tex) => hold(mathHtml(raw, tex, true)))
      .replace(/\\\[([\s\S]+?)\\\]/g, (raw, tex) => hold(mathHtml(raw, tex, true)))
      .replace(/\\\(([\s\S]+?)\\\)/g, (raw, tex) => hold(mathHtml(raw, tex, false)))
    return escapeHtml(guarded)
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .split(/\n{2,}/)
      .filter(block => block.trim() !== '')
      .map(block => {
        const lines = block.split('\n').filter(line => line.trim() !== '')
        return isTable(lines) ? renderTable(lines) : `<p>${block.replace(/\n/g, '<br>')}</p>`
      })
      .join('')
      .replace(/\u0000(\d+)\u0000/g, (_, held) => stash[held])
  })
  .join('')

const formatBytes = bytes => {
  const units = ['B', 'KiB', 'MiB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit === 0 ? value : value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

const formatTokenCount = value => {
  if (value < 1000) return String(value)
  if (value < 100000) return `${(value / 1000).toFixed(1).replace(/\.0$/, '')}k`
  return `${Math.round(value / 1000)}k`
}

const summarize = (tool, args) => {
  try {
    const parsed = JSON.parse(args)
    return parsed.command || parsed.path || parsed.file_path || args
  } catch {
    return args
  }
}

const HINTS = {
  idle: 'Enter to send · Shift+Enter for a newline',
  running: 'Enter steers · Alt+Enter queues · Esc pauses',
  pausing: 'Enter steers on resume · Alt+Enter queues · Esc aborts',
  paused: 'Enter resumes (text steers) · Alt+Enter queues',
  aborting: 'Aborting…',
}

const PLACEHOLDERS = {
  idle: 'Ask ava to do something…',
  running: 'Steer the current turn…',
  pausing: 'Steer the resumed turn…',
  paused: 'Continue, or steer the resumed turn…',
  aborting: 'Aborting…',
}

const BUTTON_MODES = { running: 'pause', pausing: 'abort', paused: 'resume' }
const BUTTON_TITLES = {
  send: 'Send',
  pause: 'Pause after the current step',
  abort: 'Abort now',
  resume: 'Resume',
}
const STATUS_LABELS = {
  running: 'Working',
  pausing: 'Pausing · finishing the current step',
  aborting: 'Aborting',
}

const COMMAND_HINTS = {
  model: '/model [ID]  choose the model; applies at the next step',
  effort: '/effort [LEVEL]  set reasoning effort; "none" clears it',
  compact: '/compact  summarize older history now',
  context: '/context  what the model sees now, by kind and size',
  skills: '/skills  list the skills the model can load',
  login: '/login [PROVIDER]  store an API key for a provider',
  logout: '/logout [PROVIDER]  remove a stored API key',
  theme: '/theme  toggle light and dark',
  copy: '/copy [code]  copy the last answer, or its last code block',
  new: '/new  start a fresh chat in this project',
  clear: '/clear  same as /new',
  pause: '/pause  stop after the current step',
  abort: '/abort  stop now and repair history',
  resume: '/resume  continue a paused turn',
  help: '/help  this list',
}

let rowSequence = 0
const row = (type, fields = {}) => ({ id: ++rowSequence, type, ...fields })

function PlusIcon({ size = 16 }) {
  return <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M8 3.5v9M3.5 8h9" /></svg>
}

function ComposeIcon() {
  return <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M11.2 2.3l2.5 2.5L6 12.5l-3.2.7.7-3.2 7.7-7.7z" /></svg>
}

function FolderIcon() {
  return <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M1.8 12.5v-9h4.3l1.4 1.8h6.7v7.2a.8.8 0 01-.8.8H2.6a.8.8 0 01-.8-.8z" /></svg>
}

function CaretIcon() {
  return <svg className="projectChevron" width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M4.5 2.5l3.5 3.5-3.5 3.5" /></svg>
}

function ChevronIcon() {
  return <svg className="chev" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M4.5 2.5l3.5 3.5-3.5 3.5" /></svg>
}

function ArchiveIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 4.5h11M3.5 4.5l.8 8h7.4l.8-8M6 7.5h4M2 2h12v2.5H2z" /></svg>
}

function ThemeIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><circle cx="8" cy="8" r="3.25" /><path d="M8 1v1.5M8 13.5V15M15 8h-1.5M2.5 8H1M12.95 3.05l-1.06 1.06M4.11 11.89l-1.06 1.06M12.95 12.95l-1.06-1.06M4.11 4.11L3.05 3.05" strokeLinecap="round" /></svg>
}

function MenuIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M2.5 4h11M2.5 8h11M2.5 12h11" /></svg>
}

function CloseIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M4 4l8 8M12 4l-8 8" /></svg>
}

function BackIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M9.5 3.5L5 8l4.5 4.5" /></svg>
}

function UpIcon() {
  return <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M8 12.5v-9M4 7.5L8 3.5l4 4" /></svg>
}

function ImageIcon() {
  return <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2" y="2.5" width="12" height="11" rx="1.5" /><circle cx="5.5" cy="6" r="1.2" /><path d="M3.5 11l3-3 2.2 2.2 1.4-1.4 2.4 2.2" /></svg>
}

function FileIcon({ glyph = false }) {
  return <svg className={glyph ? 'glyph' : undefined} width={glyph ? 13 : 14} height={glyph ? 13 : 14} viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M8 1.5H4a1 1 0 00-1 1v9a1 1 0 001 1h6a1 1 0 001-1V4.5L8 1.5z" /><path d="M8 1.5v3h3" /></svg>
}

function BashIcon() {
  return <svg className="glyph" width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 4l2.5 2.5L3 9M7.5 10h3.5" /></svg>
}

function SendIcon() {
  return <>
    <svg className="arrow" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M8 12.5v-9M4 7.5L8 3.5l4 4" /></svg>
    <svg className="square" width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><rect x="5" y="5" width="6" height="6" rx="1.2" /></svg>
    <svg className="play" width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M5 3.5v9l7-4.5z" /></svg>
  </>
}

function AttachmentChip({ name, kind, size, preview, onRemove }) {
  return <span className="chip attachmentChip">
    {preview
      ? <img src={preview} alt="" />
      : <span className="attachmentGlyph">{kind === 'image' ? <ImageIcon /> : <FileIcon />}</span>}
    <span className="attachmentCopy">
      <span className="attachmentName">{name}</span>
      <span className="attachmentDetail">{kind === 'image' ? 'Image' : 'File'} · {formatBytes(size)}</span>
    </span>
    {onRemove && <button className="iconBtn attachmentRemove" type="button" aria-label={`Remove ${name}`} onClick={onRemove}>×</button>}
  </span>
}

function ChatRow({ chat, current, status, onSelect, onArchive }) {
  const label = chat.archived ? 'Unarchive chat' : 'Archive chat'
  return <div className="chatGroup" data-selected={current || undefined}>
    <button className="row chat" onClick={onSelect}>
      <span className="dot" data-state={current && status !== 'idle' ? status : undefined} />
      <span className="rowName">{chat.title || 'New chat'}</span>
    </button>
    <button className="iconBtn archiveChat" type="button" aria-label={label} title={label} onClick={onArchive}><ArchiveIcon /></button>
  </div>
}

function Rail({ projects, current, status, open, archiveOpen, mobileOpen, onNew, onToggleProject, onToggleArchive, onSelect, onArchive }) {
  return <aside id="rail" data-mobile-open={mobileOpen || undefined}>
    <div className="railHead">
      <span className="wordmark">ava</span>
      <button className="iconBtn" title="New chat" aria-label="New chat" onClick={onNew}><PlusIcon /></button>
    </div>
    <div className="railList">
      <button className="row newChat" onClick={onNew}><span className="glyphSlot"><ComposeIcon /></span><span className="rowName">New chat</span></button>
      <div className="railGroup">Projects</div>
      {projects.map(project => {
        const expanded = open.has(project.id)
        const archived = project.chats.filter(chat => chat.archived)
        const archivedExpanded = archiveOpen.has(project.id)
        return <div className="projectBlock" data-open={expanded || undefined} key={project.id}>
          <button className="row project" data-open={expanded || undefined} title={project.path} onClick={() => onToggleProject(project.id)}>
            <span className="glyphSlot"><CaretIcon /></span>
            <span className="glyphSlot"><FolderIcon /></span>
            <span className="rowName">{project.name}</span>
          </button>
          <div className="chats">
            {project.chats.filter(chat => !chat.archived).map(chat => <ChatRow key={chat.id} chat={chat} current={chat.id === current} status={status} onSelect={() => onSelect(chat.id)} onArchive={() => onArchive(chat, true)} />)}
            {archived.length > 0 && <div className="archivedGroup" data-open={archivedExpanded || undefined}>
              <button className="row archivedToggle" aria-expanded={archivedExpanded} onClick={() => onToggleArchive(project.id)}><span className="rowName">Archived</span><CaretIcon /></button>
              <div className="archivedList">
                {archived.map(chat => <ChatRow key={chat.id} chat={chat} current={chat.id === current} status={status} onSelect={() => onSelect(chat.id)} onArchive={() => onArchive(chat, false)} />)}
              </div>
            </div>}
          </div>
        </div>
      })}
    </div>
  </aside>
}

function ToolRow({ entry }) {
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
  return <div className="tool" data-open={open || undefined}>
    <button className="toolRow" onClick={() => setOpen(value => !value)}>
      <span className="leading">{entry.tool === 'bash' ? <BashIcon /> : <FileIcon glyph />}<ChevronIcon /></span>
      <span className="toolName">{entry.tool}</span>
      <span className="toolArg">{summary}</span>
      <span className="toolDuration">{duration}</span>
    </button>
    <div className="toolBody"><div className="term" data-error={entry.isError || undefined}><div className="termHead">{summary || entry.args}</div><pre className="termOut">{entry.text || '…'}</pre></div></div>
  </div>
}

function TranscriptRow({ entry }) {
  if (entry.type === 'user') {
    const blocks = entry.blocks || []
    const text = blocks.filter(block => block.kind === 'text').map(block => block.text).join('\n')
    const attachments = blocks.filter(block => block.kind === 'image' || block.kind === 'file_text')
    return <div className="userRow"><div className="bubble">
      {text}
      {attachments.length > 0 && <span className="attachments">{attachments.map((block, index) => <AttachmentChip key={`${block.display_path}-${index}`} name={block.display_path} kind={block.kind} size={block.byte_size} />)}</span>}
    </div></div>
  }
  if (entry.type === 'assistant') {
    return <div className="assistant" dangerouslySetInnerHTML={{ __html: renderMarkdown(entry.text) }} />
  }
  if (entry.type === 'tool') return <ToolRow entry={entry} />
  if (entry.type === 'error') {
    return <div className="errorRow"><span className="dot" /><div><span className="errorTitle">Error</span><span className="errorText">{entry.text}</span></div></div>
  }
  return <div className="notice">{entry.text}</div>
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
  return <div className="status" data-state={status}><span className="statusLabel">{STATUS_LABELS[status]}</span><span className="clock">{seconds}s</span></div>
}

function EmptyState({ project }) {
  return <div className="empty">
    <div className="emptyTitle">{project ? project.name : 'New chat'}</div>
    <div className="emptySub">{project ? `ava will work in ${project.path}` : 'Pick a project directory to start.'}</div>
  </div>
}

function Modal({ modal, projects, onClose, onBack, onBrowse, onUseFolder, onStartChat, onConfirm }) {
  const inputRef = useRef(null)
  const listRef = useRef(null)
  const picker = modal?.kind === 'projects'
  const browsing = picker && modal.browse !== null

  useEffect(() => {
    if (modal?.kind === 'generic' && modal.input) inputRef.current?.focus()
    listRef.current?.scrollTo(0, 0)
  }, [modal])

  if (!modal) return <div id="overlay" />

  const title = picker ? (browsing ? 'Choose a folder' : 'Start a new chat') : modal.title
  const note = modal.error || (picker
    ? (browsing ? 'ava runs its tools inside the folder you choose.' : 'Chats are grouped by project directory.')
    : modal.note || '')

  return <div id="overlay" data-open onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <div className="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
      <div className="modalHead">
        {browsing && <button className="iconBtn" title="Back" aria-label="Back" onClick={onBack}><BackIcon /></button>}
        <span className="modalTitle" id="modalTitle">{title}</span>
        <button className="iconBtn" title="Close" aria-label="Close" onClick={onClose}><CloseIcon /></button>
      </div>
      {browsing && <div className="modalPath">{modal.browse.path}</div>}
      <div className="modalList" ref={listRef}>
        {picker && !browsing && projects.length === 0 && <div className="modalNote" style={{ padding: 8 }}>No projects yet - browse for a folder to add one.</div>}
        {picker && !browsing && projects.map(project => <button className="pick" key={project.id} onClick={() => onStartChat(project.id)}><FolderIcon /><span className="pickText"><span className="pickName">{project.name}</span><span className="pickPath">{project.path}</span></span></button>)}
        {browsing && modal.browse.parent && <button className="pick" onClick={() => onBrowse(modal.browse.parent)}><UpIcon /><span className="pickText"><span className="pickName">..</span></span></button>}
        {browsing && modal.browse.entries.map(entry => <button className="pick" key={entry.path} onClick={() => onBrowse(entry.path)}><FolderIcon /><span className="pickText"><span className="pickName">{entry.name}</span></span></button>)}
        {browsing && modal.browse.entries.length === 0 && !modal.browse.parent && <div className="modalNote" style={{ padding: 8 }}>Nothing to open here.</div>}
        {!picker && modal.text && <div className="modalText">{modal.text}</div>}
        {!picker && modal.input && <input ref={inputRef} className="modalInput" type={modal.input.type || 'text'} placeholder={modal.input.placeholder || ''} autoComplete="off" onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); onConfirm(event.currentTarget.value) } }} />}
        {!picker && (modal.rows || []).map((item, index) => <button className="pick" data-selected={item.selected || undefined} key={`${item.label}-${index}`} onClick={item.run}><span className="pickText"><span className="pickName">{item.label}</span><span className="pickPath">{item.detail || ''}</span></span></button>)}
      </div>
      <div className="modalFoot">
        <span className="modalNote">{note}</span>
        {picker && !browsing && <button className="button" onClick={() => onBrowse('')}>Browse…</button>}
        {browsing && <button className="button" data-primary onClick={onUseFolder}>Use this folder</button>}
        {!picker && modal.confirm && <button className="button" data-primary onClick={() => onConfirm(inputRef.current?.value || '')}>{modal.confirm.label}</button>}
      </div>
    </div>
  </div>
}

function App() {
  const [projects, setProjects] = useState([])
  const [current, setCurrent] = useState(null)
  const [projectId, setProjectId] = useState(null)
  const [openProjects, setOpenProjects] = useState(new Set())
  const [archiveOpen, setArchiveOpen] = useState(new Set())
  const [status, setStatus] = useState('idle')
  const [statusInfo, setStatusInfo] = useState(null)
  const [transcript, setTranscript] = useState([])
  const [empty, setEmpty] = useState(true)
  const [pending, setPending] = useState(new Map())
  const [modelSelection, setModelSelection] = useState(null)
  const [modal, setModal] = useState(null)
  const [draft, setDraft] = useState('')
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashActive, setSlashActive] = useState(0)
  const [staged, setStaged] = useState([])
  const [dragging, setDragging] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [controlling, setControlling] = useState(false)
  const [railOpen, setRailOpen] = useState(false)

  const currentRef = useRef(null)
  const selectionRef = useRef(0)
  const streamRef = useRef(null)
  const displayedInputsRef = useRef(new Set())
  const tailRef = useRef(null)
  const lastAssistantRef = useRef('')
  const modelSelectionRef = useRef(null)
  const projectIdRef = useRef(null)
  const statusRef = useRef('idle')
  const controllingRef = useRef(false)
  const submittingRef = useRef(false)
  const stagedRef = useRef([])
  const draftRef = useRef(null)
  const fileInputRef = useRef(null)
  const scrollRef = useRef(null)
  const followRef = useRef(true)
  const forceFollowRef = useRef(false)
  const pastedSequenceRef = useRef(0)

  currentRef.current = current
  projectIdRef.current = projectId
  statusRef.current = status
  controllingRef.current = controlling
  submittingRef.current = submitting
  stagedRef.current = staged
  modelSelectionRef.current = modelSelection

  const currentProject = projects.find(project => project.id === projectId) || null
  const currentChat = projects.flatMap(project => project.chats).find(chat => chat.id === current) || null
  const archived = Boolean(currentChat?.archived)
  const unavailable = archived || submitting || status === 'aborting'
  const buttonMode = BUTTON_MODES[status] || 'send'
  const buttonTitle = archived ? 'Archived' : submitting ? 'Sending' : BUTTON_TITLES[buttonMode]

  const slashCandidates = useMemo(() => {
    if (!/^\/[a-z]*$/.test(draft)) return []
    const prefix = draft.slice(1)
    return Object.keys(COMMAND_HINTS).filter(name => name.startsWith(prefix))
  }, [draft])

  const updateChat = updated => {
    setProjects(items => items.map(project => ({
      ...project,
      chats: project.chats.map(chat => chat.id === updated.id ? { ...chat, ...updated } : chat),
    })))
  }

  const addTranscript = entry => {
    setEmpty(false)
    setTranscript(items => [...items, entry])
  }

  const addAside = entry => {
    setEmpty(false)
    setTranscript(items => {
      const tailId = tailRef.current
      const index = tailId === null ? -1 : items.findIndex(item => item.id === tailId)
      if (index === -1) return [...items, entry]
      return [...items.slice(0, index), entry, ...items.slice(index)]
    })
  }

  const addNotice = text => addAside(row('notice', { text }))
  const addError = error => addAside(row('error', { text: String(error?.message || error) }))
  const closeTail = () => { tailRef.current = null }

  const addUser = blocks => {
    closeTail()
    forceFollowRef.current = true
    addTranscript(row('user', { blocks: blocks || [] }))
  }

  const appendDelta = delta => {
    setEmpty(false)
    if (tailRef.current === null) {
      const entry = row('assistant', { text: delta })
      tailRef.current = entry.id
      setTranscript(items => [...items, entry])
      return
    }
    const id = tailRef.current
    setTranscript(items => items.map(item => item.id === id ? { ...item, text: item.text + delta } : item))
  }

  const applyEvent = event => {
    if (event.kind === 'inbox/spliced') {
      setPending(items => {
        const next = new Map(items)
        for (const message of event.inserted || []) next.set(message.id, event.target)
        return next
      })
      return
    }
    if (event.kind === 'step/claimed') {
      for (const message of event.messages || []) {
        if (!message.id || !displayedInputsRef.current.has(message.id)) addUser(message.blocks)
        if (message.id) displayedInputsRef.current.add(message.id)
      }
      setPending(items => {
        const next = new Map(items)
        for (const message of event.messages || []) if (message.id) next.delete(message.id)
        return next
      })
      return
    }
    if (event.kind === 'user/message') addUser(event.blocks)
    else if (event.kind === 'selection') {
      setModelSelection({ provider: event.provider, model: event.model, effort: event.effort ?? null })
      if (event.warning) addError(event.warning)
    } else if (event.kind === 'assistant/chunk') appendDelta(event.delta)
    else if (event.kind === 'assistant/message') {
      const blocks = event.blocks || []
      const text = blocks.filter(block => block.kind === 'text').map(block => block.text).join('')
      if (text) {
        if (tailRef.current === null) appendDelta(text)
        else {
          const id = tailRef.current
          setTranscript(items => items.map(item => item.id === id ? { ...item, text } : item))
        }
        lastAssistantRef.current = text
      }
      for (const block of blocks.filter(block => block.kind === 'tool_call')) {
        closeTail()
        addTranscript(row('tool', {
          callId: block.call_id,
          tool: block.tool_name,
          args: block.arguments_json,
          text: '',
          isError: false,
          elapsed: null,
        }))
      }
    } else if (event.kind === 'tool/result') {
      const durations = new Map((event.durations || []).map(item => [item.call_id, item.elapsed_ms]))
      setTranscript(items => items.map(item => {
        const block = (event.blocks || []).find(value => value.kind === 'tool_result' && value.call_id === item.callId)
        return block ? { ...item, text: block.text || '(no output)', isError: Boolean(block.is_error), elapsed: durations.get(block.call_id) } : item
      }))
    } else if (event.kind === 'step/end') closeTail()
    else if (event.kind === 'turn/end') {
      closeTail()
      if (event.reason === 'user_pause') addNotice('Paused after a completed step')
      else if (event.reason === 'user_abort') {
        setPending(new Map())
        addNotice('Aborted; history repaired')
      } else if (event.reason === 'interrupted') addNotice('The previous run was interrupted; the session was repaired')
    } else if (event.kind === 'compaction/seed') addNotice('Context compacted')
    else if (event.kind === 'compaction/failed' || event.kind === 'drive/error') addError(event.message)
  }

  const openStream = (id, selection) => {
    streamRef.current?.close()
    const stream = new EventSource(`/api/chats/${id}/events`)
    stream.onmessage = message => {
      if (currentRef.current !== id || selectionRef.current !== selection) return
      applyEvent(JSON.parse(message.data))
    }
    stream.addEventListener('status', message => {
      if (currentRef.current !== id || selectionRef.current !== selection) return
      const info = JSON.parse(message.data)
      setStatus(info.status)
      setStatusInfo(info)
      setModelSelection({ provider: info.provider, model: info.model, effort: info.effort ?? null })
    })
    streamRef.current = stream
  }

  const selectChat = async id => {
    setRailOpen(false)
    const selection = ++selectionRef.current
    currentRef.current = id
    setCurrent(id)
    streamRef.current?.close()
    streamRef.current = null
    displayedInputsRef.current = new Set()
    tailRef.current = null
    lastAssistantRef.current = ''
    setPending(new Map())
    setModelSelection(null)
    setStatusInfo(null)
    setTranscript([])
    setEmpty(false)

    try {
      const chat = await api.open(id)
      if (selectionRef.current !== selection) return
      setProjectId(chat.project_id)
      projectIdRef.current = chat.project_id
      setOpenProjects(items => new Set(items).add(chat.project_id))
      updateChat({ id: chat.id, title: chat.title, status: chat.status, archived: chat.archived })
      for (const event of chat.events || []) applyEvent(event)
      setEmpty(!(chat.events || []).length)
      setStatus(chat.status)
      openStream(id, selection)
      if (!chat.archived) window.setTimeout(() => draftRef.current?.focus(), 0)
    } catch (error) {
      addError(error)
    }
  }

  const openPicker = () => {
    setRailOpen(false)
    setModal({ kind: 'projects', browse: null, error: '' })
  }
  const closeModal = () => setModal(null)

  const browse = async path => {
    try {
      const listing = await api.browse(path)
      setModal({ kind: 'projects', browse: listing, error: '' })
    } catch (error) {
      setModal(value => ({ ...value, error: String(error.message || error) }))
    }
  }

  const startChat = async id => {
    try {
      const created = await api.createChat(id)
      setProjects(items => items.map(project => project.id === id
        ? { ...project, chats: [created, ...project.chats] }
        : project))
      setOpenProjects(items => new Set(items).add(id))
      closeModal()
      await selectChat(created.id)
    } catch (error) {
      setModal(value => ({ ...value, error: String(error.message || error) }))
    }
  }

  const useFolder = async () => {
    if (modal?.kind !== 'projects' || !modal.browse) return
    try {
      const added = await api.addProject(modal.browse.path)
      setProjects(items => {
        const exists = items.some(project => project.id === added.id)
        return exists ? items : [{ ...added, chats: added.chats || [] }, ...items]
      })
      await startChat(added.id)
    } catch (error) {
      setModal(value => ({ ...value, error: String(error.message || error) }))
    }
  }

  const setArchived = async (chat, value) => {
    try {
      const updated = await api.archive(chat.id, value)
      updateChat(updated)
      if (currentRef.current === chat.id) setStatus(updated.status)
    } catch (error) {
      if (currentRef.current === chat.id) addError(error)
      else console.error(error)
    }
  }

  const showModal = config => setModal({ kind: 'generic', ...config, error: '' })

  const applySelection = async body => {
    if (!currentRef.current) return
    try {
      const selection = await api.selectModel(currentRef.current, body)
      setModelSelection(selection)
      addNotice(`Next step uses ${selection.model}${selection.effort ? ` with effort ${selection.effort}` : ''}`)
    } catch (error) {
      addError(error)
    }
  }

  const toggleTheme = () => {
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches
    const active = document.documentElement.dataset.theme || (dark ? 'dark' : 'light')
    const next = active === 'dark' ? 'light' : 'dark'
    document.documentElement.dataset.theme = next
    localStorage.setItem('ava-theme', next)
  }

  const control = async action => {
    if (!currentRef.current || controllingRef.current) return
    controllingRef.current = true
    setControlling(true)
    try {
      if (action === 'resume') await api.resume(currentRef.current)
      else await api.cancel(currentRef.current, action)
    } catch (error) {
      addError(error)
    } finally {
      controllingRef.current = false
      setControlling(false)
    }
  }

  const runCommand = async line => {
    const match = line.match(/^\/(\S+)\s*(.*)$/)
    const name = match?.[1]
    const argument = match?.[2].trim() || ''
    if (!name || !(name in COMMAND_HINTS)) {
      addError(`Unknown command ${line.split(' ')[0]}; try /help`)
      return
    }
    try {
      if (name === 'model') {
        if (!currentRef.current) return
        if (argument) return await applySelection({ model: argument })
        const listed = await api.models(currentRef.current)
        showModal({
          title: 'Choose a model',
          note: listed.catalog_available
            ? `${listed.provider}: the provider catalog plus configured aliases`
            : `${listed.provider}: the catalog was unavailable; configured choices only`,
          rows: listed.models.map(model => ({ label: model, selected: model === listed.model, run: () => { closeModal(); applySelection({ model }) } })),
        })
      } else if (name === 'effort') {
        if (!currentRef.current) return
        if (argument) return await applySelection({ effort: argument === 'none' ? null : argument })
        const listed = await api.models(currentRef.current)
        const values = listed.effort_values || []
        if (!values.length) return addNotice('The current model does not advertise reasoning effort')
        showModal({
          title: 'Reasoning effort',
          note: listed.model,
          rows: [...values, 'none'].map(value => ({ label: value, selected: (listed.effort ?? 'none') === value, run: () => { closeModal(); applySelection({ effort: value === 'none' ? null : value }) } })),
        })
      } else if (name === 'compact') {
        if (!currentRef.current) return
        addNotice((await api.compact(currentRef.current)).message)
      } else if (name === 'context') {
        if (!currentRef.current) return
        const report = await api.context(currentRef.current)
        const total = report.estimated_tokens || 1
        const tokens = value => value.toLocaleString()
        const share = value => {
          const percent = Math.round((value / total) * 100)
          return percent === 0 && value > 0 ? '<1%' : `${percent}%`
        }
        const budget = report.context_window
          ? ` · window ${tokens(report.context_window)} (compacts at ${report.threshold_percent}%)`
          : ' · window unknown (compaction off)'
        const measured = report.measured_input_tokens === null
          ? 'no provider measurement yet'
          : `last request measured ${tokens(report.measured_input_tokens)} input tokens`
        showModal({
          title: `Context · ~${tokens(report.estimated_tokens)} tokens`,
          note: `Estimated from bytes; ${measured}${budget}${report.compacted ? ' · the window starts at the newest compaction summary' : ''}`,
          rows: [...report.sections].sort((a, b) => b.tokens - a.tokens).map(section => ({
            label: `${section.label} - ${tokens(section.tokens)} tokens · ${share(section.tokens)}`,
            detail: `${section.count} item${section.count === 1 ? '' : 's'} · ${formatBytes(section.bytes)}${section.kind === 'attachment_images' ? ' · images are priced at a fixed 1,200 tokens each' : ''}`,
            run: () => {},
          })),
        })
      } else if (name === 'skills') {
        if (!currentRef.current) return
        const skills = await api.skills(currentRef.current)
        showModal({
          title: 'Skills',
          note: skills.length ? 'The model reads a skill only when it decides it is relevant.' : 'No skills found in .agents/skills or ~/.codex/skills.',
          rows: skills.map(skill => ({ label: `${skill.name} [${skill.scope}]`, detail: `${skill.description} - ${skill.path}`, run: () => {} })),
        })
      } else if (name === 'login') {
        const provider = argument || modelSelectionRef.current?.provider || ''
        showModal({
          title: `API key for ${provider || 'a provider'}`,
          note: 'Stored in $AVA_HOME/auth.json (0600); an environment variable still takes precedence.',
          input: { type: 'password', placeholder: provider ? `${provider} API key` : 'first /login PROVIDER' },
          confirm: {
            label: 'Save',
            run: async key => {
              if (!provider || !key) return
              try {
                const result = await api.login(provider, key)
                closeModal()
                const failed = Object.entries(result.failed)
                addNotice(`Stored a key for ${provider}${failed.length ? `; not reloaded: ${failed.map(([id, why]) => `${id} (${why})`).join(', ')}` : ''}`)
              } catch (error) {
                setModal(value => ({ ...value, error: String(error.message || error) }))
              }
            },
          },
        })
      } else if (name === 'logout') {
        const provider = argument || modelSelectionRef.current?.provider
        if (!provider) return addNotice('Which provider? /logout PROVIDER')
        await api.logout(provider)
        addNotice(`Removed the stored key for ${provider}`)
      } else if (name === 'theme') toggleTheme()
      else if (name === 'copy') {
        let text = lastAssistantRef.current
        if (!text) return addNotice('Nothing to copy yet')
        if (argument === 'code') {
          const blocks = [...text.matchAll(/```[^\n]*\n([\s\S]*?)```/g)]
          if (!blocks.length) return addNotice('The last answer has no fenced code block')
          text = blocks[blocks.length - 1][1]
        }
        await navigator.clipboard.writeText(text)
        addNotice(argument === 'code' ? 'Copied the last code block' : 'Copied the last answer')
      } else if (name === 'new' || name === 'clear') {
        projectIdRef.current ? await startChat(projectIdRef.current) : openPicker()
      } else if (name === 'pause' || name === 'abort' || name === 'resume') await control(name)
      else if (name === 'help') {
        showModal({
          title: 'Commands',
          note: 'Enter steers a running turn · Alt+Enter queues a follow-up · Esc pauses, then aborts',
          text: Object.values(COMMAND_HINTS).join('\n'),
        })
      }
    } catch (error) {
      addError(error)
    }
  }

  const clearStaged = () => {
    for (const entry of stagedRef.current) if (entry.preview) URL.revokeObjectURL(entry.preview)
    stagedRef.current = []
    setStaged([])
  }

  const stageFiles = files => {
    if (unavailable) return false
    const additions = []
    for (let file of files) {
      if (!file.name.trim()) {
        const rawSubtype = (file.type.split('/')[1] || 'bin').split(/[+;]/)[0]
        const subtype = rawSubtype.replace(/[^a-z0-9]/gi, '').toLowerCase() || 'bin'
        file = new File([file], `pasted-${++pastedSequenceRef.current}.${subtype}`, { type: file.type, lastModified: file.lastModified })
      }
      additions.push({ id: `${Date.now()}-${Math.random()}`, file, preview: file.type.startsWith('image/') ? URL.createObjectURL(file) : '' })
    }
    if (!additions.length) return false
    setStaged(items => [...items, ...additions])
    return true
  }

  const removeStaged = id => {
    setStaged(items => {
      const entry = items.find(item => item.id === id)
      if (entry?.preview) URL.revokeObjectURL(entry.preview)
      return items.filter(item => item.id !== id)
    })
  }

  const readBase64 = file => new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ''))
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`))
    reader.readAsDataURL(file)
  })

  const submit = async (delivery, { resume = false } = {}) => {
    if (submittingRef.current || currentChat?.archived) return
    const text = draft.trim()
    if (text === '' && stagedRef.current.length === 0) return
    if (!currentRef.current) {
      openPicker()
      return
    }
    submittingRef.current = true
    setSubmitting(true)
    setEmpty(false)
    try {
      const chatId = currentRef.current
      const attachments = await Promise.all(stagedRef.current.map(async entry => ({
        kind: entry.file.type.startsWith('image/') ? 'image' : 'file',
        name: entry.file.name,
        data_base64: await readBase64(entry.file),
      })))
      const accepted = await api.message(chatId, text, attachments, delivery)
      clearStaged()
      setDraft('')
      updateChat(accepted.chat)
      if (resume) await api.resume(chatId)
    } catch (error) {
      addError(error)
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  const onEnter = altKey => {
    const line = draft.trim()
    if (line.startsWith('/') && stagedRef.current.length === 0) {
      setDraft('')
      setSlashOpen(false)
      runCommand(line)
      return
    }
    const isEmpty = line === '' && stagedRef.current.length === 0
    if (statusRef.current === 'paused') {
      if (altKey) {
        if (!isEmpty) submit('followup')
        return
      }
      if (isEmpty) control('resume')
      else submit('steer', { resume: true })
      return
    }
    if (!isEmpty) submit(altKey || statusRef.current === 'idle' ? 'followup' : 'steer')
  }

  const pickSlash = name => {
    setDraft(`/${name} `)
    setSlashOpen(false)
    window.setTimeout(() => draftRef.current?.focus(), 0)
  }

  const onDraftKeyDown = event => {
    if (slashOpen && slashCandidates.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        setSlashActive(index => (index + (event.key === 'ArrowDown' ? 1 : slashCandidates.length - 1)) % slashCandidates.length)
        return
      }
      if (event.key === 'Tab') {
        event.preventDefault()
        pickSlash(slashCandidates[slashActive])
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        setSlashOpen(false)
        return
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        const command = slashCandidates[slashActive]
        setDraft('')
        setSlashOpen(false)
        runCommand(`/${command}`)
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      onEnter(event.altKey)
    }
  }

  useEffect(() => {
    let active = true
    api.projects().catch(() => []).then(items => {
      if (!active) return
      setProjects(items)
      const first = items.flatMap(project => project.chats)[0]
      if (first) selectChat(first.id)
      else {
        setEmpty(true)
        openPicker()
        window.setTimeout(() => draftRef.current?.focus(), 0)
      }
    })
    return () => {
      active = false
      streamRef.current?.close()
      for (const entry of stagedRef.current) if (entry.preview) URL.revokeObjectURL(entry.preview)
    }
  }, [])

  useEffect(() => {
    const preventDrop = event => event.preventDefault()
    const onKey = event => {
      if (event.key !== 'Escape') return
      if (modal) {
        setModal(null)
        return
      }
      if (railOpen) {
        setRailOpen(false)
        return
      }
      if (slashOpen) {
        setSlashOpen(false)
        return
      }
      if (statusRef.current === 'running') control('pause')
      else if (statusRef.current === 'pausing') control('abort')
    }
    window.addEventListener('dragover', preventDrop)
    window.addEventListener('drop', preventDrop)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('dragover', preventDrop)
      window.removeEventListener('drop', preventDrop)
      window.removeEventListener('keydown', onKey)
    }
  }, [modal, railOpen, slashOpen])

  useEffect(() => {
    if (slashCandidates.length) {
      setSlashOpen(true)
      setSlashActive(index => Math.min(index, slashCandidates.length - 1))
    } else {
      setSlashOpen(false)
      setSlashActive(0)
    }
  }, [slashCandidates])

  useLayoutEffect(() => {
    const textarea = draftRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 336)}px`
  }, [draft])

  useLayoutEffect(() => {
    const scroll = scrollRef.current
    if (!scroll) return
    if (forceFollowRef.current || followRef.current) scroll.scrollTop = scroll.scrollHeight
    forceFollowRef.current = false
  }, [transcript, status])

  const pendingCounts = { next_turn: 0, next_step: 0 }
  for (const target of pending.values()) pendingCounts[target] += 1
  const contextAvailable = Number.isFinite(statusInfo?.context_remaining_percent) && statusInfo?.context_window_tokens
  const modelTitle = modelSelection
    ? `${modelSelection.provider} · ${modelSelection.model}${modelSelection.effort ? ` · effort ${modelSelection.effort}` : ''} (/model)`
    : ''

  return <>
    <Rail
      projects={projects}
      current={current}
      status={status}
      open={openProjects}
      archiveOpen={archiveOpen}
      mobileOpen={railOpen}
      onNew={openPicker}
      onToggleProject={id => setOpenProjects(items => { const next = new Set(items); next.has(id) ? next.delete(id) : next.add(id); return next })}
      onToggleArchive={id => setArchiveOpen(items => { const next = new Set(items); next.has(id) ? next.delete(id) : next.add(id); return next })}
      onSelect={selectChat}
      onArchive={setArchived}
    />
    {railOpen && <button className="railBackdrop" aria-label="Close navigation" onClick={() => setRailOpen(false)} />}
    <main>
      <header>
        <button className="iconBtn mobileMenu" title="Open navigation" aria-label="Open navigation" onClick={() => setRailOpen(true)}><MenuIcon /></button>
        <span className="dot" data-state={status} />
        <span id="title">{currentChat ? currentChat.title || 'New chat' : 'No chat'}</span>
        <button className="iconBtn" title="Toggle theme" aria-label="Toggle theme" onClick={toggleTheme}><ThemeIcon /></button>
      </header>
      <div id="scroll" ref={scrollRef} onScroll={event => {
        const target = event.currentTarget
        followRef.current = target.scrollHeight - target.scrollTop - target.clientHeight < 120
      }}>
        <div id="flow"><div id="column">
          {transcript.map(entry => <TranscriptRow entry={entry} key={entry.id} />)}
          {empty && <EmptyState project={currentProject} />}
          <WorkingStatus status={status} />
        </div></div>
        <div id="seat">
          <div
            id="card"
            data-drag={dragging || undefined}
            onDragEnter={event => { event.preventDefault(); if (!unavailable) setDragging(true) }}
            onDragOver={event => { event.preventDefault(); if (!unavailable) setDragging(true) }}
            onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false) }}
            onDrop={event => { event.preventDefault(); event.stopPropagation(); setDragging(false); stageFiles(event.dataTransfer?.files || []) }}
          >
            {staged.length > 0 && <div className="attachments" id="attachmentTray">{staged.map(entry => <AttachmentChip key={entry.id} name={entry.file.name} kind={entry.file.type.startsWith('image/') ? 'image' : 'file'} size={entry.file.size} preview={entry.preview} onRemove={() => removeStaged(entry.id)} />)}</div>}
            <input ref={fileInputRef} type="file" multiple hidden onChange={event => { stageFiles(event.target.files); event.target.value = '' }} />
            {slashOpen && slashCandidates.length > 0 && <div className="slashMenu">{slashCandidates.map((name, index) => <button className="pick" type="button" data-active={index === slashActive || undefined} key={name} onMouseDown={event => { event.preventDefault(); pickSlash(name) }}><span className="pickText"><span className="pickName">/{name}</span><span className="pickPath">{COMMAND_HINTS[name].replace(/^\/\S+(\s+\[\S+\])?\s+/, '')}</span></span></button>)}</div>}
            <textarea
              id="draft"
              ref={draftRef}
              rows="1"
              value={draft}
              disabled={unavailable}
              placeholder={PLACEHOLDERS[status] || PLACEHOLDERS.idle}
              onChange={event => setDraft(event.target.value)}
              onBlur={() => window.setTimeout(() => setSlashOpen(false), 100)}
              onKeyDown={onDraftKeyDown}
              onPaste={event => { if (stageFiles(event.clipboardData?.files || [])) event.preventDefault() }}
            />
            {archived && <div className="archiveNotice"><span>Archived - unarchive to continue</span><button className="button" onClick={() => setArchived(currentChat, false)}>Unarchive</button></div>}
            {(pendingCounts.next_step > 0 || pendingCounts.next_turn > 0) && <div className="pending">
              {pendingCounts.next_step > 0 && <span className="chip">{pendingCounts.next_step} steering for the next step</span>}
              {pendingCounts.next_turn > 0 && <span className="chip">{pendingCounts.next_turn} queued follow-up{pendingCounts.next_turn === 1 ? '' : 's'}</span>}
            </div>}
            <div className="actions">
              <button className="iconBtn" title="Attach files" aria-label="Attach files" disabled={unavailable} onClick={() => fileInputRef.current?.click()}><PlusIcon /></button>
              <button className="chip" title="Invocation directory" onClick={openPicker}><FolderIcon /><span className="chipName">{currentProject?.name || 'No project'}</span></button>
              <span className="hint" data-running={status !== 'idle' && !submitting || undefined}>{submitting ? 'Sending…' : HINTS[status] || HINTS.idle}</span>
              <button className="send" data-mode={buttonMode} title={buttonTitle} aria-label={buttonTitle} disabled={archived || submitting || controlling || status === 'aborting'} onClick={() => buttonMode === 'pause' || buttonMode === 'abort' ? control(buttonMode) : onEnter(false)}><SendIcon /></button>
            </div>
          </div>
          {statusInfo && <div className="statusBar" aria-label="Chat status">
            <div className="statusContext">
              {modelSelection && <button className="statusAction" id="modelChip" title={modelTitle} onClick={() => runCommand('/model')}><span className="chipName">{modelSelection.model}{modelSelection.effort ? ` · ${modelSelection.effort}` : ''}</span></button>}
              <span className="statusSeparator" aria-hidden="true">·</span>
              <span id="cwd" title={statusInfo.cwd || ''}>{statusInfo.cwd || ''}</span>
            </div>
            {contextAvailable && <button className="statusAction" id="contextStatus" title={`About ${formatTokenCount(statusInfo.context_used_tokens)} of ${formatTokenCount(statusInfo.context_window_tokens)} tokens used. Show context details (/context)`} onClick={() => runCommand('/context')}>Context {statusInfo.context_remaining_percent}% left</button>}
          </div>}
        </div>
      </div>
    </main>
    <Modal
      modal={modal}
      projects={projects}
      onClose={closeModal}
      onBack={() => setModal({ kind: 'projects', browse: null, error: '' })}
      onBrowse={browse}
      onUseFolder={useFolder}
      onStartChat={startChat}
      onConfirm={value => modal?.confirm?.run(value)}
    />
  </>
}

const storedTheme = localStorage.getItem('ava-theme')
if (storedTheme) document.documentElement.dataset.theme = storedTheme

createRoot(document.getElementById('root')).render(<App />)
