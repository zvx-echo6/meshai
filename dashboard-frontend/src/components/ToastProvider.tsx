import { createContext, useContext, useState, useCallback, useEffect, ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, AlertCircle, Info, X } from 'lucide-react'
import type { Alert } from '@/lib/api'

interface Toast {
  id: string
  alert: Alert
  dismissedAt?: number
}

interface ToastContextValue {
  addToast: (alert: Alert) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

export function useToast() {
  const context = useContext(ToastContext)
  if (!context) {
    throw new Error('useToast must be used within a ToastProvider')
  }
  return context
}

function getSeverityStyles(severity: string) {
  switch (severity?.toLowerCase()) {
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
        bg: 'bg-blue-500/10',
        border: 'border-blue-500',
        icon: Info,
        iconColor: 'text-blue-500',
      }
  }
}

function ToastItem({
  toast,
  onDismiss,
  onNavigate,
}: {
  toast: Toast
  onDismiss: () => void
  onNavigate: () => void
}) {
  const styles = getSeverityStyles(toast.alert.severity)
  const Icon = styles.icon

  // Auto-dismiss after 8 seconds
  useEffect(() => {
    const timer = setTimeout(onDismiss, 8000)
    return () => clearTimeout(timer)
  }, [onDismiss])

  return (
    <div
      className={`${styles.bg} border ${styles.border} rounded-lg shadow-lg overflow-hidden animate-slide-in cursor-pointer`}
      onClick={onNavigate}
      role="alert"
    >
      <div className="flex items-start gap-3 p-4">
        {/* Severity bar */}
        <div className={`w-1 self-stretch -ml-4 -my-4 ${styles.border.replace('border', 'bg')}`} />

        <Icon size={18} className={styles.iconColor} />

        <div className="flex-1 min-w-0 pr-2">
          <div className="text-sm font-medium text-slate-200 mb-0.5">
            {toast.alert.type.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}
          </div>
          <div className="text-sm text-slate-300 line-clamp-2">
            {toast.alert.message}
          </div>
        </div>

        <button
          onClick={(e) => {
            e.stopPropagation()
            onDismiss()
          }}
          className="text-slate-400 hover:text-slate-200 transition-colors"
        >
          <X size={16} />
        </button>
      </div>
    </div>
  )
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const navigate = useNavigate()

  const addToast = useCallback((alert: Alert) => {
    const id = `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`
    setToasts((prev) => [...prev, { id, alert }])
  }, [])

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const handleNavigate = useCallback(() => {
    navigate('/alerts')
  }, [navigate])

  return (
    <ToastContext.Provider value={{ addToast }}>
      {children}

      {/* Toast container - fixed bottom right */}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm w-full pointer-events-none">
        {toasts.map((toast) => (
          <div key={toast.id} className="pointer-events-auto">
            <ToastItem
              toast={toast}
              onDismiss={() => dismissToast(toast.id)}
              onNavigate={handleNavigate}
            />
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
