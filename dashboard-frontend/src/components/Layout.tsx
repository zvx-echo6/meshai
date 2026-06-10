import { ReactNode, useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  LayoutDashboard,
  Radio,
  Cloud,
  Settings,
  Bell,
  BellRing,
  BookOpen,
  Sliders,
  Droplets,
  MapPin,
} from 'lucide-react'
import { fetchStatus, type SystemStatus } from '@/lib/api'
import { useWebSocket } from '@/hooks/useWebSocket'
import { useToast } from './ToastProvider'
import RestartBanner from './RestartBanner'

interface LayoutProps {
  children: ReactNode
}

const navItems = [
  { path: '/', label: 'Dashboard', icon: LayoutDashboard },
  { path: '/mesh', label: 'Mesh', icon: Radio },
  { path: '/environment', label: 'Environment', icon: Cloud },
  { path: '/config', label: 'Config', icon: Settings },
  { path: '/alerts', label: 'Alerts', icon: Bell },
  { path: '/notifications', label: 'Notifications', icon: BellRing },
  { path: '/reference', label: 'Reference', icon: BookOpen },
  { path: '/adapter-config', label: 'Adapter Config', icon: Sliders },
  { path: '/gauge-sites', label: 'Gauge Sites', icon: Droplets },
  { path: '/town-anchors', label: 'Town Anchors', icon: MapPin },
]

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const mins = Math.floor((seconds % 3600) / 60)

  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

function getPageTitle(pathname: string): string {
  const item = navItems.find((i) => i.path === pathname)
  return item?.label || 'Dashboard'
}

export default function Layout({ children }: LayoutProps) {
  const location = useLocation()
  const { connected, lastAlert } = useWebSocket()
  const { addToast } = useToast()
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [lastAlertId, setLastAlertId] = useState<string | null>(null)

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
        <div className="p-5 border-b border-border">
          <div className="flex items-center gap-3">
            <div className="w-[3px] h-8 bg-accent flex-shrink-0" />
            <div>
              <div className="font-sans font-bold text-white text-[15px] leading-tight tracking-tight">MeshAI</div>
              <div className="text-xs font-mono text-[#333]">
                v{status?.version || '...'}
              </div>
            </div>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 py-4">
          {navItems.map((item) => {
            const isActive = location.pathname === item.path
            const Icon = item.icon
            return (
              <Link
                key={item.path}
                to={item.path}
                className={`flex items-center gap-3 px-5 py-3 text-sm font-sans transition-colors relative ${
                  isActive
                    ? 'text-white'
                    : 'text-[#444] hover:text-[#888]'
                }`}
              >
                {isActive && (
                  <div className="absolute right-0 top-0 bottom-0 w-0.5 bg-accent" />
                )}
                <Icon size={18} />
                {item.label}
              </Link>
            )
          })}
        </nav>

        {/* Connection status */}
        <div className="p-5 border-t border-border">
          <div className="flex items-center gap-2 mb-2">
            <div
              className={`w-2 h-2 rounded-full ${
                status?.connected ? 'bg-green-500' : 'bg-red-500'
              }`}
            />
            <span className="text-xs font-sans text-[#444]">
              {status?.connected ? 'Connected' : 'Disconnected'}
            </span>
          </div>
          <div className="text-xs font-mono text-[#333] truncate">
            {status?.connection_type?.toUpperCase()}: {status?.connection_target}
          </div>
          <div className="text-xs font-sans text-[#333] mt-1">
            Uptime: <span className="font-mono">{status ? formatUptime(status.uptime_seconds) : '...'}</span>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Header */}
        <header className="h-14 flex-shrink-0 border-b border-border bg-bg-card flex items-center justify-between px-6">
          <h1 className="text-lg font-sans font-semibold text-white">
            {getPageTitle(location.pathname)}
          </h1>
          <div className="flex items-center gap-6">
            {/* Live indicator */}
            <div className="flex items-center gap-2">
              <div
                className={`w-2 h-2 rounded-full ${
                  connected ? 'bg-accent animate-pulse-slow' : 'bg-[#333]'
                }`}
              />
              <span className="text-xs font-sans text-[#444]">
                {connected ? 'Live' : 'Offline'}
              </span>
            </div>
            {/* Clock */}
            <div className="text-sm font-mono text-[#333]">
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
