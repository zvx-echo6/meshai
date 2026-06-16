// v0.6-tail-3 RestartBanner.tsx
//
// Sticky top banner that becomes visible when any /api/config/<section> PUT
// returns restart_required:true. Reads from localStorage so the banner
// persists across page navigations until the user explicitly restarts or
// dismisses.
//
// Producer: pages doing a config PUT call notifyRestartRequired(...).
// Consumer: this component, mounted once at the Layout level, listens to
// the 'meshai:restart-required' window CustomEvent and to the 'storage'
// event so a tab opened in two windows stays in sync.

import { useEffect, useState, useCallback } from 'react'
import { AlertTriangle, RotateCw, X } from 'lucide-react'

const LS_KEY = 'meshai.restartRequired.v1'

interface RestartState {
  required: boolean
  changedKeys: string[]
  ts: number  // when the most recent restart-required PUT happened
}


function readState(): RestartState {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return { required: false, changedKeys: [], ts: 0 }
    const parsed = JSON.parse(raw)
    return {
      required: Boolean(parsed.required),
      changedKeys: Array.isArray(parsed.changedKeys) ? parsed.changedKeys : [],
      ts: Number(parsed.ts) || 0,
    }
  } catch {
    return { required: false, changedKeys: [], ts: 0 }
  }
}


export function notifyRestartRequired(changedKeys: string[]) {
  const state: RestartState = {
    required: true,
    changedKeys: [...new Set(changedKeys)],
    ts: Date.now(),
  }
  localStorage.setItem(LS_KEY, JSON.stringify(state))
  window.dispatchEvent(new CustomEvent('meshai:restart-required', { detail: state }))
}


export function clearRestartRequired() {
  localStorage.removeItem(LS_KEY)
  window.dispatchEvent(new CustomEvent('meshai:restart-required',
    { detail: { required: false, changedKeys: [], ts: 0 } }))
}


export default function RestartBanner() {
  const [state, setState] = useState<RestartState>(() => readState())
  const [restarting, setRestarting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Subscribe to both same-tab CustomEvent and cross-tab storage event.
  useEffect(() => {
    const onCustom = (e: Event) => {
      const detail = (e as CustomEvent).detail as RestartState
      setState(detail)
    }
    const onStorage = (e: StorageEvent) => {
      if (e.key === LS_KEY) setState(readState())
    }
    window.addEventListener('meshai:restart-required', onCustom)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener('meshai:restart-required', onCustom)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  const onRestart = useCallback(async () => {
    setRestarting(true)
    setError(null)
    try {
      const res = await fetch('/api/system/restart', { method: 'POST' })
      if (!res.ok && res.status !== 202) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `HTTP ${res.status}`)
      }
      // Clear the banner immediately; the container will tear down and the
      // dashboard will need a refresh anyway.
      clearRestartRequired()
    } catch (e) {
      setError(String(e))
      setRestarting(false)
    }
  }, [])

  const onDismiss = useCallback(() => {
    clearRestartRequired()
  }, [])

  if (!state.required) return null

  return (
    <div className="bg-yellow-900/40 border-b border-yellow-700 text-yellow-100 px-4 py-2 text-sm flex items-center gap-3">
      <AlertTriangle className="w-4 h-4 flex-shrink-0 text-yellow-300" />
      <div className="flex-1 min-w-0">
        <strong>Container restart required</strong>
        {state.changedKeys.length > 0 && (
          <span className="text-yellow-300 ml-2">
            ({state.changedKeys.length} key{state.changedKeys.length === 1 ? '' : 's'}:{' '}
            <span className="font-mono text-xs">{state.changedKeys.slice(0, 3).join(', ')}{state.changedKeys.length > 3 ? ', …' : ''}</span>)
          </span>
        )}
        <span className="ml-2 text-yellow-300/80">
          for these changes to take effect. Until then the runtime keeps its boot-time configuration. Restart-required keys include anything under Config → environmental (feed_source, central URL), the LLM backend swap, and the dispatcher cold-start grace window. Other keys take effect on the next handler call.
        </span>
        {error && <div className="text-red-400 text-xs mt-1">{error}</div>}
      </div>
      <button
        onClick={onRestart}
        disabled={restarting}
        className="flex items-center gap-1 px-3 py-1 bg-yellow-700 hover:bg-yellow-600 disabled:opacity-50 rounded text-white text-xs">
        <RotateCw className={`w-3 h-3 ${restarting ? 'animate-spin' : ''}`} />
        {restarting ? 'Restarting…' : 'Restart now'}
      </button>
      <button
        onClick={onDismiss}
        className="text-yellow-300 hover:text-white px-1"
        title="Dismiss (you can still restart later)">
        <X className="w-4 h-4" />
      </button>
    </div>
  )
}
