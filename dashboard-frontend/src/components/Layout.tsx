import { ReactNode, useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  LayoutDashboard,
  Radio,
  Cloud,
  Settings,
  Bell,
} from 'lucide-react'
import { fetchStatus, type SystemStatus } from '@/lib/api'
import { useWebSocket } from '@/hooks/useWebSocket'

interface LayoutProps {
  children: ReactNode
}

const navItems = [
  { path: '/', label: 'Dashboard', icon: LayoutDashboard },
  { path: '/mesh', label: 'Mesh', icon: Radio },
  { path: '/environment', label: 'Environment', icon: Cloud },
  { path: '/config', label: 'Config', icon: Settings },
  { path: '/alerts', label: 'Alerts', icon: Bell },
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
  const { connected } = useWebSocket()
  const [status, setStatus] = useState<SystemStatus | null>(null)
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
    <div className="flex h-screen overflow-hidden bg-bg text-slate-200">
      {/* Sidebar */}
      <aside className="w-[220px] flex-shrink-0 bg-bg-card border-r border-border flex flex-col overflow-y-auto">
        {/* Logo */}
        <div className="p-5 border-b border-border">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center text-white font-bold text-xl">
              M
            </div>
            <div>
              <div className="font-semibold text-lg">MeshAI</div>
              <div className="text-xs text-slate-500 font-mono">
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
                className={`flex items-center gap-3 px-5 py-3 text-sm transition-colors relative ${
                  isActive
                    ? 'text-blue-400 bg-blue-500/10'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-bg-hover'
                }`}
              >
                {isActive && (
                  <div className="absolute left-0 top-0 bottom-0 w-0.5 bg-blue-500" />
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
            <span className="text-xs text-slate-400">
              {status?.connected ? 'Connected' : 'Disconnected'}
            </span>
          </div>
          <div className="text-xs text-slate-500 font-mono truncate">
            {status?.connection_type?.toUpperCase()}: {status?.connection_target}
          </div>
          <div className="text-xs text-slate-500 mt-1">
            Uptime: {status ? formatUptime(status.uptime_seconds) : '...'}
          </div>
        </div>
      </aside>

      {/* Main content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Header */}
        <header className="h-14 flex-shrink-0 border-b border-border bg-bg-card flex items-center justify-between px-6">
          <h1 className="text-lg font-semibold">
            {getPageTitle(location.pathname)}
          </h1>
          <div className="flex items-center gap-6">
            {/* Live indicator */}
            <div className="flex items-center gap-2">
              <div
                className={`w-2 h-2 rounded-full ${
                  connected ? 'bg-green-500 animate-pulse-slow' : 'bg-slate-500'
                }`}
              />
              <span className="text-xs text-slate-400">
                {connected ? 'Live' : 'Offline'}
              </span>
            </div>
            {/* Clock */}
            <div className="text-sm font-mono text-slate-400">
              {timeStr} MT
            </div>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
    </div>
  )
}
