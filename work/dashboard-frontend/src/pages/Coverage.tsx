import { useEffect, useState, useCallback, useRef } from 'react'
import {
  MapContainer,
  TileLayer,
  Rectangle,
  CircleMarker,
  useMapEvents,
  useMap,
} from 'react-leaflet'
import type { LatLngTuple } from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { Save, RotateCcw, MousePointer, Plus, Trash2 } from 'lucide-react'
import { notifyRestartRequired } from '@/components/RestartBanner'
import { useDirty } from '@/context/DirtyContext'

// Fix Leaflet default marker icon (mirrors GeoMap.tsx)
import L from 'leaflet'
import markerIcon from 'leaflet/dist/images/marker-icon.png'
import markerIcon2x from 'leaflet/dist/images/marker-icon-2x.png'
import markerShadow from 'leaflet/dist/images/marker-shadow.png'
// @ts-expect-error — Leaflet icon fix for Vite
delete L.Icon.Default.prototype._getIconUrl
L.Icon.Default.mergeOptions({ iconUrl: markerIcon, iconRetinaUrl: markerIcon2x, shadowUrl: markerShadow })

// Round Leaflet's ~14-decimal coords to 6dp (some downstream APIs reject >7)
const round6 = (n: number) => Math.round(n * 1_000_000) / 1_000_000

// Per-area rectangle colors for the map
const AREA_COLORS = ['#f59e0b', '#60a5fa', '#34d399', '#a78bfa', '#f87171', '#fb923c']

type ClickMode = 'idle' | 'awaiting-first' | 'awaiting-second'

interface Area {
  name: string
  west: number
  south: number
  east: number
  north: number
}

interface CoverageConfig {
  bbox: number[]
  enabled: boolean
  excluded_adapters: string[]
  areas: Area[]
}

// Adapters that participate in the coverage area system
const COVERAGE_ADAPTERS: { key: string; label: string }[] = [
  { key: 'fires',      label: 'NIFC Fire Perimeters' },
  { key: 'nws',        label: 'NWS Weather Alerts' },
  { key: 'wzdx',       label: 'WZDx Work Zones' },
  { key: 'usgs_quake', label: 'USGS Earthquakes' },
  { key: 'firms',      label: 'NASA FIRMS Hotspots' },
  { key: 'roads511',   label: '511 Road Conditions' },
  { key: 'usgs',       label: 'USGS Stream Gauges' },
  { key: 'avalanche',  label: 'Avalanche Advisories' },
  { key: 'traffic',    label: 'TomTom Traffic' },
  { key: 'satpass',    label: 'Satellite Passes' },
  { key: 'ducting',    label: 'Tropospheric Ducting' },
]

// Fit map to bounds on the first time bounds is available; never re-fits after
// that so user-driven pan/zoom is preserved.
function InitialFit({ bounds }: { bounds: [[number, number], [number, number]] | null }) {
  const map = useMap()
  const hasFitted = useRef(false)
  useEffect(() => {
    if (!hasFitted.current && bounds) {
      map.fitBounds(bounds, { padding: [40, 40] })
      hasFitted.current = true
    }
  }, [map, bounds])
  return null
}

// Two-click rectangle draw: first click = first corner, second click = opposite
// corner. A CircleMarker shows the pending first corner while awaiting-second.
function MapClickHandler({
  mode,
  firstCorner,
  onFirstClick,
  onSecondClick,
}: {
  mode: ClickMode
  firstCorner: LatLngTuple | null
  onFirstClick: (pt: LatLngTuple) => void
  onSecondClick: (pt: LatLngTuple) => void
}) {
  useMapEvents({
    click(e) {
      const pt: LatLngTuple = [e.latlng.lat, e.latlng.lng]
      if (mode === 'awaiting-first') onFirstClick(pt)
      else if (mode === 'awaiting-second') onSecondClick(pt)
    },
  })
  return firstCorner ? (
    <CircleMarker
      center={firstCorner}
      radius={6}
      pathOptions={{ color: '#f59e0b', fillColor: '#f59e0b', fillOpacity: 1 }}
    />
  ) : null
}

export default function Coverage() {
  const [config, setConfig] = useState<CoverageConfig | null>(null)
  const [original, setOriginal] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const { setDirty } = useDirty()

  const [clickMode, setClickMode] = useState<ClickMode>('idle')
  const [firstCorner, setFirstCorner] = useState<LatLngTuple | null>(null)

  useEffect(() => {
    document.title = 'Coverage — MeshAI'
    fetch('/api/config')
      .then((r) => {
        if (!r.ok) throw new Error('Failed to fetch config')
        return r.json()
      })
      .then((data) => {
        const raw = data.coverage ?? { bbox: [], enabled: true, excluded_adapters: [], areas: [] }
        // Normalize areas; back-compat: seed from legacy bbox if areas is empty
        let areas: Area[] = Array.isArray(raw.areas) ? (raw.areas as Area[]) : []
        if (areas.length === 0 && Array.isArray(raw.bbox) && raw.bbox.length === 4) {
          const [w, s, e, n] = raw.bbox as number[]
          areas = [{ name: 'Area 1', west: w, south: s, east: e, north: n }]
        }
        const cov: CoverageConfig = {
          bbox: Array.isArray(raw.bbox) ? (raw.bbox as number[]) : [],
          enabled: (raw.enabled as boolean) ?? true,
          excluded_adapters: Array.isArray(raw.excluded_adapters)
            ? (raw.excluded_adapters as string[])
            : [],
          areas,
        }
        setConfig(cov)
        setOriginal(JSON.stringify(cov))
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  const hasChanges = config !== null && JSON.stringify(config) !== original

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const areas = config?.areas ?? []

  // Union bounds for initial map fit: [[S, W], [N, E]] in Leaflet convention
  const unionBounds: [[number, number], [number, number]] | null =
    areas.length > 0
      ? [
          [Math.min(...areas.map((a) => a.south)), Math.min(...areas.map((a) => a.west))],
          [Math.max(...areas.map((a) => a.north)), Math.max(...areas.map((a) => a.east))],
        ]
      : null

  // Default center: continental US
  const defaultCenter: LatLngTuple = [39.5, -98.35]

  const handleFirstClick = useCallback((pt: LatLngTuple) => {
    setFirstCorner(pt)
    setClickMode('awaiting-second')
  }, [])

  // Second click: complete draw and ADD a new area to the list
  const handleSecondClick = useCallback(
    (pt: LatLngTuple) => {
      if (!firstCorner) return
      const [lat1, lon1] = firstCorner
      const [lat2, lon2] = pt
      setConfig((prev) => {
        if (!prev) return prev
        const newArea: Area = {
          name: `Area ${prev.areas.length + 1}`,
          west:  round6(Math.min(lon1, lon2)),
          south: round6(Math.min(lat1, lat2)),
          east:  round6(Math.max(lon1, lon2)),
          north: round6(Math.max(lat1, lat2)),
        }
        return { ...prev, areas: [...prev.areas, newArea] }
      })
      setFirstCorner(null)
      setClickMode('idle')
    },
    [firstCorner],
  )

  const updateArea = (idx: number, field: keyof Area, val: string) => {
    setConfig((prev) => {
      if (!prev) return prev
      const updated = prev.areas.map((a, i) => {
        if (i !== idx) return a
        if (field === 'name') return { ...a, name: val }
        const n = parseFloat(val)
        if (isNaN(n)) return a
        return { ...a, [field]: n } as Area
      })
      return { ...prev, areas: updated }
    })
  }

  const deleteArea = (idx: number) => {
    setConfig((prev) =>
      prev ? { ...prev, areas: prev.areas.filter((_, i) => i !== idx) } : prev,
    )
  }

  const toggleExcluded = (key: string) => {
    if (!config) return
    const cur = config.excluded_adapters ?? []
    setConfig({
      ...config,
      excluded_adapters: cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key],
    })
  }

  const discard = () => {
    if (original) {
      setConfig(JSON.parse(original) as CoverageConfig)
      setClickMode('idle')
      setFirstCorner(null)
    }
  }

  const save = async () => {
    if (!config) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const res = await fetch('/api/config/coverage', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        // Send areas as authoritative; clear legacy bbox so areas takes precedence
        body: JSON.stringify({
          areas: config.areas,
          bbox: [],
          enabled: config.enabled,
          excluded_adapters: config.excluded_adapters,
        }),
      })
      const result = await res.json()
      if (!res.ok) throw new Error((result as { detail?: string }).detail || 'Save failed')
      setOriginal(JSON.stringify(config))
      setSuccess('Coverage saved')
      setTimeout(() => setSuccess(null), 3000)
      if ((result as { restart_required?: boolean }).restart_required) {
        notifyRestartRequired(
          Array.isArray((result as { changed_keys?: unknown }).changed_keys)
            ? ((result as { changed_keys: string[] }).changed_keys)
            : [],
        )
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
        Loading coverage config…
      </div>
    )
  }
  if (!config) {
    return (
      <div className="flex items-center justify-center h-64 text-red-400">
        {error || 'No config'}
      </div>
    )
  }

  const drawHint =
    clickMode === 'awaiting-first'
      ? 'Click the first corner of the new area on the map…'
      : clickMode === 'awaiting-second'
        ? 'Click the opposite corner to complete the area…'
        : areas.length > 0
          ? `${areas.length} area${areas.length !== 1 ? 's' : ''} defined`
          : 'No areas defined — draw one on the map or enter coordinates below.'

  return (
    <div className="space-y-6 max-w-4xl">
      {/* Page description + action bar */}
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm text-[#777]">
          Define one or more bounding boxes that scope every native adapter's geographic
          focus (set-union of all areas). Adapters with "Use own config" on ignore these
          areas and use the geographic settings on the{' '}
          <a href="/environment" className="text-accent hover:underline">
            Data Feeds
          </a>{' '}
          page.
        </p>
        {hasChanges && (
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
        )}
      </div>

      {error && <div className="text-sm text-red-400 bg-red-500/10 p-3">{error}</div>}
      {success && <div className="text-sm text-green-400 bg-green-500/10 p-3">{success}</div>}

      {/* Master enable toggle */}
      <div className="border border-border p-4 flex items-center justify-between">
        <div>
          <span className="text-sm font-medium text-[#e0e0e0]">
            Use coverage areas to scope all adapters
          </span>
          <p className="text-xs text-[#666] mt-0.5">
            When disabled, every adapter uses its own geographic config regardless of the
            areas below.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setConfig({ ...config, enabled: !config.enabled })}
          className={`relative w-10 h-5 rounded-full transition-colors ${
            config.enabled ? 'bg-accent' : 'bg-[#333]'
          }`}
        >
          <span
            className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
              config.enabled ? 'translate-x-5' : ''
            }`}
          />
        </button>
      </div>

      {/* Leaflet map */}
      <div className="border border-border overflow-hidden">
        {/* Map toolbar */}
        <div className="bg-bg-card border-b border-border px-4 py-2 flex items-center justify-between gap-4">
          <span className="text-xs text-[#777] min-w-0 truncate font-mono">{drawHint}</span>
          <div className="flex items-center gap-2 flex-shrink-0">
            {clickMode !== 'idle' && (
              <button
                onClick={() => {
                  setClickMode('idle')
                  setFirstCorner(null)
                }}
                className="px-2 py-1 text-xs text-[#777] hover:text-white border border-border"
              >
                Cancel
              </button>
            )}
            <button
              onClick={() => setClickMode('awaiting-first')}
              disabled={clickMode !== 'idle'}
              className="flex items-center gap-1 px-2 py-1 text-xs bg-accent/20 hover:bg-accent/30 text-accent border border-accent/30 disabled:opacity-40"
            >
              <MousePointer size={12} />
              Add area
            </button>
          </div>
        </div>

        <MapContainer
          center={defaultCenter}
          zoom={4}
          style={{ width: '100%', height: '400px' }}
          className="z-0"
        >
          <TileLayer
            url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>, &copy; <a href="https://carto.com/attributions">CARTO</a>'
          />
          <InitialFit bounds={unionBounds} />
          {areas.map((area, i) => {
            const color = AREA_COLORS[i % AREA_COLORS.length]
            const bounds: [[number, number], [number, number]] = [
              [area.south, area.west],
              [area.north, area.east],
            ]
            return (
              <Rectangle
                key={i}
                bounds={bounds}
                pathOptions={{ color, fillColor: color, fillOpacity: 0.08, weight: 2 }}
              />
            )
          })}
          <MapClickHandler
            mode={clickMode}
            firstCorner={firstCorner}
            onFirstClick={handleFirstClick}
            onSecondClick={handleSecondClick}
          />
        </MapContainer>
      </div>

      {/* Areas list / editor */}
      <div className="border border-border p-4 space-y-4">
        <div className="flex items-center justify-between">
          <div className="text-xs font-sans font-medium uppercase tracking-widest text-[#666]">
            Coverage Areas
          </div>
          <button
            onClick={() => setClickMode('awaiting-first')}
            disabled={clickMode !== 'idle'}
            className="flex items-center gap-1 px-2 py-1 text-xs bg-accent/20 hover:bg-accent/30 text-accent border border-accent/30 disabled:opacity-40"
          >
            <Plus size={12} /> Add area
          </button>
        </div>

        {areas.length === 0 ? (
          <p className="text-xs text-[#555]">
            No areas defined. Draw one on the map or click "Add area" to start.
          </p>
        ) : (
          <div className="space-y-3">
            {areas.map((area, i) => {
              const color = AREA_COLORS[i % AREA_COLORS.length]
              return (
                <div key={i} className="border border-border p-3 space-y-2">
                  {/* Name row with color swatch and delete */}
                  <div className="flex items-center gap-2">
                    <span
                      className="w-3 h-3 rounded-sm flex-shrink-0"
                      style={{ backgroundColor: color }}
                    />
                    <input
                      type="text"
                      value={area.name}
                      onChange={(e) => updateArea(i, 'name', e.target.value)}
                      className="flex-1 bg-[#0d0d0d] border border-border px-2 py-1 text-sm font-medium text-[#e0e0e0]"
                      placeholder="Area name"
                    />
                    <button
                      onClick={() => deleteArea(i)}
                      title="Delete area"
                      className="flex items-center gap-1 px-2 py-1 text-xs text-[#777] hover:text-red-400 border border-border"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                  {/* W / S / E / N coordinate inputs */}
                  <div className="grid grid-cols-4 gap-2">
                    {(['west', 'south', 'east', 'north'] as const).map((field) => (
                      <div key={field}>
                        <label className="text-xs text-[#777] mb-1 block capitalize">
                          {field}
                        </label>
                        <input
                          type="number"
                          step="0.000001"
                          value={area[field]}
                          onChange={(e) => updateArea(i, field, e.target.value)}
                          className="w-full bg-[#0d0d0d] border border-border px-2 py-1.5 text-xs font-mono"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        <p className="text-xs text-[#666]">
          Decimal degrees. W/E = longitude; S/N = latitude. Each area is a bounding box;
          the coverage filter uses the set-union of all areas. Drawn coordinates are
          rounded to 6 decimal places.
        </p>
      </div>

      {/* Per-adapter override toggles */}
      <div className="border border-border p-4 space-y-4">
        <div>
          <div className="text-xs font-sans font-medium uppercase tracking-widest text-[#666] mb-1">
            Adapter Overrides
          </div>
          <p className="text-xs text-[#777]">
            Toggle "Use own config" to have that adapter ignore the coverage areas.
            Its geographic settings (state, bbox, corridors, observers…) then become
            active on the{' '}
            <a href="/environment" className="text-accent hover:underline">
              Data Feeds
            </a>{' '}
            page.
          </p>
        </div>
        <div className="divide-y divide-border">
          {COVERAGE_ADAPTERS.map(({ key, label }) => {
            const isExcluded = config.excluded_adapters?.includes(key) ?? false
            return (
              <div key={key} className="flex items-center justify-between py-2.5">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-sm text-[#e0e0e0]">{label}</span>
                  {isExcluded ? (
                    <span className="text-[10px] text-accent/70 uppercase tracking-wide">
                      own config
                    </span>
                  ) : (
                    <span className="text-[10px] text-[#555] uppercase tracking-wide">
                      coverage areas
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 flex-shrink-0">
                  {isExcluded && (
                    <a href="/environment" className="text-xs text-accent hover:underline">
                      Configure
                    </a>
                  )}
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <span className="text-xs text-[#666] whitespace-nowrap">Use own config</span>
                    <button
                      type="button"
                      onClick={() => toggleExcluded(key)}
                      className={`relative w-8 h-4 rounded-full transition-colors ${
                        isExcluded ? 'bg-accent' : 'bg-[#333]'
                      }`}
                    >
                      <span
                        className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${
                          isExcluded ? 'translate-x-4' : ''
                        }`}
                      />
                    </button>
                  </label>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Bottom save bar */}
      {hasChanges && (
        <div className="flex justify-end gap-2 pb-2">
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
      )}
    </div>
  )
}
