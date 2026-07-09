import { useEffect, useState } from 'react'
import { Activity, Clock, CheckCircle, MinusCircle, Radio } from 'lucide-react'
import { fetchActivity, type ActivityEntry } from '@/lib/api'

// --- helpers ---------------------------------------------------------------

// sent_at is stored as int(time.time()) epoch SECONDS on new rows. Guard null
// and detect seconds-vs-ms so we render correct local time either way.
function formatSentAt(sent_at: number | string | null): string {
  if (sent_at === null || sent_at === undefined || sent_at === '') return '—'
  let d: Date
  if (typeof sent_at === 'number') {
    // epoch seconds -> ms (values below ~1e12 are seconds)
    d = new Date(sent_at < 1e12 ? sent_at * 1000 : sent_at)
  } else {
    const asNum = Number(sent_at)
    d = Number.isFinite(asNum) && sent_at.trim() !== ''
      ? new Date(asNum < 1e12 ? asNum * 1000 : asNum)
      : new Date(sent_at)
  }
  return isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

// Mesh badge styling per transport family.
function transportBadge(transport: string | null) {
  switch (transport) {
    case 'meshtastic':
      return { label: 'Meshtastic', cls: 'bg-blue-500/15 text-blue-400 border border-blue-500/30' }
    case 'meshcore':
      return { label: 'MeshCore', cls: 'bg-green-500/15 text-green-400 border border-green-500/30' }
    default:
      return { label: 'Legacy', cls: 'bg-slate-500/15 text-slate-400 border border-slate-600/40' }
  }
}

// Channel label: Meshtastic index (#n) or MeshCore name; '—' when null.
function channelLabel(channel: string | number | null): string {
  if (channel === null || channel === undefined || channel === '') return '—'
  if (typeof channel === 'number') return `ch ${channel}`
  return channel.startsWith('#') ? channel : `#${channel}`
}

// Explicit label table for known source_event_table values.
const TABLE_LABELS: Record<string, string> = {
  nws_alerts:                 'Weather',
  fires:                      'Fire',
  fire_digest_broadcasts:     'Fire digest',
  satpass_events:             'Satellite',
  band_conditions_broadcasts: 'Band',
  traffic_events:             'Traffic',
  quake_events:               'Quake',
  swpc_events:                'Space Wx',
  gauge_readings:             'Hydro',
  event_log:                  'Avalanche',
}

// Text-prefix / emoji heuristics for legacy NULL-source rows.
// Derived from actual formatter outputs:
//   fires.py / firms.py  → "🔥 …"
//   work_zone.py (wzdx/511/traffic) → "🚧 …"
//   avalanche.py         → "⛷ …"
//   hydro.py             → "🌊 …"
//   swpc.py              → "🧲 …" | "☀️ …"
//   quake.py             → "🌐 …"
//   nws.py / composer (legacy) → "⏳ WX: …" | "🌡️ …" (heat) | "🌬️ …" | "⛈️ …" | "🌩️ …"
//   incident_handler.py (legacy) → "⚠️ Road Incident …" | "🚫 Road Closed …"
// NOTE: more-specific prefixes must appear before shorter ones that share a leading char.
const TEXT_HINTS: Array<[string, string]> = [
  ['🔥', 'Fire'],
  ['🚧', 'Traffic'],
  ['⚠️ Road Incident', 'Traffic'],  // legacy road-incident rows (⚠️ = U+26A0+FE0F)
  ['🚫', 'Traffic'],                 // legacy road-closure rows
  ['⛷', 'Avalanche'],
  ['🌊', 'Hydro'],
  ['🧲', 'Space Wx'],
  ['☀️', 'Space Wx'],
  ['🌐', 'Quake'],
  ['⏳', 'Weather'],   // legacy weather-watch rows: "⏳ WX: …"
  ['🌡️', 'Weather'],  // legacy heat-alert rows (NWS heat format)
  ['🌬️', 'Weather'],  // legacy NWS wind/SPS rows: "🌬️ Special Weather Statement …"
  ['⛈️', 'Weather'],  // legacy NWS severe thunderstorm rows
  ['🌩️', 'Weather'],  // legacy NWS thunderstorm-update rows
]

// Type/family tag derived from source_event_table, with text-prefix fallback
// for legacy NULL-source rows (native adapter broadcasts before the fix).
function familyLabel(table: string | null, text?: string | null): string {
  if (table) return TABLE_LABELS[table] ?? table.replace(/_/g, ' ').replace(/s$/, '')
  // NULL source_event_table: try emoji prefix heuristic on the broadcast text.
  if (text) {
    for (const [prefix, label] of TEXT_HINTS) {
      if (text.startsWith(prefix)) return label
    }
  }
  return 'broadcast'
}

// --- component -------------------------------------------------------------

// Transport filter options.
const TRANSPORTS = [
  { value: 'all', label: 'All meshes' },
  { value: 'meshtastic', label: 'Meshtastic' },
  { value: 'meshcore', label: 'MeshCore' },
]

// Category filter options. Values are the raw source_event_table stored on
// each broadcast row; labels are operator-friendly. Kept explicit (not
// derived from the current window) so lower-frequency categories like
// weather are reachable even when chatty satpass/band rows dominate the feed.
const CATEGORIES = [
  { value: 'all', label: 'All types' },
  { value: 'nws_alerts', label: 'Weather' },
  { value: 'fires', label: 'Fires' },
  { value: 'satpass_events', label: 'Satellite' },
  { value: 'band_conditions_broadcasts', label: 'Band' },
  { value: 'traffic_events', label: 'Traffic' },
]

const PAGE = 100

export default function ActivityLog() {
  const [entries, setEntries] = useState<ActivityEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [transport, setTransport] = useState('all')
  const [category, setCategory] = useState('all')
  const [limit, setLimit] = useState(PAGE)

  useEffect(() => {
    document.title = 'Activity Log — MeshAI'
  }, [])

  useEffect(() => {
    let alive = true
    const load = () => {
      fetchActivity(limit, transport, category)
        .then((data) => {
          if (!alive) return
          setEntries(data)
          setError(null)
          setLoading(false)
        })
        .catch((err) => {
          if (!alive) return
          setError(err.message)
          setLoading(false)
        })
    }
    load()
    const interval = setInterval(load, 5000)
    return () => {
      alive = false
      clearInterval(interval)
    }
  }, [limit, transport, category])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading activity…</div>
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
    <div className="space-y-4">
      <div className="bg-bg-card border border-border">
        <div className="p-4 border-b border-border flex items-center flex-wrap gap-2">
          <Activity size={14} className="text-[#f59e0b]" />
          <h2 className="text-sm font-medium text-slate-300">
            Activity Log
          </h2>

          {/* Filters — default 'all' shows the full feed (every category,
              both meshes). Narrowing surfaces lower-frequency categories
              (e.g. weather) and the MeshCore side. */}
          <select
            value={transport}
            onChange={(e) => { setTransport(e.target.value); setLimit(PAGE) }}
            className="text-xs bg-bg-hover text-slate-300 border border-border rounded px-2 py-1"
          >
            {TRANSPORTS.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
          <select
            value={category}
            onChange={(e) => { setCategory(e.target.value); setLimit(PAGE) }}
            className="text-xs bg-bg-hover text-slate-300 border border-border rounded px-2 py-1"
          >
            {CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>{c.label}</option>
            ))}
          </select>

          <span className="text-xs text-slate-500 ml-auto">
            {entries.length} broadcast{entries.length === 1 ? '' : 's'} · newest first
          </span>
        </div>

        {entries.length === 0 ? (
          <div className="flex items-center gap-2 text-slate-500 p-8">
            <Radio size={18} />
            <span>No outbound broadcasts recorded yet.</span>
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {entries.map((e) => {
              const badge = transportBadge(e.transport)
              return (
                <li key={e.id} className="p-4 hover:bg-bg-hover transition-colors">
                  <div className="flex items-start gap-3">
                    {/* status indicator */}
                    <div className="pt-0.5">
                      {e.success === 1 ? (
                        <CheckCircle size={16} className="text-green-500" />
                      ) : e.success === 0 ? (
                        <MinusCircle size={16} className="text-amber-500" />
                      ) : (
                        <MinusCircle size={16} className="text-slate-600" />
                      )}
                    </div>

                    <div className="flex-1 min-w-0">
                      {/* meta row: mesh badge, channel, family, status */}
                      <div className="flex items-center flex-wrap gap-2 mb-1">
                        <span className={`text-xs px-2 py-0.5 rounded-full ${badge.cls}`}>
                          {badge.label}
                        </span>
                        <span className="text-xs px-2 py-0.5 rounded-full bg-bg-hover text-slate-400 border border-border">
                          {channelLabel(e.channel)}
                        </span>
                        <span className="text-xs px-2 py-0.5 rounded-full bg-[#f59e0b]/10 text-[#f59e0b]">
                          {familyLabel(e.source_event_table, e.text)}
                        </span>
                        {e.success === 1 && (
                          <span className="text-xs text-green-500">Sent</span>
                        )}
                        {e.success === 0 && (
                          <span className="text-xs text-amber-500">Skip</span>
                        )}
                        {(e.success === null || e.success === undefined) && (
                          <span className="text-xs text-slate-500">—</span>
                        )}
                      </div>

                      {/* message text */}
                      <div className="text-sm text-slate-200 break-words whitespace-pre-wrap">
                        {e.text || <span className="text-slate-500 italic">(no text)</span>}
                      </div>

                      {/* timestamp */}
                      <div className="flex items-center gap-1 mt-1.5 text-xs text-slate-500 font-mono">
                        <Clock size={12} />
                        {formatSentAt(e.sent_at)}
                      </div>
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        )}

        {/* Load more — pages further back so lower-frequency categories
            (weather, older MeshCore sends) remain reachable even when chatty
            satpass/band broadcasts fill the newest window. Shown only when the
            page came back full (more rows likely exist). */}
        {entries.length >= limit && (
          <div className="p-3 border-t border-border flex justify-center">
            <button
              onClick={() => setLimit((n) => n + PAGE)}
              className="text-xs px-3 py-1 rounded bg-bg-hover text-slate-300 border border-border hover:bg-bg-card transition-colors"
            >
              Load more
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
