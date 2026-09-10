import React, { useState } from 'react'

import { field, linkButton, modalBackdrop, modalCard, primaryButton, secondaryButton } from '../ui'
import { useDialogFocus } from '../useDialogFocus'

export function ModelModal({ catalog, selection, busy, loadError, onRetry, onClose, onSettings, onApply }) {
  const dialogRef = useDialogFocus()
  const [provider, setProvider] = useState(selection?.provider || '')
  const [model, setModel] = useState(selection?.model || '')
  const [effort, setEffort] = useState(selection?.effort ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const providers = catalog?.providers || []
  const connection = providers.find(item => item.id === provider)
  const models = connection?.models || []
  const profile = models.find(item => item.id === model)
  const efforts = profile?.effort_values || []
  const submit = async event => {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      await onApply({ provider, model, effort: effort || null })
    } catch (caught) {
      setError(String(caught?.message || caught))
      setSaving(false)
    }
  }

  return <div className={`${modalBackdrop} px-4 py-8`} onMouseDown={event => { if (event.target === event.currentTarget && !saving) onClose() }}>
    <form className={`${modalCard} max-h-full w-full max-w-[480px] overflow-y-auto p-5`} ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="conversation-model-title" onSubmit={submit} onKeyDown={event => { if (saving && event.key === 'Escape') event.stopPropagation() }}>
      <h2 className="text-[15px] font-semibold" id="conversation-model-title">Conversation model</h2>
      <p className="mt-2 text-xs leading-5 text-muted">Only this conversation changes. Other conversations and provider credentials stay the same.</p>
      {!catalog ? <div className="py-6 text-sm text-muted">{loadError || 'Checking provider connections…'}{loadError && <button type="button" className={`${secondaryButton} mt-3`} onClick={onRetry}>Try again</button>}</div> : !providers.length ? <p className="py-6 text-sm text-muted">No connected providers. Add credentials in Settings, then try again.</p> : <fieldset disabled={saving} className="mt-5 grid gap-4">
        <label className="text-xs text-muted">Provider<select autoFocus className={`${field} mt-1.5`} value={connection ? provider : ''} disabled={busy} onChange={event => { const next = providers.find(item => item.id === event.target.value); setProvider(next.id); setModel(next.models[0]?.id || ''); setEffort('') }}><option value="" disabled>Choose a connected provider</option>{providers.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        <label className="text-xs text-muted">Model<select className={`${field} mt-1.5`} value={profile ? model : ''} onChange={event => { setModel(event.target.value); setEffort('') }}><option value="" disabled>Choose a model</option>{models.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}</select></label>
        <label className="text-xs text-muted">Reasoning effort<select className={`${field} mt-1.5`} value={effort} disabled={!efforts.length} onChange={event => setEffort(event.target.value)}><option value="">Provider default</option>{efforts.map(value => <option key={value} value={value}>{value}</option>)}</select><span className="mt-1.5 block text-[11px]">{efforts.length ? 'Higher effort can take longer and use more tokens.' : 'This model does not advertise reasoning effort.'}</span></label>
      </fieldset>}
      {busy && <p className="mt-3 text-xs text-muted">Model and effort changes apply at the next step. Change providers after the conversation finishes.</p>}
      {error && <p className="mt-3 text-xs text-danger" role="alert">{error}</p>}
      <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line pt-4">
        <button type="button" className={`${linkButton} mr-auto`} disabled={saving} onClick={onSettings}>Manage providers</button>
        <button type="button" className={secondaryButton} disabled={saving} onClick={onClose}>Cancel</button>
        <button type="submit" className={`${primaryButton} px-4 py-2`} disabled={saving || !profile || (effort && !efforts.includes(effort))}>{saving ? 'Applying…' : 'Apply'}</button>
      </div>
    </form>
  </div>
}
