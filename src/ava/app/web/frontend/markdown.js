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

const heading = line => line.match(/^(#{1,6})[ \t]+(.+?)[ \t]*$/)

const listItem = line => {
  const match = line.match(/^([ \t]*)([-+*]|\d+[.)])[ \t]+(.*)$/)
  if (!match) return null
  return {
    indent: match[1].replace(/\t/g, '    ').length,
    ordered: /^\d/.test(match[2]),
    start: /^\d/.test(match[2]) ? Number.parseInt(match[2], 10) : null,
    text: match[3],
  }
}

const renderList = (lines, start) => {
  const first = listItem(lines[start])
  const tag = first.ordered ? 'ol' : 'ul'
  const startAttribute = first.ordered && first.start !== 1 ? ` start="${first.start}"` : ''
  let html = `<${tag}${startAttribute}>`
  let index = start

  while (index < lines.length) {
    const item = listItem(lines[index])
    if (!item || item.indent !== first.indent || item.ordered !== first.ordered) break

    html += `<li>${item.text}`
    index += 1
    while (index < lines.length) {
      const child = listItem(lines[index])
      if (!child || child.indent <= first.indent) break
      const nested = renderList(lines, index)
      html += nested.html
      index = nested.index
    }
    html += '</li>'
  }

  return { html: `${html}</${tag}>`, index }
}

const tableAt = (lines, index) => isTable(lines.slice(index, index + 2))

const renderBlocks = source => {
  const lines = source.split('\n')
  const blocks = []
  let index = 0

  while (index < lines.length) {
    if (lines[index].trim() === '') {
      index += 1
      continue
    }

    const title = heading(lines[index])
    if (title) {
      const level = title[1].length
      blocks.push(`<h${level}>${title[2]}</h${level}>`)
      index += 1
      continue
    }

    if (tableAt(lines, index)) {
      const table = lines.slice(index, index + 2)
      index += 2
      while (index < lines.length && lines[index].trim() !== '' && lines[index].includes('|')) {
        table.push(lines[index])
        index += 1
      }
      blocks.push(renderTable(table))
      continue
    }

    if (listItem(lines[index])) {
      const list = renderList(lines, index)
      blocks.push(list.html)
      index = list.index
      continue
    }

    const paragraph = []
    while (
      index < lines.length && lines[index].trim() !== '' &&
      !heading(lines[index]) && !tableAt(lines, index) && !listItem(lines[index])
    ) {
      paragraph.push(lines[index])
      index += 1
    }
    blocks.push(`<p>${paragraph.join('<br>')}</p>`)
  }

  return blocks.join('')
}

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
    return renderBlocks(
      escapeHtml(guarded).replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>'),
    ).replace(/\u0000(\d+)\u0000/g, (_, held) => stash[held])
  })
  .join('')
