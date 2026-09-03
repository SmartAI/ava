import React from 'react'

import { formatTokenCount } from '../utils'

const formatLatency = milliseconds => milliseconds < 1000
  ? `${milliseconds}ms`
  : `${(milliseconds / 1000).toFixed(1).replace(/\.0$/, '')}s`

export function StatusBar({ info, selection, onModel, onContext }) {
  if (!info) return null
  const contextAvailable = Number.isFinite(info.context_remaining_percent) && info.context_window_tokens
  const inputAvailable = Number.isFinite(info.input_tokens)
  const outputAvailable = Number.isFinite(info.output_tokens)
  const cacheAvailable = Number.isFinite(info.cache_hit_percent)
  const ttftAvailable = Number.isFinite(info.ttft_ms)
  const modelTitle = selection
    ? `${selection.provider} · ${selection.model}${selection.effort ? ` · effort ${selection.effort}` : ''} (/model)`
    : ''

  return <div className="flex min-h-6.5 w-full max-w-[780px] flex-wrap items-center gap-x-2.5 gap-y-1 px-3 pt-1 font-mono text-[11px] text-faint" aria-label="Chat status">
    <div className="flex min-w-0 flex-1 items-center gap-2">
      {selection && <button className="max-w-[45%] shrink-0 overflow-hidden text-ellipsis whitespace-nowrap text-inherit hover:text-muted focus-visible:rounded-sm focus-visible:outline-1 focus-visible:outline-offset-2 focus-visible:outline-accent" title={modelTitle} onClick={onModel}>{selection.model}{selection.effort ? ` · ${selection.effort}` : ''}</button>}
      <span className="shrink-0 text-line-heavy" aria-hidden="true">·</span>
      <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap" title={info.cwd || ''}>{info.cwd || ''}</span>
    </div>
    {(inputAvailable || outputAvailable) && <span className="shrink-0 whitespace-nowrap [font-variant-numeric:tabular-nums]" title="Provider-reported token totals for this chat">
      {inputAvailable && <span aria-label={`${info.input_tokens} input tokens`}>↑{formatTokenCount(info.input_tokens)}</span>}
      {inputAvailable && outputAvailable && ' '}
      {outputAvailable && <span aria-label={`${info.output_tokens} output tokens`}>↓{formatTokenCount(info.output_tokens)}</span>}
    </span>}
    {cacheAvailable && <span className="shrink-0 whitespace-nowrap [font-variant-numeric:tabular-nums]" title="Cached input tokens as a percentage of all input tokens">Cache {info.cache_hit_percent}%</span>}
    {ttftAvailable && <span className="shrink-0 whitespace-nowrap [font-variant-numeric:tabular-nums]" title={`${info.ttft_ms} ms from dispatch to the first content token in the latest request`}>TTFT {formatLatency(info.ttft_ms)}</span>}
    {contextAvailable && <button className="shrink-0 whitespace-nowrap text-inherit [font-variant-numeric:tabular-nums] hover:text-muted focus-visible:rounded-sm focus-visible:outline-1 focus-visible:outline-offset-2 focus-visible:outline-accent" title={`About ${formatTokenCount(info.context_used_tokens)} of ${formatTokenCount(info.context_window_tokens)} tokens used. Show context details (/context)`} onClick={onContext}>Context {info.context_remaining_percent}% left</button>}
  </div>
}
