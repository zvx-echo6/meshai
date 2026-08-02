import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { Save, RotateCcw, RefreshCw, Check, ChevronRight, Trash2, Eye, EyeOff, Copy, X } from 'lucide-react'
import { TextInput, NumberInput, Toggle, ListInput, SelectInput } from './Config'
import SerialPortPicker from '@/components/SerialPortPicker'
import { notifyRestartRequired } from '@/components/RestartBanner'
import {
  fetchConfig as apiFetchConfig,
  updateConfig as apiUpdateConfig,
  getMeshcoreChannels,
  getMeshcoreChannelsDetail,
  addMeshcoreChannel,
  removeMeshcoreChannel,
  sendTestMessage,
} from '@/lib/api'
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
  meshcore_auto_reconnect?: boolean
  meshcore_max_reconnect_attempts?: number
  meshcore_conn_type?: string
  meshcore_serial_port?: string
  meshcore_baud?: number
  meshcore_ble_address?: string
  meshcore_auto_add_contacts?: boolean
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
  // Set when one or both config fetches fail; the form still renders (using
  // safe defaults for whatever didn't load) instead of blanking the page.
  const [loadError, setLoadError] = useState<string | null>(null)

  // Test send state
  const [channelsActive, setChannelsActive] = useState(false)
  const [channels, setChannels] = useState<string[]>([])
  // Per-channel PSK hex (name -> key), captured from the companion so operators
  // can share the key with people who want to join. Masked by default.
  const [channelKeys, setChannelKeys] = useState<Record<string, string | null>>({})
  const [revealedKeys, setRevealedKeys] = useState<Set<string>>(new Set())
  const [copiedKey, setCopiedKey] = useState<string | null>(null)
  const [selectedChannel, setSelectedChannel] = useState('')
  const [testText, setTestText] = useState('')
  const [testSending, setTestSending] = useState(false)
  const [testResult, setTestResult] = useState<{ sent: boolean; detail: string } | null>(null)

  // Add / remove channel state (provisions the companion's channel table)
  const [newChannelName, setNewChannelName] = useState('')
  const [newChannelKey, setNewChannelKey] = useState('')
  const [channelSaving, setChannelSaving] = useState(false)
  const [channelRemoving, setChannelRemoving] = useState<string | null>(null)
  const [channelError, setChannelError] = useState<string | null>(null)

  const fetchConfig = useCallback(async () => {
    setLoading(true)
    // Promise.allSettled so one section failing to load can't blank the
    // whole page — each section is set independently from its own result,
    // and any failure(s) are surfaced as a dismissible banner alongside the
    // still-usable form (see the `!config` dead-end this replaces).
    const [connResult, mcResult] = await Promise.allSettled([
      apiFetchConfig('connection') as Promise<ConnectionConfig>,
      apiFetchConfig('meshcore_context') as Promise<MeshcoreContextCfg>,
    ])

    const errors: string[] = []

    if (connResult.status === 'fulfilled') {
      setConfig(connResult.value)
      setOriginalConfig(JSON.parse(JSON.stringify(connResult.value)))
    } else {
      errors.push(`connection (${connResult.reason instanceof Error ? connResult.reason.message : String(connResult.reason)})`)
      // Fall back to an empty object (never null) so the form below always
      // has something to render safe defaults from, and so the dirty-check
      // diff below still has a baseline to compare edits against.
      setConfig((c) => c ?? {})
      setOriginalConfig((c) => c ?? {})
    }

    if (mcResult.status === 'fulfilled') {
      setMcContext(mcResult.value)
      setOriginalMcContext(JSON.parse(JSON.stringify(mcResult.value)))
    } else {
      errors.push(`bot behavior (${mcResult.reason instanceof Error ? mcResult.reason.message : String(mcResult.reason)})`)
      // Leave mcContext null — the "Bot behavior" card only renders when it
      // is present, so a null value just hides that card cleanly.
    }

    setLoadError(errors.length ? `Couldn't load ${errors.join(' and ')}. Showing defaults — edits below are still safe to make and save.` : null)
    setHasChanges(false)
    setLoading(false)
  }, [])

  useEffect(() => {
    document.title = 'MeshCore Connection - MeshAI'
    fetchConfig()
  }, [fetchConfig])

  const refreshChannels = useCallback(async () => {
    try {
      // Prefer the detail endpoint (name + PSK key); it carries everything the
      // names-only list does. Fall back to names-only if detail is unavailable.
      const detail = await getMeshcoreChannelsDetail()
      const names = detail.channels.map((c) => c.name)
      const keyMap: Record<string, string | null> = {}
      for (const c of detail.channels) keyMap[c.name] = c.key
      setChannelsActive(detail.active)
      setChannels(names)
      setChannelKeys(keyMap)
      setSelectedChannel((prev) => (prev && names.includes(prev) ? prev : names[0] ?? ''))
    } catch {
      try {
        const res = await getMeshcoreChannels()
        setChannelsActive(res.active)
        setChannels(res.channels)
        setChannelKeys({})
        setSelectedChannel((prev) => (prev && res.channels.includes(prev) ? prev : res.channels[0] ?? ''))
      } catch {
        setChannelsActive(false)
      }
    }
  }, [])

  useEffect(() => {
    refreshChannels()
  }, [refreshChannels])

  const toggleRevealKey = (name: string) => {
    setRevealedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const handleCopyKey = async (name: string, key: string) => {
    try {
      await navigator.clipboard.writeText(key)
      setCopiedKey(name)
      setTimeout(() => setCopiedKey((prev) => (prev === name ? null : prev)), 1500)
    } catch {
      // Clipboard API can be blocked (non-secure context); reveal the key so
      // the operator can copy it manually instead of failing silently.
      setRevealedKeys((prev) => new Set(prev).add(name))
    }
  }

  const handleAddChannel = async () => {
    const name = newChannelName.trim()
    if (!name) return
    setChannelSaving(true)
    setChannelError(null)
    try {
      await addMeshcoreChannel(name, newChannelKey.trim())
      setNewChannelName('')
      setNewChannelKey('')
      await refreshChannels()
    } catch (err) {
      setChannelError(err instanceof Error ? err.message : 'Failed to add channel')
    } finally {
      setChannelSaving(false)
    }
  }

  const handleRemoveChannel = async (name: string) => {
    setChannelRemoving(name)
    setChannelError(null)
    try {
      await removeMeshcoreChannel(name)
      // Drop it from Observe Channels too, if it was selected there.
      setMcContext((c) => {
        if (!c || !(c.observe_channels ?? []).includes(name)) return c
        return { ...c, observe_channels: (c.observe_channels ?? []).filter((n) => n !== name) }
      })
      await refreshChannels()
    } catch (err) {
      setChannelError(err instanceof Error ? err.message : 'Failed to remove channel')
    } finally {
      setChannelRemoving(null)
    }
  }

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
    // mcContext may be null (its fetch failed) — don't let that block
    // detecting changes to the connection fields, which always load or
    // fall back to an empty-object baseline in fetchConfig.
    if (config && originalConfig) {
      const changed =
        JSON.stringify(config) !== JSON.stringify(originalConfig) ||
        (!!mcContext && !!originalMcContext && JSON.stringify(mcContext) !== JSON.stringify(originalMcContext))
      setHasChanges(changed)
    }
  }, [config, originalConfig, mcContext, originalMcContext])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const upd = (patch: Partial<ConnectionConfig>) =>
    // Build off an empty object rather than bailing when config is still
    // null (e.g. mid-retry) — edits should never be silently dropped.
    setConfig((c) => ({ ...(c ?? {}), ...patch }))

  const saveConfig = async () => {
    if (!config) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      // PUT the whole objects so sibling fields (Meshtastic connection fields,
      // any other meshcore_context keys) are preserved. Only PUT
      // meshcore_context if it actually loaded — its fetch may have failed.
      const puts: Promise<{ restart_required?: boolean }>[] = [apiUpdateConfig('connection', config)]
      if (mcContext) puts.push(apiUpdateConfig('meshcore_context', mcContext))
      const results = await Promise.all(puts)
      setOriginalConfig(JSON.parse(JSON.stringify(config)))
      if (mcContext) setOriginalMcContext(JSON.parse(JSON.stringify(mcContext)))
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

  // Always render the form below, even if the fetch failed — `cfg` supplies
  // safe defaults so the connection-type/host/port fields and Save/Discard
  // stay usable. The failure itself is surfaced by the loadError banner.
  const cfg: ConnectionConfig = config ?? {}

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

      {/* Load-error banner — dismissible, with its own Retry so the page
          never dead-ends when a config section fails to fetch. The form
          below stays fully editable regardless. */}
      {loadError && (
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20 flex items-start gap-3">
          <div className="flex-1">{loadError}</div>
          <button
            onClick={fetchConfig}
            className="flex items-center gap-1 px-2 py-1 bg-red-500/20 hover:bg-red-500/30 rounded text-red-300 text-xs shrink-0"
          >
            <RefreshCw size={12} />
            Retry
          </button>
          <button
            onClick={() => setLoadError(null)}
            title="Dismiss"
            aria-label="Dismiss load error"
            className="text-red-400 hover:text-red-200 shrink-0"
          >
            <X size={14} />
          </button>
        </div>
      )}

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
          Choose how MeshAI reaches your MeshCore node. TCP talks to a companion
          frame server; Serial connects to a USB-attached node; BLE pairs over
          Bluetooth. Meshtastic is always active.
        </p>
        <SelectInput
          label="Connection Type"
          value={cfg.meshcore_conn_type ?? 'tcp'}
          onChange={(v) => upd({ meshcore_conn_type: v })}
          options={[
            { value: 'tcp', label: 'TCP (companion)' },
            { value: 'serial', label: 'Serial (USB)' },
            { value: 'ble', label: 'BLE' },
          ]}
          helper="TCP for a companion frame server, Serial for a USB node, BLE for Bluetooth"
        />
        {(cfg.meshcore_conn_type ?? 'tcp') === 'tcp' && (
          <div className="grid grid-cols-2 gap-4">
            <TextInput
              label="MeshCore Host"
              value={cfg.meshcore_host ?? ''}
              onChange={(v) => upd({ meshcore_host: v })}
              placeholder="192.168.1.100"
              helper="IP or hostname of the companion frame server"
              info="The MeshCore companion (frame server) host. Active when non-empty in TCP mode."
            />
            <NumberInput
              label="MeshCore Port"
              value={cfg.meshcore_port ?? 5525}
              onChange={(v) => upd({ meshcore_port: v })}
              min={1}
              max={65535}
              helper="MeshCore TCP port (default 5525)"
            />
          </div>
        )}
        {(cfg.meshcore_conn_type ?? 'tcp') === 'serial' && (
          <>
            <SerialPortPicker
              label="MeshCore Serial Port"
              value={cfg.meshcore_serial_port ?? ''}
              onChange={(v) => upd({ meshcore_serial_port: v })}
              helper="USB-attached MeshCore node — Detect fills a stable by-id path"
            />
            <NumberInput
              label="Baud Rate"
              value={cfg.meshcore_baud ?? 115200}
              onChange={(v) => upd({ meshcore_baud: v })}
              min={1200}
              helper="Serial baud rate (default 115200)"
            />
          </>
        )}
        {(cfg.meshcore_conn_type ?? 'tcp') === 'ble' && (
          <TextInput
            label="BLE Address"
            value={cfg.meshcore_ble_address ?? ''}
            onChange={(v) => upd({ meshcore_ble_address: v })}
            placeholder="AA:BB:CC:DD:EE:FF"
            helper="Leave blank to scan/pair the first available device"
          />
        )}
        <div className="pt-2">
          <Link
            to="/meshtastic/connection"
            className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-accent transition-colors"
          >
            &rarr; Meshtastic connection
          </Link>
        </div>
        <Toggle
          label="Auto-add contacts (AIDA adds any node it hears — required to DM anyone)"
          checked={cfg.meshcore_auto_add_contacts ?? true}
          onChange={(v) => upd({ meshcore_auto_add_contacts: v })}
          helper="Enables firmware CMD 58 (set_autoadd_config) at connect so AIDA automatically adds every node it hears an advert from as a contact, enabling DM send/decrypt without manual contact exchange"
        />
        <details className="group">
          <summary className="flex items-center gap-2 cursor-pointer text-sm text-slate-400 hover:text-slate-200">
            <ChevronRight size={14} className="group-open:rotate-90 transition-transform" />
            Advanced — MeshCore Reconnect
          </summary>
          <div className="mt-4 space-y-4 pl-6 border-l border-[#1e2a3a]">
            <Toggle
              label="Auto-reconnect (MeshCore)"
              checked={cfg.meshcore_auto_reconnect ?? true}
              onChange={(v) => upd({ meshcore_auto_reconnect: v })}
              helper="Automatically reconnect to the MeshCore companion if the link drops"
            />
            <NumberInput
              label="Max Reconnect Attempts"
              value={cfg.meshcore_max_reconnect_attempts ?? 5}
              onChange={(v) => upd({ meshcore_max_reconnect_attempts: v })}
              min={0}
              helper="Maximum reconnect attempts before giving up (0 = unlimited)"
            />
          </div>
        </details>
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
                const key = channelKeys[ch] ?? null
                const revealed = revealedKeys.has(ch)
                return (
                  <div
                    key={ch}
                    className="flex items-center gap-2 p-2 rounded hover:bg-[#0a0e17]"
                  >
                    <label
                      onClick={() => toggleObserveChannel(ch)}
                      className="flex items-center gap-2 cursor-pointer shrink-0"
                    >
                      <div className={`w-4 h-4 rounded border flex items-center justify-center ${
                        selected ? 'bg-accent border-accent' : 'border-slate-600'
                      }`}>
                        {selected && <Check size={12} className="text-white" />}
                      </div>
                      <span className="text-sm text-slate-200">{ch}</span>
                    </label>
                    {/* Channel key (PSK hex) — share this to let others join. Masked by
                        default; reveal per-row with the eye, copy with the copy button. */}
                    <div className="flex items-center gap-1 flex-1 min-w-0 justify-end">
                      {key ? (
                        <>
                          <code
                            title={revealed ? key : 'Key hidden — click the eye to reveal'}
                            className="text-xs font-mono text-slate-400 truncate max-w-[16rem]"
                          >
                            {revealed ? key : '••••••••••••••••'}
                          </code>
                          <button
                            type="button"
                            title={revealed ? 'Hide key' : 'Reveal key'}
                            aria-label={revealed ? `Hide key for ${ch}` : `Reveal key for ${ch}`}
                            onClick={() => toggleRevealKey(ch)}
                            className="p-1 text-slate-600 hover:text-slate-300"
                          >
                            {revealed ? <EyeOff size={14} /> : <Eye size={14} />}
                          </button>
                          <button
                            type="button"
                            title="Copy key to clipboard"
                            aria-label={`Copy key for ${ch}`}
                            onClick={() => handleCopyKey(ch, key)}
                            className="p-1 text-slate-600 hover:text-accent"
                          >
                            {copiedKey === ch ? <Check size={14} className="text-green-400" /> : <Copy size={14} />}
                          </button>
                        </>
                      ) : (
                        <span className="text-xs font-mono text-slate-600" title="No retrievable key for this channel">—</span>
                      )}
                    </div>
                    <button
                      type="button"
                      title={`Remove channel '${ch}' from the companion`}
                      aria-label={`Remove channel ${ch}`}
                      disabled={channelRemoving === ch}
                      onClick={() => handleRemoveChannel(ch)}
                      className="p-1 text-slate-600 hover:text-red-400 disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                )
              })}
              {channels.length === 0 && (
                <div className="text-sm text-slate-500 p-2">
                  No channels available{!channelsActive ? ' (MeshCore not connected)' : ''}
                </div>
              )}
            </div>
            <p className="text-xs text-slate-600">Choose which MeshCore channels feed MeshAI's context. Empty = none are watched — pick channels to include their chatter in what the bot knows about the mesh. Leave busy/public channels out to keep them out of context. Each channel's key (PSK) is shown on the right — reveal and copy it to share with people who want to join.</p>

            {/* Add a new channel (name + PSK) to the companion's channel table */}
            <div className="flex items-end gap-2 pt-2">
              <div className="flex-1 space-y-1">
                <label className="block text-xs text-slate-500 uppercase tracking-wide">Name</label>
                <input
                  type="text"
                  value={newChannelName}
                  onChange={(e) => setNewChannelName(e.target.value)}
                  placeholder="#channel-name"
                  disabled={!channelsActive}
                  className="w-full px-2 py-1.5 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent disabled:opacity-50"
                />
              </div>
              <div className="flex-1 space-y-1">
                <label className="block text-xs text-slate-500 uppercase tracking-wide">Key</label>
                <input
                  type="text"
                  value={newChannelKey}
                  onChange={(e) => setNewChannelKey(e.target.value)}
                  placeholder="PSK hex (32 chars) — leave blank for public #channel"
                  disabled={!channelsActive}
                  className="w-full px-2 py-1.5 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent disabled:opacity-50"
                />
              </div>
              <button
                type="button"
                onClick={handleAddChannel}
                disabled={!channelsActive || channelSaving || !newChannelName.trim()}
                className="px-3 py-1.5 bg-accent hover:bg-accent/80 disabled:opacity-50 disabled:cursor-not-allowed rounded text-sm text-white whitespace-nowrap"
              >
                {channelSaving ? 'Saving…' : 'Save'}
              </button>
            </div>
            {channelError && <p className="text-xs text-red-400">{channelError}</p>}
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
