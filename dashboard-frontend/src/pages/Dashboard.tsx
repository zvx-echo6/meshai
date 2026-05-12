import { useEffect, useState } from 'react'
import {
  fetchHealth,
  fetchSources,
  fetchAlerts,
  fetchEnvStatus,
  fetchRFPropagation,
  type MeshHealth,
  type SourceHealth,
  type Alert,
  type EnvStatus,
  type RFPropagation,
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
} from 'lucide-react'

function HealthGauge({ health }: { health: MeshHealth }) {
  const score = health.score
  const tier = health.tier

  // Color based on score
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
        {/* Background circle */}
        <circle
          cx="50"
          cy="50"
          r="45"
          fill="none"
          stroke="#1e2a3a"
          strokeWidth="8"
        />
        {/* Progress arc */}
        <circle
          cx="50"
          cy="50"
          r="45"
          fill="none"
          stroke={color}
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference - progress}
          transform="rotate(-90 50 50)"
          className="transition-all duration-500"
        />
        {/* Score text */}
        <text
          x="50"
          y="46"
          textAnchor="middle"
          className="fill-slate-100 font-mono text-2xl font-bold"
          style={{ fontSize: '24px' }}
        >
          {score.toFixed(1)}
        </text>
        <text
          x="50"
          y="62"
          textAnchor="middle"
          className="fill-slate-400 text-xs"
          style={{ fontSize: '10px' }}
        >
          {tier}
        </text>
      </svg>
    </div>
  )
}

function PillarBar({
  label,
  value,
}: {
  label: string
  value: number
}) {
  const getColor = (v: number) => {
    if (v >= 80) return 'bg-green-500'
    if (v >= 60) return 'bg-amber-500'
    return 'bg-red-500'
  }

  return (
    <div className="flex items-center gap-3">
      <div className="w-24 text-xs text-slate-400 truncate">{label}</div>
      <div className="flex-1 h-2 bg-border rounded-full overflow-hidden">
        <div
          className={`h-full ${getColor(value)} transition-all duration-300`}
          style={{ width: `${value}%` }}
        />
      </div>
      <div className="w-12 text-right text-xs font-mono text-slate-300">
        {value.toFixed(1)}
      </div>
    </div>
  )
}

function AlertItem({ alert }: { alert: Alert }) {
  const getSeverityStyles = (severity: string) => {
    switch (severity.toLowerCase()) {
      case 'critical':
      case 'emergency':
        return {
          bg: 'bg-red-500/10',
          border: 'border-red-500',
          icon: AlertCircle,
          iconColor: 'text-red-500',
        }
      case 'warning':
        return {
          bg: 'bg-amber-500/10',
          border: 'border-amber-500',
          icon: AlertTriangle,
          iconColor: 'text-amber-500',
        }
      default:
        return {
          bg: 'bg-green-500/10',
          border: 'border-green-500',
          icon: Info,
          iconColor: 'text-green-500',
        }
    }
  }

  const styles = getSeverityStyles(alert.severity)
  const Icon = styles.icon

  return (
    <div
      className={`p-3 rounded-lg ${styles.bg} border-l-2 ${styles.border} flex items-start gap-3`}
    >
      <Icon size={16} className={styles.iconColor} />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-200">{alert.message}</div>
        <div className="text-xs text-slate-500 mt-1">
          {alert.timestamp || 'Just now'}
        </div>
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
        <div className="text-xs text-slate-500">
          {source.node_count} nodes * {source.type}
        </div>
      </div>
    </div>
  )
}

function StatCard({
  icon: Icon,
  label,
  value,
  subvalue,
}: {
  icon: typeof Radio
  label: string
  value: string | number
  subvalue?: string
}) {
  return (
    <div className="bg-bg-card border border-border rounded-lg p-4">
      <div className="flex items-center gap-2 text-slate-400 mb-2">
        <Icon size={14} />
        <span className="text-xs">{label}</span>
      </div>
      <div className="font-mono text-xl text-slate-100">{value}</div>
      {subvalue && (
        <div className="text-xs text-slate-500 mt-1">{subvalue}</div>
      )}
    </div>
  )
}

function RFPropagationCard({ propagation }: { propagation: RFPropagation | null }) {
  if (!propagation) {
    return (
      <div className="bg-bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4">
          RF Propagation
        </h2>
        <div className="text-slate-500">
          <p>Loading propagation data...</p>
        </div>
      </div>
    )
  }

  const hf = propagation.hf
  const ducting = propagation.uhf_ducting

  const getAssessmentColor = (assessment?: string) => {
    if (!assessment) return 'text-slate-400'
    switch (assessment.toLowerCase()) {
      case 'excellent':
        return 'text-green-400'
      case 'good':
        return 'text-green-500'
      case 'fair':
        return 'text-amber-500'
      case 'poor':
        return 'text-red-500'
      default:
        return 'text-slate-400'
    }
  }

  const getDuctingColor = (condition?: string) => {
    if (!condition) return 'text-slate-400'
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

  const hasHF = hf && (hf.band_assessment || hf.sfi || hf.kp_current !== undefined)
  const hasDucting = ducting && ducting.condition

  return (
    <div className="bg-bg-card border border-border rounded-lg p-6">
      <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
        <Zap size={14} />
        RF Propagation
      </h2>

      {/* HF Section */}
      <div className="mb-4">
        <div className="text-xs text-slate-500 mb-1">HF Bands</div>
        {hasHF ? (
          <div className="space-y-1">
            <div className={`text-sm font-medium ${getAssessmentColor(hf.band_assessment)}`}>
              {hf.band_assessment || 'Unknown'}
            </div>
            <div className="text-xs text-slate-400">
              SFI {hf.sfi?.toFixed(0) || '?'} / Kp {hf.kp_current?.toFixed(1) || '?'}
            </div>
            {hf.r_scale !== undefined && hf.r_scale > 0 && (
              <div className="text-xs text-amber-500">
                R{hf.r_scale} Radio Blackout
              </div>
            )}
          </div>
        ) : (
          <div className="text-sm text-slate-500">No HF data</div>
        )}
      </div>

      {/* UHF Ducting Section */}
      <div>
        <div className="text-xs text-slate-500 mb-1">UHF 906 MHz</div>
        {hasDucting ? (
          <div className="space-y-1">
            <div className={`text-sm font-medium ${getDuctingColor(ducting.condition)}`}>
              {ducting.condition === 'normal'
                ? 'Normal'
                : ducting.condition?.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase())}
            </div>
            {ducting.condition !== 'normal' && ducting.min_gradient !== undefined && (
              <div className="text-xs text-slate-400">
                dM/dz: {ducting.min_gradient} M-units/km
              </div>
            )}
            {ducting.condition !== 'normal' && (
              <div className="text-xs text-blue-400">
                Extended range likely
              </div>
            )}
          </div>
        ) : (
          <div className="text-sm text-slate-500">No ducting data</div>
        )}
      </div>
    </div>
  )
}

export default function Dashboard() {
  const [health, setHealth] = useState<MeshHealth | null>(null)
  const [sources, setSources] = useState<SourceHealth[]>([])
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [envStatus, setEnvStatus] = useState<EnvStatus | null>(null)
  const [rfProp, setRFProp] = useState<RFPropagation | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const { lastHealth } = useWebSocket()

  useEffect(() => {
    Promise.all([
      fetchHealth(),
      fetchSources(),
      fetchAlerts(),
      fetchEnvStatus(),
      fetchRFPropagation().catch(() => null),
    ])
      .then(([h, src, a, e, rf]) => {
        setHealth(h)
        setSources(src)
        setAlerts(a)
        setEnvStatus(e)
        setRFProp(rf)
        setLoading(false)
      })
      .catch((err) => {
        setError(err.message)
        setLoading(false)
      })
  }, [])

  // Update health from WebSocket
  useEffect(() => {
    if (lastHealth) {
      setHealth(lastHealth)
    }
  }, [lastHealth])

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
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Mesh Health */}
      <div className="bg-bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4">Mesh Health</h2>
        {health && (
          <>
            <HealthGauge health={health} />
            <div className="mt-6 space-y-3">
              <PillarBar label="Infrastructure" value={health.pillars.infrastructure} />
              <PillarBar label="Utilization" value={health.pillars.utilization} />
              <PillarBar label="Behavior" value={health.pillars.behavior} />
              <PillarBar label="Power" value={health.pillars.power} />
            </div>
          </>
        )}
      </div>

      {/* Alerts + Stats */}
      <div className="lg:col-span-2 space-y-6">
        {/* Active Alerts */}
        <div className="bg-bg-card border border-border rounded-lg p-6">
          <h2 className="text-sm font-medium text-slate-400 mb-4">
            Active Alerts
          </h2>
          {alerts.length > 0 ? (
            <div className="space-y-3">
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
          <StatCard
            icon={Radio}
            label="Nodes Online"
            value={health?.total_nodes || 0}
            subvalue={`${health?.unlocated_count || 0} unlocated`}
          />
          <StatCard
            icon={Cpu}
            label="Infrastructure"
            value={`${health?.infra_online || 0}/${health?.infra_total || 0}`}
            subvalue={
              health?.infra_online === health?.infra_total
                ? 'All online'
                : 'Some offline'
            }
          />
          <StatCard
            icon={Activity}
            label="Utilization"
            value={`${health?.util_percent?.toFixed(1) || 0}%`}
            subvalue={`${health?.flagged_nodes || 0} flagged`}
          />
          <StatCard
            icon={MapPin}
            label="Regions"
            value={health?.total_regions || 0}
            subvalue={`${health?.battery_warnings || 0} battery warnings`}
          />
        </div>
      </div>

      {/* Mesh Sources */}
      <div className="bg-bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4">
          Mesh Sources ({sources.length})
        </h2>
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

      {/* Environmental Feeds */}
      <div className="bg-bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4">
          Environmental Feeds
        </h2>
        {envStatus?.enabled ? (
          <div className="text-slate-400">
            {envStatus.feeds.length} feeds active
          </div>
        ) : (
          <div className="text-slate-500">
            <p>Environmental feeds not enabled.</p>
            <p className="text-xs mt-2">
              Enable in config.yaml
            </p>
          </div>
        )}
      </div>

      {/* RF Propagation */}
      <RFPropagationCard propagation={rfProp} />
    </div>
  )
}
