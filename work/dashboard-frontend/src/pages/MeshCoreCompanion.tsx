import { useState, useEffect } from 'react'
import { Bot } from 'lucide-react'
import {
  fetchMeshcoreSelf,
  getMeshcoreChannels,
  type MeshcoreSelf,
  type MeshcoreChannels,
} from '../lib/api'

export default function MeshCoreCompanion() {
  const [self, setSelf] = useState<MeshcoreSelf | null>(null)
  const [channels, setChannels] = useState<MeshcoreChannels | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

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
                </dl>
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
