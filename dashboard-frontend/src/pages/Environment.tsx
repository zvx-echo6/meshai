import { useEffect, useState, type ReactNode } from 'react'
import {
  Cloud, Flame, Radio, Car, Mountain, Satellite, Activity, Server,
  Save, RotateCcw, RefreshCw, AlertCircle, AlertTriangle, Info,
} from 'lucide-react'
import {
  Toggle, TextInput, NumberInput, SelectInput, ListInput, NumberListInput,
  US_STATES,
} from './Config'
import {
  fetchEnvStatus, fetchEnvActive,
  type EnvStatus, type EnvEvent,
} from '@/lib/api'

type FeedSource = 'native' | 'central'

interface EnvConfig {
  enabled: boolean
  nws_zones: string[]
  nws: { enabled: boolean; user_agent: string; tick_seconds: number; severity_min: string; feed_source?: FeedSource }
  swpc: { enabled: boolean; feed_source?: FeedSource }
  ducting: { enabled: boolean; tick_seconds: number; latitude: number; longitude: number; feed_source?: FeedSource }
  fires: { enabled: boolean; tick_seconds: number; state: string; feed_source?: FeedSource }
  avalanche: { enabled: boolean; tick_seconds: number; center_ids: string[]; season_months: number[]; feed_source?: FeedSource }
  usgs: { enabled: boolean; tick_seconds: number; sites: string[]; feed_source?: FeedSource }
  usgs_quake: { enabled: boolean; tick_seconds: number; feed_url: string; global_mag_floor: number; regional_mag_floor: number; regional_radius_mi: number; escalate_mag_floor: number; broadcast_pager_alerts: string[]; region: string; feed_source?: FeedSource }
  traffic: { enabled: boolean; tick_seconds: number; api_key: string; corridors: { name: string; lat: number; lon: number }[]; feed_source?: FeedSource }
  roads511: { enabled: boolean; tick_seconds: number; api_key: string; base_url: string; endpoints: string[]; bbox: number[]; feed_source?: FeedSource }
  wzdx: { enabled: boolean; tick_seconds: number; api_key: string; base_url: string; endpoints: string[]; bbox: number[]; feed_source?: FeedSource }
  firms: { enabled: boolean; tick_seconds: number; map_key: string; source: string; bbox: number[]; day_range: number; confidence_min: string; proximity_km: number; feed_source?: FeedSource }
  central?: { enabled: boolean; url: string; durable: string; region: string }
}

// WFIGS adapter config shape
interface WfigsConfig {
  allowed_incident_types: string[]
  freshness_seconds: number
  cooldown_seconds: number
  broadcast_on_acres: boolean
  broadcast_on_contained: boolean
}

// Fires adapter config shape (digest settings)
interface FiresConfig {
  digest_enabled: boolean
  digest_schedule: string[]
  digest_timezone: string
}

// ITD 511 adapter config shape
interface Roads511Config {
  min_severity: string
  enabled_categories: string[]
  enabled_sub_types: string[]
}

// WZDx adapter config shape
interface WzdxConfig {
  broadcast: boolean
  min_severity: string
  sub_types: string[]
}

// NWS adapter config shape
interface NwsConfig {
  broadcast_severities: string[]
  duplicate_allowed_after_seconds: number
}

// Avalanche adapter config shape
interface AvalancheConfig {
  min_danger_level: number
}

interface TomtomConfig {
  min_magnitude: number
  drop_non_present: boolean
  drop_zero_magnitude: boolean
}


type FeedHealth = EnvStatus['feeds'][number]

// ---------------------------------------------------------------- status cards
function FeedStatusCard({ feed }: { feed: FeedHealth }) {
  const color = !feed.is_loaded ? 'bg-red-500' : feed.consecutive_errors > 0 ? 'bg-amber-500' : 'bg-green-500'
  const text = !feed.is_loaded ? 'Not loaded' : feed.consecutive_errors > 0 ? `${feed.consecutive_errors} errors` : 'Healthy'
  const lastFetch = feed.last_fetch ? new Date(feed.last_fetch * 1000).toLocaleTimeString() : 'Never'
  return (
    <div className="bg-bg-hover rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className={`w-2 h-2 rounded-full ${color}`} />
          <span className="text-sm font-medium text-slate-200 uppercase">{feed.source}</span>
        </div>
        <span className="text-xs text-slate-400">{text}</span>
      </div>
      <div className="text-xs text-slate-500 space-y-1">
        <div>Events: {feed.event_count}</div>
        <div>Last fetch: {lastFetch}</div>
        {feed.last_error && <div className="text-amber-500 truncate">{feed.last_error}</div>}
      </div>
    </div>
  )
}

function EventCard({ event }: { event: EnvEvent }) {
  const sev = event.severity.toLowerCase()
  const styles = (sev === 'extreme' || sev === 'severe' || sev === 'immediate')
    ? { bg: 'bg-red-500/10', border: 'border-red-500', Icon: AlertCircle, color: 'text-red-500' }
    : (sev === 'moderate' || sev === 'warning' || sev === 'priority')
    ? { bg: 'bg-amber-500/10', border: 'border-amber-500', Icon: AlertTriangle, color: 'text-amber-500' }
    : { bg: 'bg-blue-500/10', border: 'border-blue-500', Icon: Info, color: 'text-blue-500' }
  const Icon = styles.Icon
  return (
    <div className={`p-3 rounded-lg ${styles.bg} border-l-2 ${styles.border}`}>
      <div className="flex items-start gap-3">
        <Icon size={16} className={styles.color} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-medium text-slate-200">{event.event_type}</span>
            <span className={`text-xs px-1.5 py-0.5 rounded ${styles.bg} ${styles.color}`}>{event.severity}</span>
          </div>
          <div className="text-sm text-slate-300">{event.headline}</div>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- feed_source toggle
function FeedSourceToggle({ value, onChange, disabled, centralDisabled }: {
  value: FeedSource; onChange: (v: FeedSource) => void; disabled: boolean; centralDisabled: boolean
}) {
  const base = 'px-2 py-1 text-xs transition-colors'
  return (
    <div className={`flex rounded border border-[#1e2a3a] overflow-hidden ${disabled ? 'opacity-40' : ''}`}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange('native')}
        className={`${base} ${value === 'native' ? 'bg-accent text-white' : 'text-slate-400 hover:text-slate-200'}`}
      >native</button>
      <button
        type="button"
        disabled={disabled || centralDisabled}
        title={centralDisabled ? 'Central not available for this adapter' : ''}
        onClick={() => { if (!centralDisabled) onChange('central') }}
        className={`${base} ${centralDisabled ? 'text-slate-600 cursor-not-allowed' : value === 'central' ? 'bg-accent text-white' : 'text-slate-400 hover:text-slate-200'}`}
      >central</button>
    </div>
  )
}

// ---------------------------------------------------------------- adapter panel
function AdapterPanel({ title, subtitle, enabled, onEnabled, feedSource, onFeedSource, hasCentral, nativeOnly, hasKey, health, events, children }: {
  title: string; subtitle?: string
  enabled: boolean; onEnabled: (v: boolean) => void
  feedSource: FeedSource; onFeedSource: (v: FeedSource) => void
  hasCentral: boolean; nativeOnly: boolean; hasKey: boolean
  health?: FeedHealth; events?: EnvEvent[]; children?: ReactNode
}) {
  const centralDisabled = nativeOnly || !hasCentral
  return (
    <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <span className="text-sm font-medium text-slate-300">{title}</span>
          {subtitle && <p className="text-xs text-slate-600">{subtitle}</p>}
        </div>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1">
            <span className="text-[10px] uppercase tracking-wide text-slate-600">source</span>
            <FeedSourceToggle value={feedSource} onChange={onFeedSource} disabled={!enabled} centralDisabled={centralDisabled} />
          </div>
          <Toggle label="" checked={enabled} onChange={onEnabled} />
        </div>
      </div>
      {!hasKey && (
        <div className="text-xs text-amber-400 bg-amber-500/10 rounded p-2">
          API key not configured — contact admin
        </div>
      )}
      {nativeOnly && (
        <div className="text-[11px] text-slate-600">Central not available for this adapter — native only</div>
      )}
      <div className={enabled ? 'space-y-3' : 'space-y-3 opacity-40 pointer-events-none select-none'}>
        {children}
      </div>
      {(health || (events && events.length > 0)) && (
        <div className="pt-2 border-t border-[#1e2a3a] space-y-3">
          <div className="text-[10px] uppercase tracking-wide text-slate-600">Live status</div>
          {health ? <FeedStatusCard feed={health} /> : <div className="text-xs text-slate-600">No status reported.</div>}
          {events && events.length > 0 && (
            <div className="space-y-2">
              {events.slice(0, 5).map((e, i) => <EventCard key={i} event={e} />)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- families
type AdapterKey = 'nws' | 'fires' | 'firms' | 'swpc' | 'ducting' | 'traffic' | 'roads511' | 'wzdx' | 'usgs_quake' | 'usgs' | 'avalanche'

interface AdapterMeta { label: string; subtitle: string; health: string; hasCentral: boolean; nativeOnly: boolean; hasKey: boolean }

const META: Record<AdapterKey, AdapterMeta> = {
  nws: { label: 'NWS Weather Alerts', subtitle: 'National Weather Service alerts', health: 'nws', hasCentral: true, nativeOnly: false, hasKey: true },
  fires: { label: 'NIFC Fire Perimeters', subtitle: 'Active wildfires (National Interagency Fire Center)', health: 'nifc', hasCentral: true, nativeOnly: false, hasKey: true },
  firms: { label: 'NASA FIRMS Hotspots', subtitle: 'Satellite thermal-anomaly detections', health: 'firms', hasCentral: true, nativeOnly: false, hasKey: false },
  swpc: { label: 'NOAA Space Weather (SWPC)', subtitle: 'Solar indices, geomagnetic storms', health: 'swpc', hasCentral: true, nativeOnly: false, hasKey: true },
  ducting: { label: 'Tropospheric Ducting', subtitle: 'VHF/UHF extended-range conditions', health: 'ducting', hasCentral: false, nativeOnly: true, hasKey: true },
  traffic: { label: 'TomTom Traffic', subtitle: 'Traffic flow on monitored corridors', health: 'traffic', hasCentral: true, nativeOnly: false, hasKey: true },
  roads511: { label: '511 Road Conditions', subtitle: 'State DOT road events and closures', health: 'roads511', hasCentral: true, nativeOnly: false, hasKey: false },
  wzdx: { label: 'WZDx Work Zones', subtitle: 'Planned road work and construction events from ITD', health: 'roads511', hasCentral: true, nativeOnly: false, hasKey: true },
  usgs_quake: { label: 'USGS Earthquakes', subtitle: 'Seismic events from the USGS feed', health: 'usgs_quake', hasCentral: true, nativeOnly: false, hasKey: true },
  usgs: { label: 'USGS Stream Gauges', subtitle: 'River and stream water levels', health: 'usgs', hasCentral: true, nativeOnly: false, hasKey: true },
  avalanche: { label: 'Avalanche Advisories', subtitle: 'Backcountry avalanche danger ratings', health: 'avalanche', hasCentral: false, nativeOnly: true, hasKey: true },
}

const FAMILIES: { key: string; label: string; icon: typeof Cloud; adapters: AdapterKey[] }[] = [
  { key: 'central', label: 'Central', icon: Server, adapters: [] },
  { key: 'weather', label: 'Weather', icon: Cloud, adapters: ['nws'] },
  { key: 'fire', label: 'Fire', icon: Flame, adapters: ['fires', 'firms'] },
  { key: 'rf', label: 'RF Propagation', icon: Radio, adapters: ['swpc', 'ducting'] },
  { key: 'roads', label: 'Roads', icon: Car, adapters: ['traffic', 'roads511', 'wzdx'] },
  { key: 'geohazards', label: 'Geohazards', icon: Mountain, adapters: ['usgs_quake', 'usgs', 'avalanche'] },
  { key: 'tracking', label: 'Tracking', icon: Satellite, adapters: [] },
  { key: 'mesh', label: 'Mesh Health', icon: Activity, adapters: [] },
]

// ---------------------------------------------------------------- main page
export default function Environment() {
  const [env, setEnv] = useState<EnvConfig | null>(null)
  const [original, setOriginal] = useState<string>('')
  const [status, setStatus] = useState<EnvStatus | null>(null)
  const [events, setEvents] = useState<EnvEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [restartRequired, setRestartRequired] = useState(false)
  const [family, setFamily] = useState('weather')
  const [adapter, setAdapter] = useState<AdapterKey | null>('nws')

  // WFIGS/fires adapter config state
  const [wfigsConfig, setWfigsConfig] = useState<WfigsConfig>({
    allowed_incident_types: ['WF'],
    freshness_seconds: 0,
    cooldown_seconds: 28800,
    broadcast_on_acres: true,
    broadcast_on_contained: true,
  })
  const [wfigsOriginal, setWfigsOriginal] = useState<string>("")
  const [firesConfig, setFiresConfig] = useState<FiresConfig>({
    digest_enabled: true,
    digest_schedule: ["06:00", "18:00"],
    digest_timezone: "America/Boise",
  })
  const [firesOriginal, setFiresOriginal] = useState<string>("")
  const [tomtomConfig, setTomtomConfig] = useState<TomtomConfig>({
    min_magnitude: 4,
    drop_non_present: true,
    drop_zero_magnitude: true,
  })
  const [tomtomOriginal, setTomtomOriginal] = useState<string>("")
  const [roads511Config, setRoads511Config] = useState<Roads511Config>({
    min_severity: "None",
    enabled_categories: ["incident", "closure"],
    enabled_sub_types: ["accident", "road_closed", "closure", "lane_closed", "vehicle_on_fire", "flooding", "debris"],
  })
  const [roads511Original, setRoads511Original] = useState<string>("")
  const [wzdxConfig, setWzdxConfig] = useState<WzdxConfig>({
    broadcast: false,
    min_severity: "Minor",
    sub_types: ["road_works", "lane_closed", "road_closed"],
  })
  const [wzdxOriginal, setWzdxOriginal] = useState<string>("")
  const [nwsConfig, setNwsConfig] = useState<NwsConfig>({
    broadcast_severities: ["Extreme", "Severe"],
    duplicate_allowed_after_seconds: 3600,
  })
  const [nwsOriginal, setNwsOriginal] = useState<string>("")
  const [avalancheConfig, setAvalancheConfig] = useState<AvalancheConfig>({
    min_danger_level: 3,
  })
  const [avalancheOriginal, setAvalancheOriginal] = useState<string>("")


  useEffect(() => {
    document.title = 'Environment — MeshAI'
    ;(async () => {
      try {
        const res = await fetch('/api/config/environmental')
        const data = await res.json()
        setEnv(data)
        setOriginal(JSON.stringify(data))

        // Load adapter-config for wfigs
        try {
          const wfigsRes = await fetch("/api/adapter-config/wfigs")
          if (wfigsRes.ok) {
            const wfigsData = await wfigsRes.json()
            const cfg: WfigsConfig = {
              allowed_incident_types: wfigsData.allowed_incident_types?.value ?? ['WF'],
              freshness_seconds: wfigsData.freshness_seconds?.value ?? 0,
              cooldown_seconds: wfigsData.cooldown_seconds?.value ?? 28800,
              broadcast_on_acres: wfigsData.broadcast_on_acres?.value ?? true,
              broadcast_on_contained: wfigsData.broadcast_on_contained?.value ?? true,
            }
            setWfigsConfig(cfg)
            setWfigsOriginal(JSON.stringify(cfg))
          }
        } catch { /* adapter-config optional */ }

        // Load adapter-config for fires (digest settings)
        try {
          const firesRes = await fetch("/api/adapter-config/fires")
          if (firesRes.ok) {
            const firesData = await firesRes.json()
            const cfg: FiresConfig = {
              digest_enabled: firesData.digest_enabled?.value ?? true,
              digest_schedule: firesData.digest_schedule?.value ?? ["06:00", "18:00"],
              digest_timezone: firesData.digest_timezone?.value ?? "America/Boise",
            }
            setFiresConfig(cfg)
            setFiresOriginal(JSON.stringify(cfg))
          }
        } catch { /* adapter-config optional */ }

        // Load adapter-config for tomtom_incidents
        try {
          const ttRes = await fetch("/api/adapter-config/tomtom_incidents")
          if (ttRes.ok) {
            const ttData = await ttRes.json()
            const cfg: TomtomConfig = {
              min_magnitude: ttData.min_magnitude?.value ?? 4,
              drop_non_present: ttData.drop_non_present?.value ?? true,
              drop_zero_magnitude: ttData.drop_zero_magnitude?.value ?? true,
            }
            setTomtomConfig(cfg)
            setTomtomOriginal(JSON.stringify(cfg))
          }
        } catch { /* adapter-config optional */ }

        // Load adapter-config for itd_511
        try {
          const r511Res = await fetch("/api/adapter-config/itd_511")
          if (r511Res.ok) {
            const r511Data = await r511Res.json()
            const cfg: Roads511Config = {
              min_severity: r511Data.min_severity?.value ?? "None",
              enabled_categories: r511Data.enabled_categories?.value ?? ["incident", "closure"],
              enabled_sub_types: r511Data.enabled_sub_types?.value ?? ["accident", "road_closed", "closure", "lane_closed", "vehicle_on_fire", "flooding", "debris"],
            }
            setRoads511Config(cfg)
            setRoads511Original(JSON.stringify(cfg))
          }
        } catch { /* adapter-config optional */ }

        // Load adapter-config for wzdx
        try {
          const wzdxRes = await fetch("/api/adapter-config/wzdx")
          if (wzdxRes.ok) {
            const wzdxData = await wzdxRes.json()
            const cfg: WzdxConfig = {
              broadcast: wzdxData.broadcast?.value ?? false,
              min_severity: wzdxData.min_severity?.value ?? "Minor",
              sub_types: wzdxData.sub_types?.value ?? ["road_works", "lane_closed", "road_closed"],
            }
            setWzdxConfig(cfg)
            setWzdxOriginal(JSON.stringify(cfg))
          }
        } catch { /* adapter-config optional */ }

        // Load adapter-config for nws
        try {
          const nwsRes = await fetch("/api/adapter-config/nws")
          if (nwsRes.ok) {
            const nwsData = await nwsRes.json()
            const cfg: NwsConfig = {
              broadcast_severities: nwsData.broadcast_severities?.value ?? ["Extreme", "Severe"],
              duplicate_allowed_after_seconds: nwsData.duplicate_allowed_after_seconds?.value ?? 3600,
            }
            setNwsConfig(cfg)
            setNwsOriginal(JSON.stringify(cfg))
          }
        } catch { /* adapter-config optional */ }

        // Load adapter-config for avalanche
        try {
          const avyRes = await fetch("/api/adapter-config/avalanche")
          if (avyRes.ok) {
            const avyData = await avyRes.json()
            const cfg: AvalancheConfig = {
              min_danger_level: avyData.min_danger_level?.value ?? 3,
            }
            setAvalancheConfig(cfg)
            setAvalancheOriginal(JSON.stringify(cfg))
          }
        } catch { /* adapter-config optional */ }

      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load config')
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  useEffect(() => {
    const load = async () => {
      try {
        setStatus(await fetchEnvStatus())
        setEvents(await fetchEnvActive())
      } catch { /* status is best-effort */ }
    }
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [])

  const hasEnvChanges = env !== null && JSON.stringify(env) !== original
  const hasWfigsChanges = JSON.stringify(wfigsConfig) !== wfigsOriginal
  const hasFiresChanges = JSON.stringify(firesConfig) !== firesOriginal
  const hasTomtomChanges = JSON.stringify(tomtomConfig) !== tomtomOriginal
  const hasRoads511Changes = JSON.stringify(roads511Config) !== roads511Original
  const hasWzdxChanges = JSON.stringify(wzdxConfig) !== wzdxOriginal
  const hasNwsChanges = JSON.stringify(nwsConfig) !== nwsOriginal
  const hasAvalancheChanges = JSON.stringify(avalancheConfig) !== avalancheOriginal
  const hasChanges = hasEnvChanges || hasWfigsChanges || hasFiresChanges || hasTomtomChanges || hasRoads511Changes || hasWzdxChanges || hasNwsChanges || hasAvalancheChanges

  
  const saveAdapterConfig = async (adapterName: string, key: string, value: unknown) => {
    const res = await fetch(`/api/adapter-config/${adapterName}/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `Failed to save ${adapterName}.${key}`)
    }
  }

const save = async () => {
    if (!env) return
    setSaving(true); setError(null); setSuccess(null)
    try {
      // Save environmental config
      if (hasEnvChanges) {
        const res = await fetch('/api/config/environmental', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(env),
        })
        const result = await res.json()
        if (!res.ok) throw new Error(result.detail || 'Save failed')
        setOriginal(JSON.stringify(env))
        if (result.restart_required) setRestartRequired(true)
      }

      // Save wfigs adapter config changes
      if (hasWfigsChanges) {
        const orig = JSON.parse(wfigsOriginal) as WfigsConfig
        if (wfigsConfig.freshness_seconds !== orig.freshness_seconds) {
          await saveAdapterConfig("wfigs", "freshness_seconds", wfigsConfig.freshness_seconds)
        }
        if (JSON.stringify(wfigsConfig.allowed_incident_types) !== JSON.stringify(orig.allowed_incident_types)) {
          await saveAdapterConfig("wfigs", "allowed_incident_types", wfigsConfig.allowed_incident_types)
        }
        if (wfigsConfig.cooldown_seconds !== orig.cooldown_seconds) {
          await saveAdapterConfig("wfigs", "cooldown_seconds", wfigsConfig.cooldown_seconds)
        }
        if (wfigsConfig.broadcast_on_acres !== orig.broadcast_on_acres) {
          await saveAdapterConfig("wfigs", "broadcast_on_acres", wfigsConfig.broadcast_on_acres)
        }
        if (wfigsConfig.broadcast_on_contained !== orig.broadcast_on_contained) {
          await saveAdapterConfig("wfigs", "broadcast_on_contained", wfigsConfig.broadcast_on_contained)
        }
        setWfigsOriginal(JSON.stringify(wfigsConfig))
      }

      // Save fires adapter config changes (digest)
      if (hasFiresChanges) {
        const orig = JSON.parse(firesOriginal) as FiresConfig
        if (firesConfig.digest_enabled !== orig.digest_enabled) {
          await saveAdapterConfig("fires", "digest_enabled", firesConfig.digest_enabled)
        }
        if (JSON.stringify(firesConfig.digest_schedule) !== JSON.stringify(orig.digest_schedule)) {
          await saveAdapterConfig("fires", "digest_schedule", firesConfig.digest_schedule)
        }
        if (firesConfig.digest_timezone !== orig.digest_timezone) {
          await saveAdapterConfig("fires", "digest_timezone", firesConfig.digest_timezone)
        }
        setFiresOriginal(JSON.stringify(firesConfig))
      }

      // Save tomtom adapter config changes
      if (hasTomtomChanges) {
        const orig = JSON.parse(tomtomOriginal) as TomtomConfig
        if (tomtomConfig.min_magnitude !== orig.min_magnitude) {
          await saveAdapterConfig("tomtom_incidents", "min_magnitude", tomtomConfig.min_magnitude)
        }
        if (tomtomConfig.drop_non_present !== orig.drop_non_present) {
          await saveAdapterConfig("tomtom_incidents", "drop_non_present", tomtomConfig.drop_non_present)
        }
        if (tomtomConfig.drop_zero_magnitude !== orig.drop_zero_magnitude) {
          await saveAdapterConfig("tomtom_incidents", "drop_zero_magnitude", tomtomConfig.drop_zero_magnitude)
        }
        setTomtomOriginal(JSON.stringify(tomtomConfig))
      }

      // Save itd_511 adapter config changes
      if (hasRoads511Changes) {
        const orig = JSON.parse(roads511Original) as Roads511Config
        if (roads511Config.min_severity !== orig.min_severity) {
          await saveAdapterConfig("itd_511", "min_severity", roads511Config.min_severity)
        }
        if (JSON.stringify(roads511Config.enabled_categories) !== JSON.stringify(orig.enabled_categories)) {
          await saveAdapterConfig("itd_511", "enabled_categories", roads511Config.enabled_categories)
        }
        if (JSON.stringify(roads511Config.enabled_sub_types) !== JSON.stringify(orig.enabled_sub_types)) {
          await saveAdapterConfig("itd_511", "enabled_sub_types", roads511Config.enabled_sub_types)
        }
        setRoads511Original(JSON.stringify(roads511Config))
      }

      // Save wzdx adapter config changes
      if (hasWzdxChanges) {
        const orig = JSON.parse(wzdxOriginal) as WzdxConfig
        if (wzdxConfig.broadcast !== orig.broadcast) {
          await saveAdapterConfig("wzdx", "broadcast", wzdxConfig.broadcast)
        }
        if (wzdxConfig.min_severity !== orig.min_severity) {
          await saveAdapterConfig("wzdx", "min_severity", wzdxConfig.min_severity)
        }
        if (JSON.stringify(wzdxConfig.sub_types) !== JSON.stringify(orig.sub_types)) {
          await saveAdapterConfig("wzdx", "sub_types", wzdxConfig.sub_types)
        }
        setWzdxOriginal(JSON.stringify(wzdxConfig))
      }

      // Save nws adapter config changes
      if (hasNwsChanges) {
        const orig = JSON.parse(nwsOriginal) as NwsConfig
        if (JSON.stringify(nwsConfig.broadcast_severities) !== JSON.stringify(orig.broadcast_severities)) {
          await saveAdapterConfig("nws", "broadcast_severities", nwsConfig.broadcast_severities)
        }
        if (nwsConfig.duplicate_allowed_after_seconds !== orig.duplicate_allowed_after_seconds) {
          await saveAdapterConfig("nws", "duplicate_allowed_after_seconds", nwsConfig.duplicate_allowed_after_seconds)
        }
        setNwsOriginal(JSON.stringify(nwsConfig))
      }

      // Save avalanche adapter config changes
      if (hasAvalancheChanges) {
        const orig = JSON.parse(avalancheOriginal) as AvalancheConfig
        if (avalancheConfig.min_danger_level !== orig.min_danger_level) {
          await saveAdapterConfig("avalanche", "min_danger_level", avalancheConfig.min_danger_level)
        }
        setAvalancheOriginal(JSON.stringify(avalancheConfig))
      }

      setSuccess('Config saved')
      setTimeout(() => setSuccess(null), 3000)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const discard = () => {
    if (env) setEnv(JSON.parse(original))
    setWfigsConfig(JSON.parse(wfigsOriginal || JSON.stringify(wfigsConfig)))
    setFiresConfig(JSON.parse(firesOriginal || JSON.stringify(firesConfig)))
    setTomtomConfig(JSON.parse(tomtomOriginal || JSON.stringify(tomtomConfig)))
    setRoads511Config(JSON.parse(roads511Original || JSON.stringify(roads511Config)))
    setWzdxConfig(JSON.parse(wzdxOriginal || JSON.stringify(wzdxConfig)))
    setNwsConfig(JSON.parse(nwsOriginal || JSON.stringify(nwsConfig)))
    setAvalancheConfig(JSON.parse(avalancheOriginal || JSON.stringify(avalancheConfig)))
  }
  const restart = async () => {
    try { await fetch('/api/restart', { method: 'POST' }); setRestartRequired(false); setSuccess('Restart initiated') }
    catch { setError('Restart failed') }
  }

  const up = (patch: Partial<EnvConfig>) => env && setEnv({ ...env, ...patch })

  if (loading) return <div className="flex items-center justify-center h-64 text-slate-400">Loading environmental config…</div>
  if (!env) return <div className="flex items-center justify-center h-64 text-red-400">{error || 'No config'}</div>

  const healthFor = (key: AdapterKey): FeedHealth | undefined =>
    status?.feeds.find((f) => f.source === META[key].health)
  const eventsFor = (key: AdapterKey): EnvEvent[] =>
    events.filter((e) => e.source === META[key].health)

  const fam = FAMILIES.find((f) => f.key === family)!
  const activeAdapter: AdapterKey | null =
    fam.adapters.length === 0 ? null : (adapter && fam.adapters.includes(adapter) ? adapter : fam.adapters[0])

  // -- per-adapter settings forms (preserve all existing settings) --
  const renderSettings = (key: AdapterKey) => {
    switch (key) {
      case 'nws': return (<>
        <ListInput label="NWS Zones" value={env.nws_zones} onChange={(v) => up({ nws_zones: v })} helper="Zone IDs like IDZ016, IDZ030" infoLink="https://www.weather.gov/pimar/PubZone" />
        {env.nws.feed_source !== 'central' && (
          <>
            <TextInput label="User Agent" value={env.nws.user_agent} onChange={(v) => up({ nws: { ...env.nws, user_agent: v } })} placeholder="(MeshAI, you@email.com)" helper="Format: (app_name, contact_email)" />
            <div className="grid grid-cols-2 gap-4">
              <NumberInput label="Tick Seconds" value={env.nws.tick_seconds} onChange={(v) => up({ nws: { ...env.nws, tick_seconds: v } })} min={30} />
              <SelectInput label="Min Severity" value={env.nws.severity_min} onChange={(v) => up({ nws: { ...env.nws, severity_min: v } })} options={[{ value: 'minor', label: 'Minor' }, { value: 'moderate', label: 'Moderate' }, { value: 'severe', label: 'Severe' }, { value: 'extreme', label: 'Extreme' }]} />
            </div>
          </>
        )}
        {env.nws.feed_source === 'central' && (
          <div className="border-t border-slate-700/50 pt-4 mt-4">
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">Broadcast Filters</div>
            <div className="mb-3">
              <div className="text-xs text-slate-400 mb-2">Severities to broadcast</div>
              <div className="flex gap-6">
                {['Extreme', 'Severe', 'Moderate', 'Minor'].map((sev) => (
                  <label key={sev} className="flex items-center gap-2 cursor-pointer">
                    <input type="checkbox" checked={nwsConfig.broadcast_severities.includes(sev)}
                      onChange={(e) => {
                        const cur = nwsConfig.broadcast_severities
                        setNwsConfig({ ...nwsConfig, broadcast_severities: e.target.checked ? [...cur, sev] : cur.filter(s => s !== sev) })
                      }}
                      className="w-4 h-4 rounded accent-blue-500" />
                    <span className="text-sm text-slate-300">{sev}</span>
                  </label>
                ))}
              </div>
            </div>
            <NumberInput label="Re-broadcast Cooldown (seconds)" value={nwsConfig.duplicate_allowed_after_seconds}
              onChange={(v) => setNwsConfig({ ...nwsConfig, duplicate_allowed_after_seconds: v })}
              min={0} helper="Minimum seconds before the same alert ID can be re-broadcast" />
          </div>
        )}
      </>)
      case 'swpc': return <div className="text-xs text-slate-500">No additional settings.</div>
      case 'ducting': return (
        <div className="grid grid-cols-3 gap-4">
          <NumberInput label="Tick Seconds" value={env.ducting.tick_seconds} onChange={(v) => up({ ducting: { ...env.ducting, tick_seconds: v } })} min={60} />
          <NumberInput label="Latitude" value={env.ducting.latitude} onChange={(v) => up({ ducting: { ...env.ducting, latitude: v } })} step={0.01} />
          <NumberInput label="Longitude" value={env.ducting.longitude} onChange={(v) => up({ ducting: { ...env.ducting, longitude: v } })} step={0.01} />
        </div>)
      case 'fires': return (
        <div className="space-y-6">
          {env.fires.feed_source !== 'central' && (
            <div className="grid grid-cols-2 gap-4">
              <NumberInput label="Tick Seconds" value={env.fires.tick_seconds} onChange={(v) => up({ fires: { ...env.fires, tick_seconds: v } })} min={60} />
              <SelectInput label="State" value={env.fires.state} onChange={(v) => up({ fires: { ...env.fires, state: v } })} options={US_STATES} />
            </div>
          )}
          <div>
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">Incident Types</div>
            <div className="flex gap-6">
              {[['WF', 'Wildfire'], ['RX', 'Prescribed Burn'], ['OTHER', 'Other']].map(([val, label]) => (
                <label key={val} className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={wfigsConfig.allowed_incident_types?.includes(val) ?? val === 'WF'}
                    onChange={(e) => { const cur = wfigsConfig.allowed_incident_types ?? ['WF']; setWfigsConfig({ ...wfigsConfig, allowed_incident_types: e.target.checked ? [...cur, val] : cur.filter(t => t !== val) }) }}
                    className="w-4 h-4 rounded accent-blue-500" />
                  <span className="text-sm text-slate-300">{label}</span>
                </label>
              ))}
            </div>
          </div>
          <div>
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">Broadcast Triggers</div>
            <div className="space-y-2">
              <label className="flex items-center justify-between">
                <span className="text-sm text-slate-300">Broadcast on acres increase</span>
                <input type="checkbox" checked={wfigsConfig.broadcast_on_acres} onChange={(e) => setWfigsConfig({ ...wfigsConfig, broadcast_on_acres: e.target.checked })} className="w-4 h-4 rounded accent-blue-500" />
              </label>
              <label className="flex items-center justify-between">
                <span className="text-sm text-slate-300">Broadcast on containment increase</span>
                <input type="checkbox" checked={wfigsConfig.broadcast_on_contained} onChange={(e) => setWfigsConfig({ ...wfigsConfig, broadcast_on_contained: e.target.checked })} className="w-4 h-4 rounded accent-blue-500" />
              </label>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <NumberInput label="Update Cooldown (hours)" value={Math.round(wfigsConfig.cooldown_seconds / 3600)} onChange={(v) => setWfigsConfig({ ...wfigsConfig, cooldown_seconds: v * 3600 })} min={0} helper="Minimum hours between updates for the same fire" />
            <NumberInput label="Freshness Window (hours)" value={Math.round(wfigsConfig.freshness_seconds / 3600)} onChange={(v) => setWfigsConfig({ ...wfigsConfig, freshness_seconds: v * 3600 })} min={0} helper="0 = always broadcast regardless of event age" />
          </div>
        </div>
      )
      case 'avalanche': return (
        <div className="space-y-6">
          {env.avalanche.feed_source !== 'central' && (
            <div className="grid grid-cols-2 gap-4">
              <NumberInput label="Tick Seconds" value={env.avalanche.tick_seconds}
                onChange={(v) => up({ avalanche: { ...env.avalanche, tick_seconds: v } })}
                min={60} />
              <NumberListInput label="Season Months" value={env.avalanche.season_months}
                onChange={(v) => up({ avalanche: { ...env.avalanche, season_months: v } })}
                helper="e.g., 12, 1, 2, 3, 4" />
            </div>
          )}
          <ListInput label="Center IDs" value={env.avalanche.center_ids}
            onChange={(v) => up({ avalanche: { ...env.avalanche, center_ids: v } })}
            helper="e.g., SNFAC" infoLink="https://avalanche.org/avalanche-centers/" />
          <div>
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">
              Broadcast Settings
            </div>
            <div className="grid grid-cols-2 gap-4">
              <SelectInput label="Min Danger Level" value={String(avalancheConfig.min_danger_level)}
                onChange={(v) => setAvalancheConfig({ ...avalancheConfig, min_danger_level: Number(v) })}
                options={[
                  { value: "3", label: "3 — Considerable" },
                  { value: "4", label: "4 — High" },
                  { value: "5", label: "5 — Extreme" },
                ]}
                helper="Minimum avalanche danger level to broadcast" />
            </div>
          </div>
        </div>
      )
      case 'usgs': return (<>
        <NumberInput label="Tick Seconds" value={env.usgs.tick_seconds} onChange={(v) => up({ usgs: { ...env.usgs, tick_seconds: v } })} min={900} helper="Minimum 15 min (900s). tick_seconds is the native-mode poll interval; ignored when this adapter is set to feed_source=central." />
        <ListInput label="Site IDs" value={env.usgs.sites} onChange={(v) => up({ usgs: { ...env.usgs, sites: v } })} helper="USGS gauge site numbers" infoLink="https://waterdata.usgs.gov/nwis" />
      </>)
      case 'usgs_quake': return (
        <div className="space-y-6">
          {env.usgs_quake.feed_source !== 'central' && (
            <div className="grid grid-cols-2 gap-4">
              <NumberInput label="Tick Seconds" value={env.usgs_quake.tick_seconds}
                onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, tick_seconds: v } })}
                min={60} />
              <TextInput label="Region Tag" value={env.usgs_quake.region}
                onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, region: v } })} />
            </div>
          )}
          <div>
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">
              Magnitude Thresholds
            </div>
            <div className="grid grid-cols-2 gap-4">
              <NumberInput label="Global Floor" value={env.usgs_quake.global_mag_floor}
                onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, global_mag_floor: v } })}
                step={0.1} min={0} helper="Broadcast anywhere at or above this magnitude" />
              <NumberInput label="Regional Floor" value={env.usgs_quake.regional_mag_floor}
                onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, regional_mag_floor: v } })}
                step={0.1} min={0} helper="Reduced floor within regional radius" />
              <NumberInput label="Regional Radius (mi)" value={env.usgs_quake.regional_radius_mi}
                onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, regional_radius_mi: v } })}
                min={50} helper="Radius around region centroid for reduced floor" />
              <NumberInput label="Escalation Floor" value={env.usgs_quake.escalate_mag_floor}
                onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, escalate_mag_floor: v } })}
                step={0.1} min={0} helper="Magnitude at which broadcast uses warning emoji" />
            </div>
          </div>
          <div>
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">
              PAGER Alert Levels
            </div>
            <div className="text-xs text-slate-500 mb-2">
              Broadcast at any magnitude when USGS PAGER alert reaches these levels
            </div>
            <div className="flex gap-6">
              {(['green','yellow','orange','red'] as const).map((level) => (
                <label key={level} className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox"
                    checked={env.usgs_quake.broadcast_pager_alerts.includes(level)}
                    onChange={(e) => {
                      const cur = env.usgs_quake.broadcast_pager_alerts
                      up({ usgs_quake: { ...env.usgs_quake,
                        broadcast_pager_alerts: e.target.checked
                          ? [...cur, level]
                          : cur.filter((l) => l !== level) }})
                    }}
                    className="w-4 h-4 rounded accent-blue-500" />
                  <span className="text-sm text-slate-300 capitalize">{level}</span>
                </label>
              ))}
            </div>
          </div>
        </div>
      )
      case 'traffic': return (<>
        <TextInput label="API Key" value={env.traffic.api_key} onChange={(v) => up({ traffic: { ...env.traffic, api_key: v } })} type="password" helper="developer.tomtom.com" />
        <NumberInput label="Tick Seconds" value={env.traffic.tick_seconds} onChange={(v) => up({ traffic: { ...env.traffic, tick_seconds: v } })} min={60} />
        <div className="text-xs text-slate-500 mt-2">Corridors:</div>
        {(env.traffic.corridors || []).map((c, i) => (
          <div key={i} className="grid grid-cols-4 gap-2 items-end">
            <TextInput label="Name" value={c.name} onChange={(v) => { const n = [...env.traffic.corridors]; n[i] = { ...c, name: v }; up({ traffic: { ...env.traffic, corridors: n } }) }} />
            <NumberInput label="Lat" value={c.lat} onChange={(v) => { const n = [...env.traffic.corridors]; n[i] = { ...c, lat: v }; up({ traffic: { ...env.traffic, corridors: n } }) }} step={0.01} />
            <NumberInput label="Lon" value={c.lon} onChange={(v) => { const n = [...env.traffic.corridors]; n[i] = { ...c, lon: v }; up({ traffic: { ...env.traffic, corridors: n } }) }} step={0.01} />
            <button onClick={() => up({ traffic: { ...env.traffic, corridors: env.traffic.corridors.filter((_, j) => j !== i) } })} className="px-2 py-2 text-xs text-red-400 hover:text-red-300 border border-red-400/30 rounded">Remove</button>
          </div>
        ))}
        <button onClick={() => up({ traffic: { ...env.traffic, corridors: [...(env.traffic.corridors || []), { name: '', lat: 0, lon: 0 }] } })} className="text-xs text-accent hover:underline">+ Add Corridor</button>
        <div className="border-t border-slate-700/50 pt-4 mt-4">
          <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">Broadcast Filters</div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-xs text-slate-400 mb-1 block">Minimum Magnitude</label>
              <select
                value={tomtomConfig.min_magnitude}
                onChange={(e) => setTomtomConfig({...tomtomConfig, min_magnitude: parseInt(e.target.value)})}
                className="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm"
              >
                <option value={1}>1 — Minor (all)</option>
                <option value={2}>2 — Moderate (yellow+)</option>
                <option value={3}>3 — Major (orange+)</option>
                <option value={4}>4 — Severe (red only)</option>
              </select>
              <p className="text-xs text-slate-500 mt-1">Drop TomTom incidents below this severity level</p>
            </div>
          </div>
          <div className="mt-3 space-y-2">
            <label className="flex items-center justify-between">
              <span className="text-sm text-slate-300">Drop non-present time validity</span>
              <input type="checkbox" checked={tomtomConfig.drop_non_present} onChange={(e) => setTomtomConfig({...tomtomConfig, drop_non_present: e.target.checked})} className="w-4 h-4 rounded accent-blue-500" />
            </label>
            <label className="flex items-center justify-between">
              <span className="text-sm text-slate-300">Drop zero-magnitude events</span>
              <input type="checkbox" checked={tomtomConfig.drop_zero_magnitude} onChange={(e) => setTomtomConfig({...tomtomConfig, drop_zero_magnitude: e.target.checked})} className="w-4 h-4 rounded accent-blue-500" />
            </label>
          </div>
        </div>
      </>)
      case 'roads511': return (<>
        <TextInput label="Base URL" value={env.roads511.base_url} onChange={(v) => up({ roads511: { ...env.roads511, base_url: v } })} placeholder="https://511.yourstate.gov/api/v2" />
        <TextInput label="API Key" value={env.roads511.api_key} onChange={(v) => up({ roads511: { ...env.roads511, api_key: v } })} type="password" helper="Leave empty if not required" />
        <NumberInput label="Tick Seconds" value={env.roads511.tick_seconds} onChange={(v) => up({ roads511: { ...env.roads511, tick_seconds: v } })} min={60} />
        <ListInput label="Endpoints" value={env.roads511.endpoints} onChange={(v) => up({ roads511: { ...env.roads511, endpoints: v } })} helper="e.g., /get/event" />
        <div className="grid grid-cols-4 gap-2">
          {(['West', 'South', 'East', 'North'] as const).map((lbl, i) => (
            <NumberInput key={lbl} label={lbl} value={env.roads511.bbox?.[i] ?? 0} onChange={(v) => { const b = [...(env.roads511.bbox || [0, 0, 0, 0])]; b[i] = v; up({ roads511: { ...env.roads511, bbox: b } }) }} step={0.01} />
          ))}
        </div>
        <div className="border-t border-slate-700/50 pt-4 mt-4">
          <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">Broadcast Filters</div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-xs text-slate-400 mb-1 block">Minimum Severity</label>
              <select
                value={roads511Config.min_severity}
                onChange={(e) => setRoads511Config({...roads511Config, min_severity: e.target.value})}
                className="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm"
              >
                <option value="None">None (all)</option>
                <option value="Minor">Minor+</option>
                <option value="Major">Major only</option>
              </select>
              <p className="text-xs text-slate-500 mt-1">Drop ITD 511 events below this severity</p>
            </div>
          </div>
          <div className="mt-4">
            <div className="text-xs text-slate-400 mb-2">Categories</div>
            <div className="flex gap-6">
              {([['incident', 'Incident'], ['closure', 'Closure'], ['special_event', 'Special Event']] as const).map(([val, label]) => (
                <label key={val} className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={roads511Config.enabled_categories.includes(val)}
                    onChange={(e) => { const cur = roads511Config.enabled_categories; setRoads511Config({...roads511Config, enabled_categories: e.target.checked ? [...cur, val] : cur.filter(c => c !== val)}) }}
                    className="w-4 h-4 rounded accent-blue-500" />
                  <span className="text-sm text-slate-300">{label}</span>
                </label>
              ))}
            </div>
          </div>
          <div className="mt-4">
            <div className="text-xs text-slate-400 mb-2">Sub-types</div>
            <div className="grid grid-cols-2 gap-2">
              {([['accident', 'Crash'], ['road_closed', 'Road Closed'], ['lane_closed', 'Lane Closure'], ['vehicle_on_fire', 'Vehicle Fire'], ['flooding', 'Flooding'], ['debris', 'Debris'], ['road_works', 'Road Works'], ['disabled_vehicle', 'Disabled Vehicle']] as const).map(([val, label]) => (
                <label key={val} className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={roads511Config.enabled_sub_types.includes(val)}
                    onChange={(e) => { const cur = roads511Config.enabled_sub_types; setRoads511Config({...roads511Config, enabled_sub_types: e.target.checked ? [...cur, val] : cur.filter(s => s !== val)}) }}
                    className="w-4 h-4 rounded accent-blue-500" />
                  <span className="text-sm text-slate-300">{label}</span>
                </label>
              ))}
            </div>
          </div>
        </div>
      </>)
      case 'wzdx': return (<>
        {env.wzdx?.feed_source !== 'central' && (
          <>
            <TextInput label="Base URL" value={env.wzdx?.base_url ?? ''} onChange={(v) => up({ wzdx: { ...env.wzdx!, base_url: v } })} placeholder="https://511.yourstate.gov/api/v2" />
            <TextInput label="API Key" value={env.wzdx?.api_key ?? ''} onChange={(v) => up({ wzdx: { ...env.wzdx!, api_key: v } })} type="password" helper="Leave empty if not required" />
            <NumberInput label="Tick Seconds" value={env.wzdx?.tick_seconds ?? 300} onChange={(v) => up({ wzdx: { ...env.wzdx!, tick_seconds: v } })} min={60} />
            <ListInput label="Endpoints" value={env.wzdx?.endpoints ?? ['/get/event']} onChange={(v) => up({ wzdx: { ...env.wzdx!, endpoints: v } })} helper="e.g., /get/event" />
            <div className="grid grid-cols-4 gap-2">
              {(['West', 'South', 'East', 'North'] as const).map((lbl, i) => (
                <NumberInput key={lbl} label={lbl} value={env.wzdx?.bbox?.[i] ?? 0} onChange={(v) => { const b = [...(env.wzdx?.bbox || [0, 0, 0, 0])]; b[i] = v; up({ wzdx: { ...env.wzdx!, bbox: b } }) }} step={0.01} />
              ))}
            </div>
            <div className="text-xs text-slate-500">Bounding box [W,S,E,N] geographic filter</div>
          </>
        )}
        <div className="border-t border-slate-700/50 pt-4 mt-4">
          <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">Broadcast Settings</div>
          <label className="flex items-center justify-between">
            <span className="text-sm text-slate-300">Broadcast work zone events</span>
            <input type="checkbox" checked={wzdxConfig.broadcast}
              onChange={(e) => setWzdxConfig({...wzdxConfig, broadcast: e.target.checked})}
              className="w-4 h-4 rounded accent-blue-500" />
          </label>
          {wzdxConfig.broadcast ? (
            <div className="space-y-3 mt-3">
              <div>
                <label className="text-xs text-slate-400 mb-1 block">Min Severity</label>
                <select
                  value={wzdxConfig.min_severity}
                  onChange={(e) => setWzdxConfig({...wzdxConfig, min_severity: e.target.value})}
                  className="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-sm"
                >
                  <option value="None">None (all)</option>
                  <option value="Minor">Minor+</option>
                  <option value="Major">Major only</option>
                </select>
              </div>
              <div>
                <div className="text-xs text-slate-400 mb-2">Sub-types</div>
                <div className="flex gap-6">
                  {([['road_works', 'Road Works'], ['lane_closed', 'Lane Closure'], ['road_closed', 'Road Closed']] as const).map(([val, label]) => (
                    <label key={val} className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={wzdxConfig.sub_types.includes(val)}
                        onChange={(e) => { const cur = wzdxConfig.sub_types; setWzdxConfig({...wzdxConfig, sub_types: e.target.checked ? [...cur, val] : cur.filter(s => s !== val)}) }}
                        className="w-4 h-4 rounded accent-blue-500" />
                      <span className="text-sm text-slate-300">{label}</span>
                    </label>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <p className="text-xs text-slate-500 mt-2">Work zone events stored for LLM context only {'\u2014'} no mesh broadcasts.</p>
          )}
        </div>
      </>)
      case 'firms': return (<>
        <TextInput label="MAP Key" value={env.firms.map_key} onChange={(v) => up({ firms: { ...env.firms, map_key: v } })} type="password" helper="firms.modaps.eosdis.nasa.gov/api/area/" infoLink="https://firms.modaps.eosdis.nasa.gov/api/area/" />
        <NumberInput label="Tick Seconds" value={env.firms.tick_seconds} onChange={(v) => up({ firms: { ...env.firms, tick_seconds: v } })} min={300} />
        <SelectInput label="Satellite Source" value={env.firms.source} onChange={(v) => up({ firms: { ...env.firms, source: v } })} options={[{ value: 'VIIRS_SNPP_NRT', label: 'VIIRS SNPP (NRT)' }, { value: 'VIIRS_NOAA20_NRT', label: 'VIIRS NOAA-20 (NRT)' }, { value: 'MODIS_NRT', label: 'MODIS (NRT)' }]} />
        <div className="grid grid-cols-3 gap-4">
          <NumberInput label="Day Range" value={env.firms.day_range} onChange={(v) => up({ firms: { ...env.firms, day_range: v } })} min={1} max={10} />
          <SelectInput label="Min Confidence" value={env.firms.confidence_min} onChange={(v) => up({ firms: { ...env.firms, confidence_min: v } })} options={[{ value: 'low', label: 'Low' }, { value: 'nominal', label: 'Nominal' }, { value: 'high', label: 'High' }]} />
          <NumberInput label="Proximity (km)" value={env.firms.proximity_km} onChange={(v) => up({ firms: { ...env.firms, proximity_km: v } })} step={0.5} />
        </div>
        <div className="grid grid-cols-4 gap-2">
          {(['West', 'South', 'East', 'North'] as const).map((lbl, i) => (
            <NumberInput key={lbl} label={lbl} value={env.firms.bbox?.[i] ?? 0} onChange={(v) => { const b = [...(env.firms.bbox || [0, 0, 0, 0])]; b[i] = v; up({ firms: { ...env.firms, bbox: b } }) }} step={0.01} />
          ))}
        </div>
      </>)
    }
  }

  const a = env as unknown as Record<AdapterKey, { enabled: boolean; feed_source?: FeedSource }>
  const setAdapterField = (key: AdapterKey, patch: { enabled?: boolean; feed_source?: FeedSource }) => {
    const cur = (env as any)[key] || {}
    up({ [key]: { ...cur, ...patch } } as unknown as Partial<EnvConfig>)
  }

  return (
    <div className="space-y-6">
      {/* Header + master enable + save bar */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-200">Environment</h1>
        <div className="flex items-center gap-3">
          <Toggle label="Feeds Enabled" checked={env.enabled} onChange={(v) => up({ enabled: v })} />
          {hasChanges && (
            <>
              <button onClick={discard} className="flex items-center gap-1 px-3 py-1.5 text-sm text-slate-400 hover:text-slate-200 border border-border rounded">
                <RotateCcw size={14} /> Discard
              </button>
              <button onClick={save} disabled={saving} className="flex items-center gap-1 px-3 py-1.5 text-sm bg-accent text-white rounded disabled:opacity-50">
                <Save size={14} /> {saving ? 'Saving…' : 'Save'}
              </button>
            </>
          )}
        </div>
      </div>

      {error && <div className="text-sm text-red-400 bg-red-500/10 rounded p-3">{error}</div>}
      {success && <div className="text-sm text-green-400 bg-green-500/10 rounded p-3">{success}</div>}
      {restartRequired && (
        <div className="flex items-center justify-between text-sm text-amber-400 bg-amber-500/10 border border-amber-500/30 rounded p-3">
          <span className="flex items-center gap-2"><RefreshCw size={14} /> A restart is required for some changes to take effect.</span>
          <button onClick={restart} className="px-3 py-1 bg-amber-500/20 hover:bg-amber-500/30 rounded">Restart now</button>
        </div>
      )}


      {/* Family tabs */}
      <div className="flex gap-1 border-b border-border overflow-x-auto">
        {FAMILIES.map(({ key, label, icon: Icon }) => (
          <button key={key} onClick={() => { setFamily(key); const f = FAMILIES.find((x) => x.key === key)!; setAdapter(f.adapters[0] ?? null) }}
            className={`flex items-center gap-2 px-4 py-2 text-sm whitespace-nowrap border-b-2 -mb-px transition-colors ${family === key ? 'border-accent text-accent' : 'border-transparent text-slate-400 hover:text-slate-200'}`}>
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {/* Central Connection tab */}
      {family === 'central' && env.central && (
        <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm font-medium text-slate-300">Central Connection</span>
              <p className="text-xs text-slate-600">NATS JetStream source for any adapter set to "central"</p>
            </div>
            <Toggle label="" checked={!!env.central.enabled}
                    onChange={(v) => up({ central: { ...env.central!, enabled: v } })} />
          </div>
          <div className={env.central.enabled ? "space-y-3" : "space-y-3 opacity-40 pointer-events-none select-none"}>
            <TextInput label="URL" value={env.central.url || ""}
                       onChange={(v) => up({ central: { ...env.central!, url: v } })}
                       placeholder="nats://central.echo6.mesh:4222" />
            <TextInput label="Durable" value={env.central.durable || ""}
                       onChange={(v) => up({ central: { ...env.central!, durable: v } })}
                       placeholder="meshai-v04" />
            <TextInput label="Region" value={env.central.region || ""}
                       onChange={(v) => up({ central: { ...env.central!, region: v } })}
                       placeholder="us.id"
                       helper="Central v0.9.20 region token (dotted, e.g. 'us.id'). Empty = bare wildcards (all-US firehose). Each adapter is either Central or native, never both — see Reference → OR-not-AND Architecture for why." />
          </div>
        </div>
      )}


      {/* Tracking placeholder */}
      {family === 'tracking' && (
        <div className="flex flex-col items-center justify-center h-[40vh] text-center">
          <Satellite size={32} className="text-slate-600 mb-4" />
          <p className="text-slate-500 max-w-md">No adapters yet. ADS-B / AIS / satellite passes are planned for v0.5.</p>
        </div>
      )}

      {/* Mesh Health (no env adapters; central greyed for future migration) */}
      {family === 'mesh' && (
        <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm font-medium text-slate-300">Mesh Health</span>
              <p className="text-xs text-slate-600">Node/infra telemetry — sourced from the mesh, not an environmental feed.</p>
            </div>
            <div className="flex items-center gap-1">
              <span className="text-[10px] uppercase tracking-wide text-slate-600">source</span>
              <FeedSourceToggle value="native" onChange={() => {}} disabled={false} centralDisabled={true} />
            </div>
          </div>
          <div className="text-[11px] text-slate-600">Central not available — reserved for a future migration.</div>
        </div>
      )}

      {/* Adapter sub-tabs + panel */}
      {fam.adapters.length > 0 && activeAdapter && (
        <>
          {fam.adapters.length > 1 && (
            <div className="flex gap-1">
              {fam.adapters.map((k) => (
                <button key={k} onClick={() => setAdapter(k)}
                  className={`px-3 py-1.5 text-sm rounded ${activeAdapter === k ? 'bg-bg-hover text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}>
                  {META[k].label}
                </button>
              ))}
            </div>
          )}
          <AdapterPanel
            title={META[activeAdapter].label}
            subtitle={META[activeAdapter].subtitle}
            enabled={a[activeAdapter]?.enabled ?? false}
            onEnabled={(v) => setAdapterField(activeAdapter, { enabled: v })}
            feedSource={a[activeAdapter]?.feed_source ?? 'native'}
            onFeedSource={(v) => setAdapterField(activeAdapter, { feed_source: v })}
            hasCentral={META[activeAdapter].hasCentral}
            nativeOnly={META[activeAdapter].nativeOnly}
            hasKey={META[activeAdapter].hasKey}
            health={healthFor(activeAdapter)}
            events={eventsFor(activeAdapter)}
          >
            {renderSettings(activeAdapter)}
          </AdapterPanel>
        </>
      )}
    </div>
  )
}
