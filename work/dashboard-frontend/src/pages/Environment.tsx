import { useEffect, useState, type ReactNode } from 'react'
import {
 Cloud, Flame, Radio, Car, Mountain, Satellite, Activity, Server,
 Save, RotateCcw, RefreshCw, AlertCircle, AlertTriangle, Info, Bell,
 Sliders, ChevronRight,
} from 'lucide-react'
import {
 Toggle, TextInput, NumberInput, SelectInput, ListInput, NumberListInput,
 US_STATES,
} from './Config'
import {
 fetchEnvStatus, fetchEnvActive,
 type EnvStatus, type EnvEvent,
} from '@/lib/api'
import { TOGGLE_FAMILY_META, type NotificationToggle, type NotificationsConfig } from './Notifications'
import AdapterConfig, { CURATED_KEYS } from './AdapterConfig'
import { ManagedSecret } from '@/components/ManagedSecret'

type FeedSource = 'native' | 'central'

interface EnvConfig {
 enabled: boolean
 nws_zones: string[]
 nws: { enabled: boolean; user_agent: string; tick_seconds: number; severity_min: string; areas?: string[]; feed_source?: FeedSource }
 swpc: { enabled: boolean; feed_source?: FeedSource }
 ducting: { enabled: boolean; tick_seconds: number; latitude: number; longitude: number; feed_source?: FeedSource }
 fires: { enabled: boolean; tick_seconds: number; state: string; feed_source?: FeedSource }
 avalanche: { enabled: boolean; tick_seconds: number; center_ids: string[]; season_months: number[]; feed_source?: FeedSource }
 usgs: { enabled: boolean; tick_seconds: number; sites: string[]; flood_thresholds?: Record<string, { flow?: number; height?: number }>; feed_source?: FeedSource }
 usgs_quake: { enabled: boolean; tick_seconds: number; feed_url: string; min_magnitude?: number; bbox?: number[]; global_mag_floor: number; regional_mag_floor: number; regional_radius_mi: number; escalate_mag_floor: number; broadcast_pager_alerts: string[]; region: string; feed_source?: FeedSource }
 traffic: { enabled: boolean; tick_seconds: number; api_key: string; corridors: { name: string; lat: number; lon: number }[]; feed_source?: FeedSource }
 roads511: { enabled: boolean; tick_seconds: number; api_key: string; base_url: string; endpoints: string[]; bbox: number[]; feed_source?: FeedSource }
 wzdx: { enabled: boolean; tick_seconds: number; api_key: string; base_url: string; endpoints: string[]; bbox: number[]; states: string[]; registry_url: string; registry_ttl?: number; feed_source?: FeedSource }
 firms: { enabled: boolean; tick_seconds: number; map_key: string; source: string; bbox: number[]; day_range: number; confidence_min: string; proximity_km: number; feed_source?: FeedSource }
 // Native satpass (SGP4) YAML layer — drives env/satpass.py + env/tle_fetch.py.
 // Distinct from the Central adapter_config/satpass layer (see SatpassConfig
 // interface + satpassConfig state below). `observers` seeds observer_locations.
 satpass: {
  enabled: boolean
  observers: { slug: string; name: string; lat: number; lon: number; alt_m: number }[]
  tle_groups: string[]
  norad_ids: number[]
  min_elevation_deg: number
  window_hours: number
  tle_refresh_seconds: number
  broadcast_lead_seconds?: number
  feed_source?: FeedSource
 }
 central?: { enabled: boolean; url: string; durable: string; region: string; connect_timeout?: number }
 geocoder?: { url?: string; timeout_seconds?: number; radius_km?: number; limit?: number }
}

// Sane defaults for the native satpass block so a GET payload predating
// phase-4c (no `environmental.satpass`) doesn't crash the editors.
const SATPASS_NATIVE_DEFAULT: EnvConfig['satpass'] = {
 enabled: false,
 observers: [],
 tle_groups: ['weather', 'stations'],
 norad_ids: [],
 min_elevation_deg: 10,
 window_hours: 24,
 tle_refresh_seconds: 21600,
 broadcast_lead_seconds: 3600,
 feed_source: 'central',
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

// Satpass adapter config shape
interface SatpassConfig {
 enabled: boolean
 observers: string[]
 min_elevation: number
 norad_ids: string[]
 max_broadcasts_per_hour: number
 dry_run: boolean
}

// SWPC adapter config shape
interface SwpcConfig {
 geomag_kp_floor: number
 flare_class_floor: string
 proton_pfu_floor: number
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
  <div className="bg-bg-hover p-4">
   <div className="flex items-center justify-between mb-2">
    <div className="flex items-center gap-2">
     <div className={`w-2 h-2 rounded-full ${color}`} />
     <span className="text-sm font-medium text-white uppercase">{feed.source}</span>
    </div>
    <span className="text-xs text-[#777]">{text}</span>
   </div>
   <div className="text-xs font-mono text-[#666] space-y-1">
    <div>Events: {feed.event_count}</div>
    <div>Last fetch: {lastFetch}</div>
    {feed.last_error && <div className="text-accent truncate">{feed.last_error}</div>}
   </div>
  </div>
 )
}

function EventCard({ event }: { event: EnvEvent }) {
 const sev = event.severity.toLowerCase()
 const styles = (sev === 'extreme' || sev === 'severe' || sev === 'immediate')
  ? { bg: 'bg-red-500/10', border: 'border-red-500', Icon: AlertCircle, color: 'text-red-500' }
  : (sev === 'moderate' || sev === 'warning' || sev === 'priority')
  ? { bg: 'bg-accent/10', border: 'border-amber-500', Icon: AlertTriangle, color: 'text-accent' }
  : { bg: 'bg-sky-400/10', border: 'border-sky-400', Icon: Info, color: 'text-sky-400' }
 const Icon = styles.Icon
 return (
  <div className={`p-3 ${styles.bg} border-l-2 ${styles.border}`}>
   <div className="flex items-start gap-3">
    <Icon size={16} className={styles.color} />
    <div className="flex-1 min-w-0">
     <div className="flex items-center gap-2 mb-1">
      <span className="text-sm font-medium text-white">{event.event_type}</span>
      <span className={`text-xs px-1.5 py-0.5 ${styles.bg} ${styles.color}`}>{event.severity}</span>
     </div>
     <div className="text-sm font-sans text-[#e0e0e0]">{event.headline}</div>
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
  <div className={`flex border border-border overflow-hidden ${disabled ? 'opacity-40' : ''}`}>
   <button
    type="button"
    disabled={disabled}
    onClick={() => onChange('native')}
    className={`${base} ${value === 'native' ? 'bg-accent text-white' : 'text-[#777] hover:text-white'}`}
   >native</button>
   <button
    type="button"
    disabled={disabled || centralDisabled}
    title={centralDisabled ? 'Central not available for this adapter' : ''}
    onClick={() => { if (!centralDisabled) onChange('central') }}
    className={`${base} ${centralDisabled ? 'text-[#666] cursor-not-allowed' : value === 'central' ? 'bg-accent text-white' : 'text-[#777] hover:text-white'}`}
   >central</button>
  </div>
 )
}

// ---------------------------------------------------------------- adapter panel
function AdapterPanel({ title, subtitle, enabled, onEnabled, feedSource, onFeedSource, hasCentral, nativeOnly, hasKey, health, events, children, llmContext, onLlmContext }: {
 title: string; subtitle?: string
 enabled: boolean; onEnabled: (v: boolean) => void
 feedSource: FeedSource; onFeedSource: (v: FeedSource) => void
 hasCentral: boolean; nativeOnly: boolean; hasKey: boolean
 health?: FeedHealth; events?: EnvEvent[]; children?: ReactNode
 /** Current value of include_in_llm_context; undefined = not applicable */
 llmContext?: boolean
 /** Called when user toggles include_in_llm_context */
 onLlmContext?: (v: boolean) => void
}) {
 const centralDisabled = nativeOnly || !hasCentral
 return (
  <div className="border border-border p-4 space-y-3">
   <div className="flex items-center justify-between">
    <div>
     <span className="text-sm font-medium text-[#e0e0e0]">{title}</span>
     {subtitle && <p className="text-xs text-[#666]">{subtitle}</p>}
    </div>
    <div className="flex items-center gap-4">
     {onLlmContext !== undefined && (
      <label className="flex items-center gap-1.5 cursor-pointer select-none" title="Include this adapter's data in LLM (bot) context">
       <input
        type="checkbox"
        checked={llmContext ?? true}
        onChange={(e) => onLlmContext(e.target.checked)}
        className="w-3.5 h-3.5 accent-[#f59e0b]"
       />
       <span className="text-[10px] uppercase tracking-wide text-[#666]">LLM</span>
      </label>
     )}
     <div className="flex items-center gap-1">
      <span className="text-[10px] uppercase tracking-wide text-[#666]">source</span>
      <FeedSourceToggle value={feedSource} onChange={onFeedSource} disabled={!enabled} centralDisabled={centralDisabled} />
     </div>
     <Toggle label="" checked={enabled} onChange={onEnabled} />
    </div>
   </div>
   {!hasKey && (
    <div className="text-xs text-accent bg-accent/10 p-2">
     API key required — set it in the field below
    </div>
   )}
   {nativeOnly && (
    <div className="text-[11px] text-[#666]">Central not available for this adapter — native only</div>
   )}
   <div className={enabled ? 'space-y-3' : 'space-y-3 opacity-40 pointer-events-none select-none'}>
    {children}
   </div>
   {(health || (events && events.length > 0)) && (
    <div className="pt-2 border-t border-border space-y-3">
     <div className="text-[10px] uppercase tracking-wide text-[#666]">Live status</div>
     {health ? <FeedStatusCard feed={health} /> : <div className="text-xs text-[#666]">No status reported.</div>}
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
type AdapterKey = 'nws' | 'fires' | 'firms' | 'swpc' | 'ducting' | 'traffic' | 'roads511' | 'wzdx' | 'usgs_quake' | 'usgs' | 'avalanche' | 'satpass'

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
 avalanche: { label: 'Avalanche Advisories', subtitle: 'Backcountry avalanche danger ratings', health: 'avalanche', hasCentral: true, nativeOnly: false, hasKey: true },
 satpass: { label: 'Satellite Passes', subtitle: 'Observer pass alerts via Central', health: 'satpass', hasCentral: true, nativeOnly: false, hasKey: true },
}

// Keyed adapters → their secret env var (matches secrets_store.SECRET_LABELS).
// Adapters NOT listed here are keyless and never show the "API key required"
// banner. The banner for a keyed adapter is driven by the LIVE secret status
// from GET /api/secrets, not the static META.hasKey hint below.
const ADAPTER_SECRET_ENV: Partial<Record<AdapterKey, string>> = {
 firms: 'FIRMS_MAP_KEY',
 roads511: 'ROADS511_API_KEY',
 traffic: 'TOMTOM_API_KEY',
 // wzdx is keyless (FHWA registry) — no banner
}

const FAMILIES: { key: string; label: string; icon: typeof Cloud; adapters: AdapterKey[] }[] = [
 { key: 'central', label: 'Central', icon: Server, adapters: [] },
 { key: 'weather', label: 'Weather', icon: Cloud, adapters: ['nws'] },
 { key: 'fire', label: 'Fire', icon: Flame, adapters: ['fires', 'firms'] },
 { key: 'rf', label: 'RF Propagation', icon: Radio, adapters: ['swpc', 'ducting'] },
 { key: 'roads', label: 'Roads', icon: Car, adapters: ['traffic', 'roads511', 'wzdx'] },
 { key: 'geohazards', label: 'Geohazards', icon: Mountain, adapters: ['usgs_quake', 'usgs', 'avalanche'] },
 { key: 'tracking', label: 'Tracking', icon: Satellite, adapters: ['satpass'] },
 { key: 'mesh', label: 'Mesh Health', icon: Activity, adapters: [] },
 { key: 'family_settings', label: 'Family Settings', icon: Bell, adapters: [] },
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

 // Top-level tab: 'curated' = existing panels, 'advanced' = raw key/value editor
 const [pageTab, setPageTab] = useState<'curated' | 'advanced'>('curated')

 // include_in_llm_context per backend adapter name — fetched from /api/adapter-meta
 const [llmMeta, setLlmMeta] = useState<Record<string, boolean>>({})

 // Live secret set/unset status per env var — fetched from /api/secrets (same
 // source the ManagedSecret widget uses). Drives the truthful "API key required"
 // banner so it only shows when a keyed adapter's secret is genuinely unset.
 const [secretStatus, setSecretStatus] = useState<Record<string, boolean>>({})

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
 const [swpcConfig, setSwpcConfig] = useState<SwpcConfig>({
  geomag_kp_floor: 7.0,
  flare_class_floor: "X1",
  proton_pfu_floor: 10.0,
 })
 const [swpcOriginal, setSwpcOriginal] = useState<string>("")
 const [satpassConfig, setSatpassConfig] = useState<SatpassConfig>({
  enabled: false,
  observers: [],
  min_elevation: 30,
  norad_ids: [],
  max_broadcasts_per_hour: 4,
  dry_run: true,
 })
 const [satpassOriginal, setSatpassOriginal] = useState<string>("")

 // USGS flood_thresholds advanced JSON editor: raw text buffer (so invalid
 // intermediate edits stay in the textarea) + inline parse error. Only valid
 // parses are committed to env state via up().
 const [floodThreshText, setFloodThreshText] = useState<string | null>(null)
 const [floodThreshErr, setFloodThreshErr] = useState<string | null>(null)

 // Coverage config (best-effort; used to conditionally hide scope fields that
 // are driven by the universal bbox when an adapter is not excluded).
 const [coverageEnabled, setCoverageEnabled] = useState(false)
 const [coverageExcluded, setCoverageExcluded] = useState<string[]>([])

 // ── Notification family gating state ──────────────────────────────────────
 const [notifConfig, setNotifConfig] = useState<NotificationsConfig | null>(null)
 const [notifOriginal, setNotifOriginal] = useState<string>('')
 const [notifSaving, setNotifSaving] = useState(false)
 const [notifError, setNotifError] = useState<string | null>(null)
 const [notifSuccess, setNotifSuccess] = useState<string | null>(null)


 useEffect(() => {
  document.title = 'Environment — MeshAI'
  ;(async () => {
   try {
    const res = await fetch('/api/config/environmental')
    const data = await res.json()
    // Backfill native-adapter fields that older payloads may omit, so the
    // GUI editors always have a concrete shape to bind to (and so the
    // round-trip PUT restores them rather than dropping them).
    data.satpass = { ...SATPASS_NATIVE_DEFAULT, ...(data.satpass ?? {}) }
    data.wzdx = { states: ['ID'], registry_url: '', ...(data.wzdx ?? {}) }
    setEnv(data)
    setOriginal(JSON.stringify(data))

    // Helper: normalize GET /api/adapter-config/{adapter} response.
    // The API returns an ARRAY [{key, value}, ...]. Convert to {key: {value}} map
    // so callers can read data.field?.value just like an object response.
    const toMap = (arr: unknown): Record<string, { value: unknown }> => {
     const result: Record<string, { value: unknown }> = {}
     if (Array.isArray(arr)) {
      for (const r of arr as Array<{ key: string; value: unknown }>) {
       result[r.key] = { value: r.value }
      }
     }
     return result
    }

    // Load adapter-config for wfigs (array → object fix: line ~350)
    try {
     const wfigsRes = await fetch("/api/adapter-config/wfigs")
     if (wfigsRes.ok) {
      const wfigsData = toMap(await wfigsRes.json())
      const cfg: WfigsConfig = {
       allowed_incident_types: (wfigsData.allowed_incident_types?.value as string[]) ?? ['WF'],
       freshness_seconds: (wfigsData.freshness_seconds?.value as number) ?? 0,
       cooldown_seconds: (wfigsData.cooldown_seconds?.value as number) ?? 28800,
       broadcast_on_acres: (wfigsData.broadcast_on_acres?.value as boolean) ?? true,
       broadcast_on_contained: (wfigsData.broadcast_on_contained?.value as boolean) ?? true,
      }
      setWfigsConfig(cfg)
      setWfigsOriginal(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

    // Load adapter-config for fires/digest (array → object fix: line ~367)
    try {
     const firesRes = await fetch("/api/adapter-config/fires")
     if (firesRes.ok) {
      const firesData = toMap(await firesRes.json())
      const cfg: FiresConfig = {
       digest_enabled: (firesData.digest_enabled?.value as boolean) ?? true,
       digest_schedule: (firesData.digest_schedule?.value as string[]) ?? ["06:00", "18:00"],
       digest_timezone: (firesData.digest_timezone?.value as string) ?? "America/Boise",
      }
      setFiresConfig(cfg)
      setFiresOriginal(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

    // Load adapter-config for tomtom_incidents (array → object fix: line ~382)
    try {
     const ttRes = await fetch("/api/adapter-config/tomtom_incidents")
     if (ttRes.ok) {
      const ttData = toMap(await ttRes.json())
      const cfg: TomtomConfig = {
       min_magnitude: (ttData.min_magnitude?.value as number) ?? 4,
       drop_non_present: (ttData.drop_non_present?.value as boolean) ?? true,
       drop_zero_magnitude: (ttData.drop_zero_magnitude?.value as boolean) ?? true,
      }
      setTomtomConfig(cfg)
      setTomtomOriginal(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

    // Load adapter-config for itd_511 (array → object fix: line ~398)
    try {
     const r511Res = await fetch("/api/adapter-config/itd_511")
     if (r511Res.ok) {
      const r511Data = toMap(await r511Res.json())
      const cfg: Roads511Config = {
       min_severity: (r511Data.min_severity?.value as string) ?? "None",
       enabled_categories: (r511Data.enabled_categories?.value as string[]) ?? ["incident", "closure"],
       enabled_sub_types: (r511Data.enabled_sub_types?.value as string[]) ?? ["accident", "road_closed", "closure", "lane_closed", "vehicle_on_fire", "flooding", "debris"],
      }
      setRoads511Config(cfg)
      setRoads511Original(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

    // Load adapter-config for wzdx (array → object fix: line ~413)
    try {
     const wzdxRes = await fetch("/api/adapter-config/wzdx")
     if (wzdxRes.ok) {
      const wzdxData = toMap(await wzdxRes.json())
      const cfg: WzdxConfig = {
       broadcast: (wzdxData.broadcast?.value as boolean) ?? false,
       min_severity: (wzdxData.min_severity?.value as string) ?? "Minor",
       sub_types: (wzdxData.sub_types?.value as string[]) ?? ["road_works", "lane_closed", "road_closed"],
      }
      setWzdxConfig(cfg)
      setWzdxOriginal(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

    // Load adapter-config for nws (array → object fix: line ~427)
    try {
     const nwsRes = await fetch("/api/adapter-config/nws")
     if (nwsRes.ok) {
      const nwsData = toMap(await nwsRes.json())
      const cfg: NwsConfig = {
       broadcast_severities: (nwsData.broadcast_severities?.value as string[]) ?? ["Extreme", "Severe"],
       duplicate_allowed_after_seconds: (nwsData.duplicate_allowed_after_seconds?.value as number) ?? 3600,
      }
      setNwsConfig(cfg)
      setNwsOriginal(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

    // Load adapter-config for avalanche (array → object fix: line ~441)
    try {
     const avyRes = await fetch("/api/adapter-config/avalanche")
     if (avyRes.ok) {
      const avyData = toMap(await avyRes.json())
      const cfg: AvalancheConfig = {
       min_danger_level: (avyData.min_danger_level?.value as number) ?? 3,
      }
      setAvalancheConfig(cfg)
      setAvalancheOriginal(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

    // Load adapter-config for swpc (array → object fix: line ~453)
    try {
     const swpcRes = await fetch("/api/adapter-config/swpc")
     if (swpcRes.ok) {
      const swpcData = toMap(await swpcRes.json())
      const cfg: SwpcConfig = {
       geomag_kp_floor: (swpcData.geomag_kp_floor?.value as number) ?? 7.0,
       flare_class_floor: (swpcData.flare_class_floor?.value as string) ?? "X1",
       proton_pfu_floor: (swpcData.proton_pfu_floor?.value as number) ?? 10.0,
      }
      setSwpcConfig(cfg)
      setSwpcOriginal(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

    // Load adapter-meta for include_in_llm_context per adapter
    try {
     const metaRes = await fetch('/api/adapter-meta')
     if (metaRes.ok) {
      const metaData = await metaRes.json() as Record<string, { include_in_llm_context?: boolean }>
      const llmMap: Record<string, boolean> = {}
      for (const [k, v] of Object.entries(metaData)) {
       llmMap[k] = v.include_in_llm_context ?? true
      }
      setLlmMeta(llmMap)
     }
    } catch { /* best-effort */ }

    // Load adapter-config for satpass
    try {
     const satpassRes = await fetch("/api/adapter-config/satpass")
     if (satpassRes.ok) {
      const satpassArr = await satpassRes.json()
      const satpassData: Record<string, any> = {}
      for (const r of satpassArr) satpassData[r.key] = r
      const cfg: SatpassConfig = {
       enabled: satpassData.enabled?.value ?? false,
       observers: satpassData.observers?.value ?? [],
       min_elevation: satpassData.min_elevation?.value ?? 30,
       norad_ids: satpassData.norad_ids?.value ?? [],
       max_broadcasts_per_hour: satpassData.max_broadcasts_per_hour?.value ?? 4,
       dry_run: satpassData.dry_run?.value ?? true,
      }
      setSatpassConfig(cfg)
      setSatpassOriginal(JSON.stringify(cfg))
     }
    } catch { /* adapter-config optional */ }

   } catch (e) {
    setError(e instanceof Error ? e.message : 'Failed to load config')
   } finally {
    setLoading(false)
   }
  })()
 }, [])

 // Fetch notification family gating config separately (best-effort)
 useEffect(() => {
  ;(async () => {
   try {
    const res = await fetch('/api/config/notifications')
    if (res.ok) {
     const data: NotificationsConfig = await res.json()
     setNotifConfig(data)
     setNotifOriginal(JSON.stringify(data))
    }
   } catch { /* best-effort */ }
  })()
 }, [])

 // Fetch coverage config (best-effort) to conditionally hide scope fields
 // that are driven by the universal bbox when the adapter is not excluded.
 useEffect(() => {
  ;(async () => {
   try {
    const res = await fetch('/api/config/coverage')
    if (res.ok) {
     const data = await res.json()
     setCoverageEnabled(!!data.enabled)
     setCoverageExcluded(Array.isArray(data.excluded_adapters) ? data.excluded_adapters : [])
    }
   } catch { /* best-effort; degrade gracefully — scope fields always show if fetch fails */ }
  })()
 }, [])

 // Fetch live secret status once on mount (best-effort) for the key banner.
 useEffect(() => {
  ;(async () => {
   try {
    const res = await fetch('/api/secrets')
    if (res.ok) {
     const data = await res.json() as { env_var: string; is_set: boolean }[]
     const map: Record<string, boolean> = {}
     for (const s of data) map[s.env_var] = s.is_set
     setSecretStatus(map)
    }
   } catch { /* best-effort */ }
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
 const hasSwpcChanges = JSON.stringify(swpcConfig) !== swpcOriginal
 const hasSatpassChanges = JSON.stringify(satpassConfig) !== satpassOriginal
 const hasChanges = hasEnvChanges || hasWfigsChanges || hasFiresChanges || hasTomtomChanges || hasRoads511Changes || hasWzdxChanges || hasNwsChanges || hasAvalancheChanges || hasSwpcChanges || hasSatpassChanges

 
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

 // Auto-save include_in_llm_context toggle via /api/adapter-meta/{adapter}
 const saveLlmContext = async (adapterName: string, val: boolean) => {
  setLlmMeta((prev) => ({ ...prev, [adapterName]: val }))
  try {
   await fetch(`/api/adapter-meta/${adapterName}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ include_in_llm_context: val }),
   })
  } catch { /* best-effort */ }
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

   // Save swpc adapter config changes
   if (hasSwpcChanges) {
    const orig = JSON.parse(swpcOriginal) as SwpcConfig
    if (swpcConfig.geomag_kp_floor !== orig.geomag_kp_floor) {
     await saveAdapterConfig("swpc", "geomag_kp_floor", swpcConfig.geomag_kp_floor)
    }
    if (swpcConfig.flare_class_floor !== orig.flare_class_floor) {
     await saveAdapterConfig("swpc", "flare_class_floor", swpcConfig.flare_class_floor)
    }
    if (swpcConfig.proton_pfu_floor !== orig.proton_pfu_floor) {
     await saveAdapterConfig("swpc", "proton_pfu_floor", swpcConfig.proton_pfu_floor)
    }
    setSwpcOriginal(JSON.stringify(swpcConfig))
   }

   // Save satpass adapter config changes
   if (hasSatpassChanges) {
    const orig = JSON.parse(satpassOriginal) as SatpassConfig
    if (satpassConfig.enabled !== orig.enabled) {
     await saveAdapterConfig("satpass", "enabled", satpassConfig.enabled)
    }
    if (JSON.stringify(satpassConfig.observers) !== JSON.stringify(orig.observers)) {
     await saveAdapterConfig("satpass", "observers", satpassConfig.observers)
    }
    if (satpassConfig.min_elevation !== orig.min_elevation) {
     await saveAdapterConfig("satpass", "min_elevation", satpassConfig.min_elevation)
    }
    if (JSON.stringify(satpassConfig.norad_ids) !== JSON.stringify(orig.norad_ids)) {
     await saveAdapterConfig("satpass", "norad_ids", satpassConfig.norad_ids)
    }
    if (satpassConfig.max_broadcasts_per_hour !== orig.max_broadcasts_per_hour) {
     await saveAdapterConfig("satpass", "max_broadcasts_per_hour", satpassConfig.max_broadcasts_per_hour)
    }
    if (satpassConfig.dry_run !== orig.dry_run) {
     await saveAdapterConfig("satpass", "dry_run", satpassConfig.dry_run)
    }
    setSatpassOriginal(JSON.stringify(satpassConfig))
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
  setSwpcConfig(JSON.parse(swpcOriginal || JSON.stringify(swpcConfig)))
  setSatpassConfig(JSON.parse(satpassOriginal || JSON.stringify(satpassConfig)))
  setFloodThreshText(null); setFloodThreshErr(null)
 }
 const restart = async () => {
  try { await fetch('/api/restart', { method: 'POST' }); setRestartRequired(false); setSuccess('Restart initiated') }
  catch { setError('Restart failed') }
 }

 const up = (patch: Partial<EnvConfig>) => env && setEnv({ ...env, ...patch })

 // Returns true when the coverage bbox is active for this adapter (enabled and
 // not in excluded_adapters). Scope fields should be hidden in this case.
 const scopedByCoverage = (key: string) =>
  coverageEnabled && !coverageExcluded.includes(key)

 // Maps from Environment.tsx adapter key → backend adapter-meta name.
 // Used to read/write include_in_llm_context per adapter panel.
 const PANEL_META_KEY: Partial<Record<AdapterKey, string>> = {
  nws: 'nws',
  fires: 'wfigs',
  firms: 'firms',
  swpc: 'swpc',
  ducting: 'ducting',
  traffic: 'tomtom_incidents',
  roads511: 'itd_511',
  wzdx: 'wzdx',
  usgs: 'usgs',
  usgs_quake: 'usgs_quake',
  avalanche: 'avalanche',
  satpass: 'satpass',
 }

 // ── Notification family gating helpers ────────────────────────────────────
 const notifToggles: Record<string, NotificationToggle> = notifConfig?.toggles || {}
 const notifHasChanges = notifConfig !== null && JSON.stringify(notifConfig) !== notifOriginal

 const updNotif = (fam: string, patch: Partial<NotificationToggle>) => {
  if (!notifConfig) return
  const t = notifConfig.toggles || {}
  setNotifConfig({
   ...notifConfig,
   toggles: {
    ...t,
    [fam]: { ...(t[fam] || {}), name: fam, ...patch } as NotificationToggle,
   },
  })
 }

 const saveNotif = async () => {
  if (!notifConfig) return
  setNotifSaving(true)
  setNotifError(null)
  setNotifSuccess(null)
  try {
   // Re-fetch and merge only gating fields, preserving all delivery fields.
   const freshRes = await fetch('/api/config/notifications')
   if (!freshRes.ok) throw new Error('Failed to re-fetch notifications config')
   const fresh: NotificationsConfig = await freshRes.json()
   const merged: NotificationsConfig = { ...fresh, toggles: { ...(fresh.toggles || {}) } }
   const myToggles = notifConfig.toggles || {}
   for (const { key } of TOGGLE_FAMILY_META) {
    const mine = myToggles[key]
    if (!mine) continue
    const freshT = (fresh.toggles || {})[key] || {}
    merged.toggles![key] = {
     ...freshT,
     name: (freshT as NotificationToggle).name || key,
     enabled: mine.enabled,
     min_severity: mine.min_severity,
     freshness_seconds: mine.freshness_seconds ?? (freshT as NotificationToggle).freshness_seconds ?? 600,
     cooldown_seconds: mine.cooldown_seconds ?? (freshT as NotificationToggle).cooldown_seconds ?? 0,
     regions: mine.regions ?? (freshT as NotificationToggle).regions ?? [],
    } as NotificationToggle
   }
   const res = await fetch('/api/config/notifications', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(merged),
   })
   const result = await res.json()
   if (!res.ok) throw new Error(result.detail || 'Save failed')
   setNotifConfig(merged)
   setNotifOriginal(JSON.stringify(merged))
   setNotifSuccess('Family settings saved')
   setTimeout(() => setNotifSuccess(null), 3000)
  } catch (e) {
   setNotifError(e instanceof Error ? e.message : 'Save failed')
  } finally {
   setNotifSaving(false)
  }
 }

 const discardNotif = () => {
  if (notifOriginal) setNotifConfig(JSON.parse(notifOriginal))
 }

 if (loading) return <div className="flex items-center justify-center h-64 text-[#777]">Loading environmental config…</div>
 if (!env) return <div className="flex items-center justify-center h-64 text-red-400">{error || 'No config'}</div>

 const healthFor = (key: AdapterKey): FeedHealth | undefined =>
  status?.feeds.find((f) => f.source === META[key].health)
 const eventsFor = (key: AdapterKey): EnvEvent[] =>
  events.filter((e) => e.source === META[key].health)

 // Truthful key-banner logic: keyless adapters never show the banner; keyed
 // adapters show it ONLY when their real secret (GET /api/secrets) is unset.
 // While secret status is still loading (undefined), default to true so we
 // never flash a false "API key required" banner for an already-set key.
 const effectiveHasKey = (key: AdapterKey): boolean => {
  const envVar = ADAPTER_SECRET_ENV[key]
  if (!envVar) return true // keyless adapter — no key needed
  const isSet = secretStatus[envVar]
  return isSet === undefined ? true : isSet
 }

 const fam = FAMILIES.find((f) => f.key === family)!
 const activeAdapter: AdapterKey | null =
  fam.adapters.length === 0 ? null : (adapter && fam.adapters.includes(adapter) ? adapter : fam.adapters[0])

 // -- per-adapter settings forms (preserve all existing settings) --
 const renderSettings = (key: AdapterKey) => {
  switch (key) {
   case 'nws': return (<>
    {scopedByCoverage('nws') ? (
     <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
      Geographic scope (zones, areas) is set by the{' '}
      <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
      Enable "Use own config" for NWS on that page to edit zones here.
     </div>
    ) : (<>
     <ListInput label="NWS Zones" value={env.nws_zones} onChange={(v) => up({ nws_zones: v })} helper="Zone IDs like IDZ016, IDZ030" infoLink="https://www.weather.gov/pimar/PubZone" />
     <ListInput label="NWS Areas" value={env.nws.areas ?? []} onChange={(v) => up({ nws: { ...env.nws, areas: v } })} helper="State codes NWS pulls, e.g. ID" />
    </>)}
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
     <div className="border-t border-border pt-4 mt-4">
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">Broadcast Filters</div>
      <div className="mb-3">
       <div className="text-xs font-sans text-[#777] mb-2">Severities to broadcast</div>
       <div className="flex gap-6">
        {['Extreme', 'Severe', 'Moderate', 'Minor'].map((sev) => (
         <label key={sev} className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={nwsConfig.broadcast_severities.includes(sev)}
           onChange={(e) => {
            const cur = nwsConfig.broadcast_severities
            setNwsConfig({ ...nwsConfig, broadcast_severities: e.target.checked ? [...cur, sev] : cur.filter(s => s !== sev) })
           }}
           className="w-4 h-4 accent-[#f59e0b]" />
          <span className="text-sm font-sans text-[#e0e0e0]">{sev}</span>
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
   case 'swpc': return (
    <div className="space-y-6">
     <div>
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">
       Broadcast Thresholds
      </div>
      <div className="grid grid-cols-3 gap-4">
       <SelectInput label="Geomag Kp Floor" value={String(swpcConfig.geomag_kp_floor)}
        onChange={(v) => setSwpcConfig({ ...swpcConfig, geomag_kp_floor: Number(v) })}
        options={[
         { value: "5", label: "5 — G1 Minor" },
         { value: "6", label: "6 — G2 Moderate" },
         { value: "7", label: "7 — G3 Strong" },
         { value: "8", label: "8 — G4 Severe" },
         { value: "9", label: "9 — G5 Extreme" },
        ]}
        helper="Kp at or above this triggers geomag broadcast" />
       <SelectInput label="Flare Class Floor" value={swpcConfig.flare_class_floor}
        onChange={(v) => setSwpcConfig({ ...swpcConfig, flare_class_floor: v })}
        options={[
         { value: "M1", label: "M1 — R1 Minor" },
         { value: "M5", label: "M5 — R2 Moderate" },
         { value: "X1", label: "X1 — R3 Strong" },
         { value: "X10", label: "X10 — R4 Severe" },
        ]}
        helper="X-ray flare class floor for broadcast" />
       <SelectInput label="Proton pfu Floor" value={String(swpcConfig.proton_pfu_floor)}
        onChange={(v) => setSwpcConfig({ ...swpcConfig, proton_pfu_floor: Number(v) })}
        options={[
         { value: "10", label: "10 — S1 Minor" },
         { value: "100", label: "100 — S2 Moderate" },
         { value: "1000", label: "1000 — S3 Strong" },
         { value: "10000", label: "10000 — S4 Severe" },
        ]}
        helper="Proton flux (pfu) at ≥10 MeV for broadcast" />
      </div>
     </div>
    </div>
   )
   case 'ducting': return (
    <div className="space-y-3">
     <NumberInput label="Tick Seconds" value={env.ducting.tick_seconds} onChange={(v) => up({ ducting: { ...env.ducting, tick_seconds: v } })} min={60} />
     {scopedByCoverage('ducting') ? (
      <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
       Lat/lon is set by the{' '}
       <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
      </div>
     ) : (
      <div className="grid grid-cols-2 gap-4">
       <NumberInput label="Latitude" value={env.ducting.latitude} onChange={(v) => up({ ducting: { ...env.ducting, latitude: v } })} step={0.01} />
       <NumberInput label="Longitude" value={env.ducting.longitude} onChange={(v) => up({ ducting: { ...env.ducting, longitude: v } })} step={0.01} />
      </div>
     )}
    </div>)
   case 'fires': return (
    <div className="space-y-6">
     {env.fires.feed_source !== 'central' && (
      <div className="grid grid-cols-2 gap-4">
       <NumberInput label="Tick Seconds" value={env.fires.tick_seconds} onChange={(v) => up({ fires: { ...env.fires, tick_seconds: v } })} min={60} />
       {scopedByCoverage('fires') ? (
        <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50 self-end">
         State scoped by{' '}
         <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
        </div>
       ) : (
        <SelectInput label="State" value={env.fires.state} onChange={(v) => up({ fires: { ...env.fires, state: v } })} options={US_STATES} />
       )}
      </div>
     )}
     <div>
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">Incident Types</div>
      <div className="flex gap-6">
       {[['WF', 'Wildfire'], ['RX', 'Prescribed Burn'], ['OTHER', 'Other']].map(([val, label]) => (
        <label key={val} className="flex items-center gap-2 cursor-pointer">
         <input type="checkbox" checked={wfigsConfig.allowed_incident_types?.includes(val) ?? val === 'WF'}
          onChange={(e) => { const cur = wfigsConfig.allowed_incident_types ?? ['WF']; setWfigsConfig({ ...wfigsConfig, allowed_incident_types: e.target.checked ? [...cur, val] : cur.filter(t => t !== val) }) }}
          className="w-4 h-4 accent-[#f59e0b]" />
         <span className="text-sm font-sans text-[#e0e0e0]">{label}</span>
        </label>
       ))}
      </div>
     </div>
     <div>
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">Broadcast Triggers</div>
      <div className="space-y-2">
       <label className="flex items-center justify-between">
        <span className="text-sm font-sans text-[#e0e0e0]">Broadcast on acres increase</span>
        <input type="checkbox" checked={wfigsConfig.broadcast_on_acres} onChange={(e) => setWfigsConfig({ ...wfigsConfig, broadcast_on_acres: e.target.checked })} className="w-4 h-4 accent-[#f59e0b]" />
       </label>
       <label className="flex items-center justify-between">
        <span className="text-sm font-sans text-[#e0e0e0]">Broadcast on containment increase</span>
        <input type="checkbox" checked={wfigsConfig.broadcast_on_contained} onChange={(e) => setWfigsConfig({ ...wfigsConfig, broadcast_on_contained: e.target.checked })} className="w-4 h-4 accent-[#f59e0b]" />
       </label>
      </div>
     </div>
     <div className="grid grid-cols-2 gap-4">
      <NumberInput label="Update Cooldown (hours)" value={Math.round(wfigsConfig.cooldown_seconds / 3600)} onChange={(v) => setWfigsConfig({ ...wfigsConfig, cooldown_seconds: v * 3600 })} min={0} helper="Minimum hours between updates for the same fire" />
      <NumberInput label="Freshness Window (hours)" value={Math.round(wfigsConfig.freshness_seconds / 3600)} onChange={(v) => setWfigsConfig({ ...wfigsConfig, freshness_seconds: v * 3600 })} min={0} helper="0 = always broadcast regardless of event age" />
     </div>
     <div className="border-t border-border pt-4 mt-2">
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">Fire Digest</div>
      <label className="flex items-center justify-between">
       <span className="text-sm font-sans text-[#e0e0e0]">Enable daily digest</span>
       <input type="checkbox" checked={firesConfig.digest_enabled}
        onChange={(e) => setFiresConfig({ ...firesConfig, digest_enabled: e.target.checked })}
        className="w-4 h-4 accent-[#f59e0b]" />
      </label>
      {firesConfig.digest_enabled && (
       <div className="mt-3 space-y-3">
        <ListInput label="Schedule (HH:MM)" value={firesConfig.digest_schedule}
         onChange={(v) => setFiresConfig({ ...firesConfig, digest_schedule: v })}
         helper="Digest times in HH:MM format, e.g. 06:00 and 18:00" />
        <SelectInput label="Timezone" value={firesConfig.digest_timezone}
         onChange={(v) => setFiresConfig({ ...firesConfig, digest_timezone: v })}
         options={[
          { value: 'America/Boise', label: 'Mountain — America/Boise' },
          { value: 'America/Los_Angeles', label: 'Pacific — America/Los_Angeles' },
          { value: 'America/Denver', label: 'Mountain — America/Denver' },
          { value: 'America/Chicago', label: 'Central — America/Chicago' },
          { value: 'America/New_York', label: 'Eastern — America/New_York' },
          { value: 'UTC', label: 'UTC' },
         ]} />
       </div>
      )}
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
     {scopedByCoverage('avalanche') ? (
      <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
       Avalanche center IDs are set by the{' '}
       <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
      </div>
     ) : (
      <ListInput label="Center IDs" value={env.avalanche.center_ids}
       onChange={(v) => up({ avalanche: { ...env.avalanche, center_ids: v } })}
       helper="e.g., SNFAC" infoLink="https://avalanche.org/avalanche-centers/" />
     )}
     <div>
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">
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
    {scopedByCoverage('usgs') ? (
     <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
      Gauge site IDs are set by the{' '}
      <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
     </div>
    ) : (
     <ListInput label="Site IDs" value={env.usgs.sites} onChange={(v) => up({ usgs: { ...env.usgs, sites: v } })} helper="USGS gauge site numbers" infoLink="https://waterdata.usgs.gov/nwis" />
    )}
    <div>
     <label className="text-xs font-sans text-[#777] mb-1 block">Flood Thresholds (advanced JSON)</label>
     <textarea
      value={floodThreshText ?? JSON.stringify(env.usgs.flood_thresholds ?? {}, null, 2)}
      onChange={(e) => {
       const raw = e.target.value
       setFloodThreshText(raw)
       try {
        const parsed = JSON.parse(raw)
        setFloodThreshErr(null)
        up({ usgs: { ...env.usgs, flood_thresholds: parsed } })
       } catch (err) {
        setFloodThreshErr(err instanceof Error ? err.message : 'Invalid JSON')
       }
      }}
      rows={6}
      spellCheck={false}
      className="w-full bg-[#0d0d0d] border border-border px-3 py-2 text-sm font-mono"
     />
     {floodThreshErr && <p className="text-xs text-red-400 mt-1">Invalid JSON — not saved: {floodThreshErr}</p>}
     <p className="text-xs text-[#666] mt-1">Per-site flood levels, shape {'{'}"site_id": {'{'} "flow": X, "height": Y {'}'}{'}'}</p>
    </div>
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
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">
       Native Feed
      </div>
      <div className="space-y-3">
       <TextInput label="Quake Feed URL" value={env.usgs_quake.feed_url}
        onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, feed_url: v } })} />
       <div className="grid grid-cols-2 gap-4">
        <NumberInput label="Min Magnitude" value={env.usgs_quake.min_magnitude ?? 2.5}
         onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, min_magnitude: v } })}
         step={0.1} min={0} helper="Native quake magnitude floor" />
       </div>
       {scopedByCoverage('usgs_quake') ? (
        <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
         Bounding box is set by the{' '}
         <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
        </div>
       ) : (
        <NumberListInput label="Bounding Box [W, S, E, N]" value={env.usgs_quake.bbox ?? []}
         onChange={(v) => up({ usgs_quake: { ...env.usgs_quake, bbox: v } })}
         helper="Four values: west, south, east, north" />
       )}
      </div>
     </div>
     <div>
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">
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
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">
       PAGER Alert Levels
      </div>
      <div className="text-xs text-[#666] mb-2">
       Broadcast at any magnitude when USGS PAGER alert reaches these levels
      </div>
      <div className="flex gap-6">
       {(['green','yellow','orange','red'] as const).map((level) => (
        <label key={level} className="flex items-center gap-2 cursor-pointer">
         <input type="checkbox"
          checked={(env.usgs_quake.broadcast_pager_alerts ?? []).includes(level)}
          onChange={(e) => {
           const cur = env.usgs_quake.broadcast_pager_alerts ?? []
           up({ usgs_quake: { ...env.usgs_quake,
            broadcast_pager_alerts: e.target.checked
             ? [...cur, level]
             : cur.filter((l) => l !== level) }})
          }}
          className="w-4 h-4 accent-[#f59e0b]" />
         <span className="text-sm font-sans text-[#e0e0e0] capitalize">{level}</span>
        </label>
       ))}
      </div>
     </div>
    </div>
   )
   case 'traffic': return (<>
    <ManagedSecret envVar="TOMTOM_API_KEY" label="API Key" helper="developer.tomtom.com" />
    <NumberInput label="Tick Seconds" value={env.traffic.tick_seconds} onChange={(v) => up({ traffic: { ...env.traffic, tick_seconds: v } })} min={60} />
    {scopedByCoverage('traffic') ? (
     <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
      Traffic corridors are set by the{' '}
      <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
     </div>
    ) : (<>
     <div className="text-xs text-[#666] mt-2">Corridors:</div>
     {(env.traffic.corridors || []).map((c, i) => (
      <div key={i} className="grid grid-cols-4 gap-2 items-end">
       <TextInput label="Name" value={c.name} onChange={(v) => { const n = [...env.traffic.corridors]; n[i] = { ...c, name: v }; up({ traffic: { ...env.traffic, corridors: n } }) }} />
       <NumberInput label="Lat" value={c.lat} onChange={(v) => { const n = [...env.traffic.corridors]; n[i] = { ...c, lat: v }; up({ traffic: { ...env.traffic, corridors: n } }) }} step={0.01} />
       <NumberInput label="Lon" value={c.lon} onChange={(v) => { const n = [...env.traffic.corridors]; n[i] = { ...c, lon: v }; up({ traffic: { ...env.traffic, corridors: n } }) }} step={0.01} />
       <button onClick={() => up({ traffic: { ...env.traffic, corridors: env.traffic.corridors.filter((_, j) => j !== i) } })} className="px-2 py-2 text-xs text-red-400 hover:text-red-300 border border-red-400/30">Remove</button>
      </div>
     ))}
     <button onClick={() => up({ traffic: { ...env.traffic, corridors: [...(env.traffic.corridors || []), { name: '', lat: 0, lon: 0 }] } })} className="text-xs text-accent hover:underline">+ Add Corridor</button>
    </>)}
    <div className="border-t border-border pt-4 mt-4">
     <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">Broadcast Filters</div>
     <div className="grid grid-cols-2 gap-4">
      <div>
       <label className="text-xs font-sans text-[#777] mb-1 block">Minimum Magnitude</label>
       <select
        value={tomtomConfig.min_magnitude}
        onChange={(e) => setTomtomConfig({...tomtomConfig, min_magnitude: parseInt(e.target.value)})}
        className="w-full bg-[#0d0d0d] border border-border px-3 py-2 text-sm"
       >
        <option value={1}>1 — Minor (all)</option>
        <option value={2}>2 — Moderate (yellow+)</option>
        <option value={3}>3 — Major (orange+)</option>
        <option value={4}>4 — Severe (red only)</option>
       </select>
       <p className="text-xs text-[#666] mt-1">Drop TomTom incidents below this severity level</p>
      </div>
     </div>
     <div className="mt-3 space-y-2">
      <label className="flex items-center justify-between">
       <span className="text-sm font-sans text-[#e0e0e0]">Drop non-present time validity</span>
       <input type="checkbox" checked={tomtomConfig.drop_non_present} onChange={(e) => setTomtomConfig({...tomtomConfig, drop_non_present: e.target.checked})} className="w-4 h-4 accent-[#f59e0b]" />
      </label>
      <label className="flex items-center justify-between">
       <span className="text-sm font-sans text-[#e0e0e0]">Drop zero-magnitude events</span>
       <input type="checkbox" checked={tomtomConfig.drop_zero_magnitude} onChange={(e) => setTomtomConfig({...tomtomConfig, drop_zero_magnitude: e.target.checked})} className="w-4 h-4 accent-[#f59e0b]" />
      </label>
     </div>
    </div>
   </>)
   case 'roads511': return (<>
    <TextInput label="Base URL" value={env.roads511.base_url} onChange={(v) => up({ roads511: { ...env.roads511, base_url: v } })} placeholder="https://511.yourstate.gov/api/v2" />
    <ManagedSecret envVar="ROADS511_API_KEY" label="API Key" helper="Leave unset if 511 needs no key" />
    <NumberInput label="Tick Seconds" value={env.roads511.tick_seconds} onChange={(v) => up({ roads511: { ...env.roads511, tick_seconds: v } })} min={60} />
    <ListInput label="Endpoints" value={env.roads511.endpoints} onChange={(v) => up({ roads511: { ...env.roads511, endpoints: v } })} helper="e.g., /get/event" />
    {scopedByCoverage('roads511') ? (
     <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
      Bounding box is set by the{' '}
      <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
     </div>
    ) : (
     <div className="grid grid-cols-4 gap-2">
      {(['West', 'South', 'East', 'North'] as const).map((lbl, i) => (
       <NumberInput key={lbl} label={lbl} value={env.roads511.bbox?.[i] ?? 0} onChange={(v) => { const b = [...(env.roads511.bbox || [0, 0, 0, 0])]; b[i] = v; up({ roads511: { ...env.roads511, bbox: b } }) }} step={0.01} />
      ))}
     </div>
    )}
    <div className="border-t border-border pt-4 mt-4">
     <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">Broadcast Filters</div>
     <div className="grid grid-cols-2 gap-4">
      <div>
       <label className="text-xs font-sans text-[#777] mb-1 block">Minimum Severity</label>
       <select
        value={roads511Config.min_severity}
        onChange={(e) => setRoads511Config({...roads511Config, min_severity: e.target.value})}
        className="w-full bg-[#0d0d0d] border border-border px-3 py-2 text-sm"
       >
        <option value="None">None (all)</option>
        <option value="Minor">Minor+</option>
        <option value="Major">Major only</option>
       </select>
       <p className="text-xs text-[#666] mt-1">Drop ITD 511 events below this severity</p>
      </div>
     </div>
     <div className="mt-4">
      <div className="text-xs font-sans text-[#777] mb-2">Categories</div>
      <div className="flex gap-6">
       {([['incident', 'Incident'], ['closure', 'Closure'], ['special_event', 'Special Event']] as const).map(([val, label]) => (
        <label key={val} className="flex items-center gap-2 cursor-pointer">
         <input type="checkbox" checked={roads511Config.enabled_categories.includes(val)}
          onChange={(e) => { const cur = roads511Config.enabled_categories; setRoads511Config({...roads511Config, enabled_categories: e.target.checked ? [...cur, val] : cur.filter(c => c !== val)}) }}
          className="w-4 h-4 accent-[#f59e0b]" />
         <span className="text-sm font-sans text-[#e0e0e0]">{label}</span>
        </label>
       ))}
      </div>
     </div>
     <div className="mt-4">
      <div className="text-xs font-sans text-[#777] mb-2">Sub-types</div>
      <div className="grid grid-cols-2 gap-2">
       {([['accident', 'Crash'], ['road_closed', 'Road Closed'], ['lane_closed', 'Lane Closure'], ['vehicle_on_fire', 'Vehicle Fire'], ['flooding', 'Flooding'], ['debris', 'Debris'], ['road_works', 'Road Works'], ['disabled_vehicle', 'Disabled Vehicle']] as const).map(([val, label]) => (
        <label key={val} className="flex items-center gap-2 cursor-pointer">
         <input type="checkbox" checked={roads511Config.enabled_sub_types.includes(val)}
          onChange={(e) => { const cur = roads511Config.enabled_sub_types; setRoads511Config({...roads511Config, enabled_sub_types: e.target.checked ? [...cur, val] : cur.filter(s => s !== val)}) }}
          className="w-4 h-4 accent-[#f59e0b]" />
         <span className="text-sm font-sans text-[#e0e0e0]">{label}</span>
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
      <ManagedSecret envVar="WZDX_API_KEY" label="API Key" helper="Leave unset if not required" />
      <NumberInput label="Tick Seconds" value={env.wzdx?.tick_seconds ?? 300} onChange={(v) => up({ wzdx: { ...env.wzdx!, tick_seconds: v } })} min={60} />
      <ListInput label="Endpoints" value={env.wzdx?.endpoints ?? ['/get/event']} onChange={(v) => up({ wzdx: { ...env.wzdx!, endpoints: v } })} helper="e.g., /get/event" />
      {scopedByCoverage('wzdx') ? (
       <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
        Bounding box and states are set by the{' '}
        <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
       </div>
      ) : (<>
       <div className="grid grid-cols-4 gap-2">
        {(['West', 'South', 'East', 'North'] as const).map((lbl, i) => (
         <NumberInput key={lbl} label={lbl} value={env.wzdx?.bbox?.[i] ?? 0} onChange={(v) => { const b = [...(env.wzdx?.bbox || [0, 0, 0, 0])]; b[i] = v; up({ wzdx: { ...env.wzdx!, bbox: b } }) }} step={0.01} />
        ))}
       </div>
       <div className="text-xs text-[#666]">Bounding box [W,S,E,N] geographic filter</div>
       <ListInput label="States" value={env.wzdx?.states ?? []} onChange={(v) => up({ wzdx: { ...env.wzdx!, states: v } })}
        helper="2-letter state codes to include from the WZDx Feed Registry, e.g. ID, OR" />
      </>)}
      <TextInput label="Registry URL" value={env.wzdx?.registry_url ?? ''} onChange={(v) => up({ wzdx: { ...env.wzdx!, registry_url: v } })}
       placeholder="https://datahub.transportation.gov/resource/69qe-yiui.json?$limit=200"
       helper="FHWA WZDx Feed Registry (Socrata) URL — lists every state DOT feed" />
      <NumberInput label="Registry TTL (sec)" value={env.wzdx?.registry_ttl ?? 21600} onChange={(v) => up({ wzdx: { ...env.wzdx!, registry_ttl: v } })}
       min={0} helper="How often to re-fetch the WZDx registry (default 21600 = 6h)" />
     </>
    )}
    <div className="border-t border-border pt-4 mt-4">
     <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">Broadcast Settings</div>
     <label className="flex items-center justify-between">
      <span className="text-sm font-sans text-[#e0e0e0]">Broadcast work zone events</span>
      <input type="checkbox" checked={wzdxConfig.broadcast}
       onChange={(e) => setWzdxConfig({...wzdxConfig, broadcast: e.target.checked})}
       className="w-4 h-4 accent-[#f59e0b]" />
     </label>
     {wzdxConfig.broadcast ? (
      <div className="space-y-3 mt-3">
       <div>
        <label className="text-xs font-sans text-[#777] mb-1 block">Min Severity</label>
        <select
         value={wzdxConfig.min_severity}
         onChange={(e) => setWzdxConfig({...wzdxConfig, min_severity: e.target.value})}
         className="w-full bg-[#0d0d0d] border border-border px-3 py-2 text-sm"
        >
         <option value="None">None (all)</option>
         <option value="Minor">Minor+</option>
         <option value="Major">Major only</option>
        </select>
       </div>
       <div>
        <div className="text-xs font-sans text-[#777] mb-2">Sub-types</div>
        <div className="flex gap-6">
         {([['road_works', 'Road Works'], ['lane_closed', 'Lane Closure'], ['road_closed', 'Road Closed']] as const).map(([val, label]) => (
          <label key={val} className="flex items-center gap-2 cursor-pointer">
           <input type="checkbox" checked={wzdxConfig.sub_types.includes(val)}
            onChange={(e) => { const cur = wzdxConfig.sub_types; setWzdxConfig({...wzdxConfig, sub_types: e.target.checked ? [...cur, val] : cur.filter(s => s !== val)}) }}
            className="w-4 h-4 accent-[#f59e0b]" />
           <span className="text-sm font-sans text-[#e0e0e0]">{label}</span>
          </label>
         ))}
        </div>
       </div>
      </div>
     ) : (
      <p className="text-xs text-[#666] mt-2">Work zone events stored for LLM context only {'\u2014'} no mesh broadcasts.</p>
     )}
    </div>
   </>)
   case 'firms': return (<>
    <ManagedSecret envVar="FIRMS_MAP_KEY" label="MAP Key" helper="NASA FIRMS MAP_KEY" />
    <NumberInput label="Tick Seconds" value={env.firms.tick_seconds} onChange={(v) => up({ firms: { ...env.firms, tick_seconds: v } })} min={300} />
    <SelectInput label="Satellite Source" value={env.firms.source} onChange={(v) => up({ firms: { ...env.firms, source: v } })} options={[{ value: 'VIIRS_SNPP_NRT', label: 'VIIRS SNPP (NRT)' }, { value: 'VIIRS_NOAA20_NRT', label: 'VIIRS NOAA-20 (NRT)' }, { value: 'MODIS_NRT', label: 'MODIS (NRT)' }]} />
    <div className="grid grid-cols-3 gap-4">
     <NumberInput label="Day Range" value={env.firms.day_range} onChange={(v) => up({ firms: { ...env.firms, day_range: v } })} min={1} max={10} />
     <SelectInput label="Min Confidence" value={env.firms.confidence_min} onChange={(v) => up({ firms: { ...env.firms, confidence_min: v } })} options={[{ value: 'low', label: 'Low' }, { value: 'nominal', label: 'Nominal' }, { value: 'high', label: 'High' }]} />
     <NumberInput label="Proximity (km)" value={env.firms.proximity_km} onChange={(v) => up({ firms: { ...env.firms, proximity_km: v } })} step={0.5} />
    </div>
    {scopedByCoverage('firms') ? (
     <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
      Bounding box is set by the{' '}
      <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
     </div>
    ) : (
     <div className="grid grid-cols-4 gap-2">
      {(['West', 'South', 'East', 'North'] as const).map((lbl, i) => (
       <NumberInput key={lbl} label={lbl} value={env.firms.bbox?.[i] ?? 0} onChange={(v) => { const b = [...(env.firms.bbox || [0, 0, 0, 0])]; b[i] = v; up({ firms: { ...env.firms, bbox: b } }) }} step={0.01} />
      ))}
     </div>
    )}
   </>)
   case 'satpass': {
    // Armed state keys off the NATIVE enable (environmental.satpass.enabled),
    // which is what actually gates the native SGP4 broadcaster — consistent
    // with the enable-toggle bug fix. dry_run is a Central adapter_config field
    // (no native equivalent), so it still comes from satpassConfig.
    const armedState = !env.satpass.enabled
     ? { label: 'OFF', color: 'text-[#777] bg-[#1a1a1a]', desc: '' }
     : satpassConfig.dry_run
      ? { label: 'DRY RUN', color: 'text-sky-400 bg-sky-400/10 border border-sky-400/30', desc: ' — logging only, nothing transmits' }
      : { label: '⚠ LIVE', color: 'text-amber-400 bg-amber-500/20 border-2 border-amber-500 font-bold animate-pulse', desc: ' — transmitting to mesh' }
    return (
    <div className="space-y-6">
     {/* Armed-state banner */}
     <div className={`px-4 py-2.5 text-sm rounded ${armedState.color}`}>
      <span className="font-semibold">{armedState.label}</span>
      {armedState.desc && <span className="font-normal">{armedState.desc}</span>}
     </div>
     {/* Safety controls */}
     <div>
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">
       Safety Controls
      </div>
      <div className="space-y-4">
       <div className="flex items-center justify-between">
        <div>
         <span className="text-sm text-[#e0e0e0]">Dry run — log instead of transmit</span>
         <p className="text-xs text-[#666]">When enabled, passes are logged but never broadcast to mesh</p>
        </div>
        <button
         onClick={() => setSatpassConfig({ ...satpassConfig, dry_run: !satpassConfig.dry_run })}
         className={`relative w-10 h-5 rounded-full transition-colors ${satpassConfig.dry_run ? 'bg-sky-500' : 'bg-[#333]'}`}
        >
         <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${satpassConfig.dry_run ? 'translate-x-5' : ''}`} />
        </button>
       </div>
       <NumberInput label="Max broadcasts / hour" value={satpassConfig.max_broadcasts_per_hour}
        onChange={(v) => setSatpassConfig({ ...satpassConfig, max_broadcasts_per_hour: v })}
        min={1} max={60} helper="Rate cap — broadcasts exceeding this limit are dropped" />
      </div>
     </div>
     {/* Pass filters */}
     <div>
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666] mb-3">
       Pass Filters
      </div>
      <div className="grid grid-cols-2 gap-4">
       <NumberInput label="Min Elevation (deg)" value={satpassConfig.min_elevation}
        onChange={(v) => setSatpassConfig({ ...satpassConfig, min_elevation: v })}
        min={0} max={90} helper="Minimum max elevation for a pass to be broadcast" />
      </div>
     </div>
     <ListInput label="Observer Locations" value={satpassConfig.observers}
      onChange={(v) => setSatpassConfig({ ...satpassConfig, observers: v })}
      helper="Observer names to include (empty = all)" />
     <ListInput label="NORAD IDs" value={satpassConfig.norad_ids}
      onChange={(v) => setSatpassConfig({ ...satpassConfig, norad_ids: v })}
      helper="NORAD catalog IDs to broadcast (empty = broadcast nothing, opt-in only)" />

     {/* ── Native satpass (SGP4) ─────────────────────────────────────────
         Fields on the environmental.satpass YAML layer that drive the local
         TLE fetcher (env/tle_fetch.py) + SGP4 predictor (env/satpass.py).
         Only used when this adapter's source = native. `Observers` seeds the
         observer_locations table (persistence/observer_locations.py). */}
     <div className="border-t border-border pt-4 mt-2 space-y-6">
      <div className="text-[10px] font-sans font-medium uppercase tracking-widest text-[#666]">
       Native satpass (SGP4) — no Central required
      </div>

      {/* Observers — list of {slug, name, lat, lon, alt_m}; seeds observer_locations */}
      <div>
       <div className="text-xs text-[#777] mb-2">Observers (ground stations the predictor computes passes for)</div>
       {scopedByCoverage('satpass') ? (
        <div className="text-xs text-[#666] bg-bg-hover px-3 py-2 border border-border/50">
         Observer locations are set by the{' '}
         <a href="/coverage" className="text-accent hover:underline">Coverage map</a>.
        </div>
       ) : (<>
        <div className="space-y-2">
         {(env.satpass.observers || []).map((o, i) => (
          <div key={i} className="grid grid-cols-6 gap-2 items-end">
           <TextInput label="Slug" value={o.slug} onChange={(v) => { const n = [...env.satpass.observers]; n[i] = { ...o, slug: v }; up({ satpass: { ...env.satpass, observers: n } }) }} placeholder="tvly" />
           <TextInput label="Name" value={o.name} onChange={(v) => { const n = [...env.satpass.observers]; n[i] = { ...o, name: v }; up({ satpass: { ...env.satpass, observers: n } }) }} placeholder="Treasure Valley" />
           <NumberInput label="Lat" value={o.lat} onChange={(v) => { const n = [...env.satpass.observers]; n[i] = { ...o, lat: v }; up({ satpass: { ...env.satpass, observers: n } }) }} step={0.0001} />
           <NumberInput label="Lon" value={o.lon} onChange={(v) => { const n = [...env.satpass.observers]; n[i] = { ...o, lon: v }; up({ satpass: { ...env.satpass, observers: n } }) }} step={0.0001} />
           <NumberInput label="Alt (m)" value={o.alt_m} onChange={(v) => { const n = [...env.satpass.observers]; n[i] = { ...o, alt_m: v }; up({ satpass: { ...env.satpass, observers: n } }) }} step={1} />
           <button onClick={() => up({ satpass: { ...env.satpass, observers: env.satpass.observers.filter((_, j) => j !== i) } })} className="px-2 py-2 text-xs text-red-400 hover:text-red-300 border border-red-400/30">Remove</button>
          </div>
         ))}
        </div>
        <button onClick={() => up({ satpass: { ...env.satpass, observers: [...(env.satpass.observers || []), { slug: '', name: '', lat: 0, lon: 0, alt_m: 0 }] } })} className="text-xs text-accent hover:underline mt-2">+ Add Observer</button>
       </>)}
      </div>

      <ListInput label="TLE Groups" value={env.satpass.tle_groups}
       onChange={(v) => up({ satpass: { ...env.satpass, tle_groups: v } })}
       helper="Celestrak GP group selectors, e.g. weather, stations, amateur"
       infoLink="https://celestrak.org/NORAD/elements/" />

      <NumberListInput label="NORAD IDs (native)" value={env.satpass.norad_ids}
       onChange={(v) => up({ satpass: { ...env.satpass, norad_ids: v } })}
       helper="Specific NORAD catalog IDs to also fetch/predict, e.g. 25544, 33591" />

      <div className="grid grid-cols-3 gap-4">
       <NumberInput label="Min Elevation (deg)" value={env.satpass.min_elevation_deg}
        onChange={(v) => up({ satpass: { ...env.satpass, min_elevation_deg: v } })}
        min={0} max={90} helper="Native SGP4 pass filter (separate from Central min elevation above)" />
       <NumberInput label="Window (hours)" value={env.satpass.window_hours}
        onChange={(v) => up({ satpass: { ...env.satpass, window_hours: v } })}
        min={1} max={168} helper="Hours ahead to predict passes" />
       <NumberInput label="TLE Refresh (sec)" value={env.satpass.tle_refresh_seconds}
        onChange={(v) => up({ satpass: { ...env.satpass, tle_refresh_seconds: v } })}
        min={3600} helper="How often to re-fetch TLEs (default 21600 = 6h)" />
       <NumberInput label="Broadcast Lead (sec)" value={env.satpass.broadcast_lead_seconds ?? 3600}
        onChange={(v) => up({ satpass: { ...env.satpass, broadcast_lead_seconds: v } })}
        min={0} helper="How far ahead of a pass to announce" />
      </div>
     </div>
    </div>
   )}
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
    <h1 className="text-xl font-semibold text-white">Data Feeds</h1>
    <div className="flex items-center gap-3">
     {pageTab === 'curated' && (
      <>
       <Toggle label="Feeds Enabled" checked={env.enabled} onChange={(v) => up({ enabled: v })} />
       {hasChanges && (
        <>
         <button onClick={discard} className="flex items-center gap-1 px-3 py-1.5 text-sm text-[#777] hover:text-white border border-border">
          <RotateCcw size={14} /> Discard
         </button>
         <button onClick={save} disabled={saving} className="flex items-center gap-1 px-3 py-1.5 text-sm bg-accent text-white disabled:opacity-50">
          <Save size={14} /> {saving ? 'Saving…' : 'Save'}
         </button>
        </>
       )}
      </>
     )}
    </div>
   </div>

   {error && <div className="text-sm text-red-400 bg-red-500/10 p-3">{error}</div>}
   {success && <div className="text-sm text-green-400 bg-green-500/10 p-3">{success}</div>}
   {restartRequired && (
    <div className="flex items-center justify-between text-sm text-accent bg-accent/10 border border-accent/30 p-3">
     <span className="flex items-center gap-2"><RefreshCw size={14} /> A restart is required for some changes to take effect.</span>
     <button onClick={restart} className="px-3 py-1 bg-accent/20 hover:bg-amber-500/30">Restart now</button>
    </div>
   )}

   {/* Top-level tab bar: Data Feeds (curated) | Advanced (raw key/value editor) */}
   <div className="flex gap-1 border-b border-border">
    <button
     onClick={() => setPageTab('curated')}
     className={`flex items-center gap-2 px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${pageTab === 'curated' ? 'border-accent text-accent' : 'border-transparent text-[#777] hover:text-white'}`}>
     <Cloud size={15} /> Data Feeds
    </button>
    <button
     onClick={() => setPageTab('advanced')}
     className={`flex items-center gap-2 px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${pageTab === 'advanced' ? 'border-accent text-accent' : 'border-transparent text-[#777] hover:text-white'}`}>
     <Sliders size={15} /> Advanced (raw)
    </button>
   </div>

   {/* Advanced tab: raw key/value editor, curated keys filtered out */}
   {pageTab === 'advanced' && (
    <div className="-mx-6">
     <div className="px-6 pb-2 text-xs text-[#777]">
      Curated keys (owned by the Data Feeds panels above) are hidden here.
      Future or unknown keys from adapters will appear in this view.
     </div>
     <AdapterConfig excludeKeys={CURATED_KEYS} hideLlmToggle />
    </div>
   )}

   {/* Curated panels — only shown in curated tab */}
   {pageTab === 'curated' && <>

   {/* Family tabs */}
   <div className="flex gap-1 border-b border-border overflow-x-auto">
    {FAMILIES.map(({ key, label, icon: Icon }) => (
     <button key={key} onClick={() => { setFamily(key); const f = FAMILIES.find((x) => x.key === key)!; setAdapter(f.adapters[0] ?? null) }}
      className={`flex items-center gap-2 px-4 py-2 text-sm whitespace-nowrap border-b-2 -mb-px transition-colors ${family === key ? 'border-accent text-accent' : 'border-transparent text-[#777] hover:text-white'}`}>
      <Icon size={15} /> {label}
     </button>
    ))}
   </div>

   {/* Central Connection tab */}
   {family === 'central' && env.central && (
    <div className="border border-border p-4 space-y-3">
     <div className="flex items-center justify-between">
      <div>
       <span className="text-sm font-medium text-[#e0e0e0]">Central Connection</span>
       <p className="text-xs text-[#666]">NATS JetStream source for any adapter set to "central"</p>
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
      <NumberInput label="Connect Timeout (sec)" value={env.central.connect_timeout ?? 10}
            onChange={(v) => up({ central: { ...env.central!, connect_timeout: v } })}
            step={0.5} min={0} helper="NATS connect timeout for the Central consumer" />
      <TextInput label="Region" value={env.central.region || ""}
            onChange={(v) => up({ central: { ...env.central!, region: v } })}
            placeholder="us.id"
            helper="Central v0.9.20 region token (dotted, e.g. 'us.id'). Empty = bare wildcards (all-US firehose). Each adapter is either Central or native, never both — see Reference → OR-not-AND Architecture for why." />
     </div>
    </div>
   )}


   {/* Mesh Health (no env adapters; central greyed for future migration) */}
   {family === 'mesh' && (
    <div className="border border-border p-4 space-y-3">
     <div className="flex items-center justify-between">
      <div>
       <span className="text-sm font-medium text-[#e0e0e0]">Mesh Health</span>
       <p className="text-xs text-[#666]">Node/infra telemetry — sourced from the mesh, not an environmental feed.</p>
      </div>
      <div className="flex items-center gap-1">
       <span className="text-[10px] uppercase tracking-wide text-[#666]">source</span>
       <FeedSourceToggle value="native" onChange={() => {}} disabled={false} centralDisabled={true} />
      </div>
     </div>
     <div className="text-[11px] text-[#666]">Central not available — reserved for a future migration.</div>
    </div>
   )}

   {/* ── Family Settings — notification gating per family ──────────────── */}
   {family === 'family_settings' && (
    <div className="space-y-4">
     <p className="text-xs text-[#777]">
      Per-family gating: enable/disable each notification family, set its minimum severity threshold,
      freshness window, and cooldown. Delivery routing (which mesh channels, email, webhook) is
      configured on the <a href="/notifications" className="text-accent hover:underline">Meshtastic Routing</a> and{' '}
      <a href="/meshcore/routing" className="text-accent hover:underline">MeshCore Routing</a> pages.
     </p>
     {notifError && <div className="text-sm text-red-400 bg-red-500/10 p-3">{notifError}</div>}
     {notifSuccess && <div className="text-sm text-green-400 bg-green-500/10 p-3">{notifSuccess}</div>}
     {notifConfig === null ? (
      <div className="text-xs text-[#666] italic">Loading family settings…</div>
     ) : (
      <>
       <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {TOGGLE_FAMILY_META.map(({ key, label, Icon }) => {
         const t: NotificationToggle = notifToggles[key] || ({} as NotificationToggle)
         return (
          <div key={key} className="border border-border p-3 space-y-3">
           <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm text-[#e0e0e0]">
             <Icon size={15} /> {label}
            </div>
            <Toggle label="" checked={!!t.enabled} onChange={(v) => updNotif(key, { enabled: v })} />
           </div>
           <div className={t.enabled ? 'space-y-3' : 'space-y-3 opacity-40 pointer-events-none select-none'}>
            <SelectInput
             label="Min Severity"
             value={t.min_severity || 'priority'}
             onChange={(v) => updNotif(key, { min_severity: v })}
             options={[
              { value: 'routine', label: 'Routine — informational' },
              { value: 'priority', label: 'Priority — needs attention' },
              { value: 'immediate', label: 'Immediate — act now' },
             ]}
            />
            <div className="grid grid-cols-2 gap-3">
             <NumberInput
              label="Freshness (sec)"
              value={t.freshness_seconds ?? 600}
              onChange={(v) => updNotif(key, { freshness_seconds: v })}
              min={0}
              helper="Drop events older than this"
             />
             <NumberInput
              label="Cooldown (sec)"
              value={t.cooldown_seconds ?? 0}
              onChange={(v) => updNotif(key, { cooldown_seconds: v })}
              min={0}
              helper="0 = no throttle"
             />
            </div>
            <ListInput
             label="Regions"
             value={t.regions ?? []}
             onChange={(v) => updNotif(key, { regions: v })}
             helper="Empty = all regions; otherwise only these region names"
            />
           </div>
          </div>
         )
        })}
       </div>
       {notifHasChanges && (
        <div className="flex justify-end gap-2 pt-2">
         <button
          onClick={discardNotif}
          className="flex items-center gap-1 px-3 py-1.5 text-sm text-[#777] hover:text-white border border-border"
         >
          <RotateCcw size={14} /> Discard
         </button>
         <button
          onClick={saveNotif}
          disabled={notifSaving}
          className="flex items-center gap-1 px-3 py-1.5 text-sm bg-accent text-white disabled:opacity-50"
         >
          <Save size={14} /> {notifSaving ? 'Saving…' : 'Save'}
         </button>
        </div>
       )}
      </>
     )}
    </div>
   )}

   {/* Adapter sub-tabs + panel */}
   {fam.adapters.length > 0 && activeAdapter && (
    <>
     {fam.adapters.length > 1 && (
      <div className="flex gap-1">
       {fam.adapters.map((k) => (
        <button key={k} onClick={() => setAdapter(k)}
         className={`px-3 py-1.5 text-sm ${activeAdapter === k ? 'bg-bg-hover text-white' : 'text-[#777] hover:text-white'}`}>
         {META[k].label}
        </button>
       ))}
      </div>
     )}
     <AdapterPanel
      title={META[activeAdapter].label}
      subtitle={META[activeAdapter].subtitle}
      /* BUG FIX: the native satpass adapter is gated on
         environmental.satpass.enabled AND feed_source=="native" (see
         meshai/env/store.py::_register_adapter). The enable toggle previously
         wrote adapter_config.satpass.enabled (the Central layer), which never
         reached the native gate. All adapters — satpass included — now drive
         their YAML `enabled` via the generic setAdapterField path. The native
         enable is authoritative for the native adapter; the Central
         adapter_config/satpass panel below is unchanged. */
      enabled={a[activeAdapter]?.enabled ?? false}
      onEnabled={(v) => setAdapterField(activeAdapter, { enabled: v })}
      feedSource={a[activeAdapter]?.feed_source ?? 'native'}
      onFeedSource={(v) => setAdapterField(activeAdapter, { feed_source: v })}
      hasCentral={META[activeAdapter].hasCentral}
      nativeOnly={META[activeAdapter].nativeOnly}
      hasKey={effectiveHasKey(activeAdapter)}
      health={healthFor(activeAdapter)}
      events={eventsFor(activeAdapter)}
      llmContext={PANEL_META_KEY[activeAdapter] !== undefined ? (llmMeta[PANEL_META_KEY[activeAdapter]!] ?? true) : undefined}
      onLlmContext={PANEL_META_KEY[activeAdapter] !== undefined ? (v) => saveLlmContext(PANEL_META_KEY[activeAdapter]!, v) : undefined}
     >
      {renderSettings(activeAdapter)}
     </AdapterPanel>
    </>
   )}

   {/* ── Advanced: Geocoder — infra, not a feed ──────────────────────────── */}
   <details className="group border border-border p-4">
    <summary className="flex items-center gap-2 cursor-pointer text-sm font-medium text-[#e0e0e0] hover:text-white">
     <ChevronRight size={14} className="group-open:rotate-90 transition-transform" />
     Advanced: Geocoder
    </summary>
    <p className="mt-2 text-xs text-[#666]">
     Configures the Photon reverse-geocoder used to resolve place names.
    </p>
    <div className="mt-4 space-y-3 pl-6 border-l border-border">
     <TextInput
      label="Geocoder URL"
      value={env.geocoder?.url ?? 'https://photon.komoot.io'}
      onChange={(v) => up({ geocoder: { ...env.geocoder, url: v } })}
      placeholder="https://photon.komoot.io"
      helper="Photon geocoding endpoint"
     />
     <div className="grid grid-cols-2 gap-3">
      <NumberInput
       label="Timeout (s)"
       value={env.geocoder?.timeout_seconds ?? 2}
       onChange={(v) => up({ geocoder: { ...env.geocoder, timeout_seconds: v } })}
       min={0}
       step={0.5}
       helper="HTTP timeout per geocode request"
      />
      <NumberInput
       label="Search Radius (km)"
       value={env.geocoder?.radius_km ?? 80}
       onChange={(v) => up({ geocoder: { ...env.geocoder, radius_km: v } })}
       min={0}
       helper="Bias radius around the configured center for results"
      />
      <NumberInput
       label="Result Limit"
       value={env.geocoder?.limit ?? 10}
       onChange={(v) => up({ geocoder: { ...env.geocoder, limit: v } })}
       min={1}
       helper="Max candidate results to consider"
      />
     </div>
    </div>
   </details>

   </> /* end curated tab */}
  </div>
 )
}
