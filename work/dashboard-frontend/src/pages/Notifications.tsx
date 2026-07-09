import { useState, useEffect, useCallback } from 'react'
import {
  Save, RotateCcw, RefreshCw, Check,
  Eye as EyeIcon, EyeOff, Plus, X, Radio,
  Activity, Cloud, Flame, Car, Snowflake, Mountain, MapPin, Satellite, Layers,
} from 'lucide-react'
import { useDirty } from '@/context/DirtyContext'

// ── Types ────────────────────────────────────────────────────────────────────

// Integration C2: a named, reusable delivery target. Mirrors the backend
// config.py::NotificationDestination field set. Defined ONCE under
// NotificationsConfig.destinations and referenced by name from toggles/rules
// via their `destinations` list. Only the fields relevant to `type` are used.
export interface NotificationDestination {
  name: string
  type: 'mesh_broadcast' | 'meshcore_broadcast' | 'mesh_dm' | 'meshcore_dm' | 'email' | 'webhook' | 'digest'
  broadcast_channel?: number | null
  meshcore_channel?: string | null
  node_ids?: string[]
  meshcore_dm_contacts?: string[]
  smtp_host?: string
  smtp_port?: number
  smtp_user?: string
  smtp_password?: string
  smtp_tls?: boolean
  from_address?: string
  recipients?: string[]
  webhook_url?: string
  webhook_headers?: Record<string, string>
}

export interface NotificationToggle {
  name: string
  enabled: boolean
  min_severity: string
  regions: string[]
  severity_channels: Record<string, string[]>
  freshness_seconds?: number
  cooldown_seconds?: number
  broadcast_channel: number | null
  meshcore_channel?: string | null
  node_ids: string[]
  meshcore_dm_contacts: string[]
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_password: string
  smtp_tls: boolean
  from_address: string
  recipients: string[]
  webhook_url: string
  webhook_headers: Record<string, string>
  // Integration C2: reference reusable destinations by name.
  destinations?: string[]
}

export interface DigestConfig {
  schedule: string
  include: string[]
}

// Per-region per-family delivery cell. mt = Meshtastic channel index, mc = MeshCore channel name.
export interface RegionCell {
  mt: number | null
  mc: string | null
  min_severity: string
  enabled: boolean
}

// The full region_routes block stored under notifications config.
// Region routing has an independent master switch per transport: mt_enabled
// (Meshtastic) and mc_enabled (MeshCore). `enabled` is a legacy single-switch
// field tolerated on READ only (older payloads) — never written back.
export interface RegionRoutes {
  mt_enabled: boolean
  mc_enabled: boolean
  cells: Record<string, Record<string, RegionCell>>
  // Legacy: pre-split single master switch. Read-only fallback; not serialized.
  enabled?: boolean
}

export interface NotificationsConfig {
  enabled: boolean
  cold_start_grace_seconds?: number
  band_conditions_enabled?: boolean
  band_conditions_schedule?: string[]
  band_conditions_tz?: string
  digest?: DigestConfig
  rules: { name: string; [key: string]: unknown }[]
  toggles?: Record<string, NotificationToggle>
  // Integration C2: named, reusable delivery targets (name -> destination).
  destinations?: Record<string, NotificationDestination>
  // Per-region per-family routing overrides.
  region_routes?: RegionRoutes
}

// ── Constants ────────────────────────────────────────────────────────────────

// Per-mesh channel groups for the split severity matrix
const MT_CHANNELS = ['mesh_broadcast', 'mesh_dm'] as const
export const MC_CHANNELS = ['meshcore_broadcast', 'meshcore_dm'] as const
const TOGGLE_SEVERITIES = ['routine', 'priority', 'immediate']

// ── InfoButton ────────────────────────────────────────────────────────────────

export function InfoButton({ info }: { info: string }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="relative inline-block">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen(!open) }}
        className="ml-1.5 w-4 h-4 rounded-full bg-slate-700 hover:bg-slate-600 text-slate-400 hover:text-slate-200 inline-flex items-center justify-center text-xs transition-colors"
        title="More info"
      >
        ?
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-6 z-50 w-72 p-3 bg-[#1a2332] border border-[#2a3a4a] shadow-xl text-xs text-slate-300 leading-relaxed">
            {info}
          </div>
        </>
      )}
    </div>
  )
}

// ── Form components ────────────────────────────────────────────────────────────

export function TextInput({ label, value, onChange, type = 'text', placeholder = '', helper = '', info = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
  helper?: string
  info?: string
}) {
  const [showPassword, setShowPassword] = useState(false)
  const isPassword = type === 'password'

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <div className="relative">
        <input
          type={isPassword && !showPassword ? 'password' : 'text'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
        />
        {isPassword && (
          <button
            type="button"
            onClick={() => setShowPassword(!showPassword)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
          >
            {showPassword ? <EyeOff size={16} /> : <EyeIcon size={16} />}
          </button>
        )}
      </div>
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

export function NumberInput({ label, value, onChange, min, max, step = 1, helper = '', info = '' }: {
  label: string
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
  helper?: string
  info?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <input
        type="number"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        min={min}
        max={max}
        step={step}
        className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
      />
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

export function Toggle({ label, checked, onChange, helper = '', info = '' }: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  helper?: string
  info?: string
}) {
  return (
    <div className="flex items-center justify-between py-2">
      <div>
        <span className="flex items-center text-sm text-slate-300">
          {label}
          {info && <InfoButton info={info} />}
        </span>
        {helper && <p className="text-xs text-slate-600">{helper}</p>}
      </div>
      <button
        type="button"
        onClick={() => onChange(!checked)}
        className={`relative w-11 h-6 rounded-full transition-colors ${
          checked ? 'bg-accent' : 'bg-[#1e2a3a]'
        }`}
      >
        <span
          className={`absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform ${
            checked ? 'translate-x-5' : ''
          }`}
        />
      </button>
    </div>
  )
}

export function TimeInput({ label, value, onChange, helper = '', info = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  helper?: string
  info?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <input
        type="time"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
      />
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

export function ListInput({ label, value, onChange, placeholder = 'Add item...', helper = '', info = '' }: {
  label: string
  value: string[]
  onChange: (v: string[]) => void
  placeholder?: string
  helper?: string
  info?: string
}) {
  const [inputValue, setInputValue] = useState('')

  const addItem = () => {
    if (inputValue.trim() && !value.includes(inputValue.trim())) {
      onChange([...value, inputValue.trim()])
      setInputValue('')
    }
  }

  const removeItem = (index: number) => {
    onChange(value.filter((_, i) => i !== index))
  }

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <div className="flex gap-2">
        <input
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addItem())}
          className="flex-1 px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
          placeholder={placeholder}
        />
        <button
          type="button"
          onClick={addItem}
          className="px-3 py-2 bg-accent hover:bg-accent/80 rounded text-sm text-white transition-colors"
        >
          <Plus size={16} />
        </button>
      </div>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-2 mt-2">
          {value.map((item, i) => (
            <span
              key={i}
              className="inline-flex items-center gap-1 px-2 py-1 bg-[#1e2a3a] rounded text-sm text-slate-300"
            >
              {item}
              <button
                type="button"
                onClick={() => removeItem(i)}
                className="text-slate-500 hover:text-red-400"
              >
                <X size={14} />
              </button>
            </span>
          ))}
        </div>
      )}
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

// ── SeverityChannelMatrix ─────────────────────────────────────────────────────

// Reusable severity × channel matrix.
// Toggling a single channel only touches that exact string in the severity row —
// all other channels (including those owned by the other mesh section) are preserved.
export function SeverityChannelMatrix({
  channels,
  severityChannels,
  onChange,
}: {
  channels: readonly string[]
  severityChannels: Record<string, string[]>
  onChange: (updated: Record<string, string[]>) => void
}) {
  const colLabel = (c: string) =>
    c.replace('meshcore_', 'mc_').replace('mesh_', '').replace(/_/g, ' ')
  return (
    <table className="text-xs w-full">
      <thead>
        <tr>
          <th className="text-left text-slate-600 font-normal w-20">severity</th>
          {channels.map((c) => (
            <th key={c} className="text-slate-500 font-normal px-1 whitespace-nowrap">{colLabel(c)}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {TOGGLE_SEVERITIES.map((sev) => (
          <tr key={sev}>
            <td className="text-slate-400 pr-2 whitespace-nowrap">{sev}</td>
            {channels.map((ch) => {
              const on = (severityChannels[sev] || []).includes(ch)
              return (
                <td key={ch} className="text-center">
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={(e) => {
                      const cur: Record<string, string[]> = { ...severityChannels }
                      const arr = new Set(cur[sev] || [])
                      if (e.target.checked) arr.add(ch)
                      else arr.delete(ch)
                      cur[sev] = Array.from(arr)
                      onChange(cur)
                    }}
                  />
                </td>
              )
            })}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ── Family metadata ────────────────────────────────────────────────────────────

export type FamilyMeta = { key: string; label: string; Icon: typeof Activity }

// Static base list of built-in families with their curated icons. This stays the
// canonical export (Environment.tsx still imports it directly). Dynamic/generic
// families registered at runtime are layered on top via useFamilies() below —
// this list is NEVER mutated.
export const TOGGLE_FAMILY_META: FamilyMeta[] = [
  { key: 'mesh_health', label: 'Mesh Health', Icon: Activity },
  { key: 'weather', label: 'Weather', Icon: Cloud },
  { key: 'fire', label: 'Fire', Icon: Flame },
  { key: 'rf_propagation', label: 'RF Propagation', Icon: Radio },
  { key: 'roads', label: 'Roads', Icon: Car },
  { key: 'avalanche', label: 'Avalanche', Icon: Snowflake },
  { key: 'satpass', label: 'Satellite Passes', Icon: Satellite },
  { key: 'seismic', label: 'Seismic', Icon: Mountain },
  { key: 'tracking', label: 'Tracking', Icon: MapPin },
]

// Shape returned by GET /api/notifications/families.
type ApiFamily = { key: string; label: string; dynamic: boolean }

// Merge the static built-ins with API-reported families. Static entries win on a
// key collision (keep their curated icon + label); any family NOT already in the
// static set is appended with a generic Layers icon and its API label.
function mergeFamilies(apiFamilies: ApiFamily[]): FamilyMeta[] {
  const seen = new Set(TOGGLE_FAMILY_META.map((f) => f.key))
  const extra: FamilyMeta[] = []
  for (const f of apiFamilies) {
    if (!f || !f.key || seen.has(f.key)) continue
    seen.add(f.key)
    extra.push({ key: f.key, label: f.label || f.key, Icon: Layers })
  }
  return [...TOGGLE_FAMILY_META, ...extra]
}

// Module-level cache so the several consumers of useFamilies() share a SINGLE fetch.
let _familiesCache: FamilyMeta[] | null = null
let _familiesPromise: Promise<FamilyMeta[]> | null = null

// Returns the MERGED family list (static built-ins + registered dynamic families).
export function useFamilies(): FamilyMeta[] {
  const [families, setFamilies] = useState<FamilyMeta[]>(_familiesCache ?? TOGGLE_FAMILY_META)

  useEffect(() => {
    let cancelled = false
    if (_familiesCache) {
      setFamilies(_familiesCache)
      return
    }
    if (!_familiesPromise) {
      _familiesPromise = fetch('/api/notifications/families')
        .then((r) => (r.ok ? r.json() : []))
        .then((data: unknown) => {
          const merged = mergeFamilies(Array.isArray(data) ? (data as ApiFamily[]) : [])
          _familiesCache = merged
          return merged
        })
        .catch(() => TOGGLE_FAMILY_META)
    }
    _familiesPromise.then((merged) => {
      if (!cancelled) setFamilies(merged)
    })
    return () => {
      cancelled = true
    }
  }, [])

  return families
}

// ── Merge helpers ─────────────────────────────────────────────────────────────

// Merge only the MT-channel-owned delivery fields of `mine` into `fresh`,
// preserving MeshCore entries (meshcore_*) and all gating fields.
function mergeMeshtasticAndOtherFields(
  fresh: NotificationToggle | undefined,
  mine: NotificationToggle,
  key: string,
): NotificationToggle {
  const base: NotificationToggle = fresh ? { ...fresh } : { ...mine, name: key }
  base.name = mine.name || base.name || key

  // Merge severity_channels: keep meshcore_* from fresh, overlay non-meshcore from mine.
  const freshSC = fresh?.severity_channels || {}
  const mineSC = mine.severity_channels || {}
  const severities = new Set([...Object.keys(freshSC), ...Object.keys(mineSC)])
  const mergedSC: Record<string, string[]> = {}
  severities.forEach((sev) => {
    const meshcoreOnly = (freshSC[sev] || []).filter((c) => c.startsWith('meshcore_'))
    const nonMeshcore = (mineSC[sev] || []).filter((c) => !c.startsWith('meshcore_'))
    mergedSC[sev] = [...meshcoreOnly, ...nonMeshcore]
  })
  base.severity_channels = mergedSC

  // Overlay MT delivery fields
  base.broadcast_channel = mine.broadcast_channel
  base.node_ids = mine.node_ids

  // NOTE: do NOT overlay gating fields (enabled, min_severity, freshness_seconds,
  // cooldown_seconds, regions) — those are managed by Data Feeds > Family Settings.
  return base
}

// Merge region_routes, overlaying only the MT (.mt) field from mine and
// preserving the MC (.mc) field from fresh. Drops cells where both are null.
function mergeRegionRoutesForMt(
  fresh: RegionRoutes | undefined,
  mine: RegionRoutes | undefined,
): RegionRoutes {
  // This page OWNS mt_enabled (from its own toggle); carry mc_enabled through
  // UNCHANGED from the freshly-fetched server value so we never clobber it.
  const mt_enabled = mine?.mt_enabled ?? mine?.enabled ?? false
  const mc_enabled = fresh?.mc_enabled ?? false
  const freshCells = fresh?.cells || {}
  const mineCells = mine?.cells || {}

  const allFamilies = new Set([...Object.keys(freshCells), ...Object.keys(mineCells)])
  const newCells: Record<string, Record<string, RegionCell>> = {}

  for (const fam of allFamilies) {
    const freshFam = freshCells[fam] || {}
    const mineFam = mineCells[fam] || {}
    const allRegions = new Set([...Object.keys(freshFam), ...Object.keys(mineFam)])
    const newFam: Record<string, RegionCell> = {}

    for (const region of allRegions) {
      const freshCell = freshFam[region]
      const mineCell = mineFam[region]
      const merged: RegionCell = {
        mt: mineCell !== undefined ? mineCell.mt : (freshCell?.mt ?? null),
        mc: freshCell?.mc ?? null,
        min_severity: mineCell?.min_severity ?? freshCell?.min_severity ?? 'routine',
        enabled: mineCell?.enabled ?? freshCell?.enabled ?? true,
      }
      const mcVal = merged.mc
      if (merged.mt !== null || (mcVal !== null && mcVal.trim() !== '')) {
        newFam[region] = merged
      }
    }

    if (Object.keys(newFam).length > 0) {
      newCells[fam] = newFam
    }
  }

  return { mt_enabled, mc_enabled, cells: newCells }
}

// ── MeshtasticDeliveryGrid ────────────────────────────────────────────────────

// Always-visible per-family Meshtastic delivery grid with in-card region routing.
function MeshtasticDeliveryGrid({
  toggles,
  onChange,
  regions,
  regionRoutes,
  onRegionRoutesChange,
}: {
  toggles: Record<string, NotificationToggle>
  onChange: (t: Record<string, NotificationToggle>) => void
  regions: string[]
  regionRoutes: RegionRoutes | undefined
  onRegionRoutesChange: (r: RegionRoutes) => void
}) {
  const families = useFamilies()
  const upd = (fam: string, patch: Partial<NotificationToggle>) =>
    onChange({ ...toggles, [fam]: { ...(toggles[fam] || {}), ...patch } as NotificationToggle })

  // Local map: family key -> explicitly toggled on/off.
  // undefined = fall back to derived state (any MT cell set in regionRoutes).
  const [regionExpandedMap, setRegionExpandedMap] = useState<Record<string, boolean | undefined>>({})

  const setMtForRegion = (family: string, region: string, mt: number | null) => {
    const existing: RegionCell = regionRoutes?.cells?.[family]?.[region] ?? {
      mt: null, mc: null, min_severity: 'routine', enabled: true,
    }
    const newCells = {
      ...(regionRoutes?.cells || {}),
      [family]: {
        ...(regionRoutes?.cells?.[family] || {}),
        [region]: { ...existing, mt },
      },
    }
    onRegionRoutesChange({
      mt_enabled: regionRoutes?.mt_enabled ?? regionRoutes?.enabled ?? false,
      mc_enabled: regionRoutes?.mc_enabled ?? false,
      cells: newCells,
    })
  }

  const clearMtForFamily = (family: string) => {
    const familyCells = regionRoutes?.cells?.[family] || {}
    const clearedFamily: Record<string, RegionCell> = {}
    for (const [r, c] of Object.entries(familyCells)) {
      clearedFamily[r] = { ...c, mt: null }
    }
    const newCells = { ...(regionRoutes?.cells || {}), [family]: clearedFamily }
    onRegionRoutesChange({
      mt_enabled: regionRoutes?.mt_enabled ?? regionRoutes?.enabled ?? false,
      mc_enabled: regionRoutes?.mc_enabled ?? false,
      cells: newCells,
    })
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        Meshtastic Delivery
        <InfoButton info="Per-family Meshtastic delivery matrix. Choose which channels fire at each severity, the broadcast channel index, and DM node IDs. Family on/off and severity threshold are configured on the Data Feeds page." />
      </div>
      <div className="border border-[#1e2a3a] p-3">
        <Toggle
          label="Enable Meshtastic region routing"
          checked={regionRoutes?.mt_enabled ?? regionRoutes?.enabled ?? false}
          onChange={(on) =>
            onRegionRoutesChange({
              mt_enabled: on,
              mc_enabled: regionRoutes?.mc_enabled ?? false,
              cells: regionRoutes?.cells || {},
            })
          }
          helper="Master switch for per-region Meshtastic channel routing. When off, families deliver only to their default Meshtastic channels."
        />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {families.map(({ key, label, Icon }) => {
          const t = toggles[key] || ({} as NotificationToggle)
          const familyCells = regionRoutes?.cells?.[key] || {}
          // Derived: any MT cell non-null
          const hasAnyMt = regions.some(r => familyCells[r]?.mt != null)
          // Local override takes precedence; fall back to derived
          const localOverride = regionExpandedMap[key]
          const showRegion = localOverride !== undefined ? localOverride : hasAnyMt

          return (
            <div key={key} className="border border-[#1e2a3a] p-3 space-y-3">
              <div className="flex items-center gap-2 text-sm text-slate-200">
                <Icon size={15} /> {label}
              </div>

              {/* MT delivery matrix */}
              <div className="space-y-3 p-3 bg-[#0a0e17] border border-[#1e2a3a]">
                <div className="flex items-center gap-2 text-xs font-medium text-slate-300">
                  <Radio size={13} />
                  Meshtastic
                </div>
                <SeverityChannelMatrix
                  channels={MT_CHANNELS}
                  severityChannels={t.severity_channels || {}}
                  onChange={(sc) => upd(key, { severity_channels: sc })}
                />
                <NumberInput
                  label="Broadcast channel"
                  value={t.broadcast_channel ?? 0}
                  onChange={(v) => upd(key, { broadcast_channel: v })}
                  min={0}
                  helper="Meshtastic channel index (0 = LongFast primary)"
                  info="The Meshtastic channel index used for mesh_broadcast delivery. 0 = primary channel."
                />
                <ListInput
                  label="DM node IDs"
                  value={t.node_ids || []}
                  onChange={(v) => upd(key, { node_ids: v })}
                  placeholder="!hex_id"
                  helper="Meshtastic DM recipients (hex node IDs)"
                  info="Hex node IDs for mesh_dm delivery (e.g. !a1b2c3d4). Used when mesh_dm is enabled for a severity."
                />
              </div>

              {/* Region-based routing expand */}
              <div className="border border-[#1e2a3a] p-3 space-y-2">
                <Toggle
                  label="Region-based routing"
                  checked={showRegion}
                  onChange={(on) => {
                    setRegionExpandedMap(m => ({ ...m, [key]: on }))
                    if (!on) clearMtForFamily(key)
                  }}
                  helper="Route this family to different MT channels per region"
                />
                {showRegion && (
                  regions.length === 0 ? (
                    <p className="text-xs text-slate-500 italic">
                      No regions yet — add them on the{' '}
                      <a href="/coverage" className="text-accent hover:underline">Coverage</a> page.
                    </p>
                  ) : (
                    <div className="space-y-1.5 pt-1">
                      {regions.map(region => {
                        const cell: RegionCell = familyCells[region] ?? {
                          mt: null, mc: null, min_severity: 'routine', enabled: true,
                        }
                        return (
                          <div key={region} className="flex items-center gap-2">
                            <span className="text-xs text-slate-400 flex-1 min-w-0 truncate">{region}</span>
                            <input
                              type="number"
                              value={cell.mt !== null ? cell.mt : ''}
                              onChange={(e) => {
                                const raw = e.target.value
                                setMtForRegion(key, region, raw === '' ? null : parseInt(raw, 10))
                              }}
                              min={0}
                              max={7}
                              placeholder="ch"
                              className="w-16 px-2 py-1 bg-[#0a0e17] border border-[#1e2a3a] rounded text-xs text-slate-200 font-mono focus:outline-none focus:border-accent"
                            />
                          </div>
                        )
                      })}
                    </div>
                  )
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Notifications page ────────────────────────────────────────────────────────

export default function Notifications() {
  const { setDirty } = useDirty()
  const families = useFamilies()
  const [config, setConfig] = useState<NotificationsConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<NotificationsConfig | null>(null)
  const [regionNames, setRegionNames] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  const fetchConfig = useCallback(async () => {
    try {
      const [configRes, regionsRes] = await Promise.all([
        fetch('/api/config/notifications'),
        fetch('/api/notifications/regions'),
      ])
      if (!configRes.ok) throw new Error('Failed to fetch notifications config')
      const configData: NotificationsConfig = await configRes.json()
      const regionsData: string[] = regionsRes.ok ? await regionsRes.json() : []
      setConfig(configData)
      setOriginalConfig(JSON.parse(JSON.stringify(configData)))
      setRegionNames(Array.isArray(regionsData) ? regionsData : [])
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'Meshtastic Routing - MeshAI'
    fetchConfig()
  }, [fetchConfig])

  useEffect(() => {
    if (config && originalConfig) {
      setHasChanges(JSON.stringify(config) !== JSON.stringify(originalConfig))
    }
  }, [config, originalConfig])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const saveConfig = async () => {
    if (!config) return

    setSaving(true)
    setError(null)
    setSuccess(null)

    try {
      // Re-fetch the live config and merge ONLY MT delivery fields + region_routes MT overlay
      // so we never clobber gating fields (enabled/min_severity/freshness/cooldown) managed by
      // Data Feeds, nor MeshCore fields managed by the MeshCore Routing page.
      const freshRes = await fetch('/api/config/notifications')
      if (!freshRes.ok) throw new Error('Failed to re-fetch notifications config')
      const fresh: NotificationsConfig = await freshRes.json()

      const merged: NotificationsConfig = {
        ...fresh,
        toggles: { ...(fresh.toggles || {}) },
        region_routes: mergeRegionRoutesForMt(fresh.region_routes, config.region_routes),
      }
      const myToggles = config.toggles || {}
      for (const { key } of families) {
        const mine = myToggles[key]
        if (!mine) continue
        merged.toggles![key] = mergeMeshtasticAndOtherFields(
          (fresh.toggles || {})[key],
          mine,
          key,
        )
      }

      const res = await fetch('/api/config/notifications', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(merged),
      })

      const result = await res.json()

      if (!res.ok) {
        throw new Error((result as { detail?: string }).detail || 'Save failed')
      }

      setConfig(merged)
      setOriginalConfig(JSON.parse(JSON.stringify(merged)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('Meshtastic routing saved successfully')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const discardChanges = () => {
    if (originalConfig) {
      setConfig(JSON.parse(JSON.stringify(originalConfig)))
      setHasChanges(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading notifications config...</div>
      </div>
    )
  }

  if (!config) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Failed to load notifications config</div>
      </div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">
            Per-family Meshtastic delivery. Family gating (enable, severity threshold, freshness/cooldown) is on{' '}
            <a href="/environment" className="text-accent hover:underline">Data Feeds</a>.
            MeshCore delivery is on the{' '}
            <a href="/meshcore/routing" className="text-accent hover:underline">MeshCore Routing</a> page.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchConfig}
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
            onClick={saveConfig}
            disabled={saving || !hasChanges}
            className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent/80 disabled:bg-slate-700 disabled:cursor-not-allowed rounded text-white transition-colors"
          >
            <Save size={16} />
            {saving ? 'Saving...' : 'Save'}
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

      {/* MT delivery grid */}
      <div className="bg-bg-card border border-border p-6">
        {config.toggles ? (
          <MeshtasticDeliveryGrid
            toggles={config.toggles}
            onChange={(t) => setConfig({ ...config, toggles: t })}
            regions={regionNames}
            regionRoutes={config.region_routes}
            onRegionRoutesChange={(r) => setConfig({ ...config, region_routes: r })}
          />
        ) : (
          <p className="text-sm text-slate-500">No family configuration found.</p>
        )}
      </div>
    </div>
  )
}
