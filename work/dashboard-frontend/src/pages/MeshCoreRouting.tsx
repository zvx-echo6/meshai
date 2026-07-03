import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { useDirty } from '@/context/DirtyContext'
import { Save, RotateCcw, RefreshCw, Check, MessageSquare, ExternalLink } from 'lucide-react'
import {
  SeverityChannelMatrix,
  ListInput,
  InfoButton,
  TOGGLE_FAMILY_META,
  MC_CHANNELS,
  type NotificationToggle,
  type NotificationsConfig,
} from './Notifications'

// Merge only the MeshCore-owned fields of `mine` into `fresh`, preserving every
// other (Meshtastic / Other-channels / general) field on the family. The
// severity matrix stores all channels in one dict per severity, so we keep the
// non-meshcore_* entries from the freshly-fetched config and overlay only the
// meshcore_* entries edited on this page.
function mergeMeshcoreFields(
  fresh: NotificationToggle | undefined,
  mine: NotificationToggle,
  key: string,
): NotificationToggle {
  const base: NotificationToggle = fresh ? { ...fresh } : { ...mine, name: key }
  base.name = base.name || key

  const freshSC = fresh?.severity_channels || {}
  const mineSC = mine.severity_channels || {}
  const severities = new Set([...Object.keys(freshSC), ...Object.keys(mineSC)])
  const mergedSC: Record<string, string[]> = {}
  severities.forEach((sev) => {
    const nonMeshcore = (freshSC[sev] || []).filter((c) => !c.startsWith('meshcore_'))
    const meshcore = (mineSC[sev] || []).filter((c) => c.startsWith('meshcore_'))
    mergedSC[sev] = [...nonMeshcore, ...meshcore]
  })
  base.severity_channels = mergedSC
  base.meshcore_channel = mine.meshcore_channel ?? null
  base.meshcore_dm_contacts = mine.meshcore_dm_contacts || []
  return base
}

export default function MeshCoreRouting() {
  const { setDirty } = useDirty()
  const [config, setConfig] = useState<NotificationsConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<NotificationsConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  const fetchConfig = useCallback(async () => {
    try {
      const res = await fetch('/api/config/notifications')
      if (!res.ok) throw new Error('Failed to fetch notifications config')
      const data: NotificationsConfig = await res.json()
      setConfig(data)
      setOriginalConfig(JSON.parse(JSON.stringify(data)))
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'MeshCore Routing - MeshAI'
    fetchConfig()
  }, [fetchConfig])

  useEffect(() => {
    if (config && originalConfig) {
      setHasChanges(JSON.stringify(config) !== JSON.stringify(originalConfig))
    }
  }, [config, originalConfig])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const upd = (fam: string, patch: Partial<NotificationToggle>) => {
    if (!config) return
    const toggles = config.toggles || {}
    setConfig({
      ...config,
      toggles: {
        ...toggles,
        [fam]: { ...(toggles[fam] || {}), name: fam, ...patch } as NotificationToggle,
      },
    })
  }

  const saveConfig = async () => {
    if (!config) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      // Re-fetch the live config and merge ONLY the MeshCore fields so we never
      // clobber concurrent edits made on the Meshtastic Routing page.
      const freshRes = await fetch('/api/config/notifications')
      if (!freshRes.ok) throw new Error('Failed to re-fetch notifications config')
      const fresh: NotificationsConfig = await freshRes.json()

      const merged: NotificationsConfig = { ...fresh, toggles: { ...(fresh.toggles || {}) } }
      const myToggles = config.toggles || {}
      for (const { key } of TOGGLE_FAMILY_META) {
        const mine = myToggles[key]
        if (!mine) continue
        merged.toggles![key] = mergeMeshcoreFields((fresh.toggles || {})[key], mine, key)
      }

      const res = await fetch('/api/config/notifications', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(merged),
      })
      const result = await res.json()
      if (!res.ok) throw new Error(result.detail || 'Save failed')

      setConfig(merged)
      setOriginalConfig(JSON.parse(JSON.stringify(merged)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('MeshCore routing saved successfully')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const discardChanges = () => {
    if (originalConfig) {
      setConfig(JSON.parse(JSON.stringify(originalConfig)))
      setHasChanges(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading MeshCore routing...</div>
      </div>
    )
  }

  if (!config) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Failed to load notifications config</div>
      </div>
    )
  }

  const toggles = config.toggles || {}

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">
            Per-family MeshCore delivery. Choose which channels fire at each severity, the
            MeshCore channel name, and DM contacts.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchConfig}
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

      {/* Cross-link note: gating on Data Feeds, MT+Other delivery on Meshtastic Routing */}
      <div className="flex items-start gap-2 p-3 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-400">
        <ExternalLink size={16} className="text-accent mt-0.5 flex-shrink-0" />
        <div>
          Family gating (enable, severity threshold, freshness/cooldown) is on{' '}
          <Link to="/environment" className="text-accent hover:underline">
            Data Feeds
          </Link>
          . Meshtastic and email/webhook/digest delivery is on{' '}
          <Link to="/notifications" className="text-accent hover:underline">
            Meshtastic Routing
          </Link>
          . This page edits only the MeshCore delivery for each family.
        </div>
      </div>

      {/* Status messages */}
      {error && (
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{error}</div>
      )}
      {success && (
        <div className="p-3 text-sm bg-green-500/10 text-green-400 border border-green-500/20">
          <Check size={14} className="inline mr-2" />
          {success}
        </div>
      )}

      {/* Per-family MeshCore delivery */}
      <div className="bg-bg-card border border-border p-6 space-y-4">
        <div className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
          MeshCore Delivery
          <InfoButton info="For each notification family, choose which MeshCore channels fire at each severity, the MeshCore channel name to broadcast on, and the DM contacts to unicast to. Enabling a family and its severity threshold are configured on the Data Feeds page." />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {TOGGLE_FAMILY_META.map(({ key, label, Icon }) => {
            const t = toggles[key] || ({} as NotificationToggle)
            return (
              <div key={key} className="border border-[#1e2a3a] p-3 space-y-3">
                <div className="flex items-center gap-2 text-sm text-slate-200">
                  <Icon size={15} /> {label}
                </div>

                <div className="space-y-3 p-3 bg-[#0a0e17] border border-[#1e2a3a]">
                  <div className="flex items-center gap-2 text-xs font-medium text-slate-300">
                    <MessageSquare size={13} />
                    MeshCore
                  </div>
                  <SeverityChannelMatrix
                    channels={MC_CHANNELS}
                    severityChannels={t.severity_channels || {}}
                    onChange={(sc) => upd(key, { severity_channels: sc })}
                  />
                  <div className="space-y-1">
                    <label className="text-xs text-slate-500 uppercase tracking-wide">
                      MeshCore channel name
                    </label>
                    <input
                      type="text"
                      value={t.meshcore_channel != null ? t.meshcore_channel : ''}
                      onChange={(e) =>
                        upd(key, { meshcore_channel: e.target.value === '' ? null : e.target.value })
                      }
                      placeholder="AIDA"
                      className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
                    />
                    <p className="text-xs text-slate-600">
                      Channel name on your MeshCore companion (e.g. AIDA). Blank = not broadcast on
                      MeshCore.
                    </p>
                  </div>
                  <ListInput
                    label="MeshCore DM contacts"
                    value={t.meshcore_dm_contacts || []}
                    onChange={(v) => upd(key, { meshcore_dm_contacts: v })}
                    placeholder="contact name or pubkey"
                    helper="MeshCore DM recipients (names or pubkeys)"
                    info="Contact names or pubkeys on the MeshCore companion. Used when meshcore_dm is enabled for a severity."
                  />
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
