import { useState, useEffect } from 'react'
import { Radio } from 'lucide-react'
import {
  getChannels,
  getMeshcoreChannelsDetail,
  type MeshtasticChannel,
  type MeshcoreChannelsDetail,
} from '../lib/api'

/**
 * Read-only Channels overview.
 *
 * Two independent sections, one per mesh family:
 *   - Meshtastic channels (routes by channel index)
 *   - MeshCore channels (routes by channel name; no index/slot)
 *
 * Nothing here transmits or mutates — it is a status view only.
 */
export default function Channels() {
  const [mt, setMt] = useState<MeshtasticChannel[] | null>(null)
  const [mc, setMc] = useState<MeshcoreChannelsDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    document.title = 'Channels - MeshAI'
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setLoading(true)
      setError(null)
      try {
        const [mtData, mcData] = await Promise.all([
          getChannels(),
          getMeshcoreChannelsDetail(),
        ])
        if (cancelled) return
        setMt(mtData)
        setMc(mcData)
      } catch (err) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Failed to load channels')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const mtChannels = mt ?? []
  const mcChannels = mc?.active ? mc.channels : []

  return (
    <div className="max-w-3xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center gap-4">
        <div className="flex-shrink-0 w-12 h-12 rounded-lg bg-[#0a0e17] border border-[#1e2a3a] flex items-center justify-center">
          <Radio size={24} className="text-accent" />
        </div>
        <div>
          <h2 className="text-xl font-semibold text-slate-100">Channels</h2>
          <p className="text-sm text-[#777]">
            Channels configured on each connected radio. Read-only.
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
          {/* Meshtastic channels */}
          <div className="bg-bg-card border border-border">
            <div className="px-4 py-3 border-b border-border">
              <h3 className="text-sm font-medium text-slate-200">Meshtastic Channels</h3>
              <p className="text-xs text-[#555] mt-1">Routes by channel index.</p>
            </div>
            {mtChannels.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-sm text-slate-200">
                  <thead className="bg-[#161616] border-b border-border">
                    <tr>
                      <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-left">Index</th>
                      <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-left">Name</th>
                      <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-left">Role</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {mtChannels.map((ch) => (
                      <tr key={ch.index} className="hover:bg-bg-hover">
                        <td className="px-3 py-2 font-mono text-xs">{ch.index}</td>
                        <td className="px-3 py-2">{ch.name}</td>
                        <td className="px-3 py-2 text-xs text-[#999]">{ch.role}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="px-4 py-3 text-sm text-[#777]">
                Node offline — channels unavailable
              </div>
            )}
          </div>

          {/* MeshCore channels */}
          <div className="bg-bg-card border border-border">
            <div className="px-4 py-3 border-b border-border">
              <h3 className="text-sm font-medium text-slate-200">MeshCore Channels</h3>
              <p className="text-xs text-[#555] mt-1">Routes by channel name.</p>
            </div>
            {mcChannels.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-sm text-slate-200">
                  <thead className="bg-[#161616] border-b border-border">
                    <tr>
                      <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-left">Name</th>
                      <th className="px-3 py-2 font-sans text-[9px] uppercase tracking-widest text-[#666] text-left">On-air hash</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {mcChannels.map((ch) => (
                      <tr key={ch.name} className="hover:bg-bg-hover">
                        <td className="px-3 py-2">{ch.name}</td>
                        <td className="px-3 py-2 font-mono text-xs text-[#999]">
                          {ch.hash != null ? `0x${ch.hash}` : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="px-4 py-3 text-sm text-[#777]">
                MeshCore not connected
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
