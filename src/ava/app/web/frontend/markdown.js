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

export const renderMarkdown = source => source
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
