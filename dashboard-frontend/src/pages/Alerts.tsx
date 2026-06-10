import { useEffect, useState, useCallback } from 'react'
import {
  Bell,
  AlertTriangle,
  AlertCircle,

  CheckCircle,
  Clock,
  Filter,
  ChevronLeft,
  ChevronRight,
  Radio,
  Zap,

  Cloud,
  Wifi,
  WifiOff,
  Battery,
  Users,
} from 'lucide-react'
import {
  fetchAlerts,
  fetchAlertHistory,
  fetchSubscriptions,
  type Alert,
  type AlertHistoryItem,
  type Subscription,
} from '@/lib/api'

interface Node {
  node_num: number
  node_id_hex: string
  short_name: string
  long_name: string
}
import { useWebSocket } from '@/hooks/useWebSocket'

// Alert type icons mapping
const alertTypeIcons: Record<string, typeof Bell> = {
  infra_offline: WifiOff,
  infra_recovery: Wifi,
  battery_warning: Battery,
  battery_critical: Battery,
  battery_emergency: Battery,
  hf_blackout: Zap,
  uhf_ducting: Radio,
  weather_warning: Cloud,
  weather_watch: Cloud,
  new_router: Radio,
  packet_flood: AlertTriangle,
  sustained_high_util: AlertTriangle,
  region_blackout: AlertCircle,
  default: Bell,
}

function getAlertIcon(type: string) {
  return alertTypeIcons[type] || alertTypeIcons.default
}

function getSeverityStyles(severity: string) {
  switch (severity?.toLowerCase()) {
    case 'immediate':
      return {
        bg: 'bg-red-500/10',
        border: 'border-red-500',
        badge: 'bg-red-500/20 text-red-400',
        iconColor: 'text-red-500',
      }
    case 'priority':
      return {
        bg: 'bg-amber-500/10',
        border: 'border-amber-500',
        badge: 'bg-amber-500/20 text-amber-400',
        iconColor: 'text-amber-500',
      }
    case 'routine':
    default:
      return {
        bg: 'bg-[#f59e0b]/10',
        border: 'border-[#f59e0b]',
        badge: 'bg-[#f59e0b]/20 text-[#f59e0b]',
        iconColor: 'text-[#f59e0b]',
      }
  }
}

function formatTimeAgo(timestamp: string | number): string {
  const date = typeof timestamp === 'number' ? new Date(timestamp * 1000) : new Date(timestamp)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffSec = Math.floor(diffMs / 1000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHour = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHour / 24)

  if (diffSec < 60) return 'Just now'
  if (diffMin < 60) return `${diffMin}m ago`
  if (diffHour < 24) return `${diffHour}h ago`
  return `${diffDay}d ago`
}

function formatDateTime(timestamp: string | number): string {
  const date = typeof timestamp === 'number' ? new Date(timestamp * 1000) : new Date(timestamp)
  return date.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
  return `${Math.floor(seconds / 86400)}d`
}

// Active Alert Card Component
function ActiveAlertCard({
  alert,
  onAcknowledge,
}: {
  alert: Alert
  onAcknowledge: (alert: Alert) => void
}) {
  const styles = getSeverityStyles(alert.severity)
  const Icon = getAlertIcon(alert.type)

  return (
    <div className={`p-4 ${styles.bg} border-l-4 ${styles.border}`}>
      <div className="flex items-start gap-3">
        <Icon size={20} className={styles.iconColor} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className={`text-xs px-2 py-0.5 rounded-full ${styles.badge}`}>
              {alert.severity?.toUpperCase()}
            </span>
            <span className="text-xs text-slate-500">{alert.type}</span>
          </div>
          <div className="text-sm text-slate-200">{alert.message}</div>
          <div className="flex items-center gap-4 mt-2 text-xs text-slate-500">
            <span className="flex items-center gap-1">
              <Clock size={12} />
              {alert.timestamp ? formatTimeAgo(alert.timestamp) : 'Just now'}
            </span>
            {alert.scope_value && (
              <span>{alert.scope_type}: {alert.scope_value}</span>
            )}
          </div>
        </div>
        <button
          onClick={() => onAcknowledge(alert)}
          className="px-3 py-1 text-xs text-slate-400 hover:text-slate-200 border border-border rounded hover:bg-bg-hover transition-colors"
        >
          Acknowledge
        </button>
      </div>
    </div>
  )
}

// Alert History Table Component
function AlertHistoryTable({
  history,
  typeFilter,
  severityFilter,
  onTypeFilterChange,
  onSeverityFilterChange,
  page,
  totalPages,
  onPageChange,
}: {
  history: AlertHistoryItem[]
  typeFilter: string
  severityFilter: string
  onTypeFilterChange: (v: string) => void
  onSeverityFilterChange: (v: string) => void
  page: number
  totalPages: number
  onPageChange: (p: number) => void
}) {
  const alertTypes = [
    'all',
    'infra_offline',
    'infra_recovery',
    'battery_warning',
    'battery_critical',
    'hf_blackout',
    'uhf_ducting',
    'weather_warning',
    'new_router',
    'packet_flood',
  ]

  const severities = ["all", "immediate", "priority", "routine"]

  return (
    <div className="bg-bg-card border border-border">
      {/* Filters */}
      <div className="p-4 border-b border-border flex items-center gap-4">
        <div className="flex items-center gap-2">
          <Filter size={14} className="text-slate-400" />
          <span className="text-sm text-slate-400">Filter:</span>
        </div>
        <select
          value={typeFilter}
          onChange={(e) => onTypeFilterChange(e.target.value)}
          className="bg-bg border border-border rounded px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-[#f59e0b]"
        >
          {alertTypes.map((t) => (
            <option key={t} value={t}>
              {t === 'all' ? 'All Types' : t.replace(/_/g, ' ')}
            </option>
          ))}
        </select>
        <select
          value={severityFilter}
          onChange={(e) => onSeverityFilterChange(e.target.value)}
          className="bg-bg border border-border rounded px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-[#f59e0b]"
        >
          {severities.map((s) => (
            <option key={s} value={s}>
              {s === 'all' ? 'All Severities' : s.charAt(0).toUpperCase() + s.slice(1)}
            </option>
          ))}
        </select>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border">
              <th className="text-left text-xs font-medium text-slate-400 p-4">Time</th>
              <th className="text-left text-xs font-medium text-slate-400 p-4">Type</th>
              <th className="text-left text-xs font-medium text-slate-400 p-4">Severity</th>
              <th className="text-left text-xs font-medium text-slate-400 p-4">Message</th>
              <th className="text-left text-xs font-medium text-slate-400 p-4">Duration</th>
            </tr>
          </thead>
          <tbody>
            {history.length > 0 ? (
              history.map((item, i) => {
                const styles = getSeverityStyles(item.severity)
                return (
                  <tr key={item.id || i} className="border-b border-border hover:bg-bg-hover">
                    <td className="p-4 text-sm text-slate-400 font-mono whitespace-nowrap">
                      {formatDateTime(item.timestamp)}
                    </td>
                    <td className="p-4 text-sm text-slate-300">
                      {item.type.replace(/_/g, ' ')}
                    </td>
                    <td className="p-4">
                      <span className={`text-xs px-2 py-0.5 rounded-full ${styles.badge}`}>
                        {item.severity}
                      </span>
                    </td>
                    <td className="p-4 text-sm text-slate-200 max-w-md truncate">
                      {item.message}
                    </td>
                    <td className="p-4 text-sm text-slate-400 font-mono">
                      {item.duration ? formatDuration(item.duration) : '-'}
                    </td>
                  </tr>
                )
              })
            ) : (
              <tr>
                <td colSpan={5} className="p-8 text-center text-slate-500">
                  No alert history available
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="p-4 border-t border-border flex items-center justify-between">
          <span className="text-sm text-slate-400">
            Page {page} of {totalPages}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1}
              className="p-2 text-slate-400 hover:text-slate-200 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <ChevronLeft size={16} />
            </button>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page >= totalPages}
              className="p-2 text-slate-400 hover:text-slate-200 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// Subscription Card Component
function SubscriptionCard({ subscription, nodes }: { subscription: Subscription; nodes: Node[] }) {
  const resolveNodeName = (userId: string): string => {
    const node = nodes.find(n =>
      n.node_id_hex === userId ||
      String(n.node_num) === userId ||
      n.short_name === userId
    )
    if (node) {
      return node.long_name && node.long_name !== node.short_name
        ? `${node.short_name} (${node.long_name})`
        : node.short_name
    }
    return userId
  }
  const formatSchedule = () => {
    if (subscription.sub_type === 'alerts') {
      return 'Real-time'
    }
    const time = subscription.schedule_time || '0000'
    const hours = parseInt(time.slice(0, 2))
    const minutes = time.slice(2)
    const period = hours >= 12 ? 'PM' : 'AM'
    const displayHour = hours % 12 || 12
    let schedule = `${displayHour}:${minutes} ${period}`
    if (subscription.sub_type === 'weekly' && subscription.schedule_day) {
      schedule += ` ${subscription.schedule_day.charAt(0).toUpperCase()}${subscription.schedule_day.slice(1)}`
    }
    return schedule
  }

  const getTypeIcon = () => {
    switch (subscription.sub_type) {
      case 'alerts':
        return Bell
      case 'daily':
        return Clock
      case 'weekly':
        return Clock
      default:
        return Bell
    }
  }

  const Icon = getTypeIcon()

  return (
    <div className="p-4 bg-bg-hover border border-border">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 bg-[#f59e0b]/10 flex items-center justify-center">
          <Icon size={18} className="text-[#f59e0b]" />
        </div>
        <div className="flex-1">
          <div className="text-sm text-slate-200 font-medium">
            {subscription.sub_type.charAt(0).toUpperCase() + subscription.sub_type.slice(1)}
            {subscription.scope_type !== 'mesh' && subscription.scope_value && (
              <span className="text-slate-400 font-normal ml-2">
                ({subscription.scope_type}: {subscription.scope_value})
              </span>
            )}
          </div>
          <div className="text-xs text-slate-500 mt-0.5">
            {formatSchedule()} • {resolveNodeName(subscription.user_id)}
          </div>
        </div>
        <div className={`w-2 h-2 rounded-full ${subscription.enabled ? 'bg-green-500' : 'bg-slate-500'}`} />
      </div>
    </div>
  )
}

export default function Alerts() {
  const [activeAlerts, setActiveAlerts] = useState<Alert[]>([])
  const [history, setHistory] = useState<AlertHistoryItem[]>([])
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([])
  const [nodes, setNodes] = useState<Node[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Filters and pagination
  const [typeFilter, setTypeFilter] = useState('all')
  const [severityFilter, setSeverityFilter] = useState('all')
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const pageSize = 20

  // Acknowledged alerts (local state only)
  const [acknowledged, setAcknowledged] = useState<Set<string>>(new Set())

  const { lastAlert } = useWebSocket()

  // Set page title
  useEffect(() => {
    document.title = 'Alerts — MeshAI'
  }, [])

  // Load data
  useEffect(() => {
    Promise.all([
      fetchAlerts().catch(() => []),
      fetchAlertHistory(pageSize, 0).catch(() => ({ items: [], total: 0 })),
      fetchSubscriptions().catch(() => []),
      fetch('/api/nodes').then(r => r.json()).catch(() => []),
    ])
      .then(([alerts, historyData, subs, nodeData]) => {
        setActiveAlerts(alerts)
        if (Array.isArray(historyData)) {
          setHistory(historyData)
          setTotalPages(1)
        } else {
          setHistory(historyData.items || [])
          setTotalPages(Math.ceil((historyData.total || 0) / pageSize))
        }
        setSubscriptions(subs)
        setNodes(nodeData)
        setLoading(false)
      })
      .catch((err) => {
        setError(err.message)
        setLoading(false)
      })
  }, [])

  // Handle new alerts from WebSocket
  useEffect(() => {
    if (lastAlert) {
      setActiveAlerts((prev) => {
        // Avoid duplicates
        const exists = prev.some(
          (a) => a.type === lastAlert.type && a.message === lastAlert.message
        )
        if (exists) return prev
        return [lastAlert, ...prev]
      })
    }
  }, [lastAlert])

  // Reload history when filters or page change
  useEffect(() => {
    const offset = (page - 1) * pageSize
    fetchAlertHistory(pageSize, offset, typeFilter, severityFilter)
      .then((data) => {
        if (Array.isArray(data)) {
          setHistory(data)
          setTotalPages(1)
        } else {
          setHistory(data.items || [])
          setTotalPages(Math.ceil((data.total || 0) / pageSize))
        }
      })
      .catch(() => {
        // Keep current data on error
      })
  }, [page, typeFilter, severityFilter])

  const handleAcknowledge = useCallback((alert: Alert) => {
    const key = `${alert.type}-${alert.message}-${alert.timestamp}`
    setAcknowledged((prev) => new Set([...prev, key]))
  }, [])

  // Filter out acknowledged alerts
  const visibleAlerts = activeAlerts.filter((alert) => {
    const key = `${alert.type}-${alert.message}-${alert.timestamp}`
    return !acknowledged.has(key)
  })

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading alerts...</div>
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
      {/* Active Alerts */}
      <div className="bg-bg-card border border-border p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
          <AlertTriangle size={14} />
          Active Alerts ({visibleAlerts.length})
        </h2>
        {visibleAlerts.length > 0 ? (
          <div className="space-y-3">
            {visibleAlerts.map((alert, i) => (
              <ActiveAlertCard
                key={`${alert.type}-${alert.timestamp}-${i}`}
                alert={alert}
                onAcknowledge={handleAcknowledge}
              />
            ))}
          </div>
        ) : (
          <div className="flex items-center gap-2 text-slate-500 py-8">
            <CheckCircle size={20} className="text-green-500" />
            <span>No active alerts — all systems nominal</span>
          </div>
        )}
      </div>

      {/* Alert History */}
      <div>
        <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
          <Clock size={14} />
          Alert History
        </h2>
        <AlertHistoryTable
          history={history}
          typeFilter={typeFilter}
          severityFilter={severityFilter}
          onTypeFilterChange={(v) => {
            setTypeFilter(v)
            setPage(1)
          }}
          onSeverityFilterChange={(v) => {
            setSeverityFilter(v)
            setPage(1)
          }}
          page={page}
          totalPages={totalPages}
          onPageChange={setPage}
        />
      </div>

      {/* Subscriptions */}
      <div className="bg-bg-card border border-border p-6">
        <h2 className="text-sm font-medium text-slate-400 mb-4 flex items-center gap-2">
          <Users size={14} />
          Mesh Subscriptions ({subscriptions.length})
        </h2>
        {subscriptions.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {subscriptions.map((sub) => (
              <SubscriptionCard key={sub.id} subscription={sub} nodes={nodes} />
            ))}
          </div>
        ) : (
          <div className="text-slate-500 py-4">
            <p>No active subscriptions.</p>
            <p className="text-xs mt-2">
              Manage subscriptions via <code className="text-[#f59e0b]">!subscribe</code> on mesh. Broadcasts arrive with one of three prefixes — <strong>New:</strong> (first sight), <strong>Update:</strong> (material change), or <strong>Active:</strong> (clock-driven reminder while the event is still live). See <a href="/reference#broadcast-types" className="text-[#f59e0b] hover:underline">Broadcast Types</a> and <a href="/reference#reminders" className="text-[#f59e0b] hover:underline">Reminder System</a> in Reference.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
