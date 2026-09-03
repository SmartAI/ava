import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyPendingEvent,
  emptyPending,
  pendingAttachments,
  pendingMessages,
  pendingPreview,
  pendingText,
} from './pending.js'

const splice = (target, index, removed, ...inserted) => ({
  kind: 'inbox/spliced',
  target,
  index,
  removed,
  inserted,
})

const message = (id, text) => ({ id, blocks: [{ kind: 'text', text }] })

test('pending inbox events preserve target order and message previews', () => {
  let pending = emptyPending()
  pending = applyPendingEvent(pending, splice('next_turn', 0, 0, message('m-1', 'later')))
  pending = applyPendingEvent(pending, splice('next_step', 0, 0, message('m-2', 'steer\nnow')))
  pending = applyPendingEvent(pending, splice('next_turn', 1, 0, message('m-3', 'last')))

  assert.deepEqual(pendingMessages(pending).map(item => item.id), ['m-2', 'm-1', 'm-3'])
  assert.equal(pendingPreview(pending.next_step[0]), 'steer now')
  assert.equal(pendingText(pending.next_turn[0]), 'later')
})

test('revisions, deletions, claims, and aborts fold without stale rows', () => {
  let pending = emptyPending()
  pending = applyPendingEvent(pending, splice('next_turn', 0, 0, message('m-1', 'first'), message('m-2', 'second')))
  pending = applyPendingEvent(pending, splice('next_turn', 0, 1, message('m-3', 'revised')))
  assert.deepEqual(pending.next_turn.map(item => item.id), ['m-3', 'm-2'])

  pending = applyPendingEvent(pending, splice('next_turn', 1, 1))
  assert.deepEqual(pending.next_turn.map(item => item.id), ['m-3'])

  pending = applyPendingEvent(pending, {
    kind: 'step/claimed',
    messages: [{ id: 'm-3', blocks: [] }],
  })
  assert.deepEqual(pending, emptyPending())

  pending = applyPendingEvent(pending, splice('next_step', 0, 0, message('m-4', 'doomed')))
  pending = applyPendingEvent(pending, { kind: 'turn/end', reason: 'user_abort' })
  assert.deepEqual(pending, emptyPending())
})

test('attachment-only messages have a useful preview and remain revisable', () => {
  const queued = {
    id: 'm-1',
    target: 'next_turn',
    blocks: [
      { kind: 'image', display_path: 'screen.png' },
      { kind: 'file_text', display_path: 'notes.txt' },
    ],
  }
  assert.equal(pendingPreview(queued), 'screen.png, notes.txt')
  assert.equal(pendingText(queued), '')
  assert.equal(pendingAttachments(queued).length, 2)
})
