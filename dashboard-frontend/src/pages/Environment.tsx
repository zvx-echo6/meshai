import { useEffect, useState } from 'react'
import {
  Cloud,
  Sun,
  AlertTriangle,
  AlertCircle,
  Info,
  CheckCircle,
  Activity,
  Wind,
  Flame,
  Mountain,
  Droplets,
  Car,
  Satellite,
} from 'lucide-react'
import {
  fetchEnvStatus,
  fetchEnvActive,
  fetchSWPC,
  fetchDucting,
  fetchFires,
  fetchAvalanche,
  fetchStreams,
  fetchTraffic,
  fetchRoads,
  fetchHotspots,
  type EnvStatus,
  type EnvEvent,
  type SWPCStatus,
  type DuctingStatus,
  type FireEvent,
  type AvalancheResponse,
  type StreamGaugeEvent,
  type TrafficEvent,
  type RoadEvent,
  type HotspotEvent,

} from '@/lib/api'

function FeedStatusCard({ feed }: { feed: { source: string; is_loaded: boolean; last_error: string | null; consecutive_errors: number; event_count: number; last_fetch: number } }) {
  const getStatusColor = () => {
    if (!feed.is_loaded) return 'bg-red-500'
    if (feed.consecutive_errors > 0) return 'bg-amber-500'
    return 'bg-green-500'
  }

  const getStatusText = () => {
    if (!feed.is_loaded) return 'Not loaded'
    if (feed.consecutive_errors > 0) return `${feed.consecutive_errors} errors`
    return 'Healthy'
  }

  const formatLastFetch = (ts: number) => {
    if (!ts) return 'Never'
    const date = new Date(ts * 1000)
    return date.toLocaleTimeString()
  }

  return (
    <div className="bg-bg-hover rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className={`w-2 h-2 rounded-full ${getStatusColor()}`} />
          <span className="text-sm font-medium text-slate-200 uppercase">
            {feed.source}
          </span>
        </div>
        <span className="text-xs text-slate-400">{getStatusText()}</span>
      </div>
      <div className="text-xs text-slate-500 space-y-1">
        <div>Events: {feed.event_count}</div>
        <div>Last fetch: {formatLastFetch(feed.last_fetch)}</div>
        {feed.last_error && (
          <div className="text-amber-500 truncate">{feed.last_error}</div>
        )}
      </div>
    </div>
  )
}

function AlertEventCard({ event }: { event: EnvEvent }) {
  const getSeverityStyles = (severity: string) => {
    switch (severity.toLowerCase()) {
      case 'extreme':
      case 'severe':
        return {
          bg: 'bg-red-500/10',
          border: 'border-red-500',
          icon: AlertCircle,
          iconColor: 'text-red-500',
        }
      case 'moderate':
      case 'warning':
        return {
          bg: 'bg-amber-500/10',
          border: 'border-amber-500',
          icon: AlertTriangle,
          iconColor: 'text-amber-500',
        }
      case 'minor':
        return {
          bg: 'bg-yellow-500/10',
          border: 'border-yellow-500',
          icon: Info,
          iconColor: 'text-yellow-500',
        }
      default:
        return {
          bg: 'bg-slate-500/10',
          border: 'border-slate-500',
          icon: Info,
          iconColor: 'text-slate-400',
        }
    }
  }

  const styles = getSeverityStyles(event.severity)
  const Icon = styles.icon

  const formatExpires = (ts?: number) => {
    if (!ts) return null
    const date = new Date(ts * 1000)
    return date.toLocaleString()
  }

  return (
    <div className={`p-4 rounded-lg ${styles.bg} border-l-2 ${styles.border}`}>
      <div className="flex items-start gap-3">
        <Icon size={18} className={styles.iconColor} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-medium text-slate-200">
              {event.event_type}
            </span>
            <span className={`text-xs px-1.5 py-0.5 rounded ${styles.bg} ${styles.iconColor}`}>
              {event.severity}
            </span>
          </div>
          <div className="text-sm text-slate-300 mb-2">{event.headline}</div>
          {event.description && (
            <div className="text-xs text-slate-400 mb-2 line-clamp-2">
              {event.description}
            </div>
          )}
          <div className="flex items-center gap-4 text-xs text-slate-500">
            <span className="uppercase">{event.source}</span>
            {event.expires && (
              <span>Expires: {formatExpires(event.expires)}</span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function SolarIndicesPanel({ swpc }: { swpc: SWPCStatus | null }) {
  if (!swpc || !swpc.enabled) {
    return (
      <div className="bg-bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
          <Sun size={14} />
          Solar/Geomagnetic Indices
        </h2>
        <div className="text-slate-500">Data not available</div>
      </div>
    )
  }

  const getKpColor = (kp?: number) => {
    if (kp === undefined) return 'text-slate-400'
    if (kp <= 2) return 'text-green-500'
    if (kp <= 4) return 'text-amber-500'
    if (kp <= 6) return 'text-orange-500'
    return 'text-red-500'
  }

  const getScaleColor = (scale?: number) => {
    if (scale === undefined || scale === 0) return 'text-green-500'
    if (scale <= 2) return 'text-amber-500'
    if (scale <= 3) return 'text-orange-500'
    return 'text-red-500'
  }

  return (
    <div className="bg-bg-card border border-border rounded-lg p-6">
      <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
        <Sun size={14} />
        Solar/Geomagnetic Indices
      </h2>

      <div className="grid grid-cols-2 gap-4 mb-4">
        {/* SFI */}
        <div className="bg-bg-hover rounded-lg p-3">
          <div className="text-xs text-slate-500 mb-1">Solar Flux Index</div>
          <div className="text-2xl font-mono text-slate-100">
            {swpc.sfi?.toFixed(0) ?? '—'}
          </div>
          <div className="text-xs text-slate-500">SFI (10.7 cm)</div>
        </div>

        {/* Kp */}
        <div className="bg-bg-hover rounded-lg p-3">
          <div className="text-xs text-slate-500 mb-1">Planetary K-Index</div>
          <div className={`text-2xl font-mono ${getKpColor(swpc.kp_current)}`}>
            {swpc.kp_current?.toFixed(1) ?? '—'}
          </div>
          <div className="text-xs text-slate-500">Kp</div>
        </div>
      </div>

      {/* NOAA Scales */}
      <div className="bg-bg-hover rounded-lg p-3 mb-4">
        <div className="text-xs text-slate-500 mb-2">NOAA Space Weather Scales</div>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1">
            <span className="text-xs text-slate-400">R:</span>
            <span className={`text-sm font-mono ${getScaleColor(swpc.r_scale)}`}>
              {swpc.r_scale ?? 0}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <span className="text-xs text-slate-400">S:</span>
            <span className={`text-sm font-mono ${getScaleColor(swpc.s_scale)}`}>
              {swpc.s_scale ?? 0}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <span className="text-xs text-slate-400">G:</span>
            <span className={`text-sm font-mono ${getScaleColor(swpc.g_scale)}`}>
              {swpc.g_scale ?? 0}
            </span>
          </div>
        </div>
        <div className="text-xs text-slate-500 mt-2">
          Radio Blackout / Solar Radiation / Geomagnetic Storm
        </div>
      </div>

      {/* Active Warnings */}
      {swpc.active_warnings && swpc.active_warnings.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs text-slate-500">Active Warnings</div>
          {swpc.active_warnings.slice(0, 3).map((warning, i) => (
            <div
              key={i}
              className="text-xs text-amber-400 bg-amber-500/10 rounded p-2"
            >
              {warning}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function DuctingPanel({ ducting }: { ducting: DuctingStatus | null }) {
  if (!ducting || !ducting.enabled) {
    return (
      <div className="bg-bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
          <Wind size={14} />
          Tropospheric Ducting
        </h2>
        <div className="text-slate-500">Data not available</div>
      </div>
    )
  }

  const getConditionColor = (condition?: string) => {
    switch (condition) {
      case 'normal':
        return 'text-green-500'
      case 'super_refraction':
        return 'text-amber-500'
      case 'surface_duct':
      case 'elevated_duct':
        return 'text-blue-400'
      default:
        return 'text-slate-400'
    }
  }

  const formatCondition = (condition?: string) => {
    if (!condition) return 'Unknown'
    return condition.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase())
  }

  return (
    <div className="bg-bg-card border border-border rounded-lg p-6">
      <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
        <Wind size={14} />
        Tropospheric Ducting
      </h2>

      {/* Condition */}
      <div className="bg-bg-hover rounded-lg p-4 mb-4">
        <div className="text-xs text-slate-500 mb-1">Condition</div>
        <div className={`text-xl font-medium ${getConditionColor(ducting.condition)}`}>
          {formatCondition(ducting.condition)}
        </div>
      </div>

      {/* Refractivity Gradient */}
      <div className="grid grid-cols-2 gap-4 mb-4">
        <div className="bg-bg-hover rounded-lg p-3">
          <div className="text-xs text-slate-500 mb-1">Min Gradient</div>
          <div className="text-lg font-mono text-slate-100">
            {ducting.min_gradient ?? '—'}
          </div>
          <div className="text-xs text-slate-500">M-units/km</div>
        </div>

        {ducting.duct_thickness_m && (
          <div className="bg-bg-hover rounded-lg p-3">
            <div className="text-xs text-slate-500 mb-1">Duct Thickness</div>
            <div className="text-lg font-mono text-slate-100">
              {ducting.duct_thickness_m}
            </div>
            <div className="text-xs text-slate-500">meters</div>
          </div>
        )}

        {ducting.duct_base_m && (
          <div className="bg-bg-hover rounded-lg p-3">
            <div className="text-xs text-slate-500 mb-1">Duct Base</div>
            <div className="text-lg font-mono text-slate-100">
              {ducting.duct_base_m}
            </div>
            <div className="text-xs text-slate-500">meters AGL</div>
          </div>
        )}
      </div>

      {/* Reference */}
      <div className="text-xs text-slate-500 bg-bg-hover rounded p-2">
        <div>dM/dz reference:</div>
        <div className="mt-1 space-y-0.5">
          <div>&gt;79: Normal propagation</div>
          <div>0–79: Super-refraction</div>
          <div>&lt;0: Ducting (trapping layer)</div>
        </div>
      </div>

      {ducting.last_update && (
        <div className="text-xs text-slate-500 mt-3">
          Last update: {ducting.last_update}
        </div>
      )}
    </div>
  )
}

export default function Environment() {
  const [envStatus, setEnvStatus] = useState<EnvStatus | null>(null)
  const [events, setEvents] = useState<EnvEvent[]>([])
  const [swpc, setSWPC] = useState<SWPCStatus | null>(null)
  const [ducting, setDucting] = useState<DuctingStatus | null>(null)
  const [fires, setFires] = useState<FireEvent[]>([])
  const [avalanche, setAvalanche] = useState<AvalancheResponse | null>(null)
  const [streams, setStreams] = useState<StreamGaugeEvent[]>([])
  const [traffic, setTraffic] = useState<TrafficEvent[]>([])
  const [roads, setRoads] = useState<RoadEvent[]>([])
  const [hotspots, setHotspots] = useState<HotspotEvent[]>([])
  const [newIgnitions, setNewIgnitions] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([
      fetchEnvStatus().catch(() => null),
      fetchEnvActive().catch(() => []),
      fetchSWPC().catch(() => null),
      fetchDucting().catch(() => null),
      fetchFires().catch(() => []),
      fetchAvalanche().catch(() => null),
      fetchStreams().catch(() => []),
      fetchTraffic().catch(() => []),
      fetchRoads().catch(() => []),
      fetchHotspots().catch(() => ({ hotspots: [], new_ignitions: 0 })),
    ])
      .then(([status, active, swpcData, ductingData, firesData, avyData, streamsData, trafficData, roadsData, hotspotsData]) => {
        setEnvStatus(status)
        setEvents(active)
        setSWPC(swpcData)
        setDucting(ductingData)
        setFires(firesData)
        setAvalanche(avyData)
        setStreams(streamsData || [])
        setTraffic(trafficData || [])
        setRoads(roadsData || [])
        setHotspots(hotspotsData?.hotspots || [])
        setNewIgnitions(hotspotsData?.new_ignitions || 0)
        setLoading(false)
      })
      .catch((err) => {
        setError(err.message)
        setLoading(false)
      })
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading environmental data...</div>
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

  if (!envStatus?.enabled) {
    return (
      <div className="flex flex-col items-center justify-center h-[60vh] text-center">
        <div className="w-16 h-16 rounded-full bg-bg-card border border-border flex items-center justify-center mb-6">
          <Cloud size={32} className="text-slate-500" />
        </div>
        <h2 className="text-xl font-semibold text-slate-300 mb-2">
          Environmental Feeds Disabled
        </h2>
        <p className="text-slate-500 max-w-md">
          Enable environmental feeds in config.yaml to see weather alerts,
          space weather indices, and tropospheric ducting data.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-200">Environment</h1>
        <div className="text-xs text-slate-500">
          {events.length} active event{events.length !== 1 ? 's' : ''}
        </div>
      </div>

      {/* Feed Status */}
      <div className="bg-bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
          <Activity size={14} />
          Feed Status
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {envStatus.feeds.map((feed) => (
            <FeedStatusCard key={feed.source} feed={feed} />
          ))}
        </div>
      </div>

      {/* Main content grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Solar Indices */}
        <SolarIndicesPanel swpc={swpc} />

        {/* Tropospheric Ducting */}
        <DuctingPanel ducting={ducting} />
      </div>

      {/* Fires and Avalanche */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Wildfires */}
        <div className="bg-bg-card border border-border rounded-lg p-6">
          <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
            <Flame size={14} />
            Active Wildfires ({fires.length})
          </h2>
          {fires.length > 0 ? (
            <div className="space-y-3">
              {fires.map((fire) => (
                <div
                  key={fire.event_id}
                  className={`p-3 rounded-lg ${
                    fire.severity === 'warning'
                      ? 'bg-red-500/10 border-l-2 border-red-500'
                      : fire.severity === 'watch'
                      ? 'bg-amber-500/10 border-l-2 border-amber-500'
                      : 'bg-slate-500/10 border-l-2 border-slate-500'
                  }`}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-sm font-medium text-slate-200">
                      {fire.name}
                    </span>
                    <span className={`text-xs px-1.5 py-0.5 rounded ${
                      fire.severity === 'warning'
                        ? 'bg-red-500/20 text-red-400'
                        : fire.severity === 'watch'
                        ? 'bg-amber-500/20 text-amber-400'
                        : 'bg-slate-500/20 text-slate-400'
                    }`}>
                      {fire.severity}
                    </span>
                  </div>
                  <div className="text-xs text-slate-400 space-y-1">
                    <div>{fire.acres.toLocaleString()} acres, {fire.pct_contained}% contained</div>
                    {fire.distance_km && fire.nearest_anchor && (
                      <div>{Math.round(fire.distance_km)} km from {fire.nearest_anchor}</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="flex items-center gap-2 text-slate-500 py-4">
              <CheckCircle size={16} className="text-green-500" />
              <span>No active wildfires in the area</span>
            </div>
          )}
        </div>

        {/* Avalanche */}
        <div className="bg-bg-card border border-border rounded-lg p-6">
          <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
            <Mountain size={14} />
            Avalanche Advisories
          </h2>
          {avalanche?.off_season ? (
            <div className="text-slate-500 py-4">
              <p>Off season - check back in December</p>
            </div>
          ) : avalanche && avalanche.advisories.length > 0 ? (
            <div className="space-y-3">
              {avalanche.advisories.map((avy) => (
                <div
                  key={avy.event_id}
                  className={`p-3 rounded-lg ${
                    avy.danger_level >= 4
                      ? 'bg-red-500/10 border-l-2 border-red-500'
                      : avy.danger_level >= 3
                      ? 'bg-amber-500/10 border-l-2 border-amber-500'
                      : avy.danger_level >= 2
                      ? 'bg-yellow-500/10 border-l-2 border-yellow-500'
                      : 'bg-green-500/10 border-l-2 border-green-500'
                  }`}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-sm font-medium text-slate-200">
                      {avy.zone_name}
                    </span>
                    <span className={`text-xs px-1.5 py-0.5 rounded ${
                      avy.danger_level >= 4
                        ? 'bg-red-500/20 text-red-400'
                        : avy.danger_level >= 3
                        ? 'bg-amber-500/20 text-amber-400'
                        : avy.danger_level >= 2
                        ? 'bg-yellow-500/20 text-yellow-400'
                        : 'bg-green-500/20 text-green-400'
                    }`}>
                      {avy.danger_name}
                    </span>
                  </div>
                  <div className="text-xs text-slate-400">
                    {avy.center}
                  </div>
                  {avy.travel_advice && (
                    <div className="text-xs text-slate-500 mt-2 line-clamp-2">
                      {avy.travel_advice}
                    </div>
                  )}
                </div>
              ))}
              {avalanche.advisories[0]?.center_link && (
                <a
                  href={avalanche.advisories[0].center_link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-400 hover:underline"
                >
                  View full forecast
                </a>
              )}
            </div>
          ) : (
            <div className="flex items-center gap-2 text-slate-500 py-4">
              <CheckCircle size={16} className="text-green-500" />
              <span>No avalanche advisories</span>
            </div>
          )}
        </div>
      </div>

      {/* Stream Gauges */}
      {streams.length > 0 && (
        <div className="bg-bg-card border border-border rounded-lg p-6">
          <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
            <Droplets size={14} />
            Stream Gauges ({streams.length})
          </h2>
          <div className="space-y-2">
            {streams.map((stream) => (
              <div
                key={stream.event_id}
                className={`p-3 rounded-lg ${
                  stream.severity === 'warning'
                    ? 'bg-amber-500/10 border-l-2 border-amber-500'
                    : 'bg-blue-500/10 border-l-2 border-blue-500'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm text-slate-200">
                    {stream.properties?.site_name || 'Unknown Site'}
                  </span>
                  <span className="text-sm font-mono text-slate-300">
                    {stream.properties?.value?.toLocaleString()} {stream.properties?.unit}
                  </span>
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  {stream.properties?.parameter}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Road Conditions */}
      {(traffic.length > 0 || roads.length > 0) && (
        <div className="bg-bg-card border border-border rounded-lg p-6">
          <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
            <Car size={14} />
            Road Conditions
          </h2>
          {traffic.length > 0 && (
            <div className="mb-4">
              <div className="text-xs text-slate-500 mb-2 uppercase">Traffic Flow</div>
              <div className="space-y-2">
                {traffic.map((t) => (
                  <div
                    key={t.event_id}
                    className={`p-3 rounded-lg ${
                      t.properties?.roadClosure
                        ? 'bg-red-500/10 border-l-2 border-red-500'
                        : t.properties?.speedRatio < 0.5
                        ? 'bg-amber-500/10 border-l-2 border-amber-500'
                        : t.properties?.speedRatio < 0.8
                        ? 'bg-yellow-500/10 border-l-2 border-yellow-500'
                        : 'bg-green-500/10 border-l-2 border-green-500'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-sm text-slate-200">
                        {t.properties?.corridor || 'Unknown'}
                      </span>
                      <span className="text-sm font-mono text-slate-300">
                        {t.properties?.roadClosure ? 'CLOSED' : `${Math.round(t.properties?.currentSpeed || 0)}mph`}
                      </span>
                    </div>
                    {!t.properties?.roadClosure && (
                      <div className="text-xs text-slate-500 mt-1">
                        {Math.round((t.properties?.speedRatio || 1) * 100)}% of free flow ({Math.round(t.properties?.freeFlowSpeed || 0)}mph)
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
          {roads.length > 0 && (
            <div>
              <div className="text-xs text-slate-500 mb-2 uppercase">Road Events</div>
              <div className="space-y-2">
                {roads.map((r) => (
                  <div
                    key={r.event_id}
                    className={`p-3 rounded-lg ${
                      r.properties?.is_closure
                        ? 'bg-red-500/10 border-l-2 border-red-500'
                        : 'bg-amber-500/10 border-l-2 border-amber-500'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      {r.properties?.is_closure && (
                        <span className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-400">
                          CLOSURE
                        </span>
                      )}
                      <span className="text-sm text-slate-200 line-clamp-1">
                        {r.headline}
                      </span>
                    </div>
                    <div className="text-xs text-slate-500 mt-1 uppercase">
                      {r.event_type}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Satellite Hotspots */}
      {hotspots.length > 0 && (
        <div className="bg-bg-card border border-border rounded-lg p-6">
          <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
            <Satellite size={14} />
            Satellite Hotspots ({hotspots.length})
            {newIgnitions > 0 && (
              <span className="ml-2 px-2 py-0.5 text-xs rounded-full bg-red-500/20 text-red-400 animate-pulse">
                {newIgnitions} NEW
              </span>
            )}
          </h2>
          <div className="space-y-2">
            {hotspots.map((h) => (
              <div
                key={h.event_id}
                className={`p-3 rounded-lg ${
                  h.properties?.new_ignition
                    ? 'bg-red-500/10 border-l-2 border-red-500'
                    : h.severity === 'watch'
                    ? 'bg-amber-500/10 border-l-2 border-amber-500'
                    : 'bg-orange-500/10 border-l-2 border-orange-500'
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {h.properties?.new_ignition && (
                      <span className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-400">
                        NEW
                      </span>
                    )}
                    <span className="text-sm text-slate-200">
                      {h.headline}
                    </span>
                  </div>
                  {h.properties?.frp && (
                    <span className="text-sm font-mono text-orange-400">
                      {Math.round(h.properties.frp)} MW
                    </span>
                  )}
                </div>
                <div className="text-xs text-slate-500 mt-1 flex items-center gap-3">
                  <span>Conf: {h.properties?.confidence || 'N/A'}</span>
                  {h.properties?.acq_time && <span>@{h.properties.acq_time}Z</span>}
                  {h.properties?.near_fire && (
                    <span>Near: {h.properties.near_fire}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Active Events */}
      <div className="bg-bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
          <AlertTriangle size={14} />
          Active Events ({events.length})
        </h2>
        {events.length > 0 ? (
          <div className="space-y-3">
            {events.map((event) => (
              <AlertEventCard key={event.event_id} event={event} />
            ))}
          </div>
        ) : (
          <div className="flex items-center gap-2 text-slate-500 py-4">
            <CheckCircle size={16} className="text-green-500" />
            <span>No active environmental events</span>
          </div>
        )}
      </div>
    </div>
  )
}
