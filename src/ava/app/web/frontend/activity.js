const ACTIVITY_KINDS = {
  bash: 'commands',
  edit: 'files',
  read: 'reads',
  write: 'files',
}

const ACTIVITY_LABELS = {
  commands: { active: 'Run commands', complete: 'Ran commands' },
  files: { active: 'Edit files', complete: 'Edited files' },
  reads: { active: 'Read files', complete: 'Read files' },
  tools: { active: 'Use tools', complete: 'Used tools' },
}

const isComplete = entry => Boolean(entry.isError) || Boolean(entry.text) || Number.isFinite(entry.elapsed)

export const activityLabel = entries => {
  const categories = []
  for (const entry of entries) {
    const kind = ACTIVITY_KINDS[entry.tool] || 'tools'
    let category = categories.find(item => item.kind === kind)
    if (!category) {
      category = { kind, complete: true }
      categories.push(category)
    }
    category.complete = category.complete && isComplete(entry)
  }

  return categories.map((category, index) => {
    const state = category.complete ? 'complete' : 'active'
    const label = ACTIVITY_LABELS[category.kind][state]
    return index === 0 ? label : `${label[0].toLowerCase()}${label.slice(1)}`
  }).join(', ')
}

export const groupTranscriptEntries = entries => {
  const grouped = []
  for (const entry of entries) {
    const previous = grouped[grouped.length - 1]
    if (entry.type === 'tool' && previous?.type === 'activity') {
      previous.entries.push(entry)
    } else if (entry.type === 'tool') {
      grouped.push({ id: `activity-${entry.id}`, type: 'activity', entries: [entry] })
    } else {
      grouped.push(entry)
    }
  }
  return grouped
}
