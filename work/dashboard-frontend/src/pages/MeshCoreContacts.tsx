import { Fragment, useCallback, useEffect, useState } from 'react'
import { Users } from 'lucide-react'
import {
  fetchMeshcoreContacts,
  fetchMeshcoreTelemetry,
  fetchConnectionConfig,
  pollMeshcoreContact,
  updateConfig,
  type MeshcoreContacts,
  type MeshcoreContact,
  type MeshcoreTelemetry,
  type MeshcoreTelemetryEntry,
  type MeshcoreTelemetryData,
  type MeshcorePollResult,
  type ConnectionConfig,
} from '../lib/api'

const TELEMETRY_POLL_MS = 15000
const MIN_INTERVAL_MINUTES = 5

// Relative time for epoch-seconds fields (last_advert).
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
            The companion's known contact roster &mdash; names, types, last-heard times, and
            telemetry auto-poll.
          </p>
        </div>
      </div>

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
        <div className="bg-bg-card border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-[#777]">
                <th className="px-4 py-2.5 font-medium">Name</th>
                <th className="px-4 py-2.5 font-medium">Type</th>
                <th className="px-4 py-2.5 font-medium">Last heard</th>
                <th className="px-4 py-2.5 font-medium">Position</th>
                <th className="px-4 py-2.5 font-medium">Pubkey</th>
                <th className="px-4 py-2.5 font-medium">Auto-poll</th>
                <th className="px-4 py-2.5 font-medium"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(data?.contacts ?? []).map((c) => {
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
                      <td className="px-4 py-2.5 text-slate-100">{contactName(c)}</td>
                      <td className="px-4 py-2.5">
                        <TypeBadge type={c.type} />
                      </td>
                      <td className="px-4 py-2.5 text-slate-300">{relativeTime(c.last_advert)}</td>
                      <td className="px-4 py-2.5 text-slate-300 font-mono text-xs">{position(c)}</td>
                      <td className="px-4 py-2.5 text-slate-400 font-mono text-xs">
                        {shortPubkey(c.pubkey)}
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
                        <button
                          onClick={() => handlePollNow(c)}
                          disabled={pollingId === id}
                          className="px-2 py-1 text-xs rounded bg-accent/15 text-accent hover:bg-accent/25 disabled:opacity-50"
                        >
                          {pollingId === id ? 'Polling…' : 'Poll now'}
                        </button>
                      </td>
                    </tr>
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
        </div>
      )}
    </div>
  )
}
