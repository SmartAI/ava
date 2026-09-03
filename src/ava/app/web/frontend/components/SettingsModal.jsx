import React, { useEffect, useRef, useState } from 'react'

import { CloseIcon } from './Icons'

const fieldClass = 'mt-1.5 w-full rounded-lg border border-line-strong bg-panel px-3 py-2.25 text-[13px] text-ink outline-none transition-colors placeholder:text-faint focus:border-accent disabled:cursor-not-allowed disabled:opacity-60'
const labelClass = 'block text-[12px] font-medium text-muted'
const tabClass = 'flex-1 rounded-md border-0 px-3 py-1.75 text-[13px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
const secondaryButtonClass = 'rounded-full border border-line-strong px-3.5 py-1.75 text-[13px] text-ink hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

const emptyForm = {
  provider_type: 'builtin',
  provider: 'anthropic',
  model: 'claude-sonnet-5',
  effort: '',
  family: 'openai',
  base_url: '',
}

export function SettingsModal({ settings, fontSize, loadError, onClose, onRetry, onSave }) {
  const [form, setForm] = useState(emptyForm)
  const [apiKey, setApiKey] = useState('')
  const [selectedFontSize, setSelectedFontSize] = useState(fontSize)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const firstFieldRef = useRef(null)

  useEffect(() => {
    if (!settings) return
    const family = settings.family || 'openai'
    const supportsEffort = settings.provider_type === 'custom'
      ? family === 'openai'
      : ['openai', 'deepseek', 'codex'].includes(settings.provider)
    setForm({
      provider_type: settings.provider_type,
      provider: settings.provider,
      model: settings.model,
      effort: supportsEffort ? settings.effort || '' : '',
      family,
      base_url: settings.base_url || '',
    })
    setApiKey('')
    setSelectedFontSize(fontSize)
    setError('')
    window.setTimeout(() => firstFieldRef.current?.focus(), 0)
  }, [settings, fontSize])

  const builtIns = settings?.built_in_providers || []
  const custom = form.provider_type === 'custom'
  const storesApiKey = custom || !['codex', 'llamacpp'].includes(form.provider)
  const supportsEffort = custom
    ? form.family === 'openai'
    : ['openai', 'deepseek', 'codex'].includes(form.provider)

  const changeType = providerType => {
    if (providerType === form.provider_type) return
    if (providerType === 'builtin') {
      const first = builtIns[0]
      setForm(value => ({
        ...value,
        provider_type: 'builtin',
        provider: first?.id || 'anthropic',
        model: first?.default_model || 'claude-sonnet-5',
        effort: '',
      }))
    } else {
      setForm(value => ({
        ...value,
        provider_type: 'custom',
        provider: '',
        model: '',
        effort: '',
        family: 'openai',
        base_url: '',
      }))
    }
    setApiKey('')
    setError('')
  }

  const changeProvider = provider => {
    const selected = builtIns.find(item => item.id === provider)
    setForm(value => ({
      ...value,
      provider,
      model: selected?.default_model || value.model,
      effort: '',
    }))
    setApiKey('')
  }

  const submit = async event => {
    event.preventDefault()
    setError('')
    setSaving(true)
    try {
      await onSave({
        ...form,
        provider: form.provider.trim(),
        model: form.model.trim(),
        effort: form.effort.trim() || null,
        family: custom ? form.family : null,
        base_url: custom ? form.base_url.trim() : null,
        api_key: apiKey || null,
        font_size: selectedFontSize,
      })
    } catch (caught) {
      setError(String(caught?.message || caught))
      setSaving(false)
    }
  }

  return <div className="fixed inset-0 z-40 flex items-center justify-center bg-mask px-4 py-8" onMouseDown={event => { if (event.target === event.currentTarget && !saving) onClose() }}>
    <div className="flex max-h-full w-full max-w-[560px] flex-col overflow-hidden rounded-2xl border border-line-strong bg-menu shadow-float" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <div className="flex shrink-0 items-center gap-2.5 px-5 pt-4 pb-3">
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-semibold" id="settings-title">Settings</div>
          <div className="mt-0.5 text-xs text-faint">Configure the default provider for new chats and this idle chat.</div>
        </div>
        <button className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-quiet hover:bg-hover hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" title="Close" aria-label="Close" disabled={saving} onClick={onClose}><CloseIcon /></button>
      </div>

      {!settings && <div className="flex min-h-44 flex-col items-center justify-center gap-3 px-6 py-10 text-sm text-muted">
        <span>{loadError || 'Loading settings…'}</span>
        {loadError && <button className={secondaryButtonClass} type="button" onClick={onRetry}>Try again</button>}
      </div>}

      {settings && <form className="flex min-h-0 flex-1 flex-col" onSubmit={submit}>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
          <div className="mb-5 grid grid-cols-2 gap-1 rounded-lg bg-hover p-1" aria-label="Provider type">
            <button className={`${tabClass} ${!custom ? 'bg-menu font-medium text-ink shadow-card' : 'bg-transparent text-muted hover:text-ink'}`} type="button" aria-pressed={!custom} onClick={() => changeType('builtin')}>Built-in</button>
            <button className={`${tabClass} ${custom ? 'bg-menu font-medium text-ink shadow-card' : 'bg-transparent text-muted hover:text-ink'}`} type="button" aria-pressed={custom} onClick={() => changeType('custom')}>Custom</button>
          </div>

          <div className="grid gap-4 min-[520px]:grid-cols-2">
            {!custom && <label className={labelClass}>
              Provider
              <select ref={firstFieldRef} className={fieldClass} value={form.provider} disabled={saving} onChange={event => changeProvider(event.target.value)}>
                {builtIns.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            </label>}

            {custom && <label className={labelClass}>
              Provider name
              <input ref={firstFieldRef} className={fieldClass} value={form.provider} disabled={saving} required pattern="[a-z](?:[a-z0-9]|-)*" placeholder="my-gateway" autoComplete="off" spellCheck="false" onChange={event => setForm(value => ({ ...value, provider: event.target.value }))} />
              <span className="mt-1 block text-[11px] font-normal leading-4 text-faint">Lowercase letters, numbers, and hyphens.</span>
            </label>}

            {custom && <label className={labelClass}>
              API format
              <select className={fieldClass} value={form.family} disabled={saving} onChange={event => setForm(value => ({ ...value, family: event.target.value, effort: '' }))}>
                <option value="openai">OpenAI-compatible</option>
                <option value="anthropic">Anthropic</option>
              </select>
            </label>}

            <label className={labelClass}>
              Model
              <input className={fieldClass} value={form.model} disabled={saving} required placeholder={custom ? 'company-model' : 'Model ID'} autoComplete="off" spellCheck="false" onChange={event => setForm(value => ({ ...value, model: event.target.value }))} />
            </label>

            <label className={labelClass}>
              Reasoning effort <span className="font-normal text-faint">(optional)</span>
              <input className={fieldClass} value={form.effort} disabled={saving || !supportsEffort} list="effort-values" placeholder={supportsEffort ? 'Provider default' : 'Not supported'} autoComplete="off" spellCheck="false" onChange={event => setForm(value => ({ ...value, effort: event.target.value }))} />
              <datalist id="effort-values"><option value="low" /><option value="medium" /><option value="high" /><option value="xhigh" /><option value="max" /><option value="off" /></datalist>
            </label>
          </div>

          {custom && <label className={`${labelClass} mt-4`}>
            Base URL
            <input className={`${fieldClass} font-mono`} type="url" value={form.base_url} disabled={saving} required placeholder={form.family === 'openai' ? 'https://gateway.example.com/v1' : 'https://gateway.example.com'} autoComplete="url" spellCheck="false" onChange={event => setForm(value => ({ ...value, base_url: event.target.value }))} />
            <span className="mt-1 block text-[11px] font-normal leading-4 text-faint">HTTPS is required, except for localhost endpoints.</span>
          </label>}

          {storesApiKey && <label className={`${labelClass} mt-4`}>
            API key <span className="font-normal text-faint">(optional)</span>
            <input className={`${fieldClass} font-mono`} type="password" value={apiKey} disabled={saving} placeholder="Leave blank to keep the stored key" autoComplete="new-password" onChange={event => setApiKey(event.target.value)} />
            <span className="mt-1 block text-[11px] font-normal leading-4 text-faint">Stored separately in Ava credential storage, never in settings.json.</span>
          </label>}

          {!custom && form.provider === 'codex' && <div className="mt-4 rounded-lg border border-line bg-panel px-3 py-2.5 text-xs leading-5 text-muted">Codex uses your existing Codex CLI login. Run <code className="rounded bg-code px-1 py-0.5 font-mono text-[11px]">codex login</code> if needed.</div>}
          {!custom && form.provider === 'llamacpp' && <div className="mt-4 rounded-lg border border-line bg-panel px-3 py-2.5 text-xs leading-5 text-muted">llama.cpp uses the local endpoint at <code className="rounded bg-code px-1 py-0.5 font-mono text-[11px]">http://127.0.0.1:8081/v1</code>.</div>}

          <div className="mt-5 border-t border-line pt-5">
            <div className="mb-3 text-[13px] font-semibold text-ink">Appearance</div>
            <label className={`${labelClass} max-w-56`}>
              Font size
              <select className={fieldClass} value={selectedFontSize} disabled={saving} onChange={event => setSelectedFontSize(event.target.value)}>
                <option value="small">Small</option>
                <option value="default">Default</option>
                <option value="large">Large</option>
              </select>
            </label>
          </div>

          {error && <div className="mt-4 rounded-lg border border-danger/30 bg-danger/5 px-3 py-2.5 text-xs leading-5 text-danger" role="alert">{error}</div>}
        </div>

        <div className="flex shrink-0 items-center gap-2 border-t border-line-strong px-5 py-3">
          <span className="min-w-0 flex-1 text-xs text-faint">Blank effort uses the provider default.</span>
          <button className={secondaryButtonClass} type="button" disabled={saving} onClick={onClose}>Cancel</button>
          <button className="min-w-19 rounded-full bg-accent px-4 py-1.75 text-[13px] text-white hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-60" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
      </form>}
    </div>
  </div>
}
