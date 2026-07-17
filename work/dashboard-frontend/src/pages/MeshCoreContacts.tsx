import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Copy, Download, Plus, RefreshCw, Trash2, Users } from 'lucide-react'
import {
  fetchMeshcoreContacts,
  fetchMeshcoreRouteHealth,
  fetchMeshcoreTelemetry,
  fetchConnectionConfig,
  importMeshcoreContacts,
  pollMeshcoreContact,
  refreshMeshcoreContacts,
  removeMeshcoreContact,
  updateConfig,
  type MeshcoreChannelStats,
  type MeshcoreContacts,
  type MeshcoreContact,
  type MeshcoreRefreshStats,
  type MeshcoreRouteHealth,
  type MeshcoreTelemetry,
  type MeshcoreTelemetryEntry,
  type MeshcoreTelemetryData,
  type MeshcorePollResult,
  type ConnectionConfig,
} from '../lib/api'

const TELEMETRY_POLL_MS = 15000
const MIN_INTERVAL_MINUTES = 5

// A contact not heard from in this long is flagged stale. Adverts are typically
// hours apart, so days — not hours — is the honest threshold for "gone quiet".
const STALE_AFTER_DAYS = 14
const STALE_AFTER_SECONDS = STALE_AFTER_DAYS * 86400

type SortKey = 'name' | 'type' | 'last_advert'
type TypeFilter = 'all' | 'rooms' | 'stale'

function isStale(c: MeshcoreContact): boolean {
  if (c.last_advert == null || c.last_advert <= 0) return false
  return Math.floor(Date.now() / 1000) - c.last_advert > STALE_AFTER_SECONDS
}

// Why a routing cell will not resolve, in operator language.
const DANGLING_REASON: Record<string, string> = {
  room_not_found: 'no room server with this key is on the companion',
  channel_not_found: 'this channel is not on the companion',
  not_a_room: 'this key belongs to a contact that is not a room server',
}

// Relative time for epoch-seconds fields (last_advert, last_synced_at).
// Floors the DIFFERENCE, not just the clock: last_synced_at is a float, so
// flooring only Date.now() would leave a fractional "28.6851...s ago".
function relativeTime(epochSeconds: number | null): string {
  if (epochSeconds == null) return '—'
  const diff = Math.floor(Date.now() / 1000 - epochSeconds)
  if (diff < 0) return 'just now'
  if (diff < 60) return `${diff}s ago`
  const mins = Math.floor(diff / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

// Relative time for ISO8601 timestamps (telemetry polled_at).
function relativeTimeIso(iso: string | null): string {
  if (!iso) return '—'
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return '—'
  const diff = Math.floor((Date.now() - then) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  const mins = Math.floor(diff / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

// MeshCore contact.type is a NUMBER: 0=NONE,1=chat,2=repeater,3=room,4=sensor.
const TYPE_BADGES: Record<number, { label: string; className: string }> = {
  1: { label: 'Chat', className: 'bg-sky-500/15 text-sky-400' },
  2: { label: 'Repeater', className: 'bg-amber-500/15 text-amber-400' },
  3: { label: 'Room', className: 'bg-violet-500/15 text-violet-400' },
  4: { label: 'Sensor', className: 'bg-emerald-500/15 text-emerald-400' },
}

function TypeBadge({ type }: { type: number | null }) {
  const meta = (type != null && TYPE_BADGES[type]) || {
    label: 'Unknown',
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

// Stable identifier the backend resolves against (pubkey == meshcore
// public_key; adv_name is the fallback when a contact has no key).
function contactId(c: MeshcoreContact): string {
  return c.pubkey || c.name || ''
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

// Generic sensor field rendering, in display order.
const SENSOR_FIELDS: { key: string; label: string; unit: string; digits: number }[] = [
  { key: 'battery_pct', label: 'Battery', unit: '%', digits: 0 },
  { key: 'voltage', label: 'Voltage', unit: 'V', digits: 2 },
  { key: 'temperature', label: 'Temp', unit: '°C', digits: 1 },
  { key: 'humidity', label: 'Humidity', unit: '%', digits: 0 },
  { key: 'current', label: 'Current', unit: 'A', digits: 2 },
  { key: 'illuminance', label: 'Light', unit: 'lx', digits: 0 },
  { key: 'barometer', label: 'Pressure', unit: 'hPa', digits: 1 },
  { key: 'power', label: 'Power', unit: 'W', digits: 1 },
  { key: 'altitude', label: 'Alt', unit: 'm', digits: 0 },
  { key: 'distance', label: 'Dist', unit: 'm', digits: 0 },
]

function TelemetryReadout({
  data,
  polledLabel,
}: {
  data: MeshcoreTelemetryData
  polledLabel: string
}) {
  const chips = SENSOR_FIELDS.flatMap((f) => {
    const v = data[f.key]
    if (typeof v !== 'number' || Number.isNaN(v)) return []
    return [
      <span
        key={f.key}
        className="px-2 py-0.5 text-xs rounded bg-[#0a0e17] border border-[#1e2a3a] text-slate-200"
      >
        <span className="text-[#777]">{f.label}</span> {v.toFixed(f.digits)}
        {f.unit}
      </span>,
    ]
  })
  return (
    <div className="flex flex-wrap items-center gap-2">
      {chips.length > 0 ? (
        chips
      ) : (
        <span className="text-xs text-[#777]">Telemetry received (no standard sensor fields)</span>
      )}
      <span className="text-[11px] text-[#777] ml-1">polled {polledLabel}</span>
    </div>
  )
}

export default function MeshCoreContacts() {
  const [data, setData] = useState<MeshcoreContacts | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [connectionConfig, setConnectionConfig] = useState<ConnectionConfig | null>(null)
  const [telemetry, setTelemetry] = useState<MeshcoreTelemetry | null>(null)

  // Roster management: resync / export / delete / route health.
  const [routeHealth, setRouteHealth] = useState<MeshcoreRouteHealth | null>(null)
  const [resyncing, setResyncing] = useState(false)
  const [resyncStats, setResyncStats] = useState<MeshcoreRefreshStats | null>(null)
  const [channelStats, setChannelStats] = useState<MeshcoreChannelStats | null>(null)
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null)

  // Manual add — the companion learns most contacts by advert, but a node that
  // has been rekeyed (or is not yet heard) has to be entered by key.
  const [showAdd, setShowAdd] = useState(false)
  const [addName, setAddName] = useState('')
  const [addPubkey, setAddPubkey] = useState('')
  const [addType, setAddType] = useState(1)
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [expandedKey, setExpandedKey] = useState<string | null>(null)

  // Table controls.
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all')
  const [sortKey, setSortKey] = useState<SortKey>('name')
  const [sortAsc, setSortAsc] = useState(true)

  // Per-row transient UI state.
  const [savingId, setSavingId] = useState<string | null>(null)
  const [savedId, setSavedId] = useState<string | null>(null)
  const [pollingId, setPollingId] = useState<string | null>(null)
  const [pollResults, setPollResults] = useState<Record<string, MeshcorePollResult>>({})
  const [saveError, setSaveError] = useState<string | null>(null)

  // Interval control (minutes, derived from meshcore_telemetry_interval_seconds).
  const [intervalMinutes, setIntervalMinutes] = useState<number>(30)
  const [intervalSaving, setIntervalSaving] = useState(false)
  const [intervalSaved, setIntervalSaved] = useState(false)

  useEffect(() => {
    document.title = 'MeshCore Contacts - MeshAI'
  }, [])

  // Roster (once).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setLoading(true)
      setError(null)
      try {
        const result = await fetchMeshcoreContacts()
        if (!cancelled) {
          setData(result)
          setLastSyncedAt(result.last_synced_at ?? null)
        }
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

  // Route health (once): which routing cells point at something that no longer
  // exists. Non-fatal — the roster is still useful if this check fails.
  const loadRouteHealth = useCallback(async () => {
    try {
      const health = await fetchMeshcoreRouteHealth()
      setRouteHealth(health)
    } catch {
      // non-fatal — banner simply stays hidden
    }
  }, [])

  useEffect(() => {
    loadRouteHealth()
  }, [loadRouteHealth])

  // Connection config (once) — kept whole so PUTs send it back intact.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const cfg = await fetchConnectionConfig()
        if (cancelled) return
        setConnectionConfig(cfg)
        const sec = cfg.meshcore_telemetry_interval_seconds
        if (typeof sec === 'number' && sec > 0) {
          setIntervalMinutes(Math.max(MIN_INTERVAL_MINUTES, Math.round(sec / 60)))
        }
      } catch {
        // non-fatal — auto-poll controls degrade gracefully
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Telemetry: on mount + every 15s.
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const t = await fetchMeshcoreTelemetry()
        if (!cancelled) setTelemetry(t)
      } catch {
        // non-fatal — keep last-known telemetry
      }
    }
    load()
    const id = setInterval(load, TELEMETRY_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const telemetryContacts: string[] = connectionConfig?.meshcore_telemetry_contacts ?? []

  // Match a roster contact to its telemetry entry (by pubkey or name — the
  // config list may store either identifier).
  const entryFor = useCallback(
    (c: MeshcoreContact): MeshcoreTelemetryEntry | undefined => {
      const entries = telemetry?.entries ?? []
      return entries.find((e) => e.contact === c.pubkey || (c.name != null && e.contact === c.name))
    },
    [telemetry]
  )

  const isSelected = useCallback(
    (c: MeshcoreContact): boolean =>
      telemetryContacts.includes(c.pubkey) || (c.name != null && telemetryContacts.includes(c.name)),
    [telemetryContacts]
  )

  const handleToggle = useCallback(
    async (c: MeshcoreContact, turnOn: boolean) => {
      if (!connectionConfig) return
      const id = contactId(c)
      if (!id) return
      setSaveError(null)
      setSavingId(id)
      // Build the new list: add pubkey when enabling; drop both pubkey and
      // name when disabling (either could be present).
      const current = connectionConfig.meshcore_telemetry_contacts ?? []
      let next: string[]
      if (turnOn) {
        next = current.includes(id) ? current : [...current, id]
      } else {
        next = current.filter((x) => x !== c.pubkey && x !== c.name)
      }
      const nextConfig: ConnectionConfig = {
        ...connectionConfig,
        meshcore_telemetry_contacts: next,
      }
      try {
        await updateConfig('connection', nextConfig)
        setConnectionConfig(nextConfig)
        setSavedId(id)
        setTimeout(() => setSavedId((s) => (s === id ? null : s)), 1500)
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : 'Failed to save')
      } finally {
        setSavingId((s) => (s === id ? null : s))
      }
    },
    [connectionConfig]
  )

  const handlePollNow = useCallback(async (c: MeshcoreContact) => {
    const id = contactId(c)
    if (!id) return
    setPollingId(id)
    try {
      const result = await pollMeshcoreContact(id)
      setPollResults((prev) => ({ ...prev, [id]: result }))
    } catch (err) {
      setPollResults((prev) => ({
        ...prev,
        [id]: { available: false, contact: id, detail: err instanceof Error ? err.message : 'Poll failed' },
      }))
    } finally {
      setPollingId((p) => (p === id ? null : p))
    }
  }, [])

  // Full resync: refetch the whole roster from the node and reconcile, so
  // entries the companion no longer has are dropped rather than lingering.
  const handleResync = useCallback(async () => {
    setResyncing(true)
    setResyncStats(null)
    setChannelStats(null)
    setSaveError(null)
    try {
      const result = await refreshMeshcoreContacts()
      setData({ active: result.active, contacts: result.contacts })
      setLastSyncedAt(result.last_synced_at)
      setResyncStats(result.stats)
      setChannelStats(result.channel_stats)
      // Roster and channels just changed — re-check the routing cells against them.
      loadRouteHealth()
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Resync failed')
    } finally {
      setResyncing(false)
    }
  }, [loadRouteHealth])

  // Add a contact by key. Minimal record: the companion fills in the rest when
  // the node next adverts; out_path_len -1 means "flood until a path is known".
  const handleAdd = useCallback(async () => {
    const pubkey = addPubkey.trim().toLowerCase()
    const name = addName.trim()
    if (!/^[0-9a-f]{64}$/.test(pubkey)) {
      setAddError('Pubkey must be exactly 64 hex characters (the full key, not a prefix)')
      return
    }
    if (!name) {
      setAddError('A name is required')
      return
    }
    setAdding(true)
    setAddError(null)
    try {
      const result = await importMeshcoreContacts([
        { pubkey, name, type: addType, flags: 0, out_path_len: -1, out_path: '', last_advert: 0 },
      ])
      if (result.failed > 0) {
        setAddError(result.errors[0]?.detail || 'Add failed')
        return
      }
      setShowAdd(false)
      setAddName('')
      setAddPubkey('')
      // Re-read so the new contact appears with whatever the companion stored.
      const refreshed = await fetchMeshcoreContacts()
      setData(refreshed)
      setLastSyncedAt(refreshed.last_synced_at ?? null)
      loadRouteHealth()
    } catch (err) {
      setAddError(err instanceof Error ? err.message : 'Add failed')
    } finally {
      setAdding(false)
    }
  }, [addPubkey, addName, addType, loadRouteHealth])

  // Export streams from the API (not from component state) so the file is the
  // full importable record set, not the display projection shown in the table.
  const handleExport = useCallback(() => {
    window.location.href = '/api/meshcore/contacts/export'
  }, [])

  const handleDelete = useCallback(async (c: MeshcoreContact) => {
    setDeletingId(c.pubkey)
    setSaveError(null)
    try {
      const result = await removeMeshcoreContact(c.pubkey)
      setData((prev) => ({ active: true, contacts: result.contacts, last_synced_at: prev?.last_synced_at ?? null }))
      setConfirmDelete(null)
      loadRouteHealth()
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Delete failed')
    } finally {
      setDeletingId((d) => (d === c.pubkey ? null : d))
    }
  }, [loadRouteHealth])

  const handleCopyPubkey = useCallback(async (pubkey: string) => {
    try {
      await navigator.clipboard.writeText(pubkey)
    } catch {
      // clipboard unavailable (non-secure context) — no-op
    }
  }, [])

  const toggleSort = useCallback((key: SortKey) => {
    setSortKey((prev) => {
      if (prev === key) {
        setSortAsc((asc) => !asc)
        return prev
      }
      setSortAsc(true)
      return key
    })
  }, [])

  // Names collide across keypairs, so a name alone cannot identify a contact.
  // Flag the ambiguous ones inline rather than leaving two identical rows.
  const collidingNames = useMemo(() => {
    const names = new Set<string>()
    for (const collision of routeHealth?.collisions ?? []) names.add(collision.name)
    return names
  }, [routeHealth])

  const visibleContacts = useMemo(() => {
    let list = data?.contacts ?? []
    const needle = search.trim().toLowerCase()
    if (needle) {
      list = list.filter(
        (c) =>
          (c.name ?? '').toLowerCase().includes(needle) ||
          c.pubkey.toLowerCase().includes(needle)
      )
    }
    if (typeFilter === 'rooms') list = list.filter((c) => c.type === 3)
    else if (typeFilter === 'stale') list = list.filter(isStale)

    const sorted = [...list].sort((a, b) => {
      let cmp = 0
      if (sortKey === 'name') {
        cmp = (a.name ?? '').localeCompare(b.name ?? '')
      } else if (sortKey === 'type') {
        cmp = (a.type ?? 0) - (b.type ?? 0)
      } else {
        cmp = (a.last_advert ?? 0) - (b.last_advert ?? 0)
      }
      return sortAsc ? cmp : -cmp
    })
    return sorted
  }, [data, search, typeFilter, sortKey, sortAsc])

  const staleCount = useMemo(
    () => (data?.contacts ?? []).filter(isStale).length,
    [data]
  )
  const roomCount = useMemo(
    () => (data?.contacts ?? []).filter((c) => c.type === 3).length,
    [data]
  )

  const handleSaveInterval = useCallback(async () => {
    if (!connectionConfig) return
    const minutes = Math.max(MIN_INTERVAL_MINUTES, Math.round(intervalMinutes) || MIN_INTERVAL_MINUTES)
    const nextConfig: ConnectionConfig = {
      ...connectionConfig,
      meshcore_telemetry_interval_seconds: minutes * 60,
    }
    setIntervalSaving(true)
    setIntervalSaved(false)
    setSaveError(null)
    try {
      await updateConfig('connection', nextConfig)
      setConnectionConfig(nextConfig)
      setIntervalMinutes(minutes)
      setIntervalSaved(true)
      setTimeout(() => setIntervalSaved(false), 2000)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save interval')
    } finally {
      setIntervalSaving(false)
    }
  }, [connectionConfig, intervalMinutes])

  const rosterActive = data?.active !== false
  const dangling = routeHealth?.dangling ?? []
  const collisions = routeHealth?.collisions ?? []

  return (
    <div className="max-w-5xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center gap-4">
        <div className="flex-shrink-0 w-12 h-12 rounded-lg bg-[#0a0e17] border border-[#1e2a3a] flex items-center justify-center">
          <Users size={24} className="text-accent" />
        </div>
        <div>
          <h2 className="text-xl font-semibold text-slate-100">MeshCore Contacts</h2>
          <p className="text-sm text-[#777]">
            The companion's known contact roster &mdash; names, types, last-heard times, and
            telemetry auto-poll.
          </p>
        </div>
      </div>

      {/* Dangling-route warning — a cell pointing into the void fails SILENTLY
          at send time, so this is the only place it becomes visible. */}
      {dangling.length > 0 && (
        <div className="border border-red-500/40 bg-red-500/10 p-4 space-y-2">
          <div className="flex items-start gap-3">
            <AlertTriangle size={18} className="text-red-400 flex-shrink-0 mt-0.5" />
            <div className="space-y-2 min-w-0">
              <h3 className="text-sm font-semibold text-red-300">
                {dangling.length} routing {dangling.length === 1 ? 'cell points' : 'cells point'} at a
                destination that no longer exists
              </h3>
              <p className="text-xs text-red-300/70 max-w-prose">
                These cells cannot be delivered — a send to a missing room or channel fails
                silently. Fix the target on the Routing page, or resync if the roster is stale.
              </p>
              <ul className="space-y-1">
                {dangling.map((d) => (
                  <li
                    key={`${d.family}-${d.region}-${d.target}`}
                    className="text-xs text-slate-200 flex flex-wrap items-center gap-x-2 gap-y-1"
                  >
                    <span className="px-1.5 py-0.5 rounded bg-red-500/20 text-red-300 uppercase tracking-wide text-[10px]">
                      {d.family}
                    </span>
                    <span className="text-slate-300">{d.region}</span>
                    <span className="text-[#777]">→</span>
                    <span className="font-mono text-[11px] text-red-300 break-all">{d.target}</span>
                    <span className="text-[#777]">
                      — {DANGLING_REASON[d.reason] ?? d.reason}
                    </span>
                    {!d.enabled && (
                      <span className="px-1.5 py-0.5 rounded bg-slate-600/30 text-slate-400 text-[10px] uppercase tracking-wide">
                        disabled
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* Name collisions — two keypairs advertising the same name are
          indistinguishable in any name-based picker. */}
      {collisions.length > 0 && (
        <div className="border border-amber-500/40 bg-amber-500/10 p-4">
          <div className="flex items-start gap-3">
            <AlertTriangle size={18} className="text-amber-400 flex-shrink-0 mt-0.5" />
            <div className="space-y-2 min-w-0">
              <h3 className="text-sm font-semibold text-amber-300">
                {collisions.length} duplicated {collisions.length === 1 ? 'name' : 'names'} on the roster
              </h3>
              <p className="text-xs text-amber-300/70 max-w-prose">
                These names each map to more than one public key. A name alone cannot identify
                them — always confirm the key before routing to or deleting one.
              </p>
              <ul className="space-y-1">
                {collisions.map((c) => (
                  <li key={c.name} className="text-xs text-slate-200">
                    <span className="text-slate-100">{c.name}</span>{' '}
                    <span className="text-[#777]">×{c.count}</span>
                    <span className="ml-2 font-mono text-[11px] text-amber-300/80">
                      {c.contacts.map((x) => shortPubkey(x.pubkey)).join('  ·  ')}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* Roster toolbar: sync state + resync/export */}
      {rosterActive && (
        <div className="bg-bg-card border border-border p-4 space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={handleResync}
              disabled={resyncing}
              className="flex items-center gap-2 px-3 py-1.5 text-sm rounded bg-accent/15 text-accent hover:bg-accent/25 disabled:opacity-50"
              title="Refetch the full roster from the companion and drop entries it no longer has"
            >
              <RefreshCw size={14} className={resyncing ? 'animate-spin' : undefined} />
              {resyncing ? 'Resyncing…' : 'Resync from node'}
            </button>
            <button
              onClick={handleExport}
              className="flex items-center gap-2 px-3 py-1.5 text-sm rounded bg-[#0a0e17] border border-[#1e2a3a] text-slate-200 hover:border-accent/40"
              title="Download the roster as JSON"
            >
              <Download size={14} />
              Export JSON
            </button>
            <button
              onClick={() => { setShowAdd((s) => !s); setAddError(null) }}
              className="flex items-center gap-2 px-3 py-1.5 text-sm rounded bg-[#0a0e17] border border-[#1e2a3a] text-slate-200 hover:border-accent/40"
              title="Add a contact by public key"
            >
              <Plus size={14} />
              Add contact
            </button>
            <span className="text-xs text-[#777]">
              Last synced {lastSyncedAt != null ? relativeTime(lastSyncedAt) : 'unknown'}
            </span>
            {resyncStats && (
              <span className="text-xs text-slate-300">
                <span className="text-emerald-400">+{resyncStats.added} added</span>
                {' · '}
                <span className="text-red-400">−{resyncStats.removed} removed</span>
                {' · '}
                <span className="text-[#777]">{resyncStats.updated} updated</span>
                {' · '}
                <span className="text-[#777]">{resyncStats.after} total</span>
                {channelStats && (
                  <span className="text-[#777]">
                    {' · '}channels {channelStats.after}
                    {channelStats.added.length > 0 && (
                      <span className="text-emerald-400"> +{channelStats.added.length}</span>
                    )}
                    {channelStats.removed.length > 0 && (
                      <span className="text-red-400"> −{channelStats.removed.length}</span>
                    )}
                  </span>
                )}
              </span>
            )}
          </div>

          {/* Add a contact by key — the companion learns most nodes by advert,
              but a rekeyed or out-of-range node has to be entered manually. */}
          {showAdd && (
            <div className="border border-[#1e2a3a] bg-[#0a0e17] p-3 space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <input
                  value={addName}
                  onChange={(e) => setAddName(e.target.value)}
                  placeholder="Name"
                  className="w-40 px-2 py-1 text-sm bg-bg-card border border-[#1e2a3a] rounded text-slate-100 placeholder:text-[#555]"
                />
                <input
                  value={addPubkey}
                  onChange={(e) => setAddPubkey(e.target.value)}
                  placeholder="Full 64-character hex public key"
                  className="flex-1 min-w-[280px] px-2 py-1 text-sm font-mono bg-bg-card border border-[#1e2a3a] rounded text-slate-100 placeholder:text-[#555]"
                />
                <select
                  value={addType}
                  onChange={(e) => setAddType(Number(e.target.value))}
                  className="px-2 py-1 text-sm bg-bg-card border border-[#1e2a3a] rounded text-slate-200"
                >
                  <option value={1}>Chat</option>
                  <option value={2}>Repeater</option>
                  <option value={3}>Room</option>
                  <option value={4}>Sensor</option>
                </select>
                <button
                  onClick={handleAdd}
                  disabled={adding}
                  className="px-3 py-1 text-sm rounded bg-accent/15 text-accent hover:bg-accent/25 disabled:opacity-50"
                >
                  {adding ? 'Adding…' : 'Add'}
                </button>
                <button
                  onClick={() => setShowAdd(false)}
                  className="px-2 py-1 text-sm text-[#777] hover:text-slate-200"
                >
                  Cancel
                </button>
              </div>
              {addError && <p className="text-xs text-red-400">{addError}</p>}
              <p className="text-xs text-[#777] max-w-prose">
                Writes the contact straight to the companion &mdash; nothing is transmitted. Use this
                when a node has been rebuilt with a new keypair, or is not yet in range to advert.
              </p>
            </div>
          )}

          <p className="text-xs text-[#777] max-w-prose">
            The roster and channel list are a snapshot cached from the companion at connect. Resync
            re-reads both from the node and reconciles them &mdash; the only action that removes
            entries the companion has dropped, or picks up a channel added on the radio.
          </p>
        </div>
      )}

      {/* Auto-poll interval control */}
      {rosterActive && connectionConfig && (
        <div className="bg-bg-card border border-border p-4 space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-sm text-slate-200">Auto-poll every</label>
            <input
              type="number"
              min={MIN_INTERVAL_MINUTES}
              value={intervalMinutes}
              onChange={(e) => setIntervalMinutes(Number(e.target.value))}
              className="w-20 px-2 py-1 text-sm bg-[#0a0e17] border border-[#1e2a3a] rounded text-slate-100"
            />
            <span className="text-sm text-slate-300">minutes</span>
            <button
              onClick={handleSaveInterval}
              disabled={intervalSaving}
              className="px-3 py-1 text-sm rounded bg-accent/15 text-accent hover:bg-accent/25 disabled:opacity-50"
            >
              {intervalSaving ? 'Saving…' : intervalSaved ? 'Saved' : 'Save'}
            </button>
          </div>
          <p className="text-xs text-[#777] max-w-prose">
            Polls only the nodes you select below. Keep this list small &mdash; telemetry uses mesh
            airtime. Minimum {MIN_INTERVAL_MINUTES} minutes.
          </p>
        </div>
      )}

      {saveError && (
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">
          {saveError}
        </div>
      )}

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
        <div className="bg-bg-card border border-border">
          {/* Filter / search toolbar */}
          <div className="flex flex-wrap items-center gap-3 px-4 py-3 border-b border-border">
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search name or pubkey…"
              className="flex-1 min-w-[180px] px-2 py-1 text-sm bg-[#0a0e17] border border-[#1e2a3a] rounded text-slate-100 placeholder:text-[#555]"
            />
            <div className="flex gap-1">
              {([
                { key: 'all', label: `All ${data?.contacts.length ?? 0}` },
                { key: 'rooms', label: `Rooms ${roomCount}` },
                { key: 'stale', label: `Stale ${staleCount}` },
              ] as const).map(({ key, label }) => (
                <button
                  key={key}
                  onClick={() => setTypeFilter(key)}
                  className={`px-2.5 py-1 text-xs rounded border transition-colors ${
                    typeFilter === key
                      ? 'border-accent/40 bg-accent/15 text-accent'
                      : 'border-[#1e2a3a] bg-[#0a0e17] text-[#777] hover:text-slate-200'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-[#777]">
                <th className="px-4 py-2.5 font-medium">
                  <button onClick={() => toggleSort('name')} className="hover:text-slate-200 uppercase">
                    Name{sortKey === 'name' ? (sortAsc ? ' ▲' : ' ▼') : ''}
                  </button>
                </th>
                <th className="px-4 py-2.5 font-medium">
                  <button onClick={() => toggleSort('type')} className="hover:text-slate-200 uppercase">
                    Type{sortKey === 'type' ? (sortAsc ? ' ▲' : ' ▼') : ''}
                  </button>
                </th>
                <th className="px-4 py-2.5 font-medium">
                  <button onClick={() => toggleSort('last_advert')} className="hover:text-slate-200 uppercase">
                    Last heard{sortKey === 'last_advert' ? (sortAsc ? ' ▲' : ' ▼') : ''}
                  </button>
                </th>
                <th className="px-4 py-2.5 font-medium">Position</th>
                <th className="px-4 py-2.5 font-medium">Pubkey</th>
                <th className="px-4 py-2.5 font-medium">Auto-poll</th>
                <th className="px-4 py-2.5 font-medium"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {visibleContacts.map((c) => {
                const id = contactId(c)
                const entry = entryFor(c)
                const selected = isSelected(c)
                const pollResult = pollResults[id]
                // A contact is unavailable if its cached entry says so.
                const unavailable = entry != null && entry.available === false
                const toggleDisabled = savingId === id || (unavailable && !selected)

                // Resolve the readout to show: a fresh Poll-now result wins,
                // otherwise the cached telemetry entry.
                let readoutData: MeshcoreTelemetryData | null = null
                let readoutLabel = ''
                let showUnavailable = false
                if (pollResult) {
                  if (pollResult.available && pollResult.data) {
                    readoutData = pollResult.data
                    readoutLabel = 'just now'
                  } else {
                    showUnavailable = true
                  }
                } else if (entry) {
                  if (entry.available && entry.data) {
                    readoutData = entry.data
                    readoutLabel = relativeTimeIso(entry.polled_at)
                  } else {
                    showUnavailable = true
                  }
                }
                const hasReadout = readoutData != null || showUnavailable

                return (
                  <Fragment key={c.pubkey}>
                    <tr className="hover:bg-bg-hover">
                      <td className="px-4 py-2.5 text-slate-100">
                        <div className="flex items-center gap-2">
                          <span>{contactName(c)}</span>
                          {c.name != null && collidingNames.has(c.name) && (
                            <span
                              className="px-1.5 py-0.5 text-[10px] uppercase tracking-wide rounded bg-amber-500/15 text-amber-400"
                              title="Another contact advertises this same name with a different key — check the pubkey"
                            >
                              dup name
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <TypeBadge type={c.type} />
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-2">
                          <span className="text-slate-300">{relativeTime(c.last_advert)}</span>
                          {isStale(c) && (
                            <span
                              className="px-1.5 py-0.5 text-[10px] uppercase tracking-wide rounded bg-orange-500/15 text-orange-400"
                              title={`Not heard from in over ${STALE_AFTER_DAYS} days`}
                            >
                              stale
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-slate-300 font-mono text-xs">{position(c)}</td>
                      <td className="px-4 py-2.5 text-slate-400 font-mono text-xs">
                        <div className="flex items-center gap-1.5">
                          <button
                            onClick={() => setExpandedKey((k) => (k === c.pubkey ? null : c.pubkey))}
                            className="hover:text-accent"
                            title={c.pubkey}
                          >
                            {expandedKey === c.pubkey ? c.pubkey : shortPubkey(c.pubkey)}
                          </button>
                          <button
                            onClick={() => handleCopyPubkey(c.pubkey)}
                            className="text-[#555] hover:text-accent flex-shrink-0"
                            title="Copy full pubkey"
                          >
                            <Copy size={11} />
                          </button>
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <label
                          className={`inline-flex items-center gap-2 ${
                            toggleDisabled ? 'opacity-50' : 'cursor-pointer'
                          }`}
                          title={unavailable && !selected ? 'no telemetry available' : undefined}
                        >
                          <input
                            type="checkbox"
                            checked={selected}
                            disabled={toggleDisabled}
                            onChange={(e) => handleToggle(c, e.target.checked)}
                            className="accent-accent"
                          />
                          <span className="text-xs text-slate-300">
                            {savingId === id
                              ? 'saving…'
                              : savedId === id
                              ? 'saved'
                              : unavailable && !selected
                              ? 'no telemetry'
                              : 'auto-poll'}
                          </span>
                        </label>
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            onClick={() => handlePollNow(c)}
                            disabled={pollingId === id}
                            className="px-2 py-1 text-xs rounded bg-accent/15 text-accent hover:bg-accent/25 disabled:opacity-50"
                          >
                            {pollingId === id ? 'Polling…' : 'Poll now'}
                          </button>
                          {/* Two-step delete: removal from the companion is
                              permanent — the node must be rediscovered. */}
                          {confirmDelete === c.pubkey ? (
                            <>
                              <button
                                onClick={() => handleDelete(c)}
                                disabled={deletingId === c.pubkey}
                                className="px-2 py-1 text-xs rounded bg-red-500/20 text-red-300 hover:bg-red-500/30 disabled:opacity-50"
                              >
                                {deletingId === c.pubkey ? 'Deleting…' : 'Confirm'}
                              </button>
                              <button
                                onClick={() => setConfirmDelete(null)}
                                className="px-2 py-1 text-xs rounded text-[#777] hover:text-slate-200"
                              >
                                Cancel
                              </button>
                            </>
                          ) : (
                            <button
                              onClick={() => setConfirmDelete(c.pubkey)}
                              className="p-1 rounded text-[#555] hover:text-red-400 hover:bg-red-500/10"
                              title="Remove this contact from the companion"
                            >
                              <Trash2 size={13} />
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                    {confirmDelete === c.pubkey && (
                      <tr className="bg-red-500/5">
                        <td colSpan={7} className="px-4 py-2 border-t border-red-500/20">
                          <span className="text-xs text-red-300">
                            Remove <span className="text-slate-100">{contactName(c)}</span>{' '}
                            <span className="font-mono text-[11px]">{shortPubkey(c.pubkey)}</span>{' '}
                            from the companion? It will only return if the node advertises again.
                          </span>
                        </td>
                      </tr>
                    )}
                    {hasReadout && (
                      <tr className="bg-[#0a0e17]/40">
                        <td colSpan={7} className="px-4 py-2 border-t border-border/50">
                          {readoutData ? (
                            <TelemetryReadout data={readoutData} polledLabel={readoutLabel} />
                          ) : (
                            <span className="text-xs text-[#777]">
                              no telemetry
                              {pollResult?.detail ? ` — ${pollResult.detail}` : ''}
                            </span>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
          {visibleContacts.length === 0 && (
            <div className="px-4 py-6 text-sm text-[#777]">
              No contacts match this filter.
            </div>
          )}
          </div>
        </div>
      )}
    </div>
  )
}
