import { ReactNode, useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useDirty } from '@/context/DirtyContext'
import {
  LayoutDashboard,
  Radio,
  Cloud,
  Bell,
  BellRing,
  BookOpen,
  Sliders,
  Droplets,
  MapPin,
  Wifi,
  Layers,
  Network,
  Users,
  Bot,
  Settings,
  type LucideIcon,
} from 'lucide-react'
import { fetchStatus, type SystemStatus } from '@/lib/api'
import { useWebSocket } from '@/hooks/useWebSocket'
import { useToast } from './ToastProvider'
import RestartBanner from './RestartBanner'

interface LayoutProps {
  children: ReactNode
}

interface NavItem {
  path: string
  label: string
  icon: LucideIcon
}

interface NavGroup {
  header: string
  items: NavItem[]
}

// Top-level, ungrouped items (no header).
const topNavItems: NavItem[] = [
  { path: '/', label: 'Dashboard', icon: LayoutDashboard },
  { path: '/environment', label: 'Environment', icon: Cloud },
  { path: '/alerts', label: 'Alerts', icon: Bell },
  { path: '/reference', label: 'Reference', icon: BookOpen },
  { path: '/adapter-config', label: 'Adapter Config', icon: Sliders },
  { path: '/config', label: 'Config', icon: Settings },
  { path: '/gauge-sites', label: 'Gauge Sites', icon: Droplets },
  { path: '/town-anchors', label: 'Town Anchors', icon: MapPin },
]

// Grouped sections with labeled headers. Meshtastic "Connection" and "Sources"
// are focused standalone pages (/meshtastic/connection, /meshtastic/sources).
const navGroups: NavGroup[] = [
  {
    header: 'Meshtastic',
    items: [
      { path: '/meshtastic/connection', label: 'Connection', icon: Wifi },
      { path: '/notifications', label: 'Routing', icon: BellRing },
      { path: '/mesh', label: 'Mesh', icon: Radio },
      { path: '/meshtastic/sources', label: 'Sources', icon: Layers },
    ],
  },
  {
    header: 'MeshCore',
    items: [
      { path: '/meshcore/connection', label: 'Connection', icon: Network },
      { path: '/meshcore/routing', label: 'Routing', icon: BellRing },
      { path: '/meshcore/contacts', label: 'Contacts', icon: Users },
      { path: '/meshcore/companion', label: 'Companion', icon: Bot },
    ],
  },
]

// Flattened view of every nav item (top + all groups) for title lookup.
const allNavItems: NavItem[] = [
  ...topNavItems,
  ...navGroups.flatMap((g) => g.items),
]

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const mins = Math.floor((seconds % 3600) / 60)

  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

// Renders a single nav <Link>. Items whose path carries a ?section= query are
// matched against pathname+search so only the matching deep-link highlights.
// onNavClick: called with the target path; return true to allow navigation,
// false to block it (caller shows confirm dialog).
function renderNavItem(
  item: NavItem,
  pathname: string,
  search: string,
  onNavClick: (path: string, e: React.MouseEvent) => void,
) {
  const isActive = item.path.includes('?')
    ? `${pathname}${search}` === item.path
    : pathname === item.path
  const Icon = item.icon
  return (
    <Link
      key={item.path}
      to={item.path}
      onClick={(e) => onNavClick(item.path, e)}
      className={`flex items-center gap-3 px-5 py-3 text-sm font-sans transition-colors relative ${
        isActive
          ? 'text-white bg-transparent'
          : 'text-[#777] hover:text-white hover:bg-bg-hover'
      }`}
    >
      {isActive && (
        <div className="absolute right-0 top-0 bottom-0 w-[2px] bg-[#f59e0b]" />
      )}
      <Icon size={16} />
      {item.label}
    </Link>
  )
}

function getPageTitle(fullPath: string): string {
  // Exact match first (honors any ?section= query on deep-linked items).
  const exact = allNavItems.find((i) => i.path === fullPath)
  if (exact) return exact.label
  // Fallback: match by pathname only, ignoring query strings.
  const base = fullPath.split('?')[0]
  const byPath = allNavItems.find((i) => i.path.split('?')[0] === base)
  return byPath?.label || 'Dashboard'
}

export default function Layout({ children }: LayoutProps) {
  const location = useLocation()
  const navigate = useNavigate()
  const { dirty, setDirty } = useDirty()
  const { connected, lastAlert } = useWebSocket()
  const { addToast } = useToast()
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [lastAlertId, setLastAlertId] = useState<string | null>(null)

  const handleNavClick = (path: string, e: React.MouseEvent) => {
    if (dirty) {
      e.preventDefault()
      if (window.confirm('You have unsaved changes. Discard them?')) {
        setDirty(false)
        navigate(path)
      }
    }
  }

  // Trigger toast on new alerts
  useEffect(() => {
    if (lastAlert) {
      const alertId = `${lastAlert.type}-${lastAlert.message}-${lastAlert.timestamp}`
      if (alertId !== lastAlertId) {
        setLastAlertId(alertId)
        addToast(lastAlert)
      }
    }
  }, [lastAlert, lastAlertId, addToast])
  const [currentTime, setCurrentTime] = useState(new Date())

  useEffect(() => {
    fetchStatus().then(setStatus).catch(console.error)
    const interval = setInterval(() => {
      fetchStatus().then(setStatus).catch(console.error)
    }, 30000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    const interval = setInterval(() => setCurrentTime(new Date()), 1000)
    return () => clearInterval(interval)
  }, [])

  const timeStr = currentTime.toLocaleTimeString('en-US', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })

  return (
    <div className="flex h-screen overflow-hidden bg-bg text-white">
      {/* Sidebar */}
      <aside className="w-[220px] flex-shrink-0 bg-bg-card border-r border-border flex flex-col overflow-y-auto">
        {/* Logo */}
        <div className="bg-[#000000] px-4 py-3 border-b border-border flex flex-col items-center">
          <img
            src="/meshai-logo.png"
            alt="MeshAI"
            className="w-[190px] block"
          />
          <div className="font-mono text-[10px] text-[#555] mt-1 self-start">
            v{status?.version || '...'}
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 py-4">
          {topNavItems.map((item) => renderNavItem(item, location.pathname, location.search, handleNavClick))}
          {navGroups.map((group) => (
            <div key={group.header} className="mt-4">
              <div className="px-5 pt-2 pb-1 text-[10px] font-sans font-semibold uppercase tracking-wider text-[#555]">
                {group.header}
              </div>
              {group.items.map((item) => renderNavItem(item, location.pathname, location.search, handleNavClick))}
            </div>
          ))}
        </nav>

        {/* Connection status */}
        <div className="p-5 border-t border-border">
          <div className="flex items-center gap-2 mb-2">
            <div
              className={`w-2 h-2 rounded-full ${
                status?.connected ? 'bg-green-500' : 'bg-red-500'
              }`}
            />
            <span className="text-xs font-sans text-[#777]">
              {status?.connected ? 'Connected' : 'Disconnected'}
            </span>
          </div>
          <div className="text-xs font-mono text-[#666] truncate">
            {status?.connection_type?.toUpperCase()}: {status?.connection_target}
          </div>
          <div className="text-xs font-sans text-[#666] mt-1">
            Uptime: <span className="font-mono">{status ? formatUptime(status.uptime_seconds) : '...'}</span>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Header */}
        <header className="h-14 flex-shrink-0 border-b border-border bg-bg-card flex items-center justify-between px-6">
          <h1 className="text-lg font-sans font-semibold text-white">
            {getPageTitle(location.pathname + location.search)}
          </h1>
          <div className="flex items-center gap-6">
            {/* Live indicator */}
            <div className="flex items-center gap-2">
              <div
                className={`w-2 h-2 rounded-full ${
                  connected ? 'bg-accent animate-pulse-slow' : 'bg-[#333]'
                }`}
              />
              <span className="text-xs font-sans text-[#777]">
                {connected ? 'Live' : 'Offline'}
              </span>
            </div>
            {/* Clock */}
            <div className="text-sm font-mono text-[#666]">
              {timeStr} MT
            </div>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto p-6"><RestartBanner />
        {children}</main>
      </div>
    </div>
  )
}
