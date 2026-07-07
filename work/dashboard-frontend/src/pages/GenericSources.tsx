import { useEffect, useState } from 'react'
import { Save, RotateCcw, Plus, Trash2, Eye, Loader2 } from 'lucide-react'
import { notifyRestartRequired } from '@/components/RestartBanner'
import { useDirty } from '@/context/DirtyContext'
import {
  fetchGenericSources,
  saveGenericSources,
  previewGenericSource,
  type GenericSource,
  type FieldMapping,
  type GenericSourcePreview,
} from '@/lib/api'

const SEVERITIES = ['routine', 'priority', 'immediate'] as const

function blankSource(n: number): GenericSource {
  return {
    name: `source_${n}`,
    enabled: true,
    url: '',
    items_path: 'features',
    id_path: '',
    lat_path: '',
    lon_path: '',
    geometry_path: 'geometry',
    title_path: '',
    time_path: '',
    category: 'generic_alert',
    poll_seconds: 300,
    severity: 'routine',
    field_mappings: [],
    summary_template: '',
    emoji: '',
  }
}

// Small labelled text input used throughout the cards.
function Field({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
  mono = false,
}: {
  label: string
  value: string | number
  onChange: (v: string) => void
  placeholder?: string
  type?: string
  mono?: boolean
}) {
  return (
    <div className="min-w-0">
      <label className="text-xs text-[#777] mb-1 block">{label}</label>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={`w-full bg-[#0d0d0d] border border-border px-2 py-1.5 text-xs ${
          mono ? 'font-mono' : ''
        } text-[#e0e0e0] placeholder:text-[#555]`}
      />
    </div>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] pt-1">
      {children}
    </div>
  )
}

export default function GenericSources() {
  const [sources, setSources] = useState<GenericSource[] | null>(null)
  const [original, setOriginal] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const { setDirty } = useDirty()

  // Per-card preview state, keyed by source index.
  const [previews, setPreviews] = useState<Record<number, GenericSourcePreview>>({})
  const [previewing, setPreviewing] = useState<Record<number, boolean>>({})

  useEffect(() => {
    document.title = 'Data Sources — MeshAI'
    fetchGenericSources()
      .then((data) => {
        const list = Array.isArray(data) ? data : []
        // Normalize so every field the form binds to is defined (avoids
        // controlled/uncontrolled input churn).
        const norm = list.map((s, i) => ({ ...blankSource(i + 1), ...s }))
        setSources(norm)
        setOriginal(JSON.stringify(norm))
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  const hasChanges = sources !== null && JSON.stringify(sources) !== original

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const update = (idx: number, patch: Partial<GenericSource>) => {
    setSources((prev) =>
      prev ? prev.map((s, i) => (i === idx ? { ...s, ...patch } : s)) : prev,
    )
  }

  const addSource = () => {
    setSources((prev) => [...(prev ?? []), blankSource((prev?.length ?? 0) + 1)])
  }

  const deleteSource = (idx: number) => {
    setSources((prev) => (prev ? prev.filter((_, i) => i !== idx) : prev))
    setPreviews((prev) => {
      const next = { ...prev }
      delete next[idx]
      return next
    })
  }

  const addMapping = (idx: number) => {
    setSources((prev) =>
      prev
        ? prev.map((s, i) =>
            i === idx
              ? { ...s, field_mappings: [...s.field_mappings, { source_path: '', dest_key: '' }] }
              : s,
          )
        : prev,
    )
  }

  const updateMapping = (idx: number, mIdx: number, patch: Partial<FieldMapping>) => {
    setSources((prev) =>
      prev
        ? prev.map((s, i) =>
            i === idx
              ? {
                  ...s,
                  field_mappings: s.field_mappings.map((m, j) =>
                    j === mIdx ? { ...m, ...patch } : m,
                  ),
                }
              : s,
          )
        : prev,
    )
  }

  const removeMapping = (idx: number, mIdx: number) => {
    setSources((prev) =>
      prev
        ? prev.map((s, i) =>
            i === idx
              ? { ...s, field_mappings: s.field_mappings.filter((_, j) => j !== mIdx) }
              : s,
          )
        : prev,
    )
  }

  const runPreview = async (idx: number) => {
    if (!sources) return
    const src = sources[idx]
    setPreviewing((p) => ({ ...p, [idx]: true }))
    try {
      const res = await previewGenericSource(src.url, src.items_path)
      setPreviews((p) => ({ ...p, [idx]: res }))
    } catch (e) {
      setPreviews((p) => ({
        ...p,
        [idx]: { ok: false, error: e instanceof Error ? e.message : String(e) },
      }))
    } finally {
      setPreviewing((p) => ({ ...p, [idx]: false }))
    }
  }

  const discard = () => {
    if (original) {
      setSources(JSON.parse(original) as GenericSource[])
      setPreviews({})
    }
  }

  const save = async () => {
    if (!sources) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const result = await saveGenericSources(sources)
      setOriginal(JSON.stringify(sources))
      setSuccess('Data sources saved')
      setTimeout(() => setSuccess(null), 3000)
      if (result.restart_required) {
        notifyRestartRequired(Array.isArray(result.changed_keys) ? result.changed_keys : [])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-[#777]">
        Loading data sources…
      </div>
    )
  }
  if (!sources) {
    return (
      <div className="flex items-center justify-center h-64 text-red-400">
        {error || 'No config'}
      </div>
    )
  }

  const actionButtons = (
    <div className="flex items-center gap-2 flex-shrink-0">
      <button
        onClick={discard}
        className="flex items-center gap-1 px-3 py-1.5 text-sm text-[#777] hover:text-white border border-border"
      >
        <RotateCcw size={14} /> Discard
      </button>
      <button
        onClick={save}
        disabled={saving}
        className="flex items-center gap-1 px-3 py-1.5 text-sm bg-accent text-white disabled:opacity-50"
      >
        <Save size={14} /> {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  )

  return (
    <div className="space-y-6 max-w-4xl">
      {/* Page description + action bar */}
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm text-[#777]">
          Point MeshAI at any public REST or GeoJSON feed — no code. Each source polls a URL,
          maps its JSON fields to an event via dotted paths, and broadcasts through the normal
          coverage-gated pipeline. Use <span className="text-accent">Preview</span> to fetch a
          URL and read its structure before filling in the paths.
        </p>
        {hasChanges && actionButtons}
      </div>

      {error && <div className="text-sm text-red-400 bg-red-500/10 p-3">{error}</div>}
      {success && <div className="text-sm text-green-400 bg-green-500/10 p-3">{success}</div>}

      {sources.length === 0 && (
        <div className="border border-border p-6 text-center text-sm text-[#666]">
          No data sources yet. Click “Add source” to wire up a public feed.
        </div>
      )}

      <div className="space-y-4">
        {sources.map((src, i) => {
          const preview = previews[i]
          const isPreviewing = previewing[i]
          return (
            <div key={i} className="border border-border p-4 space-y-4">
              {/* Header: name + enabled + delete */}
              <div className="flex items-center gap-3">
                <input
                  type="text"
                  value={src.name}
                  onChange={(e) => update(i, { name: e.target.value })}
                  placeholder="source name (unique id)"
                  className="flex-1 bg-[#0d0d0d] border border-border px-2 py-1.5 text-sm font-medium text-[#e0e0e0]"
                />
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <span className="text-xs text-[#666]">Enabled</span>
                  <button
                    type="button"
                    onClick={() => update(i, { enabled: !src.enabled })}
                    className={`relative w-9 h-4 rounded-full transition-colors ${
                      src.enabled ? 'bg-accent' : 'bg-[#333]'
                    }`}
                  >
                    <span
                      className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${
                        src.enabled ? 'translate-x-5' : ''
                      }`}
                    />
                  </button>
                </label>
                <button
                  onClick={() => deleteSource(i)}
                  title="Delete source"
                  className="flex items-center gap-1 px-2 py-1.5 text-xs text-[#777] hover:text-red-400 border border-border"
                >
                  <Trash2 size={12} />
                </button>
              </div>

              {/* Basics */}
              <SectionLabel>Basics</SectionLabel>
              <div className="grid grid-cols-12 gap-2">
                <div className="col-span-12">
                  <Field
                    label="URL"
                    value={src.url}
                    onChange={(v) => update(i, { url: v })}
                    placeholder="https://example.com/api/feed.geojson"
                    mono
                  />
                </div>
                <div className="col-span-3">
                  <Field
                    label="Poll seconds"
                    type="number"
                    value={src.poll_seconds}
                    onChange={(v) => update(i, { poll_seconds: parseInt(v, 10) || 0 })}
                  />
                </div>
                <div className="col-span-3">
                  <Field
                    label="Category"
                    value={src.category}
                    onChange={(v) => update(i, { category: v })}
                    placeholder="generic_alert"
                  />
                </div>
                <div className="col-span-3">
                  <label className="text-xs text-[#777] mb-1 block">Severity</label>
                  <select
                    value={src.severity}
                    onChange={(e) => update(i, { severity: e.target.value })}
                    className="w-full bg-[#0d0d0d] border border-border px-2 py-1.5 text-xs text-[#e0e0e0]"
                  >
                    {SEVERITIES.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="col-span-3">
                  <Field
                    label="Emoji"
                    value={src.emoji ?? ''}
                    onChange={(v) => update(i, { emoji: v })}
                    placeholder="⚡"
                  />
                </div>
              </div>

              {/* Extraction */}
              <SectionLabel>Extraction</SectionLabel>
              <div className="grid grid-cols-2 gap-2">
                <Field
                  label="items_path (array of items)"
                  value={src.items_path}
                  onChange={(v) => update(i, { items_path: v })}
                  placeholder="features · object.outages"
                  mono
                />
                <Field
                  label="id_path (unique id, for dedup)"
                  value={src.id_path}
                  onChange={(v) => update(i, { id_path: v })}
                  placeholder="id · omsOutageId · properties.id"
                  mono
                />
              </div>

              {/* Location */}
              <SectionLabel>Location — GeoJSON geometry OR lat + lon paths</SectionLabel>
              <div className="grid grid-cols-3 gap-2">
                <Field
                  label="geometry_path"
                  value={src.geometry_path ?? ''}
                  onChange={(v) => update(i, { geometry_path: v })}
                  placeholder="geometry"
                  mono
                />
                <Field
                  label="lat_path"
                  value={src.lat_path ?? ''}
                  onChange={(v) => update(i, { lat_path: v })}
                  placeholder="properties.lat"
                  mono
                />
                <Field
                  label="lon_path"
                  value={src.lon_path ?? ''}
                  onChange={(v) => update(i, { lon_path: v })}
                  placeholder="properties.lon"
                  mono
                />
              </div>

              {/* Display */}
              <SectionLabel>Display</SectionLabel>
              <div className="grid grid-cols-2 gap-2">
                <Field
                  label="title_path"
                  value={src.title_path ?? ''}
                  onChange={(v) => update(i, { title_path: v })}
                  placeholder="properties.headline"
                  mono
                />
                <Field
                  label="time_path"
                  value={src.time_path ?? ''}
                  onChange={(v) => update(i, { time_path: v })}
                  placeholder="properties.updated"
                  mono
                />
                <div className="col-span-2">
                  <Field
                    label="summary_template — use {dest_key} tokens from your field mappings"
                    value={src.summary_template ?? ''}
                    onChange={(v) => update(i, { summary_template: v })}
                    placeholder="⚡ Power out — {customers} affected, ETA {eta}"
                    mono
                  />
                </div>
              </div>

              {/* Field mappings */}
              <div className="border border-border p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666]">
                    Field Mappings
                  </div>
                  <button
                    onClick={() => addMapping(i)}
                    className="flex items-center gap-1 px-2 py-1 text-xs bg-accent/20 hover:bg-accent/30 text-accent border border-accent/30"
                  >
                    <Plus size={12} /> Add mapping
                  </button>
                </div>
                {src.field_mappings.length === 0 ? (
                  <p className="text-xs text-[#555]">
                    No mappings. Each mapping pulls a dotted <code>source_path</code> from an item
                    into a <code>dest_key</code> you can reference in the summary template.
                  </p>
                ) : (
                  <div className="space-y-2">
                    {src.field_mappings.map((m, mIdx) => (
                      <div key={mIdx} className="flex items-center gap-2">
                        <input
                          type="text"
                          value={m.source_path}
                          onChange={(e) => updateMapping(i, mIdx, { source_path: e.target.value })}
                          placeholder="source_path (e.g. properties.customers)"
                          className="flex-1 bg-[#0d0d0d] border border-border px-2 py-1.5 text-xs font-mono text-[#e0e0e0] placeholder:text-[#555]"
                        />
                        <span className="text-[#555] text-xs">→</span>
                        <input
                          type="text"
                          value={m.dest_key}
                          onChange={(e) => updateMapping(i, mIdx, { dest_key: e.target.value })}
                          placeholder="dest_key (e.g. customers)"
                          className="flex-1 bg-[#0d0d0d] border border-border px-2 py-1.5 text-xs font-mono text-[#e0e0e0] placeholder:text-[#555]"
                        />
                        <button
                          onClick={() => removeMapping(i, mIdx)}
                          title="Remove mapping"
                          className="flex items-center px-2 py-1.5 text-xs text-[#777] hover:text-red-400 border border-border"
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Preview */}
              <div className="border border-border p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666]">
                    Preview
                  </div>
                  <button
                    onClick={() => runPreview(i)}
                    disabled={!src.url || isPreviewing}
                    className="flex items-center gap-1 px-2 py-1 text-xs bg-accent/20 hover:bg-accent/30 text-accent border border-accent/30 disabled:opacity-40"
                  >
                    {isPreviewing ? <Loader2 size={12} className="animate-spin" /> : <Eye size={12} />}
                    {isPreviewing ? 'Fetching…' : 'Preview'}
                  </button>
                </div>
                {preview && (
                  <div className="space-y-2">
                    {preview.ok ? (
                      <div className="text-xs text-green-400">
                        HTTP {preview.status ?? '200'}
                        {typeof preview.item_count === 'number' && (
                          <span className="text-[#999]">
                            {' '}
                            — items_path resolved {preview.item_count} item
                            {preview.item_count === 1 ? '' : 's'}
                          </span>
                        )}
                      </div>
                    ) : (
                      <div className="text-xs text-red-400 break-words">{preview.error}</div>
                    )}
                    {preview.items_path_note && (
                      <div className="text-xs text-amber-400 break-words">
                        {preview.items_path_note}
                      </div>
                    )}
                    {preview.first_item && (
                      <div>
                        <div className="text-[10px] uppercase tracking-widest text-[#555] mb-1">
                          First item
                        </div>
                        <pre className="bg-[#0d0d0d] border border-border p-2 text-[11px] font-mono text-[#bbb] overflow-auto max-h-48 whitespace-pre">
                          {preview.first_item}
                        </pre>
                      </div>
                    )}
                    {preview.sample && (
                      <details open={!preview.first_item}>
                        <summary className="text-[10px] uppercase tracking-widest text-[#555] cursor-pointer">
                          Raw response
                        </summary>
                        <pre className="mt-1 bg-[#0d0d0d] border border-border p-2 text-[11px] font-mono text-[#bbb] overflow-auto max-h-72 whitespace-pre">
                          {preview.sample}
                        </pre>
                      </details>
                    )}
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* Add + bottom save bar */}
      <div className="flex items-center justify-between gap-2 pb-2">
        <button
          onClick={addSource}
          className="flex items-center gap-1 px-3 py-1.5 text-sm bg-accent/20 hover:bg-accent/30 text-accent border border-accent/30"
        >
          <Plus size={14} /> Add source
        </button>
        {hasChanges && actionButtons}
      </div>
    </div>
  )
}
