import { useEffect, useState, useMemo } from 'react'
import {
  fetchHealth,
  fetchSources,
  fetchAlerts,
  fetchEnvStatus,
  fetchEnvActive,
  fetchSWPC,
  fetchDucting,
  type MeshHealth,
  type SourceHealth,
  type Alert,
  type EnvStatus,
  type EnvEvent,
  type SWPCStatus,
  type DuctingStatus,
} from '@/lib/api'
import { useWebSocket } from '@/hooks/useWebSocket'
import {
  AlertTriangle,
  AlertCircle,
  Info,
  CheckCircle,
  Radio,
  Cpu,
  Activity,
  MapPin,
  Zap,
  Cloud,
  Flame,
  Mountain,
  Droplets,
  Car,
  Construction,
  Satellite,
  Sun,
} from 'lucide-react'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  ResponsiveContainer,
  ReferenceLine,
  LineChart,
  Line,
} from 'recharts'

// Extended types for history data
interface KpHistoryEntry {
  time: string
  value: number
}

interface ProfileEntry {
  level_hPa: number
  height_m: number
  N: number
  M: number
  T_C: number
  RH: number
}

interface ExtendedSWPCStatus extends SWPCStatus {
  kp_history?: KpHistoryEntry[]
  sfi_history?: { time: string; value: number }[]
}

interface ExtendedDuctingStatus extends DuctingStatus {
  profile?: ProfileEntry[]
  gradients?: {
    from_level: number
    to_level: number
    from_height_m: number
    to_height_m: number
    gradient: number
  }[]
  assessment?: string
  location?: { lat: number; lon: number }
}

function HealthGauge({ health }: { health: MeshHealth }) {
  const score = health.score
  const tier = health.tier

  const getColor = (s: number) => {
    if (s >= 80) return '#22c55e'
    if (s >= 60) return '#f59e0b'
    return '#ef4444'
  }

  const color = getColor(score)
  const circumference = 2 * Math.PI * 45
  const progress = (score / 100) * circumference

  return (
    <div className="flex flex-col items-center">
      <svg width="140" height="140" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="45" fill="none" stroke="#1e2a3a" strokeWidth="8" />
        <circle
          cx="50" cy="50" r="45" fill="none" stroke={color} strokeWidth="8"
          strokeLinecap="round" strokeDasharray={circumference}
          strokeDashoffset={circumference - progress} transform="rotate(-90 50 50)"
          className="transition-all duration-500"
        />
        <text x="50" y="46" textAnchor="middle" className="fill-slate-100 font-mono text-2xl font-bold" style={{ fontSize: '24px' }}>
          {score.toFixed(1)}
        </text>
        <text x="50" y="62" textAnchor="middle" className="fill-slate-400 text-xs" style={{ fontSize: '10px' }}>
          {tier}
        </text>
      </svg>
    </div>
  )
}

function PillarBar({ label, value }: { label: string; value: number }) {
  const getColor = (v: number) => {
    if (v >= 80) return 'bg-green-500'
    if (v >= 60) return 'bg-amber-500'
    return 'bg-red-500'
  }

  return (
    <div className="flex items-center gap-3">
      <div className="w-24 text-xs text-slate-400 truncate">{label}</div>
      <div className="flex-1 h-2 bg-border rounded-full overflow-hidden">
        <div className={`h-full ${getColor(value)} transition-all duration-300`} style={{ width: `${value}%` }} />
      </div>
      <div className="w-12 text-right text-xs font-mono text-slate-300">{value.toFixed(1)}</div>
    </div>
  )
}

function AlertItem({ alert }: { alert: Alert }) {
  const getSeverityStyles = (severity: string) => {
    switch (severity.toLowerCase()) {
      case 'critical':
      case 'emergency':
      case 'immediate':
        return { bg: 'bg-red-500/10', border: 'border-red-500', icon: AlertCircle, iconColor: 'text-red-500' }
      case 'warning':
      case 'priority':
        return { bg: 'bg-amber-500/10', border: 'border-amber-500', icon: AlertTriangle, iconColor: 'text-amber-500' }
      case 'routine':
      default:
        return { bg: 'bg-blue-500/10', border: 'border-blue-500', icon: Info, iconColor: 'text-blue-500' }
    }
  }

  const styles = getSeverityStyles(alert.severity)
  const Icon = styles.icon

  return (
    <div className={`p-3 rounded-lg ${styles.bg} border-l-2 ${styles.border} flex items-start gap-3`}>
      <Icon size={16} className={styles.iconColor} />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-200">{alert.message}</div>
        <div className="text-xs text-slate-500 mt-1">{alert.timestamp || 'Just now'}</div>
      </div>
    </div>
  )
}

function SourceCard({ source }: { source: SourceHealth }) {
  const getStatusColor = () => {
    if (!source.is_loaded) return 'bg-red-500'
    if (source.last_error) return 'bg-amber-500'
    return 'bg-green-500'
  }

  return (
    <div className="flex items-center gap-3 p-3 rounded-lg bg-bg-hover">
      <div className={`w-2 h-2 rounded-full ${getStatusColor()}`} />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-200 truncate">{source.name}</div>
        <div className="text-xs text-slate-500">{source.node_count} nodes · {source.type}</div>
      </div>
    </div>
  )
}

function StatCard({ icon: Icon, label, value, subvalue }: { icon: typeof Radio; label: string; value: string | number; subvalue?: string }) {
  return (
    <div className="bg-bg-card border border-border rounded-lg p-4">
      <div className="flex items-center gap-2 text-slate-400 mb-2">
        <Icon size={14} />
        <span className="text-xs">{label}</span>
      </div>
      <div className="font-mono text-xl text-slate-100">{value}</div>
      {subvalue && <div className="text-xs text-slate-500 mt-1">{subvalue}</div>}
    </div>
  )
}

// Scale badge component for R/S/G
function ScaleBadge({ label, value }: { label: string; value: number }) {
  const getColor = () => {
    if (value === 0) return 'bg-green-500/20 text-green-400 border-green-500/50'
    if (value <= 2) return 'bg-amber-500/20 text-amber-400 border-amber-500/50'
    return 'bg-red-500/20 text-red-400 border-red-500/50'
  }

  return (
    <span className={`px-2 py-1 rounded text-xs font-mono font-medium border ${getColor()}`}>
      {label}{value}
    </span>
  )
}

// Large value display for SFI/Kp
function BigValue({ label, value, unit, getColor }: { label: string; value: number | undefined; unit?: string; getColor: (v: number) => string }) {
  const color = value !== undefined ? getColor(value) : 'text-slate-400'
  return (
    <div className="text-center">
      <div className="text-xs text-slate-500 mb-1">{label}</div>
      <div className={`font-mono text-3xl font-bold ${color}`}>
        {value?.toFixed(0) ?? '—'}
      </div>
      {unit && <div className="text-xs text-slate-500">{unit}</div>}
    </div>
  )
}

// Kp trend sparkline chart
function KpTrendChart({ history }: { history: KpHistoryEntry[] }) {
  const chartData = useMemo(() => {
    if (!history || history.length === 0) return []
    // Take last 16 entries (48 hours of 3-hourly data)
    return history.slice(-16).map((entry, i) => ({
      idx: i,
      value: entry.value,
      time: entry.time,
    }))
  }, [history])

  if (chartData.length === 0) return null

  const maxKp = Math.max(...chartData.map(d => d.value), 5)
  const currentKp = chartData[chartData.length - 1]?.value ?? 0

  // Gradient color based on max Kp
  const getGradientId = () => {
    if (maxKp > 5) return 'kpGradientRed'
    if (maxKp > 3) return 'kpGradientAmber'
    return 'kpGradientGreen'
  }

  return (
    <div className="h-20 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={chartData} margin={{ top: 5, right: 5, bottom: 5, left: 5 }}>
          <defs>
            <linearGradient id="kpGradientGreen" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#22c55e" stopOpacity={0.4} />
              <stop offset="100%" stopColor="#22c55e" stopOpacity={0.05} />
            </linearGradient>
            <linearGradient id="kpGradientAmber" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.4} />
              <stop offset="100%" stopColor="#f59e0b" stopOpacity={0.05} />
            </linearGradient>
            <linearGradient id="kpGradientRed" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#ef4444" stopOpacity={0.4} />
              <stop offset="100%" stopColor="#ef4444" stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <YAxis domain={[0, Math.ceil(maxKp)]} hide />
          <XAxis dataKey="idx" hide />
          <ReferenceLine y={3} stroke="#f59e0b" strokeDasharray="3 3" strokeOpacity={0.5} />
          <ReferenceLine y={5} stroke="#ef4444" strokeDasharray="3 3" strokeOpacity={0.5} />
          <Area
            type="monotone"
            dataKey="value"
            stroke={currentKp > 5 ? '#ef4444' : currentKp > 3 ? '#f59e0b' : '#22c55e'}
            fill={`url(#${getGradientId()})`}
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
      <div className="flex justify-between text-xs text-slate-600 px-1">
        <span>48h ago</span>
        <span>now</span>
      </div>
    </div>
  )
}

// Refractivity profile chart
function RefractivityChart({ profile }: { profile: ProfileEntry[] }) {
  const chartData = useMemo(() => {
    if (!profile || profile.length === 0) return []
    return [...profile].sort((a, b) => a.height_m - b.height_m).map(p => ({
      height: p.height_m,
      M: p.M,
    }))
  }, [profile])

  if (chartData.length === 0) return null

  return (
    <div className="h-24 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={chartData} margin={{ top: 5, right: 10, bottom: 5, left: 5 }}>
          <XAxis
            dataKey="M"
            type="number"
            domain={['dataMin - 20', 'dataMax + 20']}
            tick={{ fontSize: 10, fill: '#64748b' }}
            tickLine={false}
            axisLine={{ stroke: '#334155' }}
          />
          <YAxis
            dataKey="height"
            type="number"
            domain={[0, 'dataMax']}
            tick={{ fontSize: 10, fill: '#64748b' }}
            tickLine={false}
            axisLine={{ stroke: '#334155' }}
            tickFormatter={(v) => `${(v/1000).toFixed(1)}k`}
          />
          <Line
            type="monotone"
            dataKey="M"
            stroke="#3b82f6"
            strokeWidth={2}
            dot={{ r: 3, fill: '#3b82f6' }}
          />
        </LineChart>
      </ResponsiveContainer>
      <div className="text-center text-xs text-slate-600">M-units vs Height (km)</div>
    </div>
  )
}

// RF Propagation Card
function RFPropagationCard({ swpc, ducting }: { swpc: ExtendedSWPCStatus | null; ducting: ExtendedDuctingStatus | null }) {
  const getSfiColor = (v: number) => {
    if (v >= 120) return 'text-green-400'
    if (v >= 80) return 'text-amber-400'
    return 'text-red-400'
  }

  const getKpColor = (v: number) => {
    if (v <= 3) return 'text-green-400'
    if (v <= 5) return 'text-amber-400'
    return 'text-red-400'
  }

  const getDuctingBadge = (condition?: string) => {
    if (!condition) return null
    const styles: Record<string, string> = {
      normal: 'bg-green-500/20 text-green-400 border-green-500/50',
      super_refraction: 'bg-amber-500/20 text-amber-400 border-amber-500/50',
      surface_duct: 'bg-blue-500/20 text-blue-400 border-blue-500/50',
      elevated_duct: 'bg-blue-500/20 text-blue-400 border-blue-500/50',
    }
    const labels: Record<string, string> = {
      normal: 'Normal',
      super_refraction: 'Super Refraction',
      surface_duct: 'Surface Duct',
      elevated_duct: 'Elevated Duct',
    }
    return (
      <span className={`px-2 py-1 rounded text-xs font-medium border ${styles[condition] || styles.normal}`}>
        {labels[condition] || condition}
      </span>
    )
  }

  return (
    <div className="bg-bg-card border border-border rounded-lg p-4 flex flex-col h-full">
      <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
        <Zap size={14} />
        <span title="R (Radio Blackouts), S (Solar Radiation Storms), G (Geomagnetic Storms) — NOAA SWPC scales. Kp 3 = quiet baseline, Kp >= 5 = aurora visible at mid-latitudes and HF degraded. See Reference → Solar &amp; Geomagnetic.">RF Propagation</span>
      </h2>

      {/* Top row: SFI and Kp big values */}
      <div className="flex justify-around mb-4">
        <BigValue label="SFI" value={swpc?.sfi} getColor={getSfiColor} />
        <div className="w-px bg-border" />
        <BigValue label="Kp" value={swpc?.kp_current} getColor={getKpColor} />
      </div>

      {/* R/S/G Scale badges */}
      <div className="flex justify-center gap-2 mb-4">
        <ScaleBadge label="R" value={swpc?.r_scale ?? 0} />
        <ScaleBadge label="S" value={swpc?.s_scale ?? 0} />
        <ScaleBadge label="G" value={swpc?.g_scale ?? 0} />
      </div>

      {/* Kp Trend Chart */}
      {swpc?.kp_history && swpc.kp_history.length > 0 && (
        <div className="mb-4">
          <div className="text-xs text-slate-500 mb-1">Kp Trend (48h)</div>
          <KpTrendChart history={swpc.kp_history} />
        </div>
      )}

      {/* Divider */}
      <div className="border-t border-border my-3" />

      {/* Tropospheric section */}
      <div className="flex items-center gap-2 mb-2">
        <Cloud size={14} className="text-slate-400" />
        <span className="text-xs text-slate-500">Tropospheric</span>
        {getDuctingBadge(ducting?.condition)}
      </div>

      {ducting?.min_gradient !== undefined && (
        <div className="text-xs text-slate-400 font-mono mb-2">
          dM/dz: {ducting.min_gradient.toFixed(1)} M-units/km
        </div>
      )}

      {/* Refractivity profile chart */}
      {ducting?.profile && ducting.profile.length > 0 && (
        <RefractivityChart profile={ducting.profile} />
      )}

      {/* SWPC Warnings */}
      {swpc?.active_warnings && swpc.active_warnings.length > 0 && (
        <div className="mt-auto pt-3 border-t border-border">
          <div className="text-xs text-slate-500 mb-1">SWPC Alerts</div>
          <div className="flex flex-wrap gap-1">
            {swpc.active_warnings.slice(0, 3).map((w, i) => (
              <span key={i} className="px-2 py-0.5 rounded text-xs bg-amber-500/20 text-amber-400 border border-amber-500/30 truncate max-w-full">
                {w.replace('Space Weather Message Code: ', '')}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// Source icon mapping
const SOURCE_ICONS: Record<string, { icon: typeof Cloud; color: string; label: string }> = {
  nws: { icon: Cloud, color: 'text-blue-400', label: 'NWS' },
  swpc: { icon: Sun, color: 'text-yellow-400', label: 'SWPC' },
  ducting: { icon: Radio, color: 'text-cyan-400', label: 'Tropo' },
  nifc: { icon: Flame, color: 'text-orange-400', label: 'NIFC' },
  firms: { icon: Satellite, color: 'text-red-400', label: 'FIRMS' },
  avalanche: { icon: Mountain, color: 'text-slate-300', label: 'Avy' },
  usgs: { icon: Droplets, color: 'text-blue-300', label: 'USGS' },
  traffic: { icon: Car, color: 'text-purple-400', label: 'Traffic' },
  roads: { icon: Construction, color: 'text-amber-400', label: '511' },
}

// Severity badge colors (3-level system + legacy support)
const SEVERITY_COLORS: Record<string, string> = {
  // New 3-level system
  routine: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  priority: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  immediate: 'bg-red-600/20 text-red-300 border-red-600/30',
  // NWS native (for raw event display)
  info: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  advisory: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  moderate: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  watch: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  warning: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  severe: 'bg-red-500/20 text-red-400 border-red-500/30',
  extreme: 'bg-red-600/20 text-red-300 border-red-600/30',
  critical: 'bg-red-600/20 text-red-300 border-red-600/30',
  emergency: 'bg-red-700/20 text-red-200 border-red-700/30',
}

function EventFeedItem({ event, isLocal }: { event: EnvEvent; isLocal?: boolean }) {
  const sourceConfig = SOURCE_ICONS[event.source] || { icon: Info, color: 'text-slate-400', label: event.source }
  const Icon = sourceConfig.icon
  const severityStyle = SEVERITY_COLORS[event.severity?.toLowerCase()] || SEVERITY_COLORS.info

  // Format timestamp
  const formatTime = (ts: number) => {
    const date = new Date(ts * 1000)
    const now = new Date()
    const diffMs = now.getTime() - date.getTime()
    const diffMins = Math.floor(diffMs / 60000)

    if (diffMins < 1) return 'just now'
    if (diffMins < 60) return `${diffMins}m ago`
    if (diffMins < 1440) return `${Math.floor(diffMins / 60)}h ago`
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  }

  // Build display title: prefer event_type + area_desc, fall back to headline
  const eventType = (event as Record<string, unknown>).event_type as string | undefined
  const areaDesc = (event as Record<string, unknown>).area_desc as string | undefined
  const description = (event as Record<string, unknown>).description as string | undefined

  let title = event.headline
  if (eventType && areaDesc) {
    // Shorten area description (remove "County" repetition)
    const shortArea = areaDesc.replace(/ County/g, '').split(';')[0]
    title = `${eventType} — ${shortArea}`
  } else if (eventType) {
    title = eventType
  }

  // Get first sentence of description as subtitle
  const subtitle = description ? description.split('. ')[0] : null

  return (
    <div className={`flex items-start gap-2 py-2 border-b border-border/50 last:border-0 ${isLocal ? 'border-l-2 border-l-blue-500 pl-2 -ml-2' : ''}`}>
      <Icon size={14} className={`mt-0.5 flex-shrink-0 ${sourceConfig.color}`} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className={`px-1.5 py-0.5 rounded text-xs border ${severityStyle}`}>
            {event.severity || 'info'}
          </span>
          {isLocal && (
            <span className="px-1.5 py-0.5 rounded text-xs bg-blue-500/20 text-blue-400 border border-blue-500/30" title="LOCAL: event coordinates fall inside the mesh's monitoring area (per the adapter's bbox config on Environment) — operators in this region are directly affected.">
              LOCAL
            </span>
          )}
          <span className="text-xs text-slate-500">{sourceConfig.label}</span>
          <span className="text-xs text-slate-600 ml-auto">{formatTime(event.fetched_at)}</span>
        </div>
        <div className={`text-sm truncate ${isLocal ? 'text-slate-100' : 'text-slate-300'}`}>{title}</div>
        {subtitle && (
          <div className="text-xs text-slate-500 truncate mt-0.5">{subtitle}</div>
        )}
      </div>
    </div>
  )
}

// Live Event Feed Card
function LiveEventFeed({ events, envStatus }: { events: EnvEvent[]; envStatus: EnvStatus | null }) {
  // Severity order for sorting
  const severityOrder: Record<string, number> = { immediate: 0, priority: 1, routine: 2 }

  const sortedEvents = useMemo(() => {
    // Dedup by event_id
    const seen = new Set<string>()
    const deduped = events.filter(e => {
      if (!e.event_id) return true
      if (seen.has(e.event_id)) return false
      seen.add(e.event_id)
      return true
    })

    // Sort: local first, then by severity, then by time
    return deduped.sort((a, b) => {
      const aLocal = (a as Record<string, unknown>).is_local ? 1 : 0
      const bLocal = (b as Record<string, unknown>).is_local ? 1 : 0
      if (aLocal !== bLocal) return bLocal - aLocal  // local first

      const aSev = severityOrder[a.severity?.toLowerCase() || 'routine'] ?? 2
      const bSev = severityOrder[b.severity?.toLowerCase() || 'routine'] ?? 2
      if (aSev !== bSev) return aSev - bSev  // higher severity first

      return (b.fetched_at || 0) - (a.fetched_at || 0)  // newest first
    })
  }, [events])

  // Calculate feed health summary
  const feedSummary = useMemo(() => {
    if (!envStatus?.feeds) return null
    const total = envStatus.feeds.length
    const active = envStatus.feeds.filter(f => f.is_loaded && !f.last_error).length
    const errors = envStatus.feeds.filter(f => f.last_error).map(f => f.source)
    const lastFetch = Math.max(...envStatus.feeds.map(f => f.last_fetch || 0))
    const secAgo = lastFetch ? Math.floor((Date.now() / 1000) - lastFetch) : null

    return { total, active, errors, secAgo }
  }, [envStatus])

  return (
    <div className="bg-bg-card border border-border rounded-lg p-4 flex flex-col h-full">
      <h2 className="text-sm font-medium text-slate-400 mb-3 flex items-center gap-2">
        <Activity size={14} />
        Live Event Feed
      </h2>

      {sortedEvents.length > 0 ? (
        <div className="flex-1 overflow-y-auto max-h-80 pr-1 -mr-1">
          {sortedEvents.map((event, i) => (
            <EventFeedItem
              key={event.event_id || i}
              event={event}
              isLocal={(event as Record<string, unknown>).is_local as boolean | undefined}
            />
          ))}
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center py-8">
            <CheckCircle size={24} className="text-green-500 mx-auto mb-2" />
            <div className="text-slate-400">No active events</div>
            <div className="text-xs text-slate-500">All clear</div>
          </div>
        </div>
      )}

      {/* Feed health summary */}
      {feedSummary && (
        <div className={`text-xs mt-3 pt-3 border-t border-border ${feedSummary.errors.length > 0 ? 'text-amber-400' : 'text-slate-500'}`}>
          {feedSummary.active} of {feedSummary.total} feeds active
          {feedSummary.secAgo !== null && ` · Last update ${feedSummary.secAgo}s ago`}
          {feedSummary.errors.length > 0 && (
            <span className="text-amber-400"> · {feedSummary.errors.join(', ')}: error</span>
          )}
        </div>
      )}
    </div>
  )
}

export default function Dashboard() {
  const [health, setHealth] = useState<MeshHealth | null>(null)
  const [sources, setSources] = useState<SourceHealth[]>([])
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [envStatus, setEnvStatus] = useState<EnvStatus | null>(null)
  const [envEvents, setEnvEvents] = useState<EnvEvent[]>([])
  const [swpc, setSwpc] = useState<ExtendedSWPCStatus | null>(null)
  const [ducting, setDucting] = useState<ExtendedDuctingStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const { lastHealth, lastMessage } = useWebSocket()

  useEffect(() => {
    Promise.all([
      fetchHealth(),
      fetchSources(),
      fetchAlerts(),
      fetchEnvStatus(),
      fetchEnvActive().catch(() => []),
      fetchSWPC().catch(() => null),
      fetchDucting().catch(() => null),
    ])
      .then(([h, src, a, e, events, sw, duct]) => {
        setHealth(h)
        setSources(src)
        setAlerts(a)
        setEnvStatus(e)
        setEnvEvents(events)
        setSwpc(sw as ExtendedSWPCStatus)
        setDucting(duct as ExtendedDuctingStatus)
        setLoading(false)
        document.title = 'Dashboard — MeshAI'
      })
      .catch((err) => {
        setError(err.message)
        setLoading(false)
        document.title = 'Dashboard — MeshAI'
      })
  }, [])

  // Update health from WebSocket
  useEffect(() => {
    if (lastHealth) {
      setHealth(lastHealth)
    }
  }, [lastHealth])

  // Handle WebSocket env_update messages
  useEffect(() => {
    if (lastMessage?.type === 'env_update' && lastMessage.event) {
      setEnvEvents(prev => {
        // Add new event, dedupe by event_id
        const newEvent = lastMessage.event as EnvEvent
        const filtered = prev.filter(e => e.event_id !== newEvent.event_id)
        return [newEvent, ...filtered].slice(0, 100) // Keep last 100
      })
    }
  }, [lastMessage])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Error: {error}</div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Top row: Health + Alerts + Stats */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Mesh Health */}
        <div className="bg-bg-card border border-border rounded-lg p-6">
          <h2 className="text-sm font-medium text-slate-400 mb-4">Mesh Health</h2>
          {health && (
            <>
              <HealthGauge health={health} />
              <div className="mt-6 space-y-3">
                <PillarBar label="Infrastructure" value={health.pillars?.infrastructure ?? 0} />
                <PillarBar label="Utilization" value={health.pillars?.utilization ?? 0} />
                <PillarBar label="Behavior" value={health.pillars?.behavior ?? 0} />
                <PillarBar label="Power" value={health.pillars?.power ?? 0} />
              </div>
            </>
          )}
        </div>

        {/* Alerts + Stats */}
        <div className="lg:col-span-2 space-y-6">
          {/* Active Alerts */}
          <div className="bg-bg-card border border-border rounded-lg p-6">
            <h2 className="text-sm font-medium text-slate-400 mb-4">Active Alerts</h2>
            {alerts.length > 0 ? (
              <div className="space-y-3 max-h-48 overflow-y-auto">
                {alerts.map((alert, i) => (
                  <AlertItem key={i} alert={alert} />
                ))}
              </div>
            ) : (
              <div className="flex items-center gap-2 text-slate-500 py-4">
                <CheckCircle size={16} className="text-green-500" />
                <span>No active alerts</span>
              </div>
            )}
          </div>

          {/* Quick Stats */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard icon={Radio} label="Nodes Online" value={health?.total_nodes || 0} subvalue={`${health?.unlocated_count || 0} unlocated`} />
            <StatCard icon={Cpu} label="Infrastructure" value={`${health?.infra_online || 0}/${health?.infra_total || 0}`} subvalue={health?.infra_online === health?.infra_total ? 'All online' : 'Some offline'} />
            <StatCard icon={Activity} label="Utilization" value={`${health?.util_percent?.toFixed(1) || 0}%`} subvalue={`${health?.flagged_nodes || 0} flagged`} />
            <StatCard icon={MapPin} label="Regions" value={health?.total_regions || 0} subvalue={`${health?.battery_warnings || 0} battery warnings`} />
          </div>
        </div>
      </div>

      {/* Middle row: Sources + RF Propagation + Live Feed */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Mesh Sources */}
        <div className="bg-bg-card border border-border rounded-lg p-6">
          <h2 className="text-sm font-medium text-slate-400 mb-4">Mesh Sources ({sources.length})</h2>
          {sources.length > 0 ? (
            <div className="space-y-2">
              {sources.map((source, i) => (
                <SourceCard key={i} source={source} />
              ))}
            </div>
          ) : (
            <div className="text-slate-500 py-4">No sources configured</div>
          )}
        </div>

        {/* RF Propagation */}
        <RFPropagationCard swpc={swpc} ducting={ducting} />

        {/* Live Event Feed */}
        <LiveEventFeed events={envEvents} envStatus={envStatus} />
      </div>
    </div>
  )
}
