import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { api } from './api'
import { COMMAND_HINTS } from './constants'
import { Composer } from './components/Composer'
import { Header } from './components/Header'
import { Modal } from './components/Modal'
import { Sidebar } from './components/Sidebar'
import { SettingsModal } from './components/SettingsModal'
import { StatusBar } from './components/StatusBar'
import { Transcript } from './components/Transcript'
import { emptyPending, applyPendingEvent } from './pending'
import { formatBytes, transcriptRow } from './utils'

const savedSet = key => {
  try {
    const value = JSON.parse(localStorage.getItem(key) || '[]')
    return new Set(Array.isArray(value) ? value.filter(item => typeof item === 'string') : [])
  } catch {
    return new Set()
  }
}

const loadLocal = key => {
  try { return localStorage.getItem(key) } catch { return null }
}

const saveLocal = (key, value) => {
  try { localStorage.setItem(key, value) } catch { /* Browser storage may be disabled. */ }
}

const savedFontSize = () => {
  const value = loadLocal('ava-font-size')
  return ['small', 'default', 'large'].includes(value) ? value : 'default'
}

export default function App() {
  const [projects, setProjects] = useState([])
  const [current, setCurrent] = useState(null)
  const [projectId, setProjectId] = useState(null)
  const [openProjects, setOpenProjects] = useState(() => savedSet('ava-open-projects'))
  const [archiveOpen, setArchiveOpen] = useState(() => savedSet('ava-open-archives'))
  const [status, setStatus] = useState('idle')
  const [statusInfo, setStatusInfo] = useState(null)
  const [transcript, setTranscript] = useState([])
  const [empty, setEmpty] = useState(true)
  const [pending, setPending] = useState(emptyPending)
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
  const [fontSize, setFontSize] = useState(savedFontSize)

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

  const addNotice = text => addAside(transcriptRow('notice', { text }))
  const addError = error => addAside(transcriptRow('error', { text: String(error?.message || error) }))
  const closeTail = () => { tailRef.current = null }

  const addUser = blocks => {
    closeTail()
    forceFollowRef.current = true
    addTranscript(transcriptRow('user', { blocks: blocks || [] }))
  }

  const appendDelta = delta => {
    setEmpty(false)
    if (tailRef.current === null) {
      const entry = transcriptRow('assistant', { text: delta })
      tailRef.current = entry.id
      setTranscript(items => [...items, entry])
      return
    }
    const id = tailRef.current
    setTranscript(items => items.map(item => item.id === id ? { ...item, text: item.text + delta } : item))
  }

  const applyEvent = event => {
    if (event.kind === 'inbox/spliced') {
      setPending(items => applyPendingEvent(items, event))
      return
    }
    if (event.kind === 'step/claimed') {
      for (const message of event.messages || []) {
        if (!message.id || !displayedInputsRef.current.has(message.id)) addUser(message.blocks)
        if (message.id) displayedInputsRef.current.add(message.id)
      }
      setPending(items => applyPendingEvent(items, event))
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
        addTranscript(transcriptRow('tool', {
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
        setPending(items => applyPendingEvent(items, event))
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
    setPending(emptyPending())
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
      saveLocal('ava-current-chat', id)
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

  const openProjectBrowser = () => {
    setRailOpen(false)
    setModal({ kind: 'projects', browse: null, error: '', addOnly: true, loading: true })
    browse('')
  }

  const closeModal = () => setModal(null)

  const openSettings = async () => {
    setRailOpen(false)
    setModal({ kind: 'settings', settings: null, error: '' })
    try {
      const settings = await api.settings()
      setModal(value => value?.kind === 'settings' ? { ...value, settings, error: '' } : value)
    } catch (error) {
      setModal(value => value?.kind === 'settings'
        ? { ...value, error: String(error?.message || error) }
        : value)
    }
  }

  const saveSettings = async values => {
    const { font_size: nextFontSize, ...providerSettings } = values
    const result = await api.saveSettings({ ...providerSettings, chat_id: currentRef.current })
    document.documentElement.dataset.fontSize = nextFontSize
    saveLocal('ava-font-size', nextFontSize)
    setFontSize(nextFontSize)
    if (result.applied_to_current) setModelSelection(result.selection)
    closeModal()
    if (currentRef.current) {
      addNotice(result.warning || `Settings saved · ${result.provider} · ${result.model}`)
    }
  }

  const browse = async path => {
    setModal(value => value?.kind === 'projects' ? { ...value, loading: true, error: '' } : value)
    try {
      const listing = await api.browse(path)
      setModal(value => value?.kind === 'projects'
        ? { ...value, browse: listing, loading: false, error: '' }
        : value)
    } catch (error) {
      setModal(value => value?.kind === 'projects'
        ? { ...value, loading: false, error: String(error.message || error) }
        : value)
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
      const message = String(error.message || error)
      setModal(value => value
        ? { ...value, error: message }
        : { kind: 'generic', title: 'Could not create session', text: message, error: '' })
    }
  }

  const useFolder = async () => {
    if (modal?.kind !== 'projects' || !modal.browse) return
    const addOnly = Boolean(modal.addOnly)
    try {
      const added = await api.addProject(modal.browse.path)
      setProjects(items => {
        const exists = items.some(project => project.id === added.id)
        return exists ? items : [{ ...added, chats: added.chats || [] }, ...items]
      })
      if (addOnly) {
        setOpenProjects(items => new Set(items).add(added.id))
        closeModal()
      } else {
        await startChat(added.id)
      }
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

  const revisePending = async (messageId, text) => {
    if (!currentRef.current) return false
    try {
      const result = await api.revisePending(currentRef.current, messageId, text.trim())
      updateChat(result.chat)
      return true
    } catch (error) {
      addError(error)
      return false
    }
  }

  const deletePending = async messageId => {
    if (!currentRef.current) return false
    try {
      const result = await api.deletePending(currentRef.current, messageId)
      updateChat(result.chat)
      return true
    } catch (error) {
      addError(error)
      return false
    }
  }

  const sendPending = async messageId => {
    if (!currentRef.current) return false
    try {
      const result = await api.sendPending(currentRef.current, messageId)
      updateChat(result.chat)
      return true
    } catch (error) {
      addError(error)
      return false
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
      const chats = items.flatMap(project => project.chats)
      const remembered = loadLocal('ava-current-chat')
      const first = chats.find(chat => chat.id === remembered) || chats[0]
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
    saveLocal('ava-open-projects', JSON.stringify([...openProjects]))
  }, [openProjects])

  useEffect(() => {
    saveLocal('ava-open-archives', JSON.stringify([...archiveOpen]))
  }, [archiveOpen])

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

  return <>
    <Sidebar
      projects={projects}
      current={current}
      status={status}
      selection={modelSelection}
      open={openProjects}
      archiveOpen={archiveOpen}
      mobileOpen={railOpen}
      onClose={() => setRailOpen(false)}
      onAddProject={openProjectBrowser}
      onNew={openPicker}
      onNewSession={startChat}
      onSettings={openSettings}
      onToggleProject={id => setOpenProjects(items => {
        const next = new Set(items)
        next.has(id) ? next.delete(id) : next.add(id)
        return next
      })}
      onToggleArchive={id => setArchiveOpen(items => {
        const next = new Set(items)
        next.has(id) ? next.delete(id) : next.add(id)
        return next
      })}
      onSelect={selectChat}
      onArchive={setArchived}
    />
    <main className="flex min-w-0 flex-1 flex-col">
      <Header
        title={currentChat ? currentChat.title || 'New chat' : 'No chat'}
        status={status}
        onOpenNavigation={() => setRailOpen(true)}
        onToggleTheme={toggleTheme}
      />
      <div
        className="flex min-h-0 flex-1 flex-col overflow-x-hidden overflow-y-auto [scrollbar-gutter:stable]"
        ref={scrollRef}
        onScroll={event => {
          const target = event.currentTarget
          followRef.current = target.scrollHeight - target.scrollTop - target.clientHeight < 120
        }}
      >
        <Transcript entries={transcript} empty={empty} project={currentProject} status={status} />
        <div className="composer-seat sticky bottom-0 z-10 flex shrink-0 flex-col items-center px-2 pb-2 min-[701px]:px-4">
          <Composer
            archived={archived}
            currentChat={currentChat}
            currentProject={currentProject}
            status={status}
            submitting={submitting}
            controlling={controlling}
            unavailable={unavailable}
            dragging={dragging}
            staged={staged}
            pending={pending}
            draft={draft}
            draftRef={draftRef}
            fileInputRef={fileInputRef}
            slashOpen={slashOpen}
            slashCandidates={slashCandidates}
            slashActive={slashActive}
            onDraftChange={setDraft}
            onDraftBlur={() => window.setTimeout(() => setSlashOpen(false), 100)}
            onDraftKeyDown={onDraftKeyDown}
            onPaste={event => {
              if (stageFiles(event.clipboardData?.files || [])) event.preventDefault()
            }}
            onPickSlash={pickSlash}
            onFiles={stageFiles}
            onRemoveFile={removeStaged}
            onRevisePending={revisePending}
            onDeletePending={deletePending}
            onSendPending={sendPending}
            onOpenProjects={openPicker}
            onUnarchive={(chat) => setArchived(chat, false)}
            onControl={control}
            onEnter={onEnter}
            onDragEnter={event => {
              event.preventDefault()
              if (!unavailable) setDragging(true)
            }}
            onDragLeave={event => {
              if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false)
            }}
            onDrop={event => {
              event.preventDefault()
              event.stopPropagation()
              setDragging(false)
              stageFiles(event.dataTransfer?.files || [])
            }}
          />
          <StatusBar
            info={statusInfo}
            selection={modelSelection}
            onModel={() => runCommand('/model')}
            onContext={() => runCommand('/context')}
          />
        </div>
      </div>
    </main>
    {modal?.kind === 'settings' ? <SettingsModal
      settings={modal.settings}
      fontSize={fontSize}
      loadError={modal.error}
      onClose={closeModal}
      onRetry={openSettings}
      onSave={saveSettings}
    /> : <Modal
      modal={modal}
      projects={projects}
      onClose={closeModal}
      onBack={() => setModal({ kind: 'projects', browse: null, error: '' })}
      onBrowse={browse}
      onUseFolder={useFolder}
      onStartChat={startChat}
      onConfirm={value => modal?.confirm?.run(value)}
    />}
  </>
}
