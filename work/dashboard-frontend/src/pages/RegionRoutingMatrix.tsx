import { useState, useEffect, useCallback, useRef } from 'react'
import { Save, RotateCcw, RefreshCw, Check, BellRing, AlertTriangle } from 'lucide-react'
import { useFamilies, type FamilyMeta } from './Notifications'
import ChannelPicker from '@/components/ChannelPicker'
import { useDirty } from '@/context/DirtyContext'

// ------- Types -------

interface CellValue {
  mt: number | null       // MT channel index; null = no MT delivery
  mc: string | null       // MeshCore channel name; null/'' = no MC delivery
  min_severity: string    // floor severity for this cell (default 'routine')
  enabled: boolean        // cell active?
}

type Cells = Record<string, Record<string, CellValue>>

interface RegionRouting {
  enabled: boolean        // master switch for the whole matrix
  cells: Cells            // [family][region] -> CellValue
}

// ------- Constants -------

const SEVERITY_OPTIONS = [
  { value: 'routine', label: 'Routine' },
  { value: 'priority', label: 'Priority' },
  { value: 'critical', label: 'Critical' },
  { value: 'immediate', label: 'Immediate' },
]

const DEFAULT_CELL: CellValue = {
  mt: null,
  mc: null,
  min_severity: 'routine',
  enabled: false,
}

// ------- Helpers -------

/** A cell is "filled" (included in save payload) if MT or MC is set. */
function isCellFilled(cell: CellValue): boolean {
  return cell.mt !== null || (cell.mc !== null && cell.mc.trim() !== '')
}

/**
 * Expand sparse loaded cells into a full matrix for editing.
 * Families and regions not in the loaded data get DEFAULT_CELL.
 */
function expandCells(
  loadedCells: Cells,
  familyKeys: string[],
  regions: string[],
): Cells {
  const result: Cells = {}
  for (const fam of familyKeys) {
    result[fam] = {}
    for (const region of regions) {
      result[fam][region] = {
        ...DEFAULT_CELL,
        ...(loadedCells[fam]?.[region] ?? {}),
      }
    }
  }
  return result
}

/**
 * Build the sparse payload to POST — omit any cell Matt hasn't filled in.
 */
function buildSparsePayload(routing: RegionRouting): RegionRouting {
  const cells: Cells = {}
  for (const [fam, regionMap] of Object.entries(routing.cells)) {
    for (const [region, cell] of Object.entries(regionMap)) {
      if (isCellFilled(cell)) {
        if (!cells[fam]) cells[fam] = {}
        cells[fam][region] = {
          mt: cell.mt,
          mc: cell.mc,
          min_severity: cell.min_severity,
          enabled: cell.enabled,
        }
      }
    }
  }
  return { enabled: routing.enabled, cells }
}

// ------- Component -------

export default function RegionRoutingMatrix() {
  const { setDirty } = useDirty()
  const families = useFamilies()

  // Keep a ref so fetchAll closure stays stable while still seeing latest families
  const familiesRef = useRef<FamilyMeta[]>(families)
  useEffect(() => { familiesRef.current = families }, [families])

  const [regions, setRegions] = useState<string[]>([])
  const [routing, setRouting] = useState<RegionRouting>({ enabled: false, cells: {} })
  const [originalRouting, setOriginalRouting] = useState<RegionRouting | null>(null)

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  // ---- Data fetching ----

  const fetchAll = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [regRes, routeRes] = await Promise.all([
        fetch('/api/notifications/regions'),
        fetch('/api/notifications/region-routing'),
      ])
      if (!regRes.ok) throw new Error(`Failed to fetch regions (${regRes.status})`)
      if (!routeRes.ok) throw new Error(`Failed to fetch region routing (${routeRes.status})`)

      const regData: string[] = await regRes.json()
      const routeData: RegionRouting = await routeRes.json()

      const familyKeys = familiesRef.current.map((f) => f.key)
      const expanded: RegionRouting = {
        enabled: routeData.enabled ?? false,
        cells: expandCells(routeData.cells ?? {}, familyKeys, regData),
      }

      setRegions(regData)
      setRouting(expanded)
      setOriginalRouting(JSON.parse(JSON.stringify(expanded)))
      setHasChanges(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }, []) // stable — uses familiesRef for current family list

  useEffect(() => {
    document.title = 'Region Routing - MeshAI'
    fetchAll()
  }, [fetchAll])

  // When families API adds new entries after initial load, insert missing rows
  // without re-fetching from the server (preserves any in-progress edits).
  const prevFamilyKeysRef = useRef<string[]>([])
  useEffect(() => {
    const newKeys = families.map((f) => f.key)
    const added = newKeys.filter((k) => !prevFamilyKeysRef.current.includes(k))
    prevFamilyKeysRef.current = newKeys
    if (added.length === 0 || regions.length === 0 || !originalRouting) return

    const addRows = (prev: RegionRouting): RegionRouting => {
      const newCells: Cells = { ...prev.cells }
      for (const fam of added) {
        newCells[fam] = {}
        for (const region of regions) {
          newCells[fam][region] = { ...DEFAULT_CELL }
        }
      }
      return { ...prev, cells: newCells }
    }

    setRouting(addRows)
    setOriginalRouting((prev) => (prev ? addRows(prev) : prev))
  }, [families, regions, originalRouting])

  // ---- Dirty tracking ----

  useEffect(() => {
    if (originalRouting) {
      setHasChanges(JSON.stringify(routing) !== JSON.stringify(originalRouting))
    }
  }, [routing, originalRouting])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  // ---- Cell update ----

  const updateCell = (fam: string, region: string, patch: Partial<CellValue>) => {
    setRouting((prev) => ({
      ...prev,
      cells: {
        ...prev.cells,
        [fam]: {
          ...(prev.cells[fam] ?? {}),
          [region]: { ...(prev.cells[fam]?.[region] ?? DEFAULT_CELL), ...patch },
        },
      },
    }))
  }

  // ---- Save ----

  const saveRouting = async () => {
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const payload = buildSparsePayload(routing)
      const res = await fetch('/api/notifications/region-routing', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const result = await res.json()
      if (!res.ok) throw new Error((result as { detail?: string }).detail || 'Save failed')

      setOriginalRouting(JSON.parse(JSON.stringify(routing)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('Region routing saved')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const discardChanges = () => {
    if (originalRouting) {
      setRouting(JSON.parse(JSON.stringify(originalRouting)))
      setHasChanges(false)
    }
  }

  // ---- MT budget ----

  const allMtIndices = new Set<number>()
  for (const regionMap of Object.values(routing.cells)) {
    for (const cell of Object.values(regionMap)) {
      if (cell.mt !== null && cell.mt >= 0) allMtIndices.add(cell.mt)
    }
  }
  const mtBudgetWarning = allMtIndices.size > 7

  // ---- Render ----

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading region routing...</div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm text-slate-500 max-w-prose">
          Manual region&nbsp;&times;&nbsp;family routing matrix. Rows&nbsp;= families,
          columns&nbsp;= regions. Fill each cell by hand&nbsp;— meshai creates nothing
          automatically. Only filled cells (MT or MC set) are saved.
        </p>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            onClick={fetchAll}
            className="p-2 text-slate-400 hover:text-slate-200 hover:bg-bg-hover rounded transition-colors"
            title="Refresh"
          >
            <RefreshCw size={18} />
          </button>
          <button
            onClick={discardChanges}
            disabled={!hasChanges}
            className="flex items-center gap-2 px-3 py-2 text-slate-400 hover:text-slate-200 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <RotateCcw size={16} />
            Discard
          </button>
          <button
            onClick={saveRouting}
            disabled={saving || !hasChanges}
            className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent/80 disabled:bg-slate-700 disabled:cursor-not-allowed rounded text-white transition-colors"
          >
            <Save size={16} />
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {/* Status messages */}
      {error && (
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">
          {error}
        </div>
      )}
      {success && (
        <div className="p-3 text-sm bg-green-500/10 text-green-400 border border-green-500/20">
          <Check size={14} className="inline mr-2" />
          {success}
        </div>
      )}

      {/* Master enabled switch */}
      <div className="bg-bg-card border border-border p-4 flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm text-slate-200 font-medium">
            <BellRing size={15} />
            Region Routing Enabled
          </div>
          <p className="text-xs text-slate-500 mt-1">
            When enabled, matching matrix cells override per-toggle delivery and bypass the
            toggle-level severity floor. Leave disabled to soak safely before cutover.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setRouting((prev) => ({ ...prev, enabled: !prev.enabled }))}
          className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 ${
            routing.enabled ? 'bg-accent' : 'bg-[#1e2a3a]'
          }`}
          aria-label="Toggle region routing"
        >
          <span
            className={`absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform ${
              routing.enabled ? 'translate-x-5' : ''
            }`}
          />
        </button>
      </div>

      {/* MT budget warning */}
      {mtBudgetWarning && (
        <div className="flex items-start gap-2 p-3 text-sm bg-amber-500/10 text-amber-400 border border-amber-500/20">
          <AlertTriangle size={16} className="flex-shrink-0 mt-0.5" />
          <span>
            {allMtIndices.size} distinct MT channel indices in use. Meshtastic firmware
            has a hard 8-channel ceiling (indices 0–7). Consider using one per-region
            Alerts channel (same MT index down a column) so all families for a region
            share one channel. MeshCore (MC) names are unbounded.
          </span>
        </div>
      )}

      {/* Caption */}
      <div className="text-xs text-slate-500 space-y-0.5 border-l-2 border-[#1e2a3a] pl-3">
        <p>
          <strong className="text-slate-400">MT</strong>&nbsp;= Meshtastic channel index
          for this region. Set the same index down a whole region column to use a single
          per-region "Alerts" channel (all families share it, distinguished by prefix).
        </p>
        <p>
          <strong className="text-slate-400">MC</strong>&nbsp;= MeshCore channel name for
          this exact cell (e.g.&nbsp;<code>#scid-fire</code>). MC names are unbounded so
          you can keep per-family separation here.
        </p>
        <p>
          <strong className="text-slate-400">Min Sev</strong>&nbsp;= severity floor for
          this cell only. Default "routine" routes below the global toggle floor — that's
          intentional for roads/satpass.
        </p>
      </div>

      {/* Matrix */}
      {regions.length === 0 ? (
        <div className="p-8 text-slate-500 text-sm border border-border text-center">
          No regions configured yet. Define named coverage areas in Coverage settings first,
          or configure satpass observer labels that match your coverage area names.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="border-collapse min-w-max text-sm">
            <thead>
              <tr>
                {/* Sticky family column header */}
                <th className="sticky left-0 z-20 bg-[#0d1520] border border-[#1e2a3a] px-3 py-2 text-left text-xs text-slate-500 uppercase tracking-wide min-w-[150px] whitespace-nowrap">
                  Family
                </th>
                {regions.map((region) => (
                  <th
                    key={region}
                    className="border border-[#1e2a3a] px-3 py-2 text-left text-xs text-slate-300 font-semibold min-w-[210px] bg-[#0a0e17] whitespace-nowrap"
                  >
                    {region}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {families.map(({ key, label, Icon }) => (
                <tr key={key}>
                  {/* Sticky family label */}
                  <td className="sticky left-0 z-10 bg-[#0d1520] border border-[#1e2a3a] px-3 py-3 min-w-[150px] align-middle">
                    <div className="flex items-center gap-2 text-slate-300">
                      <Icon size={14} className="flex-shrink-0" />
                      <span className="font-medium whitespace-nowrap">{label}</span>
                    </div>
                  </td>

                  {/* One cell per region */}
                  {regions.map((region) => {
                    const cell = routing.cells[key]?.[region] ?? DEFAULT_CELL
                    const filled = isCellFilled(cell)
                    return (
                      <td
                        key={region}
                        className={`border border-[#1e2a3a] p-2 align-top ${
                          filled ? 'bg-accent/5' : 'bg-[#0a0e17]'
                        }`}
                      >
                        <div className="space-y-2 min-w-[200px]">
                          {/* MT channel — ChannelPicker single with Disabled option */}
                          <ChannelPicker
                            label="MT"
                            mode="single"
                            value={cell.mt ?? -1}
                            onChange={(v) =>
                              updateCell(key, region, { mt: v === -1 ? null : v })
                            }
                            includeDisabled
                          />

                          {/* MC channel name */}
                          <div className="space-y-1">
                            <label className="block text-xs text-slate-500 uppercase tracking-wide">
                              MC
                            </label>
                            <input
                              type="text"
                              value={cell.mc ?? ''}
                              onChange={(e) =>
                                updateCell(key, region, {
                                  mc: e.target.value === '' ? null : e.target.value,
                                })
                              }
                              placeholder="#channel-name"
                              className="w-full px-2 py-1.5 bg-[#0a0e17] border border-[#1e2a3a] rounded text-xs text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
                            />
                          </div>

                          {/* Min severity */}
                          <div className="space-y-1">
                            <label className="block text-xs text-slate-500 uppercase tracking-wide">
                              Min Sev
                            </label>
                            <select
                              value={cell.min_severity}
                              onChange={(e) =>
                                updateCell(key, region, { min_severity: e.target.value })
                              }
                              className="w-full px-2 py-1.5 bg-[#0a0e17] border border-[#1e2a3a] rounded text-xs text-slate-200 focus:outline-none focus:border-accent"
                            >
                              {SEVERITY_OPTIONS.map((opt) => (
                                <option key={opt.value} value={opt.value}>
                                  {opt.label}
                                </option>
                              ))}
                            </select>
                          </div>

                          {/* Enabled */}
                          <label className="flex items-center gap-2 cursor-pointer py-0.5">
                            <input
                              type="checkbox"
                              checked={cell.enabled}
                              onChange={(e) =>
                                updateCell(key, region, { enabled: e.target.checked })
                              }
                              className="w-3.5 h-3.5 accent-accent"
                            />
                            <span className="text-xs text-slate-400">Enabled</span>
                          </label>
                        </div>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
