import React from 'react'

import { formatBytes } from '../utils'
import { FileIcon, ImageIcon } from './Icons'

export function AttachmentChip({ name, kind, size, preview, onRemove }) {
  return <span className="inline-flex max-w-60 shrink-0 cursor-default items-center gap-1.5 rounded-full bg-hover px-1.5 py-1 text-xs text-quiet">
    {preview
      ? <img className="h-7 w-7 shrink-0 rounded-md object-cover" src={preview} alt="" />
      : <span className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-line">{kind === 'image' ? <ImageIcon /> : <FileIcon />}</span>}
    <span className="min-w-0">
      <span className="block overflow-hidden text-ellipsis whitespace-nowrap text-muted">{name}</span>
      <span className="block text-[11px] text-faint">{kind === 'image' ? 'Image' : 'File'} · {formatBytes(size)}</span>
    </span>
    {onRemove && <button className="inline-flex h-5.5 w-5.5 shrink-0 items-center justify-center rounded-md text-xs text-quiet hover:bg-hover hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" type="button" aria-label={`Remove ${name}`} onClick={onRemove}>×</button>}
  </span>
}
