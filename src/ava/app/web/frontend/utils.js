export const formatBytes = bytes => {
  const units = ['B', 'KiB', 'MiB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit === 0 ? value : value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

export const formatTokenCount = value => {
  if (value < 1000) return String(value)
  if (value < 100000) return `${(value / 1000).toFixed(1).replace(/\.0$/, '')}k`
  return `${Math.round(value / 1000)}k`
}

export const summarize = (tool, args) => {
  try {
    const parsed = JSON.parse(args)
    return parsed.command || parsed.path || parsed.file_path || args
  } catch {
    return args
  }
}

let rowSequence = 0

export const transcriptRow = (type, fields = {}) => ({
  id: ++rowSequence,
  type,
  ...fields,
})
