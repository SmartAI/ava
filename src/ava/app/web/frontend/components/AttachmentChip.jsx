import React from 'react'

import { chip, chipRemove } from '../ui'
import { formatBytes } from '../utils'
import { FileIcon, ImageIcon } from './Icons'

export function AttachmentChip({ name, kind, size, preview, onRemove }) {
  return <span className={chip}>
    {preview
      ? <img className="h-7 w-7 shrink-0 rounded-md object-cover" src={preview} alt="" />
      : <span className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-line">{kind === 'image' ? <ImageIcon /> : <FileIcon />}</span>}
    <span className="min-w-0">
      <span className="block overflow-hidden text-ellipsis whitespace-nowrap text-muted">{name}</span>
      <span className="block text-[11px] text-faint">{kind === 'image' ? 'Image' : 'File'} · {formatBytes(size)}</span>
    </span>
    {onRemove && <button className={`${chipRemove} text-xs`} type="button" aria-label={`Remove ${name}`} onClick={onRemove}>×</button>}
  </span>
}
