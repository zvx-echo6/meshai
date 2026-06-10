import { useEffect, useState, useMemo } from 'react'
import {
  fetchHealth,
  fetchSources,
  fetchAlerts,
  fetchEnvStatus,
  fetchEnvActive,
  fetchSWPC,
  type MeshHealth,
  type SourceHealth,
  type Alert,
  type EnvStatus,
  type EnvEvent,
  type BandConditionsStatus,
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





// Band Conditions Card
function BandConditionsCard({ bandConditions }: { bandConditions: BandConditionsStatus | null }) {
  const getRatingEmoji = (rating?: string) => {
    switch (rating) {
      case 'Good': return '\ud83d\udfe2'  // green circle
      case 'Fair': return '\ud83d\udfe1'  // yellow circle
      case 'Poor': return '\ud83d\udd34'  // red circle
      default: return '\u2014'
    }
  }

  const getSlotEmoji = (label?: string) => {
    if (!label) return ''
    return label.includes('Night') ? '\ud83c\udf19' : '\u2600\ufe0f'
  }

  if (!bandConditions?.enabled || !bandConditions?.ratings) {
    return (
      <div className="bg-bg-card border border-border rounded-lg p-4 flex flex-col h-full">
        <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
          <Zap size={14} />
          RF Propagation
        </h2>
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center py-8">
            <div className="text-slate-400">No band conditions data</div>
          </div>
        </div>
      </div>
    )
  }

  const bands = ['80-40m', '30-20m', '17-15m', '12-10m'] as const

  return (
    <div className="bg-bg-card border border-border rounded-lg p-4 flex flex-col h-full">
      <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
        <Zap size={14} />
        RF Propagation
      </h2>

      {/* Slot label */}
      <div className="text-center mb-4">
        <span className="text-lg">{getSlotEmoji(bandConditions.slot_label)}</span>
        <span className="text-sm text-slate-300 ml-2">{bandConditions.slot_label}</span>
      </div>

      {/* Band conditions header */}
      <div className="text-xs text-slate-500 mb-3 flex items-center gap-1">
        <span>\ud83d\udce1</span> Band Conditions:
      </div>

      {/* Band rows */}
      <div className="space-y-2">
        {bands.map(band => {
          const rating = bandConditions.ratings?.[band]
          return (
            <div key={band} className="flex items-center justify-between px-2 py-1.5 rounded bg-bg-hover">
              <span className="text-sm font-mono text-slate-300">{band}</span>
              <span className="text-sm">
                {getRatingEmoji(rating)} <span className="text-slate-300 ml-1">{rating || '\u2014'}</span>
              </span>
            </div>
          )
        })}
      </div>

      {/* Footer: source and time */}
      <div className="mt-auto pt-3 border-t border-border text-xs text-slate-500">
        {bandConditions.source && (
          <span>{bandConditions.source === 'swpc_local' ? 'SWPC' : 'HamQSL'}</span>
        )}
        {bandConditions.sent_at && (
          <span className="ml-2">
            {new Date(bandConditions.sent_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
        )}
      </div>
    </div>
  )
}

// Hepburn Tropospheric Forecast Card
const TROPO_REGIONS: { code: string; label: string }[] = [
  { code: 'wam', label: 'Western North America' },
  { code: 'eam', label: 'Eastern North America' },
  { code: 'enp', label: 'Eastern North Pacific' },
  { code: 'esp', label: 'Eastern South Pacific' },
  { code: 'gca', label: 'Gulf-Caribbean' },
  { code: 'nsa', label: 'Northern South America' },
  { code: 'csa', label: 'Central South America' },
  { code: 'sat', label: 'South Atlantic' },
  { code: 'nat', label: 'North Atlantic' },
  { code: 'ena', label: 'Eastern North Atlantic' },
  { code: 'nwe', label: 'Northwestern Europe' },
  { code: 'eur', label: 'Europe' },
  { code: 'eeu', label: 'Eastern Europe' },
  { code: 'saf', label: 'South Africa' },
  { code: 'mde', label: 'Middle East' },
  { code: 'nca', label: 'North Central Asia' },
  { code: 'ind', label: 'Indian Ocean' },
  { code: 'sea', label: 'Southeast Asia' },
  { code: 'fea', label: 'Far East' },
  { code: 'esi', label: 'Eastern Siberia' },
  { code: 'anz', label: 'Australia & New Zealand' },
  { code: 'oce', label: 'Oceania' },
  { code: 'wnp', label: 'Western North Pacific' },
]

function HepburnTropoCard() {
  const [region, setRegion] = useState('wam')
  const [imgError, setImgError] = useState(false)
  const [saving, setSaving] = useState(false)

  // Load persisted region from adapter_config on mount
  useEffect(() => {
    fetch('/api/adapter-config/dashboard/tropo_region')
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d?.value && typeof d.value === 'string') {
          setRegion(d.value)
        }
      })
      .catch(() => {})
  }, [])

  const handleRegionChange = (newRegion: string) => {
    setRegion(newRegion)
    setImgError(false)
    setSaving(true)
    fetch('/api/adapter-config/dashboard/tropo_region', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: newRegion }),
    })
      .catch(() => {})
      .finally(() => setSaving(false))
  }

  const cacheBust = new Date().toISOString().slice(0, 10).replace(/-/g, '')
  const imgUrl = `https://www.dxinfocentre.com/tr_map/fcst/${region}006.png?v${cacheBust}`
  const regionLabel = TROPO_REGIONS.find(r => r.code === region)?.label || region

  return (
    <div className="bg-bg-card border border-border rounded-lg p-4 flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-medium text-slate-400 flex items-center gap-2">
          <Radio size={14} />
          Tropo Forecast (Hepburn)
        </h2>
        <div className="flex items-center gap-2">
          {saving && <span className="text-xs text-slate-500">saving...</span>}
          <select
            value={region}
            onChange={e => handleRegionChange(e.target.value)}
            className="text-xs bg-bg-hover border border-border rounded px-2 py-1 text-slate-300 focus:outline-none focus:border-slate-500"
          >
            {TROPO_REGIONS.map(r => (
              <option key={r.code} value={r.code}>{r.label}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="text-xs text-slate-500 mb-2">{regionLabel} — 6-day forecast</div>

      {imgError ? (
        <div className="flex items-center justify-center h-48 text-slate-500 text-sm">
          Failed to load forecast image
        </div>
      ) : (
        <img
          src={imgUrl}
          alt={`Hepburn tropo forecast — ${regionLabel}`}
          className="w-full rounded border border-border"
          onError={() => setImgError(true)}
        />
      )}

      <div className="text-xs text-slate-600 mt-2">
        Source: <a href="https://www.dxinfocentre.com/tropo.html" target="_blank" rel="noopener noreferrer" className="text-slate-500 hover:text-slate-300">dxinfocentre.com</a>
      </div>
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
  const [bandConditions, setBandConditions] = useState<BandConditionsStatus | null>(null)
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
    ])
      .then(([h, src, a, e, events, bc]) => {
        setHealth(h)
        setSources(src)
        setAlerts(a)
        setEnvStatus(e)
        setEnvEvents(events)
        setBandConditions(bc as BandConditionsStatus)
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
                <PillarBar label="Coverage" value={health.pillars?.coverage ?? 0} />
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
            ) : (() => {
              const highSeverityEnv = envEvents
                .filter(e => e.severity === 'immediate' || e.severity === 'priority')
                .sort((a, b) => {
                  const ord: Record<string, number> = { immediate: 0, priority: 1 }
                  const diff = (ord[a.severity] ?? 2) - (ord[b.severity] ?? 2)
                  if (diff !== 0) return diff
                  return (b.fetched_at || 0) - (a.fetched_at || 0)
                })
                .slice(0, 5)
              if (highSeverityEnv.length > 0) {
                return (
                  <div className="space-y-3 max-h-48 overflow-y-auto">
                    {highSeverityEnv.map((ev, i) => {
                      const sevStyle = ev.severity === 'immediate'
                        ? { bg: 'bg-red-500/10', border: 'border-red-500', icon: AlertCircle, iconColor: 'text-red-500' }
                        : { bg: 'bg-amber-500/10', border: 'border-amber-500', icon: AlertTriangle, iconColor: 'text-amber-500' }
                      const Icon = sevStyle.icon
                      return (
                        <div key={ev.event_id || i} className={`p-3 rounded-lg ${sevStyle.bg} border-l-2 ${sevStyle.border} flex items-start gap-3`}>
                          <Icon size={16} className={sevStyle.iconColor} />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="px-1.5 py-0.5 rounded text-xs bg-slate-500/20 text-slate-400 border border-slate-500/30 font-mono">ENV</span>
                              <span className="text-xs text-slate-500">{ev.severity}</span>
                            </div>
                            <div className="text-sm text-slate-200 mt-1">{ev.headline}</div>
                            <div className="text-xs text-slate-500 mt-1">{ev.source} · {new Date(ev.fetched_at * 1000).toLocaleTimeString()}</div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )
              }
              return (
                <div className="flex items-center gap-2 text-slate-500 py-4">
                  <CheckCircle size={16} className="text-green-500" />
                  <span>No active alerts</span>
                </div>
              )
            })()}
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
        <BandConditionsCard bandConditions={bandConditions} />

        {/* Live Event Feed */}
        <LiveEventFeed events={envEvents} envStatus={envStatus} />
      </div>

      {/* Bottom row: Tropo Forecast */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <HepburnTropoCard />
      </div>
    </div>
  )
}
