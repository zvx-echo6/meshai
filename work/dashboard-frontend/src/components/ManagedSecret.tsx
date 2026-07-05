import { useState, useEffect } from 'react'
import { Eye, EyeOff } from 'lucide-react'

interface SecretEntry {
  env_var: string
  is_set: boolean
  fields?: string[]
  label?: string
}

export function ManagedSecret({ envVar, label = 'API Key', helper = '', info: _info = '' }: {
  envVar: string
  label?: string
  helper?: string
  info?: string
}) {
  const [isSet, setIsSet] = useState<boolean | null>(null) // null = loading
  const [value, setValue] = useState('')
  const [show, setShow] = useState(false)
  const [saving, setSaving] = useState(false)
  const [restartMsg, setRestartMsg] = useState('')
  const [error, setError] = useState('')

  const fetchStatus = async () => {
    try {
      const res = await fetch('/api/secrets')
      if (res.ok) {
        const data: SecretEntry[] = await res.json()
        const entry = data.find((e) => e.env_var === envVar)
        setIsSet(entry ? entry.is_set : false)
      }
    } catch {
      setIsSet(false)
    }
  }

  useEffect(() => {
    fetchStatus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [envVar])

  const handleSave = async () => {
    if (!value.trim()) return
    setSaving(true)
    setError('')
    setRestartMsg('')
    try {
      const res = await fetch('/api/secrets/' + encodeURIComponent(envVar), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value }),
      })
      if (res.ok) {
        setValue('')
        setShow(false)
        setRestartMsg('Saved — restart required to take effect')
        await fetchStatus()
      } else {
        const body = await res.text()
        setError('Save failed: ' + (body || String(res.status)))
      }
    } catch {
      setError('Save failed: network error')
    } finally {
      setSaving(false)
    }
  }

  const placeholder = isSet
    ? 'env set — enter a new value to change'
    : 'not set — enter a value'

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {isSet === null ? (
          <span className="text-xs px-2 py-0.5 rounded ml-2 bg-slate-800 text-slate-500">Loading</span>
        ) : isSet ? (
          <span className="text-xs px-2 py-0.5 rounded ml-2 bg-green-500/10 text-green-400">Set</span>
        ) : (
          <span className="text-xs px-2 py-0.5 rounded ml-2 bg-slate-800 text-slate-500">Not set</span>
        )}
      </label>
      <div className="flex gap-2">
        <div className="relative flex-1">
          <input
            type={show ? 'text' : 'password'}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={placeholder}
            className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
          />
          <button
            type="button"
            onClick={() => setShow(!show)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
          >
            {show ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        </div>
        <button
          type="button"
          onClick={handleSave}
          disabled={!value.trim() || saving}
          className="flex items-center gap-1.5 px-4 py-1.5 text-sm bg-accent text-white rounded hover:bg-accent/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
      <p className="text-xs text-slate-600 font-mono">{envVar}</p>
      {restartMsg && <p className="text-xs text-yellow-400">{restartMsg}</p>}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  )
}
