// v0.6-4 TownAnchors table editor.
import { useEffect, useState, useCallback } from 'react'
import { Loader2, Plus, Trash2, Check, X, MapPin } from 'lucide-react'

interface TownAnchor {
  anchor_id: number
  name: string
  lat: number
  lon: number
  state: string | null
  enabled: boolean
  updated_at: number
}

const EMPTY_DRAFT: TownAnchor = {
  anchor_id: 0, name: '', lat: 0, lon: 0, state: 'ID', enabled: true, updated_at: 0,
}

export default function TownAnchors() {
  const [rows, setRows] = useState<TownAnchor[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<number | null>(null)
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState<TownAnchor>(EMPTY_DRAFT)

  const refresh = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const res = await fetch('/api/town-anchors')
      if (!res.ok) throw new Error(`GET: ${res.status}`)
      setRows(await res.json())
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }, [])
  useEffect(() => { refresh() }, [refresh])

  const beginEdit = (r: TownAnchor) => { setEditing(r.anchor_id); setDraft({ ...r }); setAdding(false) }
  const beginAdd = () => { setAdding(true); setEditing(null); setDraft({ ...EMPTY_DRAFT }) }
  const cancel = () => { setEditing(null); setAdding(false); setDraft(EMPTY_DRAFT) }

  const save = async () => {
    const url = adding ? '/api/town-anchors' : `/api/town-anchors/${editing}`
    const method = adding ? 'POST' : 'PUT'
    const res = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft),
    })
    if (!res.ok) {
      const b = await res.json().catch(() => ({}))
      alert(`save failed: ${b.detail || res.statusText}`); return
    }
    cancel(); refresh()
  }

  const remove = async (id: number) => {
    if (!confirm(`Delete anchor ${id}?`)) return
    const res = await fetch(`/api/town-anchors/${id}`, { method: 'DELETE' })
    if (!res.ok) { alert(`delete failed: ${res.status}`); return }
    refresh()
  }

  if (loading) return <div className="p-6 text-slate-400"><Loader2 className="w-5 h-5 animate-spin inline mr-2" />Loading…</div>
  if (error) return <div className="p-6 text-red-400">Load failed: {error}</div>

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-2">
        <MapPin className="w-5 h-5 text-accent" />
        <h1 className="text-xl font-semibold text-slate-100">Town Anchors</h1>
        <span className="text-xs text-slate-500 ml-2">{rows.length} towns</span>
        <button onClick={beginAdd}
          className="ml-auto flex items-center gap-1 px-3 py-1 bg-[#f59e0b] hover:bg-[#d97706] text-black font-sans font-medium text-sm">
          <Plus className="w-4 h-4" /> Add town
        </button>
      </div>
      <p className="text-xs text-slate-400 max-w-3xl">
        Lookup table for the &quot;X mi &lt;bearing&gt; of &lt;town&gt;&quot; suffix in the bot&apos;s broadcast text.
        When a fire or NWS alert renders, the bot walks: Photon nearest-town &rarr; this table &rarr; landclass &rarr;
        county/state &rarr; bare coords. Disabled rows fall through to the next anchor in the chain; the
        broadcast still goes out, it just uses a different anchor. Example: &quot;3 mi N of Almo&quot;.
        See Reference &rarr; Curation: Gauges &amp; Towns for the full chain.
      </p>

      {adding && <RowEditor draft={draft} setDraft={setDraft} onSave={save} onCancel={cancel} adding />}

      <div className="bg-slate-800/60 border border-slate-700 overflow-x-auto">
        <table className="w-full text-sm text-slate-200">
          <thead className="bg-[#161616] border-b border-border">
            <tr>
              <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-left">Name</th>
              <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-right">Lat</th>
              <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-right">Lon</th>
              <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-center">State</th>
              <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-center">On</th>
              <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666]"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-700/60">
            {rows.map(r => editing === r.anchor_id ? (
              <tr key={r.anchor_id} className="bg-bg-card border-b border-border hover:bg-bg-hover">
                <td colSpan={6} className="px-3 py-2"><RowEditor draft={draft} setDraft={setDraft} onSave={save} onCancel={cancel} /></td>
              </tr>
            ) : (
              <tr key={r.anchor_id} className="hover:bg-slate-800/50">
                <td className="px-3 py-2 capitalize">{r.name}</td>
                <td className="px-3 py-2 text-right text-xs">{r.lat.toFixed(4)}</td>
                <td className="px-3 py-2 text-right text-xs">{r.lon.toFixed(4)}</td>
                <td className="px-3 py-2 text-center text-xs">{r.state || '-'}</td>
                <td className="px-3 py-2 text-center">{r.enabled ? <Check className="w-4 h-4 text-emerald-400 inline" /> : <X className="w-4 h-4 text-slate-500 inline" />}</td>
                <td className="px-3 py-2 text-right">
                  <button onClick={() => beginEdit(r)} className="text-accent hover:text-accent text-xs mr-3">Edit</button>
                  <button onClick={() => remove(r.anchor_id)} className="text-red-400 hover:text-red-300"><Trash2 className="w-4 h-4 inline" /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}


function RowEditor({ draft, setDraft, onSave, onCancel, adding }: {
  draft: TownAnchor, setDraft: (t: TownAnchor) => void,
  onSave: () => void, onCancel: () => void, adding?: boolean,
}) {
  const upd = (k: keyof TownAnchor, v: unknown) => setDraft({ ...draft, [k]: v })
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 p-3 bg-[#1a1a1a] rounded">
      <label className="text-xs text-slate-400 col-span-2">Name (lowercased on save)
        <input className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.name} onChange={e => upd('name', e.target.value)} disabled={!adding} />
      </label>
      <label className="text-xs text-slate-400">State
        <input className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.state ?? ''} onChange={e => upd('state', e.target.value)} />
      </label>
      <label className="text-xs text-slate-400 flex items-center gap-2">
        <input type="checkbox" checked={draft.enabled} onChange={e => upd('enabled', e.target.checked)} className="accent-[#f59e0b] mt-4" />
        Enabled
      </label>
      <label className="text-xs text-slate-400">Lat
        <input type="number" step="any" className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.lat} onChange={e => upd('lat', parseFloat(e.target.value))} />
      </label>
      <label className="text-xs text-slate-400">Lon
        <input type="number" step="any" className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.lon} onChange={e => upd('lon', parseFloat(e.target.value))} />
      </label>
      <div className="col-span-2 flex items-center justify-end gap-2 mt-2">
        <button onClick={onCancel} className="px-3 py-1 text-slate-300 hover:bg-slate-700 rounded text-sm">Cancel</button>
        <button onClick={onSave} className="px-3 py-1 bg-[#f59e0b] hover:bg-[#d97706] text-black font-sans font-medium text-sm">Save</button>
      </div>
    </div>
  )
}
