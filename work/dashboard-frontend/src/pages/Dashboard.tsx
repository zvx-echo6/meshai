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
  const circumference = 2 * Math.PI * 45
  const progress = (score / 100) * circumference

  return (
    <div className="flex flex-col items-center">
      <svg width="140" height="140" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="45" fill="none" stroke="#1e1e1e" strokeWidth="8" />
        <circle
          cx="50" cy="50" r="45" fill="none" stroke="#f59e0b" strokeWidth="8"
          strokeLinecap="round" strokeDasharray={circumference}
          strokeDashoffset={circumference - progress} transform="rotate(-90 50 50)"
          className="transition-all duration-500"
        />
        <text x="50" y="46" textAnchor="middle" className="font-mono font-bold" style={{ fontSize: '24px', fill: '#f59e0b' }}>
          {score.toFixed(1)}
        </text>
        <text x="50" y="62" textAnchor="middle" className="font-sans" style={{ fontSize: '10px', fill: '#444' }}>
          {tier}
        </text>
      </svg>
    </div>
  )
}

function PillarBar({ label, value }: { label: string; value: number }) {
  const getColor = (v: number) => {
    if (v > 66) return 'bg-accent'
    if (v > 33) return 'bg-accent-dim'
    return 'bg-red-500'
  }

  return (
    <div className="flex items-center gap-2">
      <div className="w-24 text-xs font-sans text-[#777] truncate">{label}</div>
      <div className="flex-1 h-2 bg-border overflow-hidden">
        <div className={`h-full ${getColor(value)} transition-all duration-300`} style={{ width: `${value}%` }} />
      </div>
      <div className="w-12 text-right text-xs font-mono text-[#e0e0e0]">{value.toFixed(1)}</div>
    </div>
  )
}

function AlertItem({ alert }: { alert: Alert }) {
  const getSeverityStyles = (severity: string) => {
    switch (severity.toLowerCase()) {
      case 'critical':
      case 'emergency':
      case 'immediate':
        return { bg: 'bg-red-500/5', border: 'border-red-500', icon: AlertCircle, iconColor: 'text-red-500' }
      case 'warning':
      case 'priority':
        return { bg: 'bg-accent/5', border: 'border-accent', icon: AlertTriangle, iconColor: 'text-accent' }
      case 'routine':
      default:
        return { bg: 'bg-[#161616]', border: 'border-[#333]', icon: Info, iconColor: 'text-[#777]' }
    }
  }

  const styles = getSeverityStyles(alert.severity)
  const Icon = styles.icon

  return (
    <div className={`p-3 ${styles.bg} border-l-2 ${styles.border} flex items-start gap-3`}>
      <Icon size={16} className={styles.iconColor} />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-sans font-medium text-white">{alert.message}</div>
        <div className="text-[10px] font-mono text-[#666] mt-1">{alert.timestamp || 'Just now'}</div>
      </div>
    </div>
  )
}

function SourceCard({ source }: { source: SourceHealth }) {
  const getStatusColor = () => {
    if (!source.is_loaded) return 'bg-red-500'
    if (source.last_error) return 'bg-accent'
    return 'bg-green-500'
  }

  return (
    <div className="flex items-center gap-3 p-2 bg-bg-hover">
      <div className={`w-2 h-2 rounded-full ${getStatusColor()}`} />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-sans font-medium text-white truncate">{source.name}</div>
        <div className="text-[10px] font-sans text-[#666]">{source.node_count} nodes · {source.type}</div>
      </div>
    </div>
  )
}

function StatCard({ icon: Icon, label, value, subvalue, accent }: { icon: typeof Radio; label: string; value: string | number; subvalue?: string; accent?: string }) {
  return (
    <div
      className="bg-bg-card border border-border p-3"
      style={accent ? { borderTopWidth: '2px', borderTopColor: accent } : undefined}
    >
      <div className="flex items-center gap-2 mb-2">
        <Icon size={14} style={{ color: accent || '#333' }} />
        <span className="text-[9px] font-sans uppercase tracking-widest text-[#666]">{label}</span>
      </div>
      <div className="font-mono text-xl" style={{ color: accent || '#e0e0e0' }}>{value}</div>
      {subvalue && <div className="text-[9px] font-sans mt-1 text-[#666]">{subvalue}</div>}
    </div>
  )
}





// Band Conditions Card
function BandConditionsCard({ bandConditions }: { bandConditions: BandConditionsStatus | null }) {
  const getRatingColor = (rating?: string) => {
    switch (rating) {
      case 'Good': return 'bg-green-500'
      case 'Fair': return 'bg-accent'
      case 'Poor': return 'bg-red-500'
      default: return 'bg-[#333]'
    }
  }

  const getRatingTextColor = (rating?: string) => {
    switch (rating) {
      case 'Good': return 'text-green-500'
      case 'Fair': return 'text-accent'
      case 'Poor': return 'text-red-500'
      default: return 'text-[#666]'
    }
  }

  const getSlotEmoji = (label?: string) => {
    if (!label) return ''
    return label.includes('Night') ? '🌙' : '☀️'
  }

  if (!bandConditions?.enabled || !bandConditions?.ratings) {
    return (
      <div className="bg-bg-card border border-border p-4 flex flex-col h-full">
        <h2 className="text-[10px] font-sans uppercase tracking-widest text-[#666] mb-3 flex items-center gap-2">
          <Zap size={14} />
          RF Propagation
        </h2>
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center py-8">
            <div className="font-sans text-[#666]">No band conditions data</div>
          </div>
        </div>
      </div>
    )
  }

  const bands = ['80-40m', '30-20m', '17-15m', '12-10m'] as const

  return (
    <div className="bg-bg-card border border-border p-4 flex flex-col h-full">
      <h2 className="text-[10px] font-sans uppercase tracking-widest text-[#666] mb-3 flex items-center gap-2">
        <Zap size={14} />
        RF Propagation
      </h2>

      {/* Slot label */}
      <div className="text-center mb-3">
        <span className="text-lg">{getSlotEmoji(bandConditions.slot_label)}</span>
        <span className="text-sm font-sans text-[#777] ml-2">{bandConditions.slot_label}</span>
      </div>

      {/* Band conditions header */}
      <div className="text-[10px] font-sans uppercase tracking-widest text-[#666] mb-2 flex items-center gap-1">
        📡 Band Conditions
      </div>

      {/* Band rows */}
      <div className="space-y-1.5">
        {bands.map(band => {
          const rating = bandConditions.ratings?.[band]
          return (
            <div key={band} className="flex items-center justify-between px-2 py-1.5 bg-bg-hover">
              <span className="text-sm font-mono text-[#777]">{band}</span>
              <span className="text-sm flex items-center gap-2">
                <span className={`inline-block w-2 h-2 rounded-full ${getRatingColor(rating)}`} />
                <span className={`font-sans ${getRatingTextColor(rating)}`}>{rating || '—'}</span>
              </span>
            </div>
          )
        })}
      </div>

      {/* Footer: source and time */}
      <div className="mt-auto pt-3 border-t border-border text-[10px] font-sans text-[#666]">
        {bandConditions.source && (
          <span>{bandConditions.source === 'swpc_local' ? 'SWPC' : 'HamQSL'}</span>
        )}
        {bandConditions.sent_at && (
          <span className="font-mono ml-2">
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
    <div className="bg-bg-card border border-border p-4 flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-[10px] font-sans uppercase tracking-widest text-[#666] flex items-center gap-2">
          <Radio size={14} />
          Tropo Forecast (Hepburn)
        </h2>
        <div className="flex items-center gap-2">
          {saving && <span className="text-xs font-sans text-[#666]">saving...</span>}
          <select
            value={region}
            onChange={e => handleRegionChange(e.target.value)}
            className="text-xs font-sans bg-bg-hover border border-border px-2 py-1 min-h-[36px] text-[#e0e0e0] focus:outline-none focus:border-accent"
          >
            {TROPO_REGIONS.map(r => (
              <option key={r.code} value={r.code}>{r.label}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="text-xs font-sans text-[#666] mb-2">{regionLabel} — 6-day forecast</div>

      {imgError ? (
        <div className="flex items-center justify-center h-48 text-[#666] text-sm font-sans">
          Failed to load forecast image
        </div>
      ) : (
        <img
          src={imgUrl}
          alt={`Hepburn tropo forecast — ${regionLabel}`}
          className="w-full border border-border"
          onError={() => setImgError(true)}
        />
      )}

      <div className="text-[10px] font-sans text-[#666] mt-2">
        Source: <a href="https://www.dxinfocentre.com/tropo.html" target="_blank" rel="noopener noreferrer" className="text-sky-400 hover:text-sky-300">dxinfocentre.com</a>
      </div>
    </div>
  )
}

// Source icon mapping
const SOURCE_ICONS: Record<string, { icon: typeof Cloud; color: string; label: string }> = {
  nws: { icon: Cloud, color: 'text-sky-400', label: 'NWS' },
  swpc: { icon: Sun, color: 'text-accent', label: 'SWPC' },
  ducting: { icon: Radio, color: 'text-sky-500', label: 'Tropo' },
  nifc: { icon: Flame, color: 'text-red-500', label: 'NIFC' },
  firms: { icon: Satellite, color: 'text-red-400', label: 'FIRMS' },
  avalanche: { icon: Mountain, color: 'text-[#777]', label: 'Avy' },
  usgs: { icon: Droplets, color: 'text-sky-400', label: 'USGS' },
  traffic: { icon: Car, color: 'text-[#777]', label: 'Traffic' },
  roads: { icon: Construction, color: 'text-accent-dim', label: '511' },
}

// Severity badge colors (3-level system + legacy support)
const SEVERITY_COLORS: Record<string, string> = {
  // New 3-level system
  routine: 'bg-[#1e1e1e] text-[#777] border-[#222]',
  priority: 'bg-accent/5 text-accent border-accent/30',
  immediate: 'bg-red-500/5 text-red-500 border-red-500/30',
  // NWS native (for raw event display)
  info: 'bg-sky-400/10 text-sky-400 border-sky-400/30',
  advisory: 'bg-sky-400/10 text-sky-400 border-sky-400/30',
  moderate: 'bg-accent/5 text-accent-dim border-accent-dim/30',
  watch: 'bg-accent/5 text-accent border-accent/30',
  warning: 'bg-accent/5 text-accent border-accent/30',
  severe: 'bg-red-500/5 text-red-500 border-red-500/30',
  extreme: 'bg-red-500/5 text-red-500 border-red-500/30',
  critical: 'bg-red-500/5 text-red-500 border-red-500/30',
  emergency: 'bg-red-500/5 text-red-500 border-red-500/30',
}

function EventFeedItem({ event, isLocal }: { event: EnvEvent; isLocal?: boolean }) {
  const sourceConfig = SOURCE_ICONS[event.source] || { icon: Info, color: 'text-[#777]', label: event.source }
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
    <div className={`flex items-start gap-2 py-2 border-b border-border/50 last:border-0 ${isLocal ? 'border-l-2 border-l-accent pl-2 -ml-2' : ''}`}>
      <Icon size={14} className={`mt-0.5 flex-shrink-0 ${sourceConfig.color}`} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className={`px-1.5 py-0.5 text-[10px] font-sans uppercase tracking-wide border ${severityStyle}`}>
            {event.severity || 'info'}
          </span>
          {isLocal && (
            <span className="px-1.5 py-0.5 text-[10px] font-sans uppercase tracking-wide bg-accent/5 text-accent border border-accent/30" title="LOCAL: event coordinates fall inside the mesh's monitoring area (per the adapter's bbox config on Environment) — operators in this region are directly affected.">
              LOCAL
            </span>
          )}
          <span className="text-[10px] font-sans text-[#666]">{sourceConfig.label}</span>
          <span className="text-[10px] font-mono text-[#666] ml-auto">{formatTime(event.fetched_at)}</span>
        </div>
        <div className={`text-sm font-sans font-medium truncate ${isLocal ? 'text-white' : 'text-[#e0e0e0]'}`}>{title}</div>
        {subtitle && (
          <div className="text-[10px] font-sans text-[#666] truncate mt-0.5">{subtitle}</div>
        )}
      </div>
    </div>
  )
}

// Live Event Feed Card
function LiveEventFeed({ events, envStatus, embedded }: { events: EnvEvent[]; envStatus: EnvStatus | null; embedded?: boolean }) {
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

  const content = (
    <>
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
            <div className="font-sans text-[#777]">No active events</div>
            <div className="text-[10px] font-sans text-[#666]">All clear</div>
          </div>
        </div>
      )}

      {/* Feed health summary */}
      {feedSummary && (
        <div className={`text-[10px] font-sans mt-3 pt-3 border-t border-border ${feedSummary.errors.length > 0 ? 'text-red-500' : 'text-[#666]'}`}>
          <span className="font-mono">{feedSummary.active}</span> of <span className="font-mono">{feedSummary.total}</span> feeds active
          {feedSummary.secAgo !== null && <> · Last update <span className="font-mono">{feedSummary.secAgo}s</span> ago</>}
          {feedSummary.errors.length > 0 && (
            <span className="text-red-500"> · {feedSummary.errors.join(', ')}: error</span>
          )}
        </div>
      )}
    </>
  )

  if (embedded) return <div className="flex flex-col h-full">{content}</div>

  return (
    <div className="bg-bg-card border border-border p-4 flex flex-col h-full">
      <h2 className="text-[10px] font-sans uppercase tracking-widest text-[#666] mb-3 flex items-center gap-2">
        <Activity size={14} />
        Live Event Feed
      </h2>
      {content}
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
  const [alertTab, setAlertTab] = useState<'alerts' | 'feed'>('alerts')
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
        <div className="font-sans text-[#777]">Loading...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="font-sans text-red-500">Error: {error}</div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Top row: Health + Alerts + Stats */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Mesh Health */}
        <div className="bg-bg-card border border-border p-4">
          <h2 className="text-[10px] font-sans uppercase tracking-widest text-[#666] mb-3">Mesh Health</h2>
          {health && (
            <>
              <HealthGauge health={health} />
              <div className="mt-4 space-y-2">
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
        <div className="lg:col-span-2 space-y-4">
          {/* Active Alerts / Event Feed — tabbed */}
          <div className="bg-bg-card border border-border p-4">
            <div className="flex items-center gap-4 mb-3 border-b border-border">
              <button
                onClick={() => setAlertTab('alerts')}
                className={`py-2.5 -mb-px text-[10px] font-sans uppercase tracking-widest transition-colors border-b ${
                  alertTab === 'alerts'
                    ? 'border-accent text-white'
                    : 'border-transparent text-[#777]'
                }`}
              >
                Active Alerts
              </button>
              <button
                onClick={() => setAlertTab('feed')}
                className={`py-2.5 -mb-px text-[10px] font-sans uppercase tracking-widest transition-colors border-b ${
                  alertTab === 'feed'
                    ? 'border-accent text-white'
                    : 'border-transparent text-[#777]'
                }`}
              >
                Event Feed
              </button>
            </div>

            {alertTab === 'alerts' ? (
              <>
                {alerts.length > 0 ? (
                  <div className="space-y-2 max-h-48 overflow-y-auto">
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
                      <div className="space-y-2 max-h-48 overflow-y-auto">
                        {highSeverityEnv.map((ev, i) => {
                          const sevStyle = ev.severity === 'immediate'
                            ? { bg: 'bg-red-500/5', border: 'border-red-500', icon: AlertCircle, iconColor: 'text-red-500' }
                            : { bg: 'bg-accent/5', border: 'border-accent', icon: AlertTriangle, iconColor: 'text-accent' }
                          const Icon = sevStyle.icon
                          return (
                            <div key={ev.event_id || i} className={`p-3 ${sevStyle.bg} border-l-2 ${sevStyle.border} flex items-start gap-3`}>
                              <Icon size={16} className={sevStyle.iconColor} />
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2">
                                  <span className="px-1.5 py-0.5 text-[10px] font-sans uppercase tracking-wide bg-[#1e1e1e] text-[#777] border border-[#222]">ENV</span>
                                  <span className="text-[10px] font-sans text-[#666]">{ev.severity}</span>
                                </div>
                                <div className="text-sm font-sans font-medium text-white mt-1">{ev.headline}</div>
                                <div className="text-[10px] font-mono text-[#666] mt-1">{ev.source} · {new Date(ev.fetched_at * 1000).toLocaleTimeString()}</div>
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    )
                  }
                  return (
                    <div className="flex items-center gap-2 text-[#777] py-4">
                      <CheckCircle size={16} className="text-green-500" />
                      <span className="font-sans">No active alerts</span>
                    </div>
                  )
                })()}
              </>
            ) : (
              <LiveEventFeed events={envEvents} envStatus={envStatus} embedded />
            )}
          </div>

          {/* Quick Stats */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatCard icon={Radio} label="Nodes Online" value={health?.total_nodes || 0} accent="#22c55e" subvalue={`${health?.unlocated_count || 0} unlocated`} />
            <StatCard icon={Cpu} label="Infrastructure" value={`${health?.infra_online || 0}/${health?.infra_total || 0}`} accent="#38bdf8" subvalue={health?.infra_online === health?.infra_total ? 'All online' : 'Some offline'} />
            <StatCard icon={Activity} label="Utilization" value={`${health?.util_percent?.toFixed(1) || 0}%`} accent="#f59e0b" subvalue={`${health?.flagged_nodes || 0} flagged`} />
            <StatCard icon={MapPin} label="Regions" value={health?.total_regions || 0} accent="#333333" subvalue={`${health?.battery_warnings || 0} battery warnings`} />
          </div>
        </div>
      </div>

      {/* Middle row: Sources + RF Propagation + Tropo */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Mesh Sources */}
        <div className="bg-bg-card border border-border p-4">
          <h2 className="text-[10px] font-sans uppercase tracking-widest text-[#666] mb-3">Mesh Sources (<span className="font-mono">{sources.length}</span>)</h2>
          {sources.length > 0 ? (
            <div className="space-y-1">
              {sources.map((source, i) => (
                <SourceCard key={i} source={source} />
              ))}
            </div>
          ) : (
            <div className="font-sans text-[#666] py-4">No sources configured</div>
          )}
        </div>

        {/* RF Propagation */}
        <BandConditionsCard bandConditions={bandConditions} />

        {/* Tropo Forecast */}
        <HepburnTropoCard />
      </div>

    </div>
  )
}
