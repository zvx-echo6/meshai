import { useState, useEffect, useCallback } from 'react'
import { Save, RotateCcw, RefreshCw, Check } from 'lucide-react'
import { ConnectionSection, TextInput, NumberInput, Toggle, type ConnectionConfig } from './Config'
import ChannelPicker from '@/components/ChannelPicker'
import NodePicker from '@/components/NodePicker'
import { notifyRestartRequired } from '@/components/RestartBanner'
import { fetchConfig as apiFetchConfig, updateConfig as apiUpdateConfig, sendTestMessage } from '@/lib/api'
import { useDirty } from '@/context/DirtyContext'

// Only the fields the "Bot behavior" section edits are typed explicitly; the
// rest of each section (max_age, max_context_items, MQTT fields, bot name/owner,
// etc.) is preserved untouched on save via object spread.
interface ContextCfg {
  enabled?: boolean
  observe_channels?: number[]
  ignore_nodes?: string[]
  [key: string]: unknown
}

interface BotCfg {
  respond_to_dms?: boolean
  [key: string]: unknown
}

export default function MeshtasticConnection() {
  const { setDirty } = useDirty()
  const [config, setConfig] = useState<ConnectionConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<ConnectionConfig | null>(null)
  // Bot behavior: passive-context (section `context`) + respond-to-DMs (section `bot`)
  const [context, setContext] = useState<ContextCfg | null>(null)
  const [originalContext, setOriginalContext] = useState<ContextCfg | null>(null)
  const [bot, setBot] = useState<BotCfg | null>(null)
  const [originalBot, setOriginalBot] = useState<BotCfg | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  // Test send state
  const [testChannel, setTestChannel] = useState(0)
  const [testText, setTestText] = useState('')
  const [testSending, setTestSending] = useState(false)
  const [testResult, setTestResult] = useState<{ sent: boolean; detail: string } | null>(null)

  const handleTestSend = async () => {
    setTestSending(true)
    setTestResult(null)
    try {
      const result = await sendTestMessage({
        transport: 'meshtastic',
        channel: testChannel,
        text: testText.trim() || undefined,
      })
      setTestResult(result)
    } catch (err) {
      setTestResult({ sent: false, detail: err instanceof Error ? err.message : 'Send failed' })
    } finally {
      setTestSending(false)
    }
  }

  const fetchConfig = useCallback(async () => {
    setLoading(true)
    try {
      const [conn, ctx, botData] = await Promise.all([
        apiFetchConfig('connection') as Promise<ConnectionConfig>,
        apiFetchConfig('context') as Promise<ContextCfg>,
        apiFetchConfig('bot') as Promise<BotCfg>,
      ])
      setConfig(conn)
      setOriginalConfig(JSON.parse(JSON.stringify(conn)))
      setContext(ctx)
      setOriginalContext(JSON.parse(JSON.stringify(ctx)))
      setBot(botData)
      setOriginalBot(JSON.parse(JSON.stringify(botData)))
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'Meshtastic Connection - MeshAI'
    fetchConfig()
  }, [fetchConfig])

  useEffect(() => {
    if (config && originalConfig && context && originalContext && bot && originalBot) {
      const changed =
        JSON.stringify(config) !== JSON.stringify(originalConfig) ||
        JSON.stringify(context) !== JSON.stringify(originalContext) ||
        JSON.stringify(bot) !== JSON.stringify(originalBot)
      setHasChanges(changed)
    }
  }, [config, originalConfig, context, originalContext, bot, originalBot])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const saveConfig = async () => {
    if (!config || !context || !bot) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      // PUT the whole objects so sibling fields (MeshCore connection fields,
      // context max_age/max_context_items, bot name/owner) aren't clobbered.
      const results = await Promise.all([
        apiUpdateConfig('connection', config),
        apiUpdateConfig('context', context),
        apiUpdateConfig('bot', bot),
      ])
      setOriginalConfig(JSON.parse(JSON.stringify(config)))
      setOriginalContext(JSON.parse(JSON.stringify(context)))
      setOriginalBot(JSON.parse(JSON.stringify(bot)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('Meshtastic connection saved successfully')
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
    if (originalContext) setContext(JSON.parse(JSON.stringify(originalContext)))
    if (originalBot) setBot(JSON.parse(JSON.stringify(originalBot)))
    setHasChanges(false)
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading Meshtastic connection...</div>
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
            Connection to your Meshtastic radio (serial or TCP).
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
      <div className="bg-bg-card border border-border p-6">
        <ConnectionSection data={config} onChange={setConfig} />
      </div>

      {/* Bot behavior card */}
      {context && bot && (
        <div className="bg-bg-card border border-border p-6 space-y-4">
          <div className="text-xs text-slate-500 uppercase tracking-wide">Bot behavior</div>
          <Toggle
            label="Enable Passive Context"
            checked={!!context.enabled}
            onChange={(v) => setContext({ ...context, enabled: v })}
            helper="Listen to channel traffic for context"
            info="When enabled, the bot monitors mesh channels and includes recent messages in its context. This lets the bot reference things other people said on the channel."
          />
          <ChannelPicker
            label="Observe Channels"
            value={context.observe_channels ?? []}
            onChange={(v) => setContext({ ...context, observe_channels: v })}
            helper="Channels to monitor (empty = all)"
            info="Meshtastic channels to listen on. Leave empty to monitor all channels."
            mode="multi"
          />
          <NodePicker
            label="Ignore Nodes"
            value={context.ignore_nodes ?? []}
            onChange={(v) => setContext({ ...context, ignore_nodes: v })}
            helper="Nodes to exclude from context"
            info="Messages from these nodes won't be included in passive context. Useful for filtering out noisy automated nodes."
          />
          <Toggle
            label="Answer direct messages"
            checked={!!bot.respond_to_dms}
            onChange={(v) => setBot({ ...bot, respond_to_dms: v })}
            helper="When on, MeshAI replies to Meshtastic direct messages using the LLM. Applies to Meshtastic only."
          />
        </div>
      )}

      {/* Send test message card */}
      <div className="bg-bg-card border border-border p-6 space-y-4">
        <div className="text-xs text-slate-500 uppercase tracking-wide">Send Test Message</div>
        <NumberInput
          label="Channel Index"
          value={testChannel}
          onChange={setTestChannel}
          min={0}
          max={7}
          helper="Meshtastic channel number (0 = primary)"
        />
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
      </div>
    </div>
  )
}
