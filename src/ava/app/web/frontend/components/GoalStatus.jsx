import React, { useEffect, useState } from 'react'

import { formatTokenCount } from '../utils'

const labels = { active: 'Active', paused: 'Paused', blocked: 'Blocked', budget_limited: 'Limit reached', complete: 'Complete' }

export function GoalStatus({ goal, connected }) {
  const [clock, setClock] = useState({ goal, seconds: 0 })
  useEffect(() => {
    const start = Date.now()
    if (!connected) {
      setClock(previous => previous.goal === goal ? previous : { goal, seconds: 0 })
      return
    }
    setClock({ goal, seconds: 0 })
    if (!goal?.timing_running) return
    const timer = setInterval(() => setClock({ goal, seconds: Math.floor((Date.now() - start) / 1000) }), 1000)
    return () => clearInterval(timer)
  }, [goal, connected])
  if (!goal || goal.status === 'cleared') return null
  const seconds = Math.max(0, Math.floor((goal.elapsed_ms || 0) / 1000) + (clock.goal === goal ? clock.seconds : 0))
  const elapsed = seconds >= 3600 ? `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return <section aria-label="Current goal" className="mb-2 w-full max-w-[780px] rounded-xl border border-line bg-panel px-3 py-2.5">
    <div className="flex items-center gap-2 text-xs"><strong>Goal</strong><span role="status" className="text-accent">{labels[goal.status] || goal.status}</span><span className="ml-auto text-faint [font-variant-numeric:tabular-nums]" title="Worker execution time, excluding pauses and completion audits">{elapsed}</span></div>
    <div className="mt-1 line-clamp-2 text-sm break-words" title={goal.objective}>{goal.objective}</div>
    <div className="mt-1 text-xs text-faint">Turns {goal.turns} / {goal.max_turns} · Tokens {formatTokenCount(goal.tokens_used)}{goal.token_budget ? ` / ${formatTokenCount(goal.token_budget)}` : ' · no token limit'}{goal.usage_complete === false ? ' (partial usage)' : ''}</div>
    {goal.check && <div className="mt-1 truncate text-xs text-faint" title={goal.check}>Verify: {goal.check}</div>}
    {goal.reason && <div className="mt-1 line-clamp-2 text-xs text-muted break-words" title={goal.reason}>{goal.reason}</div>}
  </section>
}
