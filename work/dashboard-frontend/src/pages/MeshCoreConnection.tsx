import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { Save, RotateCcw, RefreshCw, Check } from 'lucide-react'
import { TextInput, NumberInput, Toggle, ListInput } from './Config'
import { notifyRestartRequired } from '@/components/RestartBanner'
import { fetchConfig as apiFetchConfig, updateConfig as apiUpdateConfig, getMeshcoreChannels, sendTestMessage } from '@/lib/api'
import { useDirty } from '@/context/DirtyContext'

// Only the fields this page edits are typed explicitly; the rest of the
// connection config (Meshtastic type / serial / tcp) is preserved untouched on
// save via object spread.
interface ConnectionConfig {
  type?: string
  serial_port?: string
  tcp_host?: string
  tcp_port?: number
  meshcore_host?: string
  meshcore_port?: number
  [key: string]: unknown
}

// MeshCore-native "Bot behavior" config (section `meshcore_context`).
// observe_channels are channel NAMES; ignore_contacts are contact names or
// pubkey prefixes. Unknown fields are preserved on save via object spread.
interface MeshcoreContextCfg {
  enable_passive_context?: boolean
  observe_channels?: string[]
  ignore_contacts?: string[]
  respond_to_dms?: boolean
  [key: string]: unknown
}

export default function MeshCoreConnection() {
  const { setDirty } = useDirty()
  const [config, setConfig] = useState<ConnectionConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<ConnectionConfig | null>(null)
  // Bot behavior (section `meshcore_context`)
  const [mcContext, setMcContext] = useState<MeshcoreContextCfg | null>(null)
  const [originalMcContext, setOriginalMcContext] = useState<MeshcoreContextCfg | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  // Test send state
  const [channelsActive, setChannelsActive] = useState(false)
  const [channels, setChannels] = useState<string[]>([])
  const [selectedChannel, setSelectedChannel] = useState('')
  const [testText, setTestText] = useState('')
  const [testSending, setTestSending] = useState(false)
  const [testResult, setTestResult] = useState<{ sent: boolean; detail: string } | null>(null)

  const fetchConfig = useCallback(async () => {
    setLoading(true)
    try {
      const [data, mcCtx] = await Promise.all([
        apiFetchConfig('connection') as Promise<ConnectionConfig>,
        apiFetchConfig('meshcore_context') as Promise<MeshcoreContextCfg>,
      ])
      setConfig(data)
      setOriginalConfig(JSON.parse(JSON.stringify(data)))
      setMcContext(mcCtx)
      setOriginalMcContext(JSON.parse(JSON.stringify(mcCtx)))
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'MeshCore Connection - MeshAI'
    fetchConfig()
  }, [fetchConfig])

  useEffect(() => {
    getMeshcoreChannels()
      .then((res) => {
        setChannelsActive(res.active)
        setChannels(res.channels)
        if (res.channels.length > 0) setSelectedChannel(res.channels[0])
      })
      .catch(() => {
        setChannelsActive(false)
      })
  }, [])

  const handleTestSend = async () => {
    setTestSending(true)
    setTestResult(null)
    try {
      const result = await sendTestMessage({
        transport: 'meshcore',
        channel: selectedChannel,
        text: testText.trim() || undefined,
      })
      setTestResult(result)
    } catch (err) {
      setTestResult({ sent: false, detail: err instanceof Error ? err.message : 'Send failed' })
    } finally {
      setTestSending(false)
    }
  }

  useEffect(() => {
    if (config && originalConfig && mcContext && originalMcContext) {
      const changed =
        JSON.stringify(config) !== JSON.stringify(originalConfig) ||
        JSON.stringify(mcContext) !== JSON.stringify(originalMcContext)
      setHasChanges(changed)
    }
  }, [config, originalConfig, mcContext, originalMcContext])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const upd = (patch: Partial<ConnectionConfig>) =>
    setConfig((c) => (c ? { ...c, ...patch } : c))

  const saveConfig = async () => {
    if (!config || !mcContext) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      // PUT the whole objects so sibling fields (Meshtastic connection fields,
      // any other meshcore_context keys) are preserved.
      const results = await Promise.all([
        apiUpdateConfig('connection', config),
        apiUpdateConfig('meshcore_context', mcContext),
      ])
      setOriginalConfig(JSON.parse(JSON.stringify(config)))
      setOriginalMcContext(JSON.parse(JSON.stringify(mcContext)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('MeshCore connection saved successfully')
      if (results.some((r) => r.restart_required)) {
        notifyRestartRequired([])
      }
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const discardChanges = () => {
    if (originalConfig) setConfig(JSON.parse(JSON.stringify(originalConfig)))
    if (originalMcContext) setMcContext(JSON.parse(JSON.stringify(originalMcContext)))
    setHasChanges(false)
  }

  const toggleObserveChannel = (name: string) => {
    setMcContext((c) => {
      if (!c) return c
      const current = c.observe_channels ?? []
      const next = current.includes(name)
        ? current.filter((n) => n !== name)
        : [...current, name]
      return { ...c, observe_channels: next }
    })
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading MeshCore connection...</div>
      </div>
    )
  }

  if (!config) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Failed to load connection config</div>
      </div>
    )
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">
            MeshCore node connection.
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

      {/* Form */}
      <div className="bg-bg-card border border-border p-6 space-y-4">
        <div className="text-xs text-slate-500 uppercase tracking-wide">MeshCore Connection</div>
        <p className="text-xs text-slate-500">
          Set the host and port to enable MeshCore; leave host blank to disable.
          Meshtastic is always active.
        </p>
        <div className="grid grid-cols-2 gap-4">
          <TextInput
            label="MeshCore Host"
            value={config.meshcore_host ?? ''}
            onChange={(v) => upd({ meshcore_host: v })}
            placeholder="192.168.1.100"
            helper="IP or hostname — leave blank to disable MeshCore"
            info="MeshCore is active when this field is non-empty."
          />
          <NumberInput
            label="MeshCore Port"
            value={config.meshcore_port ?? 5525}
            onChange={(v) => upd({ meshcore_port: v })}
            min={1}
            max={65535}
            helper="MeshCore TCP port (default 5525)"
          />
        </div>
        <div className="pt-2">
          <Link
            to="/meshtastic/connection"
            className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-accent transition-colors"
          >
            &rarr; Meshtastic connection
          </Link>
        </div>
      </div>

      {/* Bot behavior card — mirrors the Meshtastic Connection page, MeshCore-native */}
      {mcContext && (
        <div className="bg-bg-card border border-border p-6 space-y-4">
          <div className="text-xs text-slate-500 uppercase tracking-wide">Bot behavior</div>
          <Toggle
            label="Enable Passive Context"
            checked={!!mcContext.enable_passive_context}
            onChange={(v) => setMcContext({ ...mcContext, enable_passive_context: v })}
            helper="Listen to MeshCore channel traffic for context"
            info="When enabled, the bot monitors MeshCore channels and includes recent messages in its context so it can reference what others said."
          />
          {/* Observe MeshCore channels — multi-select of channel NAMES (empty = observe all) */}
          <div className="space-y-1">
            <label className="block text-xs text-slate-500 uppercase tracking-wide">Observe MeshCore Channels</label>
            <div className="border border-[#1e2a3a] p-2 space-y-1">
              {channels.map((ch) => {
                const selected = (mcContext.observe_channels ?? []).includes(ch)
                return (
                  <label
                    key={ch}
                    onClick={() => toggleObserveChannel(ch)}
                    className="flex items-center gap-2 p-2 rounded hover:bg-[#0a0e17] cursor-pointer"
                  >
                    <div className={`w-4 h-4 rounded border flex items-center justify-center ${
                      selected ? 'bg-accent border-accent' : 'border-slate-600'
                    }`}>
                      {selected && <Check size={12} className="text-white" />}
                    </div>
                    <span className="text-sm text-slate-200">{ch}</span>
                  </label>
                )
              })}
              {channels.length === 0 && (
                <div className="text-sm text-slate-500 p-2">
                  No channels available{!channelsActive ? ' (MeshCore not connected)' : ''}
                </div>
              )}
            </div>
            <p className="text-xs text-slate-600">Choose which MeshCore channels feed MeshAI's context. Empty = none are watched — pick channels to include their chatter in what the bot knows about the mesh. Leave busy/public channels out to keep them out of context.</p>
          </div>
          <ListInput
            label="Ignore MeshCore Contacts"
            value={mcContext.ignore_contacts ?? []}
            onChange={(v) => setMcContext({ ...mcContext, ignore_contacts: v })}
            helper="Contact names or pubkey prefixes to exclude from context (comma-separated)"
            info="Messages from these MeshCore contacts won't be included in passive context. Enter contact names or public-key prefixes."
          />
          <Toggle
            label="Answer direct messages"
            checked={!!mcContext.respond_to_dms}
            onChange={(v) => setMcContext({ ...mcContext, respond_to_dms: v })}
            helper="When on, MeshAI replies to MeshCore direct messages using the LLM. Applies to MeshCore only."
          />
        </div>
      )}

      {/* Send test message card */}
      <div className={`bg-bg-card border border-border p-6 space-y-4${!channelsActive ? ' opacity-60' : ''}`}>
        <div className="text-xs text-slate-500 uppercase tracking-wide">Send Test Message</div>
        {!channelsActive ? (
          <p className="text-sm text-slate-500">MeshCore not connected</p>
        ) : (
          <>
            <div className="space-y-1">
              <label className="text-xs text-slate-500 uppercase tracking-wide">Channel</label>
              <select
                value={selectedChannel}
                onChange={(e) => setSelectedChannel(e.target.value)}
                className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
              >
                {channels.map((ch) => (
                  <option key={ch} value={ch}>{ch}</option>
                ))}
              </select>
            </div>
            <TextInput
              label="Message (optional)"
              value={testText}
              onChange={setTestText}
              placeholder={`\u{1F9EA} MeshAI test — ${new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })}`}
            />
            <button
              onClick={handleTestSend}
              disabled={testSending}
              className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent/80 disabled:bg-slate-700 disabled:cursor-not-allowed rounded text-white text-sm transition-colors"
            >
              {testSending ? 'Sending...' : 'Send test'}
            </button>
            {testResult && (
              testResult.sent ? (
                <div className="p-3 text-sm bg-green-500/10 text-green-400 border border-green-500/20">
                  <Check size={14} className="inline mr-2" />{testResult.detail}
                </div>
              ) : (
                <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{testResult.detail}</div>
              )
            )}
          </>
        )}
      </div>
    </div>
  )
}
