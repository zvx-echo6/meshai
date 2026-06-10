// v0.6-3c Adapter Config editor.
//
// Renders one card per adapter. Each card shows:
//   - display_name + include_in_llm_context toggle at the top
//   - expandable list of (config key, type-aware widget, reset button)
//
// Auto-saves on blur (text/number inputs) or change (bool toggle + select).
// Cache invalidation is server-side -- every PUT triggers it. The handler
// reads via the in-process accessor on its next call.

import { useEffect, useState, useCallback } from 'react'
import {
  ChevronDown, ChevronRight, RotateCcw, Loader2, Check, AlertCircle,
  Sliders,
} from 'lucide-react'

interface ConfigRow {
  adapter: string
  key: string
  value: unknown
  default: unknown
  type: 'int' | 'float' | 'str' | 'bool' | 'json'
  description: string
  updated_at: number
}

interface MetaRow {
  display_name: string
  include_in_llm_context: boolean
  description: string
}

type GroupedConfig = Record<string, ConfigRow[]>
type MetaMap = Record<string, MetaRow>

type SaveStatus = 'idle' | 'saving' | 'saved' | 'error'

// Brief animation after a successful save.
const SAVED_BADGE_MS = 1500

export default function AdapterConfig() {
  const [config, setConfig] = useState<GroupedConfig>({})
  const [meta, setMeta] = useState<MetaMap>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  // Per-key save status: keyed on `${adapter}.${key}` or `meta:${adapter}`.
  const [saveStatus, setSaveStatus] = useState<Record<string, SaveStatus>>({})
  const [saveError, setSaveError] = useState<Record<string, string>>({})

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [cfgRes, metaRes] = await Promise.all([
        fetch('/api/adapter-config'),
        fetch('/api/adapter-meta'),
      ])
      if (!cfgRes.ok) throw new Error(`GET /adapter-config: ${cfgRes.status}`)
      if (!metaRes.ok) throw new Error(`GET /adapter-meta: ${metaRes.status}`)
      setConfig(await cfgRes.json())
      setMeta(await metaRes.json())
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const markStatus = useCallback((id: string, status: SaveStatus, errMsg?: string) => {
    setSaveStatus((s) => ({ ...s, [id]: status }))
    if (errMsg) setSaveError((s) => ({ ...s, [id]: errMsg }))
    if (status === 'saved') {
      setTimeout(() => {
        setSaveStatus((s) => (s[id] === 'saved' ? { ...s, [id]: 'idle' } : s))
      }, SAVED_BADGE_MS)
    }
  }, [])

  // ---------- key-level mutations ----------------------------------------

  const putValue = useCallback(async (adapter: string, key: string, value: unknown) => {
    const id = `${adapter}.${key}`
    markStatus(id, 'saving')
    try {
      const res = await fetch(`/api/adapter-config/${adapter}/${key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        const detail = body.detail || res.statusText
        markStatus(id, 'error', String(detail))
        return
      }
      const updated: ConfigRow = await res.json()
      setConfig((c) => ({
        ...c,
        [adapter]: (c[adapter] || []).map((row) => row.key === key ? updated : row),
      }))
      markStatus(id, 'saved')
    } catch (e) {
      markStatus(id, 'error', String(e))
    }
  }, [markStatus])

  const resetValue = useCallback(async (adapter: string, key: string) => {
    const id = `${adapter}.${key}`
    markStatus(id, 'saving')
    try {
      const res = await fetch(`/api/adapter-config/${adapter}/${key}/reset`, {
        method: 'POST',
      })
      if (!res.ok) {
        markStatus(id, 'error', `reset failed (${res.status})`)
        return
      }
      const updated: ConfigRow = await res.json()
      setConfig((c) => ({
        ...c,
        [adapter]: (c[adapter] || []).map((row) => row.key === key ? updated : row),
      }))
      markStatus(id, 'saved')
    } catch (e) {
      markStatus(id, 'error', String(e))
    }
  }, [markStatus])

  const putMeta = useCallback(async (adapter: string, body: Partial<MetaRow>) => {
    const id = `meta:${adapter}`
    markStatus(id, 'saving')
    try {
      const res = await fetch(`/api/adapter-meta/${adapter}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const b = await res.json().catch(() => ({}))
        markStatus(id, 'error', String(b.detail || res.statusText))
        return
      }
      const updated: MetaRow = await res.json()
      setMeta((m) => ({ ...m, [adapter]: updated }))
      markStatus(id, 'saved')
    } catch (e) {
      markStatus(id, 'error', String(e))
    }
  }, [markStatus])

  // ---------- render ------------------------------------------------------

  if (loading) {
    return (
      <div className="p-6 flex items-center gap-2 text-slate-400">
        <Loader2 className="w-5 h-5 animate-spin" /> Loading adapter config…
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-6 text-red-400">
        <AlertCircle className="w-5 h-5 inline mr-2" />
        Failed to load: {error}
      </div>
    )
  }

  // Union of all adapters: any meta row + any config-having adapter.
  const allAdapters = Array.from(new Set([
    ...Object.keys(meta),
    ...Object.keys(config),
  ])).sort()

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-2 text-slate-200">
        <Sliders className="w-5 h-5" />
        <h1 className="text-xl font-semibold">Adapter Config</h1>
        <span className="text-xs text-slate-500 ml-2">
          {Object.values(config).reduce((n, l) => n + l.length, 0)} settings across {allAdapters.length} adapters
        </span>
      </div>
      <p className="text-xs text-slate-400 max-w-3xl">
        Per-adapter tunables (thresholds, freshness windows, toggles, curation lists).
        Changes take effect on the next handler call -- no container restart needed.
        Sentence templates, emoji, and translation maps live in code by design — see the CODE rule under <a href="/reference#adapter-config" className="text-accent hover:underline">Adapter Config &amp; the CODE Rule</a> in Reference. The <strong>LLM context</strong> toggle on each card gates whether that adapter's data lands in the system prompt when you DM the bot; broadcasts are unaffected.
      </p>

      {allAdapters.map((adapter) => {
        const m = meta[adapter] || {
          display_name: adapter,
          include_in_llm_context: true,
          description: '',
        }
        const rows = config[adapter] || []
        const isExpanded = expanded[adapter] ?? false
        const metaId = `meta:${adapter}`
        const metaStatus = saveStatus[metaId] || 'idle'
        return (
          <div key={adapter} className="bg-slate-800/60 border border-slate-700 rounded-lg">
            {/* Card header */}
            <div className="p-4 flex items-start gap-4">
              <button
                onClick={() => setExpanded((e) => ({ ...e, [adapter]: !e[adapter] }))}
                className="text-slate-400 hover:text-white"
                aria-label="toggle expand"
              >
                {isExpanded
                  ? <ChevronDown className="w-5 h-5" />
                  : <ChevronRight className="w-5 h-5" />}
              </button>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <h2 className="text-base font-semibold text-slate-100">{m.display_name}</h2>
                  <code className="text-xs text-slate-500">{adapter}</code>
                  {rows.length > 0 && (
                    <span className="text-xs text-slate-400 ml-1">({rows.length} settings)</span>
                  )}
                  {rows.length === 0 && (
                    <span className="text-xs text-slate-500 ml-1 italic">(meta only)</span>
                  )}
                </div>
                {m.description && (
                  <p className="text-xs text-slate-400 mt-1">{m.description}</p>
                )}
              </div>

              {/* include_in_llm_context toggle */}
              <label className="flex items-center gap-2 text-xs text-slate-300 select-none">
                <input
                  type="checkbox"
                  checked={m.include_in_llm_context}
                  onChange={(e) => putMeta(adapter, { include_in_llm_context: e.target.checked })}
                  className="w-4 h-4 accent-cyan-500"
                />
                LLM context
                <SaveBadge status={metaStatus} error={saveError[metaId]} />
              </label>
            </div>

            {/* Expanded body */}
            {isExpanded && rows.length > 0 && (
              <div className="border-t border-slate-700 divide-y divide-slate-700/60">
                {rows.map((row) => (
                  <KeyRow
                    key={row.key}
                    row={row}
                    status={saveStatus[`${adapter}.${row.key}`] || 'idle'}
                    error={saveError[`${adapter}.${row.key}`]}
                    onCommit={(v) => putValue(adapter, row.key, v)}
                    onReset={() => resetValue(adapter, row.key)}
                  />
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}


// ---------- KeyRow ---------------------------------------------------------


interface KeyRowProps {
  row: ConfigRow
  status: SaveStatus
  error?: string
  onCommit: (v: unknown) => void
  onReset: () => void
}

function KeyRow({ row, status, error, onCommit, onReset }: KeyRowProps) {
  // Use a local draft so number/text inputs don't fight the parent state
  // mid-edit. Commit on blur.
  const [draft, setDraft] = useState<string>(stringifyForInput(row))

  // If the parent value changes (e.g. via reset), refresh the local draft.
  useEffect(() => {
    setDraft(stringifyForInput(row))
  }, [row.value, row.type])

  const isDirty = draft !== stringifyForInput(row)
  const isDefault = JSON.stringify(row.value) === JSON.stringify(row.default)

  const commit = () => {
    const parsed = parseFromInput(draft, row.type)
    if (parsed.error) return   // error shown inline; do not PUT
    if (!parsed.changed(row.value)) return
    onCommit(parsed.value)
  }

  return (
    <div className="px-6 py-3 flex items-start gap-4">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <code className="text-sm font-mono text-cyan-300">{row.key}</code>
          <span className="text-xs text-slate-500">[{row.type}]</span>
          {!isDefault && (
            <span className="text-xs text-amber-400">edited</span>
          )}
        </div>
        {row.description && (
          <p className="text-xs text-slate-400 mt-1">{row.description}</p>
        )}
      </div>

      <div className="flex items-center gap-2 min-w-[280px] justify-end">
        {row.type === 'bool' ? (
          <input
            type="checkbox"
            checked={row.value === true}
            onChange={(e) => onCommit(e.target.checked)}
            className="w-5 h-5 accent-cyan-500"
          />
        ) : row.type === 'json' ? (
          <textarea
            className="w-72 h-20 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs font-mono text-slate-100"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
          />
        ) : (
          <input
            type={row.type === 'int' || row.type === 'float' ? 'number' : 'text'}
            step={row.type === 'float' ? 'any' : '1'}
            className="w-48 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-sm text-slate-100"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
          />
        )}

        <SaveBadge status={status} error={error} dirty={isDirty} />

        <button
          onClick={onReset}
          disabled={isDefault}
          className="text-slate-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
          title="Reset to default"
        >
          <RotateCcw className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}


// ---------- SaveBadge ------------------------------------------------------


function SaveBadge({ status, error, dirty }: { status: SaveStatus; error?: string; dirty?: boolean }) {
  if (status === 'saving') return <Loader2 className="w-4 h-4 text-cyan-400 animate-spin" />
  if (status === 'saved') return <Check className="w-4 h-4 text-emerald-400" />
  if (status === 'error') return (
    <span title={error} className="text-red-400 cursor-help">
      <AlertCircle className="w-4 h-4" />
    </span>
  )
  if (dirty) return <span className="w-2 h-2 bg-amber-400 rounded-full" title="unsaved" />
  return <span className="w-4 h-4" />
}


// ---------- input <-> JSON helpers ----------------------------------------


function stringifyForInput(row: ConfigRow): string {
  if (row.type === 'bool') return String(row.value === true)
  if (row.type === 'json') return JSON.stringify(row.value, null, 2)
  if (row.value === null || row.value === undefined) return ''
  return String(row.value)
}

function parseFromInput(s: string, type: ConfigRow['type']):
  | { error: string; value: null; changed: () => boolean }
  | { error: null; value: unknown; changed: (prev: unknown) => boolean }
{
  if (type === 'int') {
    const n = Number(s)
    if (!Number.isFinite(n) || !Number.isInteger(n)) {
      return { error: 'expected integer', value: null, changed: () => false }
    }
    return { error: null, value: n, changed: (prev) => prev !== n }
  }
  if (type === 'float') {
    const n = Number(s)
    if (!Number.isFinite(n)) {
      return { error: 'expected number', value: null, changed: () => false }
    }
    return { error: null, value: n, changed: (prev) => prev !== n }
  }
  if (type === 'str') {
    return { error: null, value: s, changed: (prev) => prev !== s }
  }
  if (type === 'json') {
    try {
      const v = JSON.parse(s)
      return { error: null, value: v, changed: (prev) => JSON.stringify(prev) !== JSON.stringify(v) }
    } catch {
      return { error: 'invalid JSON', value: null, changed: () => false }
    }
  }
  // bool branch handled inline -- never reaches parseFromInput.
  return { error: null, value: s, changed: () => true }
}
