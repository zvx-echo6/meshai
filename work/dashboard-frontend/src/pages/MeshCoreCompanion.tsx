import { useState, useEffect, useCallback } from 'react'
import { Bot, Radio } from 'lucide-react'
import {
  fetchMeshcoreSelf,
  getMeshcoreChannels,
  sendMeshcoreAdvert,
  updateConfig,
  type MeshcoreSelf,
  type MeshcoreChannels,
  type TestSendResult,
} from '../lib/api'

/** Format epoch seconds as a human-readable relative time string. */
function relativeTime(epochSec: number): string {
  const diffSec = Math.floor(Date.now() / 1000 - epochSec)
  if (diffSec < 5) return 'just now'
  if (diffSec < 60) return `${diffSec}s ago`
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  return `${Math.floor(diffHr / 24)}d ago`
}

export default function MeshCoreCompanion() {
  const [self, setSelf] = useState<MeshcoreSelf | null>(null)
  const [channels, setChannels] = useState<MeshcoreChannels | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Send Advert state
  const [advertSending, setAdvertSending] = useState(false)
  const [advertResult, setAdvertResult] = useState<TestSendResult | null>(null)

  // Auto-advert control state — interval in hours (0 = disabled)
  // Loaded from connection config; editable in-page and PUTted back.
  const [advertIntervalHours, setAdvertIntervalHours] = useState<number>(3)
  const [advertIntervalSaving, setAdvertIntervalSaving] = useState(false)
  const [advertIntervalSaved, setAdvertIntervalSaved] = useState(false)

  useEffect(() => {
    document.title = 'Companion & Channels - MeshAI'
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setLoading(true)
      setError(null)
      try {
        const [selfData, channelData] = await Promise.all([
          fetchMeshcoreSelf(),
          getMeshcoreChannels(),
        ])
        if (cancelled) return
        setSelf(selfData)
        setChannels(channelData)
      } catch (err) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Failed to load companion status')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Load advert interval from connection config on mount.
  useEffect(() => {
    ;(async () => {
      try {
        const resp = await fetch('/api/config/connection')
        if (resp.ok) {
          const data = await resp.json() as Record<string, unknown>
          const sec = data['meshcore_advert_interval_seconds']
          if (typeof sec === 'number') {
            setAdvertIntervalHours(sec > 0 ? sec / 3600 : 0)
          }
        }
      } catch {
        // non-fatal — keep default
      }
    })()
  }, [])

  const handleSendAdvert = useCallback(async () => {
    setAdvertSending(true)
    setAdvertResult(null)
    try {
      const result = await sendMeshcoreAdvert()
      setAdvertResult(result)
      if (result.sent) {
        // Refresh self to pick up updated last_advert_sent.
        try {
          const updated = await fetchMeshcoreSelf()
          setSelf(updated)
        } catch {
          // non-fatal
        }
      }
    } catch (err) {
      setAdvertResult({
        sent: false,
        detail: err instanceof Error ? err.message : 'Request failed',
      })
    } finally {
      setAdvertSending(false)
    }
  }, [])

  const handleSaveAdvertInterval = useCallback(async () => {
    setAdvertIntervalSaving(true)
    setAdvertIntervalSaved(false)
    try {
      const seconds = Math.round(advertIntervalHours * 3600)
      await updateConfig('connection', { meshcore_advert_interval_seconds: seconds })
      setAdvertIntervalSaved(true)
      setTimeout(() => setAdvertIntervalSaved(false), 2000)
    } catch {
      // keep saving=false, let UI show failure implicitly
    } finally {
      setAdvertIntervalSaving(false)
    }
  }, [advertIntervalHours])

  const connected = self?.connected === true
  const channelNames = channels?.active ? channels.channels : []

  return (
    <div className="max-w-3xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center gap-4">
        <div className="flex-shrink-0 w-12 h-12 rounded-lg bg-[#0a0e17] border border-[#1e2a3a] flex items-center justify-center">
          <Bot size={24} className="text-accent" />
        </div>
        <div>
          <h2 className="text-xl font-semibold text-slate-100">Companion &amp; Channels</h2>
          <p className="text-sm text-[#777]">
            Live status for the AIDA MeshCore companion and its joined channels.
          </p>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-32">
          <div className="text-slate-400">Loading...</div>
        </div>
      ) : error ? (
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">
          {error}
        </div>
      ) : (
        <>
          {/* Status card */}
          <div className="bg-bg-card border border-border p-6">
            {connected ? (
              <div className="space-y-4">
                <div className="flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-green-500" />
                  <span className="text-sm font-medium text-green-400">Connected</span>
                </div>
                <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-4 text-sm">
                  <div>
                    <dt className="text-[#777] mb-1">Node name</dt>
                    <dd className="text-slate-100">{self?.name ?? 'unnamed'}</dd>
                  </div>
                  <div>
                    <dt className="text-[#777] mb-1">Host</dt>
                    <dd className="text-slate-100 font-mono">
                      {self?.host ?? '—'}
                      {self?.port != null ? `:${self.port}` : ''}
                    </dd>
                  </div>
                  <div className="sm:col-span-2">
                    <dt className="text-[#777] mb-1">Public key</dt>
                    <dd className="text-slate-100 font-mono text-xs break-all">
                      {self?.pubkey ?? '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[#777] mb-1">Channels joined</dt>
                    <dd className="text-slate-100">{self?.channel_count ?? 0}</dd>
                  </div>
                  {self?.last_advert_sent != null && (
                    <div>
                      <dt className="text-[#777] mb-1">Last advertised</dt>
                      <dd className="text-slate-100">{relativeTime(self.last_advert_sent)}</dd>
                    </div>
                  )}
                </dl>

                {/* Send Advert */}
                <div className="pt-2 border-t border-border space-y-2">
                  <div className="flex items-center gap-3">
                    <button
                      onClick={handleSendAdvert}
                      disabled={advertSending}
                      className="flex items-center gap-2 px-3 py-1.5 text-sm bg-accent/10 hover:bg-accent/20 text-accent border border-accent/30 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      <Radio size={14} />
                      {advertSending ? 'Sending…' : 'Send Advert'}
                    </button>
                    {advertResult != null && (
                      <span
                        className={`text-sm ${advertResult.sent ? 'text-green-400' : 'text-red-400'}`}
                      >
                        {advertResult.sent ? 'Advert sent' : advertResult.detail}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-[#555]">
                    Announce this node to the mesh so others can discover and DM it.
                  </p>
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-slate-600" />
                  <span className="text-sm font-medium text-slate-400">Not connected</span>
                </div>
                <p className="text-sm text-[#777] leading-relaxed max-w-prose">
                  The MeshCore companion is offline or inactive. No node identity or channel
                  membership is available while the companion is disconnected.
                </p>
              </div>
            )}
          </div>

          {/* Advertising settings */}
          <div className="bg-bg-card border border-border">
            <div className="px-4 py-3 border-b border-border">
              <h3 className="text-sm font-medium text-slate-200">Advertising</h3>
            </div>
            <div className="px-4 py-4 space-y-4">
              <div className="space-y-1">
                <label className="text-xs font-medium text-[#777] uppercase tracking-wide">
                  Auto-advert interval
                </label>
                <div className="flex items-center gap-3">
                  <select
                    value={advertIntervalHours}
                    onChange={(e) => setAdvertIntervalHours(Number(e.target.value))}
                    className="bg-[#0a0e17] border border-[#1e2a3a] text-slate-200 text-sm rounded px-2 py-1.5 focus:outline-none focus:border-accent"
                  >
                    <option value={0}>Disabled</option>
                    <option value={1}>Every 1 hour</option>
                    <option value={3}>Every 3 hours (default)</option>
                    <option value={6}>Every 6 hours</option>
                    <option value={12}>Every 12 hours</option>
                    <option value={24}>Every 24 hours</option>
                  </select>
                  <button
                    onClick={handleSaveAdvertInterval}
                    disabled={advertIntervalSaving}
                    className="px-3 py-1.5 text-sm bg-accent/10 hover:bg-accent/20 text-accent border border-accent/30 rounded disabled:opacity-50 transition-colors"
                  >
                    {advertIntervalSaving ? 'Saving…' : advertIntervalSaved ? 'Saved' : 'Save'}
                  </button>
                </div>
                <p className="text-xs text-[#555]">
                  AIDA sends a flood advertisement at this interval so it stays discoverable.
                  Stored in <code className="text-accent/80">connection.meshcore_advert_interval_seconds</code>.
                </p>
              </div>
            </div>
          </div>

          {/* Channel list */}
          <div className="bg-bg-card border border-border">
            <div className="px-4 py-3 border-b border-border">
              <h3 className="text-sm font-medium text-slate-200">Channels</h3>
            </div>
            {channelNames.length > 0 ? (
              <ul className="divide-y divide-border">
                {channelNames.map((name) => (
                  <li key={name} className="px-4 py-2.5 text-sm text-slate-200 font-mono">
                    {name}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="px-4 py-3 text-sm text-[#777]">No channels</div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
