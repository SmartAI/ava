import React, { useState } from 'react'

import { field, modalBackdrop, modalBody, modalCard, modalFooter, modalHeader, modalTitle, primaryButton, secondaryButton, iconButton } from '../ui'
import { useDialogFocus } from '../useDialogFocus'
import { CloseIcon } from './Icons'

const newConnection = { provider_type: 'custom', provider: '', family: 'openai', base_url: '' }

export function SettingsModal({ settings, fontSize, loadError, onClose, onRetry, onSave, onRemove, onFontSize }) {
  const dialogRef = useDialogFocus()
  const [form, setForm] = useState(null)
  const [apiKey, setApiKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const entries = settings?.providers || []
  const defaultSelection = settings?.default_selection || {}
  const defaultEntry = defaultSelection.provider ? entries.find(item => item.id === defaultSelection.provider) : null
  const current = entries.find(item => item.id === form?.provider)
  const custom = form?.provider_type === 'custom'
  const storesKey = custom || !['codex', 'llamacpp'].includes(form?.provider)

  const choose = entry => {
    setForm({ provider_type: entry.provider_type, provider: entry.id, family: entry.family || 'openai', base_url: entry.base_url || '' })
    setApiKey('')
    setError('')
    setNotice('')
  }

  const submit = async event => {
    event.preventDefault()
    setError('')
    setSaving(true)
    try {
      await onSave({ ...form, provider: form.provider.trim(), base_url: custom ? form.base_url.trim() : null, family: custom ? form.family : null, api_key: storesKey ? apiKey || null : null })
      setForm(value => ({ ...value, provider: value.provider.trim() }))
      setApiKey('')
      setNotice('Connection saved. Conversation models are unchanged.')
    } catch (caught) {
      setError(String(caught?.message || caught))
    } finally {
      setSaving(false)
    }
  }
  const remove = async () => {
    setSaving(true)
    setError('')
    try {
      await onRemove(form.provider)
      setApiKey('')
      setNotice('Stored key removed. Environment credentials, if present, still apply.')
    } catch (caught) {
      setError(String(caught?.message || caught))
    } finally {
      setSaving(false)
    }
  }

  return <div className={`${modalBackdrop} px-4 py-8`} onMouseDown={event => { if (event.target === event.currentTarget && !saving) onClose() }}>
    <div className={`${modalCard} max-h-full w-full max-w-[600px]`} ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="settings-title" onKeyDown={event => { if (saving && event.key === 'Escape') event.stopPropagation() }}>
      <div className={modalHeader}>
        <div className="flex-1"><h2 className={modalTitle} id="settings-title">Settings</h2><p className="mt-1 text-xs text-muted">Saved defaults apply to new conversations unless overridden by environment variables or launch options. Choose model and effort in each conversation.</p></div>
        <button className={iconButton} aria-label="Close settings" disabled={saving} onClick={onClose}><CloseIcon /></button>
      </div>
      <div className={`${modalBody} px-5 pb-5`}>
        {!settings ? <div className="py-8 text-sm text-muted">{loadError || 'Checking provider connections…'}{loadError && <button className={`${secondaryButton} ml-3`} onClick={onRetry}>Try again</button>}</div> : <>
          <div className="my-3 flex items-center justify-between gap-2"><h3 className="text-sm font-semibold">Provider connections</h3><button className={secondaryButton} disabled={saving} onClick={onRetry}>Refresh status</button></div>
          <p className="mb-3 -mt-1 break-words text-xs text-muted">{defaultSelection.provider ? <>Saved default: <span className="text-ink">{defaultEntry?.label || defaultSelection.provider} · {defaultSelection.model}{defaultSelection.effort ? ` · ${defaultSelection.effort}` : ''}</span></> : 'Saved default unavailable.'}</p>
          <div className="grid gap-2 min-[520px]:grid-cols-2">
            {entries.map(entry => <button key={entry.id} disabled={saving} aria-pressed={form?.provider === entry.id} className={`rounded-xl border p-3 text-left transition-[border-color,background-color,box-shadow,transform] duration-150 focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_color-mix(in_srgb,var(--ava-accent)_20%,transparent)] active:scale-[0.99] ${form?.provider === entry.id ? 'border-accent bg-selected' : 'border-line-strong hover:bg-hover'}`} onClick={() => choose(entry)}>
              <span className="flex items-center gap-1.5 text-[13px] font-medium">{entry.label}{entry.is_default && <span className="rounded-full bg-selected px-1.5 py-px text-[10px] font-semibold text-accent">Default</span>}</span>
              <span className={`mt-1 block text-xs ${entry.valid ? 'text-accent' : 'text-muted'}`}>{entry.valid ? '✓ ' : ''}{entry.status}</span>
              {entry.credential_source && <span className="mt-1 block text-[11px] text-faint">{entry.credential_source}</span>}
            </button>)}
          </div>
          <button className={`${secondaryButton} mt-3`} disabled={saving} onClick={() => { setForm(newConnection); setApiKey(''); setError(''); setNotice('') }}>Add custom provider</button>
          {form && <form className="mt-5 rounded-xl border border-line-strong p-4" onSubmit={submit}>
            <h3 className="text-sm font-semibold">{current?.label || 'New provider'}</h3>
            <p className="mt-1 text-xs leading-5 text-muted">{current?.message || 'Connect an OpenAI-compatible or Anthropic endpoint.'}</p>
            <fieldset disabled={saving} className="mt-3 grid gap-3">
              {custom && <>
                <label className="text-xs text-muted">Provider name<input className={`${field} mt-1.5`} required pattern="[a-z](?:[a-z0-9]|-)*" value={form.provider} placeholder="my-gateway" onChange={event => { setForm({ ...form, provider: event.target.value }); setApiKey('') }} /></label>
                <label className="text-xs text-muted">API format<select className={`${field} mt-1.5`} value={form.family} onChange={event => setForm({ ...form, family: event.target.value })}><option value="openai">OpenAI-compatible</option><option value="anthropic">Anthropic</option></select></label>
                <label className="text-xs text-muted">Base URL<input className={`${field} mt-1.5`} type="url" required placeholder="https://gateway.example.com/v1" value={form.base_url} onChange={event => setForm({ ...form, base_url: event.target.value })} /><span className="mt-1 block text-[11px]">HTTPS required, except for localhost.</span></label>
              </>}
              {storesKey ? <label className="text-xs text-muted">API key<input className={`${field} mt-1.5 font-mono`} type="password" value={apiKey} autoComplete="new-password" placeholder="Leave blank to keep existing credentials" onChange={event => setApiKey(event.target.value)} /><span className="mt-1 block text-[11px]">Stored separately in Ava credential storage, never in settings.json.</span></label> : <p className="text-xs text-muted">{form.provider === 'codex' ? 'Uses your Codex CLI login. Run codex login in the terminal, then refresh.' : 'Uses the local llama.cpp server at http://127.0.0.1:8081/v1.'}</p>}
              <div className="flex flex-wrap justify-end gap-2">
                {storesKey && current?.has_stored_key && <button className={secondaryButton} type="button" onClick={remove}>Remove stored key</button>}
                <button className={`${primaryButton} px-4 py-2`} type="submit">{saving ? 'Checking…' : 'Save connection'}</button>
              </div>
            </fieldset>
            {error && <p className="mt-3 text-xs text-danger" role="alert">{error}</p>}
            {notice && <p className="mt-3 text-xs text-muted" role="status">{notice}</p>}
          </form>}
        </>}
        <div className="mt-5 border-t border-line pt-4"><h3 className="mb-3 text-sm font-semibold">Appearance</h3><label className="block max-w-56 text-xs text-muted">Font size<select className={`${field} mt-1.5`} value={fontSize} onChange={event => onFontSize(event.target.value)}><option value="small">Small</option><option value="default">Default</option><option value="large">Large</option></select></label></div>
      </div>
      <div className={`${modalFooter} justify-end`}><button className={secondaryButton} disabled={saving} onClick={onClose}>Done</button></div>
    </div>
  </div>
}
