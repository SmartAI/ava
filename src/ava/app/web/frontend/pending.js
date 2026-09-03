export const emptyPending = () => ({ next_step: [], next_turn: [] })

const targets = ['next_step', 'next_turn']

export const applyPendingEvent = (pending, event) => {
  if (event.kind === 'turn/end' && event.reason === 'user_abort') return emptyPending()
  if (event.kind === 'inbox/spliced' && targets.includes(event.target)) {
    const next = { next_step: [...pending.next_step], next_turn: [...pending.next_turn] }
    const inserted = (event.inserted || []).map(message => ({
      id: message.id,
      target: event.target,
      blocks: message.blocks || [],
    }))
    next[event.target].splice(event.index, event.removed, ...inserted)
    return next
  }
  if (event.kind === 'step/claimed') {
    const claimed = new Set((event.messages || []).map(message => message.id).filter(Boolean))
    if (!claimed.size) return pending
    return {
      next_step: pending.next_step.filter(message => !claimed.has(message.id)),
      next_turn: pending.next_turn.filter(message => !claimed.has(message.id)),
    }
  }
  return pending
}

export const pendingMessages = pending => [...pending.next_step, ...pending.next_turn]

export const pendingText = message => (message.blocks || [])
  .filter(block => block.kind === 'text')
  .map(block => block.text || '')
  .join('')

export const pendingAttachments = message => (message.blocks || [])
  .filter(block => block.kind === 'image' || block.kind === 'file_text')

export const pendingPreview = message => {
  const text = pendingText(message).replace(/\s+/g, ' ').trim()
  const attachments = pendingAttachments(message)
  const attachmentText = attachments.map(block => block.display_path || 'attachment').join(', ')
  if (text && attachmentText) return `${text} · ${attachmentText}`
  return text || attachmentText || 'Message'
}
