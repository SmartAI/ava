import assert from 'node:assert/strict'
import test from 'node:test'

import { activityLabel, groupTranscriptEntries } from './activity.js'

const tool = (id, name, fields = {}) => ({
  id,
  type: 'tool',
  tool: name,
  text: '',
  isError: false,
  elapsed: null,
  ...fields,
})

test('groups consecutive tool calls but respects transcript boundaries', () => {
  const user = { id: 1, type: 'user', blocks: [] }
  const assistant = { id: 4, type: 'assistant', text: 'Done' }
  const grouped = groupTranscriptEntries([
    user,
    tool(2, 'bash'),
    tool(3, 'edit'),
    assistant,
    tool(5, 'read'),
  ])

  assert.deepEqual(grouped, [
    user,
    { id: 'activity-2', type: 'activity', entries: [tool(2, 'bash'), tool(3, 'edit')] },
    assistant,
    { id: 'activity-5', type: 'activity', entries: [tool(5, 'read')] },
  ])
})

test('groups consecutive reasoning blocks into one disclosure', () => {
  const user = { id: 1, type: 'user', blocks: [] }
  const first = { id: 2, type: 'reasoning', text: 'First thought' }
  const second = { id: 3, type: 'reasoning', text: 'Second thought' }
  const assistant = { id: 4, type: 'assistant', text: 'Done' }
  const third = { id: 5, type: 'reasoning', text: 'A later thought' }

  assert.deepEqual(groupTranscriptEntries([user, first, second, assistant, third]), [
    user,
    { id: 'reasoning-2', type: 'reasoning-group', entries: [first, second] },
    assistant,
    { id: 'reasoning-5', type: 'reasoning-group', entries: [third] },
  ])
})

test('uses active and completed command labels', () => {
  assert.equal(activityLabel([tool(1, 'bash')]), 'Run commands')
  assert.equal(
    activityLabel([tool(1, 'bash', { text: '(no output)', elapsed: 12 })]),
    'Ran commands',
  )
})

test('composes mixed activity labels and deduplicates file edits', () => {
  const complete = { text: 'ok', elapsed: 12 }
  assert.equal(
    activityLabel([
      tool(1, 'edit', complete),
      tool(2, 'write', complete),
      tool(3, 'bash', complete),
    ]),
    'Edited files, ran commands',
  )
})

test('keeps an activity category active until every matching call completes', () => {
  assert.equal(
    activityLabel([
      tool(1, 'edit', { text: 'ok' }),
      tool(2, 'bash', { isError: true }),
      tool(3, 'bash'),
    ]),
    'Edited files, run commands',
  )
})
