import { useState, useEffect, useCallback } from 'react'
import { Save, RotateCcw, RefreshCw, Check } from 'lucide-react'
import { fetchConfig as apiFetchConfig, updateConfig as apiUpdateConfig } from '@/lib/api'
import { useDirty } from '@/context/DirtyContext'
import { notifyRestartRequired } from '@/components/RestartBanner'
import {
  Toggle, NumberInput, TimeInput, InfoButton,
  type NotificationsConfig,
} from '@/pages/Notifications'

// Fires adapter config shape (digest settings)
interface FiresConfig {
  digest_enabled: boolean
  digest_schedule: string[]
  digest_timezone: string
}

interface Props {
  family?: 'meshtastic' | 'meshcore'
}

export default function ScheduledBroadcasts({ family = 'meshtastic' }: Props) {
  const { setDirty } = useDirty()

  // Notifications config state (full object — read-modify-write to preserve other fields)
  const [notifConfig, setNotifConfig] = useState<NotificationsConfig | null>(null)
  const [originalNotifConfig, setOriginalNotifConfig] = useState<NotificationsConfig | null>(null)

  // Fires adapter config state
  const [firesConfig, setFiresConfig] = useState<FiresConfig>({
    digest_enabled: true,
    digest_schedule: ['06:00', '18:00'],
    digest_timezone: 'America/Boise',
  })
  const [originalFiresConfig, setOriginalFiresConfig] = useState<string>('')

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  const fetchData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      // Load notifications config (full object for read-modify-write)
      const notif = (await apiFetchConfig('notifications')) as NotificationsConfig
      setNotifConfig(notif)
      setOriginalNotifConfig(JSON.parse(JSON.stringify(notif)))

      // Load fires adapter config
      try {
        const firesRes = await fetch('/api/adapter-config/fires')
        if (firesRes.ok) {
          const firesData = await firesRes.json()
          const fires: FiresConfig = {
            digest_enabled: firesData.digest_enabled?.value ?? true,
            digest_schedule: firesData.digest_schedule?.value ?? ['06:00', '18:00'],
            digest_timezone: firesData.digest_timezone?.value ?? 'America/Boise',
          }
          setFiresConfig(fires)
          setOriginalFiresConfig(JSON.stringify(fires))
        }
      } catch {
        // adapter-config optional — proceed with defaults
        setOriginalFiresConfig(JSON.stringify(firesConfig))
      }

      setHasChanges(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load config')
    } finally {
      setLoading(false)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    document.title = `Scheduled Broadcasts - MeshAI`
    fetchData()
  }, [fetchData])

  useEffect(() => {
    if (notifConfig && originalNotifConfig) {
      const notifChanged = JSON.stringify(notifConfig) !== JSON.stringify(originalNotifConfig)
      const firesChanged = JSON.stringify(firesConfig) !== originalFiresConfig
      setHasChanges(notifChanged || firesChanged)
    }
  }, [notifConfig, originalNotifConfig, firesConfig, originalFiresConfig])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const saveAdapterKey = async (adapter: string, key: string, value: unknown) => {
    const res = await fetch(`/api/adapter-config/${adapter}/${key}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `Failed to save ${adapter}.${key}`)
    }
  }

  const saveConfig = async () => {
    if (!notifConfig) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      // Save full notifications config (read-modify-write — band/cold-start keys round-trip)
      const result = await apiUpdateConfig('notifications', notifConfig)
      setOriginalNotifConfig(JSON.parse(JSON.stringify(notifConfig)))
      if (result.restart_required) notifyRestartRequired([])

      // Save fires adapter config — only changed keys
      const origFires = originalFiresConfig ? (JSON.parse(originalFiresConfig) as FiresConfig) : null
      if (!origFires || firesConfig.digest_enabled !== origFires.digest_enabled) {
        await saveAdapterKey('fires', 'digest_enabled', firesConfig.digest_enabled)
      }
      if (!origFires || JSON.stringify(firesConfig.digest_schedule) !== JSON.stringify(origFires.digest_schedule)) {
        await saveAdapterKey('fires', 'digest_schedule', firesConfig.digest_schedule)
      }
      if (!origFires || firesConfig.digest_timezone !== origFires.digest_timezone) {
        await saveAdapterKey('fires', 'digest_timezone', firesConfig.digest_timezone)
      }
      setOriginalFiresConfig(JSON.stringify(firesConfig))

      setHasChanges(false)
      setDirty(false)
      setSuccess('Scheduled broadcasts saved successfully')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const discardChanges = () => {
    if (originalNotifConfig) setNotifConfig(JSON.parse(JSON.stringify(originalNotifConfig)))
    if (originalFiresConfig) setFiresConfig(JSON.parse(originalFiresConfig))
    setHasChanges(false)
  }

  const subtitle = family === 'meshcore'
    ? 'MeshCore scheduled broadcasts and band condition reports.'
    : 'Meshtastic scheduled broadcasts and band condition reports.'

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading scheduled broadcasts...</div>
      </div>
    )
  }

  if (!notifConfig) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Failed to load config</div>
      </div>
    )
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header / save bar */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">{subtitle}</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchData}
            className="p-2 text-slate-400 hover:text-slate-200 hover:bg-bg-hover rounded transition-colors"
            title="Refresh"
          >
            <RefreshCw size={18} />
          </button>
          <button
            onClick={discardChanges}
            disabled={!hasChanges}
            className="flex items-center gap-2 px-3 py-2 text-slate-400 hover:text-slate-200 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <RotateCcw size={16} />
            Discard
          </button>
          <button
            onClick={saveConfig}
            disabled={saving || !hasChanges}
            className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent/80 disabled:bg-slate-700 disabled:cursor-not-allowed rounded text-white transition-colors"
          >
            <Save size={16} />
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>

      {/* Status messages */}
      {error && (
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{error}</div>
      )}
      {success && (
        <div className="p-3 text-sm bg-green-500/10 text-green-400 border border-green-500/20">
          <Check size={14} className="inline mr-2" />{success}
        </div>
      )}

      {/* Cold-start grace */}
      <div className="bg-bg-card border border-border p-6 space-y-4">
        <div className="flex items-center gap-2">
          <label className="text-xs text-slate-500 uppercase tracking-wide">Cold-start grace</label>
        </div>
        <NumberInput
          label="Grace period (seconds)"
          value={notifConfig.cold_start_grace_seconds ?? 60}
          onChange={(v) => setNotifConfig({ ...notifConfig, cold_start_grace_seconds: v })}
          min={0}
          max={600}
          helper="Suppress broadcasts for this many seconds after the first event arrives"
          info="When meshai starts seeing events for the first time, suppress mesh broadcasts for this many seconds to absorb any JetStream backlog. Persistence rows still get written; only broadcasts are suppressed."
        />
      </div>

      {/* Band Conditions */}
      <div className="bg-bg-card border border-border p-6 space-y-4">
        <div className="flex items-center gap-2">
          <label className="text-xs text-slate-500 uppercase tracking-wide">Band Conditions (HF propagation)</label>
        </div>
        <Toggle
          label="Enable scheduled band-conditions broadcasts"
          checked={notifConfig.band_conditions_enabled ?? true}
          onChange={(v) => setNotifConfig({ ...notifConfig, band_conditions_enabled: v })}
          helper="3x/day HF propagation summary (Day/Night ratings per band group). The daily fire digest (twice-daily LLM summary of active fires + the last 24h of growth/spotting) is configured separately under Adapter Config -> fires.digest_*. See Reference -> Fire Tracker (Fusion) and Reference -> Broadcast Types for the New/Update/Active prefix system."
          info="Source priority: (1) recent SWPC readings persisted locally; (2) HamQSL.com fallback; (3) silent skip if both fail. Persistence rows are written either way for an audit trail."
        />
        {(notifConfig.band_conditions_enabled ?? true) && (
          <div className="grid grid-cols-3 gap-3">
            <TimeInput
              label="Slot 1"
              value={(notifConfig.band_conditions_schedule ?? ['06:00', '14:00', '22:00'])[0] || '06:00'}
              onChange={(v) => {
                const s = [...(notifConfig.band_conditions_schedule ?? ['06:00', '14:00', '22:00'])]
                s[0] = v
                setNotifConfig({ ...notifConfig, band_conditions_schedule: s })
              }}
              helper="Morning (default 06:00 MT)"
            />
            <TimeInput
              label="Slot 2"
              value={(notifConfig.band_conditions_schedule ?? ['06:00', '14:00', '22:00'])[1] || '14:00'}
              onChange={(v) => {
                const s = [...(notifConfig.band_conditions_schedule ?? ['06:00', '14:00', '22:00'])]
                s[1] = v
                setNotifConfig({ ...notifConfig, band_conditions_schedule: s })
              }}
              helper="Afternoon (default 14:00 MT)"
            />
            <TimeInput
              label="Slot 3"
              value={(notifConfig.band_conditions_schedule ?? ['06:00', '14:00', '22:00'])[2] || '22:00'}
              onChange={(v) => {
                const s = [...(notifConfig.band_conditions_schedule ?? ['06:00', '14:00', '22:00'])]
                s[2] = v
                setNotifConfig({ ...notifConfig, band_conditions_schedule: s })
              }}
              helper="Night (default 22:00 MT)"
            />
          </div>
        )}
        <p className="text-xs text-slate-600">All times are Mountain Time (America/Boise). DST handled automatically.</p>
      </div>

      {/* Fire Digest */}
      <div className="bg-bg-card border border-border p-6 space-y-4">
        <div className="flex items-center gap-2">
          <label className="text-xs text-slate-500 uppercase tracking-wide">
            Fire Digest
            <InfoButton info="Twice-daily LLM summary of active fires + last 24h of growth/spotting events. Configured per the fires adapter." />
          </label>
        </div>
        <Toggle
          label="Enable fire digest broadcasts"
          checked={firesConfig.digest_enabled}
          onChange={(v) => setFiresConfig({ ...firesConfig, digest_enabled: v })}
          helper="Send a twice-daily digest of active fire conditions to the mesh"
        />
        {firesConfig.digest_enabled && (
          <div className="grid grid-cols-2 gap-3">
            <TimeInput
              label="Digest Slot 1"
              value={(firesConfig.digest_schedule ?? ['06:00', '18:00'])[0] || '06:00'}
              onChange={(v) => {
                const s = [...(firesConfig.digest_schedule ?? ['06:00', '18:00'])]
                s[0] = v
                setFiresConfig({ ...firesConfig, digest_schedule: s })
              }}
              helper="Morning digest (default 06:00 MT)"
            />
            <TimeInput
              label="Digest Slot 2"
              value={(firesConfig.digest_schedule ?? ['06:00', '18:00'])[1] || '18:00'}
              onChange={(v) => {
                const s = [...(firesConfig.digest_schedule ?? ['06:00', '18:00'])]
                s[1] = v
                setFiresConfig({ ...firesConfig, digest_schedule: s })
              }}
              helper="Evening digest (default 18:00 MT)"
            />
          </div>
        )}
        <div className="space-y-1">
          <label className="text-xs text-slate-500 uppercase tracking-wide">Timezone</label>
          <input
            type="text"
            value={firesConfig.digest_timezone}
            onChange={(e) => setFiresConfig({ ...firesConfig, digest_timezone: e.target.value })}
            placeholder="America/Boise"
            className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
          />
          <p className="text-xs text-slate-600">IANA timezone name (e.g. America/Boise). DST handled automatically.</p>
        </div>
      </div>
    </div>
  )
}
