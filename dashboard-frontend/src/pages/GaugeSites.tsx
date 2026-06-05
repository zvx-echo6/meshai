// v0.6-4 GaugeSites table editor.
import { useEffect, useState, useCallback } from 'react'
import { Loader2, Plus, Trash2, Check, X, Droplets } from 'lucide-react'

interface GaugeSite {
  site_id: string
  gauge_name: string
  lat: number
  lon: number
  action_ft: number | null
  flood_minor_ft: number | null
  flood_moderate_ft: number | null
  flood_major_ft: number | null
  enabled: boolean
  updated_at: number
}

const EMPTY_DRAFT: GaugeSite = {
  site_id: '', gauge_name: '', lat: 0, lon: 0,
  action_ft: null, flood_minor_ft: null, flood_moderate_ft: null, flood_major_ft: null,
  enabled: true, updated_at: 0,
}

export default function GaugeSites() {
  const [rows, setRows] = useState<GaugeSite[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState<GaugeSite>(EMPTY_DRAFT)
  const [adding, setAdding] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/gauge-sites')
      if (!res.ok) throw new Error(`GET: ${res.status}`)
      setRows(await res.json())
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const beginEdit = (r: GaugeSite) => { setEditing(r.site_id); setDraft({ ...r }); setAdding(false) }
  const beginAdd = () => { setAdding(true); setEditing(null); setDraft({ ...EMPTY_DRAFT }) }
  const cancel = () => { setEditing(null); setAdding(false); setDraft(EMPTY_DRAFT) }

  const save = async () => {
    try {
      const url = adding ? '/api/gauge-sites' : `/api/gauge-sites/${editing}`
      const method = adding ? 'POST' : 'PUT'
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      })
      if (!res.ok) {
        const b = await res.json().catch(() => ({}))
        alert(`save failed: ${b.detail || res.statusText}`)
        return
      }
      cancel()
      refresh()
    } catch (e) {
      alert(String(e))
    }
  }

  const remove = async (siteId: string) => {
    if (!confirm(`Delete ${siteId}?`)) return
    const res = await fetch(`/api/gauge-sites/${siteId}`, { method: 'DELETE' })
    if (!res.ok) { alert(`delete failed: ${res.status}`); return }
    refresh()
  }

  if (loading) return <div className="p-6 text-slate-400"><Loader2 className="w-5 h-5 animate-spin inline mr-2" />Loading…</div>
  if (error) return <div className="p-6 text-red-400">Load failed: {error}</div>

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-2">
        <Droplets className="w-5 h-5 text-cyan-400" />
        <h1 className="text-xl font-semibold text-slate-100">Gauge Sites</h1>
        <span className="text-xs text-slate-500 ml-2">{rows.length} sites</span>
        <button onClick={beginAdd}
          className="ml-auto flex items-center gap-1 px-3 py-1 bg-cyan-700 hover:bg-cyan-600 rounded text-white text-sm">
          <Plus className="w-4 h-4" /> Add site
        </button>
      </div>
      <p className="text-xs text-slate-400 max-w-3xl">
        NWS-AHPS stream gauge thresholds curated for the nwis_handler. Disabled rows are
        ignored at envelope time. Changes propagate to the handler on the next event.
      </p>

      {adding && <RowEditor draft={draft} setDraft={setDraft} onSave={save} onCancel={cancel} adding />}

      <div className="bg-slate-800/60 border border-slate-700 rounded-lg overflow-x-auto">
        <table className="w-full text-sm text-slate-200">
          <thead className="bg-slate-900 text-xs text-slate-400 uppercase">
            <tr>
              <th className="px-3 py-2 text-left">Site ID</th>
              <th className="px-3 py-2 text-left">Name</th>
              <th className="px-3 py-2 text-right">Lat,Lon</th>
              <th className="px-3 py-2 text-right">Action</th>
              <th className="px-3 py-2 text-right">Minor</th>
              <th className="px-3 py-2 text-right">Moderate</th>
              <th className="px-3 py-2 text-right">Major</th>
              <th className="px-3 py-2 text-center">On</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-700/60">
            {rows.map(r => editing === r.site_id ? (
              <tr key={r.site_id} className="bg-slate-900/40">
                <td colSpan={9} className="px-3 py-2">
                  <RowEditor draft={draft} setDraft={setDraft} onSave={save} onCancel={cancel} />
                </td>
              </tr>
            ) : (
              <tr key={r.site_id} className="hover:bg-slate-800/50">
                <td className="px-3 py-2 font-mono text-xs">{r.site_id}</td>
                <td className="px-3 py-2">{r.gauge_name}</td>
                <td className="px-3 py-2 text-right text-xs">{r.lat.toFixed(3)},{r.lon.toFixed(3)}</td>
                <td className="px-3 py-2 text-right">{r.action_ft ?? '-'}</td>
                <td className="px-3 py-2 text-right">{r.flood_minor_ft ?? '-'}</td>
                <td className="px-3 py-2 text-right">{r.flood_moderate_ft ?? '-'}</td>
                <td className="px-3 py-2 text-right">{r.flood_major_ft ?? '-'}</td>
                <td className="px-3 py-2 text-center">{r.enabled ? <Check className="w-4 h-4 text-emerald-400 inline" /> : <X className="w-4 h-4 text-slate-500 inline" />}</td>
                <td className="px-3 py-2 text-right">
                  <button onClick={() => beginEdit(r)} className="text-cyan-400 hover:text-cyan-300 text-xs mr-3">Edit</button>
                  <button onClick={() => remove(r.site_id)} className="text-red-400 hover:text-red-300"><Trash2 className="w-4 h-4 inline" /></button>
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
  draft: GaugeSite, setDraft: (g: GaugeSite) => void,
  onSave: () => void, onCancel: () => void, adding?: boolean,
}) {
  const upd = (k: keyof GaugeSite, v: unknown) => setDraft({ ...draft, [k]: v })
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 p-3 bg-slate-900/50 rounded">
      <label className="text-xs text-slate-400 col-span-2">
        Site ID
        <input className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100 font-mono text-xs"
          value={draft.site_id} onChange={e => upd('site_id', e.target.value)} disabled={!adding} />
      </label>
      <label className="text-xs text-slate-400 col-span-2">
        Gauge name
        <input className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.gauge_name} onChange={e => upd('gauge_name', e.target.value)} />
      </label>
      <label className="text-xs text-slate-400">Lat
        <input type="number" step="any" className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.lat} onChange={e => upd('lat', parseFloat(e.target.value))} />
      </label>
      <label className="text-xs text-slate-400">Lon
        <input type="number" step="any" className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.lon} onChange={e => upd('lon', parseFloat(e.target.value))} />
      </label>
      <label className="text-xs text-slate-400">Action ft
        <input type="number" step="any" className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.action_ft ?? ''} onChange={e => upd('action_ft', e.target.value === '' ? null : parseFloat(e.target.value))} />
      </label>
      <label className="text-xs text-slate-400">Minor flood ft
        <input type="number" step="any" className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.flood_minor_ft ?? ''} onChange={e => upd('flood_minor_ft', e.target.value === '' ? null : parseFloat(e.target.value))} />
      </label>
      <label className="text-xs text-slate-400">Moderate flood ft
        <input type="number" step="any" className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.flood_moderate_ft ?? ''} onChange={e => upd('flood_moderate_ft', e.target.value === '' ? null : parseFloat(e.target.value))} />
      </label>
      <label className="text-xs text-slate-400">Major flood ft
        <input type="number" step="any" className="block w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-100"
          value={draft.flood_major_ft ?? ''} onChange={e => upd('flood_major_ft', e.target.value === '' ? null : parseFloat(e.target.value))} />
      </label>
      <label className="text-xs text-slate-300 col-span-2 flex items-center gap-2 mt-2">
        <input type="checkbox" checked={draft.enabled} onChange={e => upd('enabled', e.target.checked)} className="accent-cyan-500" />
        Enabled
      </label>
      <div className="col-span-2 flex items-center justify-end gap-2 mt-2">
        <button onClick={onCancel} className="px-3 py-1 text-slate-300 hover:bg-slate-700 rounded text-sm">Cancel</button>
        <button onClick={onSave} className="px-3 py-1 bg-cyan-700 hover:bg-cyan-600 text-white rounded text-sm">Save</button>
      </div>
    </div>
  )
}
