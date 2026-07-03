import { useState, useEffect } from 'react'
import { Users } from 'lucide-react'
import {
  fetchMeshcoreContacts,
  type MeshcoreContacts,
  type MeshcoreContact,
} from '../lib/api'

function relativeTime(epochSeconds: number | null): string {
  if (epochSeconds == null) return '—'
  const diff = Math.floor(Date.now() / 1000) - epochSeconds
  if (diff < 0) return 'just now'
  if (diff < 60) return `${diff}s ago`
  const mins = Math.floor(diff / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

const TYPE_BADGES: Record<string, { label: string; className: string }> = {
  chat: { label: 'Chat', className: 'bg-sky-500/15 text-sky-400' },
  repeater: { label: 'Repeater', className: 'bg-amber-500/15 text-amber-400' },
  room: { label: 'Room', className: 'bg-violet-500/15 text-violet-400' },
  sensor: { label: 'Sensor', className: 'bg-emerald-500/15 text-emerald-400' },
}

function TypeBadge({ type }: { type: string | null }) {
  const meta = (type && TYPE_BADGES[type]) || {
    label: type ?? 'unknown',
    className: 'bg-slate-600/30 text-slate-400',
  }
  return (
    <span className={`px-2 py-0.5 text-[10px] uppercase tracking-wide rounded ${meta.className}`}>
      {meta.label}
    </span>
  )
}

function contactName(c: MeshcoreContact): string {
  if (c.name) return c.name
  if (c.pubkey) return `${c.pubkey.slice(0, 12)}…`
  return 'unnamed'
}

function shortPubkey(pubkey: string): string {
  return pubkey.length > 12 ? `${pubkey.slice(0, 12)}…` : pubkey
}

function position(c: MeshcoreContact): string {
  if (c.lat != null && c.lon != null) {
    return `${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}`
  }
  return '—'
}

export default function MeshCoreContacts() {
  const [data, setData] = useState<MeshcoreContacts | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    document.title = 'MeshCore Contacts - MeshAI'
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setLoading(true)
      setError(null)
      try {
        const result = await fetchMeshcoreContacts()
        if (!cancelled) setData(result)
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load contacts')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="max-w-4xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center gap-4">
        <div className="flex-shrink-0 w-12 h-12 rounded-lg bg-[#0a0e17] border border-[#1e2a3a] flex items-center justify-center">
          <Users size={24} className="text-accent" />
        </div>
        <div>
          <h2 className="text-xl font-semibold text-slate-100">MeshCore Contacts</h2>
          <p className="text-sm text-[#777]">
            The companion's known contact roster &mdash; names, types, and last-heard times.
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
      ) : data && data.active === false ? (
        <div className="bg-bg-card border border-border p-6">
          <p className="text-sm text-[#777] leading-relaxed max-w-prose">
            The MeshCore companion is not connected. The contact roster is unavailable until the
            companion comes online.
          </p>
        </div>
      ) : data && data.contacts.length === 0 ? (
        <div className="bg-bg-card border border-border p-6">
          <p className="text-sm text-[#777] leading-relaxed max-w-prose">
            No contacts yet. The companion is connected but has not discovered any nodes so far.
          </p>
        </div>
      ) : (
        <div className="bg-bg-card border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-[#777]">
                <th className="px-4 py-2.5 font-medium">Name</th>
                <th className="px-4 py-2.5 font-medium">Type</th>
                <th className="px-4 py-2.5 font-medium">Last heard</th>
                <th className="px-4 py-2.5 font-medium">Position</th>
                <th className="px-4 py-2.5 font-medium">Pubkey</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(data?.contacts ?? []).map((c) => (
                <tr key={c.pubkey} className="hover:bg-bg-hover">
                  <td className="px-4 py-2.5 text-slate-100">{contactName(c)}</td>
                  <td className="px-4 py-2.5">
                    <TypeBadge type={c.type} />
                  </td>
                  <td className="px-4 py-2.5 text-slate-300">{relativeTime(c.last_advert)}</td>
                  <td className="px-4 py-2.5 text-slate-300 font-mono text-xs">{position(c)}</td>
                  <td className="px-4 py-2.5 text-slate-400 font-mono text-xs">
                    {shortPubkey(c.pubkey)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-[#777]">
        Telemetry auto-poll is coming in the next pass.
      </p>
    </div>
  )
}
