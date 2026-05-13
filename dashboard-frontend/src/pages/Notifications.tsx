import { useState, useEffect, useCallback } from 'react'
import {
  Save, RotateCcw, RefreshCw, Plus, Trash2, ChevronDown, ChevronRight,
  Check, X, Eye as EyeIcon, EyeOff, ExternalLink, Send
} from 'lucide-react'

// Types
interface NotificationChannelConfig {
  id: string
  type: string
  enabled: boolean
  channel_index: number
  node_ids: string[]
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_password: string
  smtp_tls: boolean
  from_address: string
  recipients: string[]
  url: string
  headers: Record<string, string>
}

interface NotificationRuleConfig {
  name: string
  categories: string[]
  min_severity: string
  channel_ids: string[]
  override_quiet: boolean
}

interface NotificationsConfig {
  enabled: boolean
  quiet_hours_start: string
  quiet_hours_end: string
  dedup_seconds: number
  channels: NotificationChannelConfig[]
  rules: NotificationRuleConfig[]
}

interface AlertCategory {
  id: string
  name: string
  description: string
  default_severity: string
}

// InfoButton component
function InfoButton({ info, link, linkText = 'Learn more' }: { info: string; link?: string; linkText?: string }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="relative inline-block">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen(!open) }}
        className="ml-1.5 w-4 h-4 rounded-full bg-slate-700 hover:bg-slate-600 text-slate-400 hover:text-slate-200 inline-flex items-center justify-center text-xs transition-colors"
        title="More info"
      >
        ?
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-6 z-50 w-72 p-3 bg-[#1a2332] border border-[#2a3a4a] rounded-lg shadow-xl text-xs text-slate-300 leading-relaxed">
            {info}
            {link && (
              <a
                href={link}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 flex items-center gap-1 text-accent hover:underline"
                onClick={(e) => e.stopPropagation()}
              >
                {linkText} <ExternalLink size={10} />
              </a>
            )}
          </div>
        </>
      )}
    </div>
  )
}

// Form components
function TextInput({ label, value, onChange, type = 'text', placeholder = '', helper = '', info = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
  helper?: string
  info?: string
}) {
  const [showPassword, setShowPassword] = useState(false)
  const isPassword = type === 'password'

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <div className="relative">
        <input
          type={isPassword && !showPassword ? 'password' : 'text'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
        />
        {isPassword && (
          <button
            type="button"
            onClick={() => setShowPassword(!showPassword)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
          >
            {showPassword ? <EyeOff size={16} /> : <EyeIcon size={16} />}
          </button>
        )}
      </div>
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

function NumberInput({ label, value, onChange, min, max, step = 1, helper = '', info = '' }: {
  label: string
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
  helper?: string
  info?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <input
        type="number"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        min={min}
        max={max}
        step={step}
        className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
      />
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

function Toggle({ label, checked, onChange, helper = '', info = '' }: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  helper?: string
  info?: string
}) {
  return (
    <div className="flex items-center justify-between py-2">
      <div>
        <span className="flex items-center text-sm text-slate-300">
          {label}
          {info && <InfoButton info={info} />}
        </span>
        {helper && <p className="text-xs text-slate-600">{helper}</p>}
      </div>
      <button
        type="button"
        onClick={() => onChange(!checked)}
        className={`relative w-11 h-6 rounded-full transition-colors ${
          checked ? 'bg-accent' : 'bg-[#1e2a3a]'
        }`}
      >
        <span
          className={`absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform ${
            checked ? 'translate-x-5' : ''
          }`}
        />
      </button>
    </div>
  )
}

function SelectInput({ label, value, onChange, options, helper = '', info = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
  helper?: string
  info?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

function ListInput({ label, value, onChange, helper = '', info = '' }: {
  label: string
  value: string[]
  onChange: (v: string[]) => void
  helper?: string
  info?: string
}) {
  const [inputValue, setInputValue] = useState('')

  const addItem = () => {
    if (inputValue.trim() && !value.includes(inputValue.trim())) {
      onChange([...value, inputValue.trim()])
      setInputValue('')
    }
  }

  const removeItem = (index: number) => {
    onChange(value.filter((_, i) => i !== index))
  }

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <div className="flex gap-2">
        <input
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addItem())}
          className="flex-1 px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
          placeholder="Add item..."
        />
        <button
          type="button"
          onClick={addItem}
          className="px-3 py-2 bg-accent hover:bg-accent/80 rounded text-sm text-white transition-colors"
        >
          <Plus size={16} />
        </button>
      </div>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-2 mt-2">
          {value.map((item, i) => (
            <span
              key={i}
              className="inline-flex items-center gap-1 px-2 py-1 bg-[#1e2a3a] rounded text-sm text-slate-300"
            >
              {item}
              <button
                type="button"
                onClick={() => removeItem(i)}
                className="text-slate-500 hover:text-red-400"
              >
                <X size={14} />
              </button>
            </span>
          ))}
        </div>
      )}
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

// Notification Channel Card Component
function NotificationChannelCard({
  channel,
  onChange,
  onDelete,
  onTest,
}: {
  channel: NotificationChannelConfig
  onChange: (c: NotificationChannelConfig) => void
  onDelete: () => void
  onTest: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [testing, setTesting] = useState(false)

  const typeOptions = [
    { value: 'mesh_broadcast', label: 'Mesh Broadcast' },
    { value: 'mesh_dm', label: 'Mesh DM' },
    { value: 'email', label: 'Email' },
    { value: 'webhook', label: 'Webhook' },
  ]

  const typeDescriptions: Record<string, string> = {
    mesh_broadcast: 'Broadcast alerts to a mesh channel. All nodes on that channel receive the alert.',
    mesh_dm: 'Send alerts as direct messages to specific nodes.',
    email: 'Send alert emails via SMTP. Works with Gmail, Outlook, and any SMTP server.',
    webhook: 'POST alert JSON to any URL. Works with Discord webhooks, ntfy.sh, Pushover, Slack, Home Assistant, or any service that accepts HTTP POST.',
  }

  const handleTest = async () => {
    setTesting(true)
    await onTest()
    setTesting(false)
  }

  return (
    <div className="border border-[#1e2a3a] rounded-lg overflow-hidden">
      <div
        className="flex items-center justify-between p-3 bg-[#0a0e17] cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          <div className={`w-2 h-2 rounded-full ${channel.enabled ? 'bg-green-500' : 'bg-slate-500'}`} />
          <span className="font-medium text-slate-200">{channel.id || 'New Channel'}</span>
          <span className="text-xs text-slate-500 bg-[#1e2a3a] px-2 py-0.5 rounded">
            {typeOptions.find(t => t.value === channel.type)?.label || channel.type}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={(e) => { e.stopPropagation(); handleTest() }}
            disabled={testing || !channel.id}
            className="p-1.5 text-blue-400 hover:text-blue-300 hover:bg-blue-500/10 rounded disabled:opacity-50"
            title="Send test alert"
          >
            <Send size={14} />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onDelete() }}
            className="p-1 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>
      {expanded && (
        <div className="p-4 space-y-4 border-t border-[#1e2a3a]">
          <div className="grid grid-cols-2 gap-4">
            <TextInput
              label="Channel ID"
              value={channel.id}
              onChange={(v) => onChange({ ...channel, id: v })}
              helper="Unique identifier for this channel"
              info="Used to reference this channel in notification rules. Use lowercase with hyphens (e.g., 'mesh-main', 'email-admin')."
            />
            <SelectInput
              label="Type"
              value={channel.type}
              onChange={(v) => onChange({ ...channel, type: v })}
              options={typeOptions}
              info={typeDescriptions[channel.type] || 'Select a channel type'}
            />
          </div>
          <Toggle
            label="Enabled"
            checked={channel.enabled}
            onChange={(v) => onChange({ ...channel, enabled: v })}
            helper="Disable to temporarily stop alerts on this channel"
          />

          {channel.type === 'mesh_broadcast' && (
            <NumberInput
              label="Channel Index"
              value={channel.channel_index}
              onChange={(v) => onChange({ ...channel, channel_index: v })}
              min={0}
              max={7}
              helper="Mesh channel number (0-7)"
              info="The mesh channel to broadcast alerts on. Channel 0 is typically the default channel."
            />
          )}

          {channel.type === 'mesh_dm' && (
            <ListInput
              label="Node IDs"
              value={channel.node_ids}
              onChange={(v) => onChange({ ...channel, node_ids: v })}
              helper="Node IDs to receive DM alerts"
              info="Node IDs that receive direct message alerts. Enter the full node ID (e.g., '!a1b2c3d4') for each recipient."
            />
          )}

          {channel.type === 'email' && (
            <>
              <div className="grid grid-cols-2 gap-4">
                <TextInput
                  label="SMTP Host"
                  value={channel.smtp_host}
                  onChange={(v) => onChange({ ...channel, smtp_host: v })}
                  placeholder="smtp.gmail.com"
                  helper="SMTP server hostname"
                  info="The SMTP server for sending emails. Gmail: smtp.gmail.com, Outlook: smtp.office365.com"
                />
                <NumberInput
                  label="SMTP Port"
                  value={channel.smtp_port}
                  onChange={(v) => onChange({ ...channel, smtp_port: v })}
                  min={1}
                  max={65535}
                  helper="587 (TLS) or 465 (SSL)"
                  info="SMTP port. Use 587 for TLS (recommended) or 465 for SSL."
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <TextInput
                  label="SMTP User"
                  value={channel.smtp_user}
                  onChange={(v) => onChange({ ...channel, smtp_user: v })}
                  placeholder="you@gmail.com"
                  helper="Login username"
                />
                <TextInput
                  label="SMTP Password"
                  value={channel.smtp_password}
                  onChange={(v) => onChange({ ...channel, smtp_password: v })}
                  type="password"
                  helper="App password recommended"
                  info="Gmail users: use an App Password, not your regular password. Generate one at myaccount.google.com/apppasswords"
                />
              </div>
              <Toggle
                label="Use TLS"
                checked={channel.smtp_tls}
                onChange={(v) => onChange({ ...channel, smtp_tls: v })}
                helper="Encrypt SMTP connection"
                info="Enable TLS encryption for the SMTP connection. Required for most modern email servers."
              />
              <TextInput
                label="From Address"
                value={channel.from_address}
                onChange={(v) => onChange({ ...channel, from_address: v })}
                placeholder="alerts@yourdomain.com"
                helper="Sender email address"
                info="The email address that appears as the sender. Some servers require this to match your login."
              />
              <ListInput
                label="Recipients"
                value={channel.recipients}
                onChange={(v) => onChange({ ...channel, recipients: v })}
                helper="Email addresses to receive alerts"
                info="List of email addresses that will receive alerts from this channel."
              />
            </>
          )}

          {channel.type === 'webhook' && (
            <>
              <TextInput
                label="Webhook URL"
                value={channel.url}
                onChange={(v) => onChange({ ...channel, url: v })}
                placeholder="https://discord.com/api/webhooks/..."
                helper="POST endpoint for alerts"
                info="POST alert JSON to any URL. Works with Discord webhooks, ntfy.sh, Pushover, Slack, Home Assistant, or any service that accepts HTTP POST."
              />
              <div className="space-y-1">
                <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                  Headers (optional)
                  <InfoButton info="Additional HTTP headers to send with the webhook request. Useful for authentication tokens or custom headers required by the receiving service." />
                </label>
                <div className="text-xs text-slate-600">
                  Custom headers can be configured in the YAML config file
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// Notification Rule Card Component
function NotificationRuleCard({
  rule,
  categories,
  channels,
  onChange,
  onDelete,
}: {
  rule: NotificationRuleConfig
  categories: AlertCategory[]
  channels: NotificationChannelConfig[]
  onChange: (r: NotificationRuleConfig) => void
  onDelete: () => void
}) {
  const [expanded, setExpanded] = useState(false)

  const severityOptions = [
    { value: 'info', label: 'Info' },
    { value: 'advisory', label: 'Advisory' },
    { value: 'watch', label: 'Watch' },
    { value: 'warning', label: 'Warning' },
    { value: 'critical', label: 'Critical' },
    { value: 'emergency', label: 'Emergency' },
  ]

  const toggleCategory = (catId: string) => {
    const current = rule.categories || []
    if (current.includes(catId)) {
      onChange({ ...rule, categories: current.filter(c => c !== catId) })
    } else {
      onChange({ ...rule, categories: [...current, catId] })
    }
  }

  const toggleChannel = (channelId: string) => {
    const current = rule.channel_ids || []
    if (current.includes(channelId)) {
      onChange({ ...rule, channel_ids: current.filter(c => c !== channelId) })
    } else {
      onChange({ ...rule, channel_ids: [...current, channelId] })
    }
  }

  return (
    <div className="border border-[#1e2a3a] rounded-lg overflow-hidden">
      <div
        className="flex items-center justify-between p-3 bg-[#0a0e17] cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          <span className="font-medium text-slate-200">{rule.name || 'New Rule'}</span>
          <span className="text-xs text-slate-500">
            {rule.categories?.length || 0} categories → {rule.channel_ids?.length || 0} channels
          </span>
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onDelete() }}
          className="p-1 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded"
        >
          <Trash2 size={14} />
        </button>
      </div>
      {expanded && (
        <div className="p-4 space-y-4 border-t border-[#1e2a3a]">
          <TextInput
            label="Rule Name"
            value={rule.name}
            onChange={(v) => onChange({ ...rule, name: v })}
            helper="Human-readable name for this rule"
            info="A descriptive name to identify this rule. Example: 'Emergency Alerts', 'Fire Notifications', 'Infrastructure Warnings'"
          />

          <SelectInput
            label="Minimum Severity"
            value={rule.min_severity}
            onChange={(v) => onChange({ ...rule, min_severity: v })}
            options={severityOptions}
            helper="Only alerts at or above this severity"
            info="Only alerts at this severity or above will trigger this rule. 'warning' is recommended for most channels. Use 'info' to receive all alerts."
          />

          <div className="space-y-2">
            <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
              Alert Categories
              <InfoButton info="Which alert types this rule applies to. Select none to match all categories. Alerts matching any selected category (AND meeting severity threshold) will trigger this rule." />
            </label>
            <div className="text-xs text-slate-500 mb-2">
              {rule.categories?.length === 0 ? 'All categories (none selected)' : `${rule.categories?.length} selected`}
            </div>
            <div className="max-h-48 overflow-y-auto border border-[#1e2a3a] rounded-lg p-2 space-y-1">
              {categories.map((cat) => (
                <label
                  key={cat.id}
                  className="flex items-start gap-2 p-2 rounded hover:bg-[#0a0e17] cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={rule.categories?.includes(cat.id) || false}
                    onChange={() => toggleCategory(cat.id)}
                    className="mt-0.5 rounded border-slate-600 bg-[#0a0e17] text-accent focus:ring-accent"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-slate-200">{cat.name}</div>
                    <div className="text-xs text-slate-500">{cat.description}</div>
                  </div>
                </label>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
              Delivery Channels
              <InfoButton info="Which channels receive alerts matching this rule. Select at least one channel." />
            </label>
            {channels.length === 0 ? (
              <div className="text-xs text-slate-500 p-2 border border-[#1e2a3a] rounded-lg">
                No channels configured. Add channels above first.
              </div>
            ) : (
              <div className="border border-[#1e2a3a] rounded-lg p-2 space-y-1">
                {channels.map((ch) => (
                  <label
                    key={ch.id}
                    className="flex items-center gap-2 p-2 rounded hover:bg-[#0a0e17] cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={rule.channel_ids?.includes(ch.id) || false}
                      onChange={() => toggleChannel(ch.id)}
                      className="rounded border-slate-600 bg-[#0a0e17] text-accent focus:ring-accent"
                    />
                    <span className="text-sm text-slate-200">{ch.id}</span>
                    <span className="text-xs text-slate-500">({ch.type})</span>
                  </label>
                ))}
              </div>
            )}
          </div>

          <Toggle
            label="Override Quiet Hours"
            checked={rule.override_quiet}
            onChange={(v) => onChange({ ...rule, override_quiet: v })}
            helper="Send alerts even during quiet hours"
            info="When enabled, this rule sends alerts even during quiet hours. Use for critical conditions like fires or infrastructure failures."
          />
        </div>
      )}
    </div>
  )
}

// Main Notifications Page Component
export default function Notifications() {
  const [config, setConfig] = useState<NotificationsConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<NotificationsConfig | null>(null)
  const [categories, setCategories] = useState<AlertCategory[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  const fetchConfig = useCallback(async () => {
    try {
      const [configRes, categoriesRes] = await Promise.all([
        fetch('/api/config/notifications'),
        fetch('/api/notifications/categories'),
      ])
      if (!configRes.ok) throw new Error('Failed to fetch notifications config')
      const configData = await configRes.json()
      const categoriesData = await categoriesRes.json()
      setConfig(configData)
      setOriginalConfig(JSON.parse(JSON.stringify(configData)))
      setCategories(categoriesData)
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'Notifications — MeshAI'
    fetchConfig()
  }, [fetchConfig])

  useEffect(() => {
    if (config && originalConfig) {
      setHasChanges(JSON.stringify(config) !== JSON.stringify(originalConfig))
    }
  }, [config, originalConfig])

  const saveConfig = async () => {
    if (!config) return

    setSaving(true)
    setError(null)
    setSuccess(null)

    try {
      const res = await fetch('/api/config/notifications', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
      })

      const result = await res.json()

      if (!res.ok) {
        throw new Error(result.detail || 'Save failed')
      }

      setSuccess('Notifications config saved successfully')
      setOriginalConfig(JSON.parse(JSON.stringify(config)))
      setHasChanges(false)
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

  const addChannel = () => {
    if (!config) return
    const newChannel: NotificationChannelConfig = {
      id: '',
      type: 'mesh_broadcast',
      enabled: true,
      channel_index: 0,
      node_ids: [],
      smtp_host: '',
      smtp_port: 587,
      smtp_user: '',
      smtp_password: '',
      smtp_tls: true,
      from_address: '',
      recipients: [],
      url: '',
      headers: {},
    }
    setConfig({ ...config, channels: [...(config.channels || []), newChannel] })
  }

  const addRule = () => {
    if (!config) return
    const newRule: NotificationRuleConfig = {
      name: '',
      categories: [],
      min_severity: 'warning',
      channel_ids: [],
      override_quiet: false,
    }
    setConfig({ ...config, rules: [...(config.rules || []), newRule] })
  }

  const testChannel = async (channelId: string) => {
    try {
      const res = await fetch(`/api/notifications/channels/${channelId}/test`, { method: 'POST' })
      const result = await res.json()
      setTestResult(result)
      setTimeout(() => setTestResult(null), 5000)
    } catch {
      setTestResult({ success: false, message: 'Test failed' })
      setTimeout(() => setTestResult(null), 5000)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading notifications config...</div>
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

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header with actions */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">
            Configure where alerts get delivered and which conditions trigger them.
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
        <div className="p-3 rounded-lg text-sm bg-red-500/10 text-red-400 border border-red-500/20">
          {error}
        </div>
      )}
      {success && (
        <div className="p-3 rounded-lg text-sm bg-green-500/10 text-green-400 border border-green-500/20">
          <Check size={14} className="inline mr-2" />
          {success}
        </div>
      )}
      {testResult && (
        <div className={`p-3 rounded-lg text-sm ${testResult.success ? 'bg-green-500/10 text-green-400 border border-green-500/20' : 'bg-red-500/10 text-red-400 border border-red-500/20'}`}>
          {testResult.success ? <Check size={14} className="inline mr-2" /> : <X size={14} className="inline mr-2" />}
          {testResult.message}
        </div>
      )}

      {/* Main content */}
      <div className="bg-bg-card border border-border rounded-lg p-6 space-y-6">
        <Toggle
          label="Enable Notifications"
          checked={config.enabled}
          onChange={(v) => setConfig({ ...config, enabled: v })}
          helper="Master switch for all notification delivery"
          info="When disabled, no alerts will be delivered through any channel. The alert engine still runs and records alerts to history."
        />

        {config.enabled && (
          <>
            {/* Channels Section */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                  Channels
                  <InfoButton info="Where alerts get delivered. Add channels for each destination you want to receive alerts." />
                </label>
              </div>
              <p className="text-sm text-slate-500 -mt-1">
                Where alerts get delivered. Add channels for each destination you want to receive alerts.
              </p>
              {(config.channels || []).map((channel, i) => (
                <NotificationChannelCard
                  key={i}
                  channel={channel}
                  onChange={(c) => {
                    const newChannels = [...(config.channels || [])]
                    newChannels[i] = c
                    setConfig({ ...config, channels: newChannels })
                  }}
                  onDelete={() => {
                    if (confirm(`Delete channel "${channel.id || 'New Channel'}"?`)) {
                      setConfig({ ...config, channels: (config.channels || []).filter((_, j) => j !== i) })
                    }
                  }}
                  onTest={() => testChannel(channel.id)}
                />
              ))}
              <button
                onClick={addChannel}
                className="w-full py-2 border border-dashed border-[#1e2a3a] rounded-lg text-slate-500 hover:text-slate-300 hover:border-accent flex items-center justify-center gap-2 transition-colors"
              >
                <Plus size={16} /> Add Channel
              </button>
            </div>

            {/* Rules Section */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                  Rules
                  <InfoButton info="Rules connect alert categories to delivery channels. When a condition matches a rule, the alert is sent to all channels in that rule." />
                </label>
              </div>
              <p className="text-sm text-slate-500 -mt-1">
                Rules connect alert categories to delivery channels. When a condition matches a rule, the alert is sent to all channels in that rule.
              </p>
              {(config.rules || []).map((rule, i) => (
                <NotificationRuleCard
                  key={i}
                  rule={rule}
                  categories={categories}
                  channels={config.channels || []}
                  onChange={(r) => {
                    const newRules = [...(config.rules || [])]
                    newRules[i] = r
                    setConfig({ ...config, rules: newRules })
                  }}
                  onDelete={() => {
                    if (confirm(`Delete rule "${rule.name || 'New Rule'}"?`)) {
                      setConfig({ ...config, rules: (config.rules || []).filter((_, j) => j !== i) })
                    }
                  }}
                />
              ))}
              <button
                onClick={addRule}
                className="w-full py-2 border border-dashed border-[#1e2a3a] rounded-lg text-slate-500 hover:text-slate-300 hover:border-accent flex items-center justify-center gap-2 transition-colors"
              >
                <Plus size={16} /> Add Rule
              </button>
            </div>

            {/* Quiet Hours Section */}
            <div className="space-y-3">
              <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                Quiet Hours
                <InfoButton info="Suppress non-emergency alerts during sleeping hours. Emergency and critical alerts always get through. Rules with 'Override Quiet Hours' enabled will also deliver during this time." />
              </label>
              <p className="text-sm text-slate-500 -mt-1">
                Suppress non-emergency alerts during sleeping hours. Emergency and critical alerts always get through.
              </p>
              <div className="grid grid-cols-2 gap-4">
                <TextInput
                  label="Start Time"
                  value={config.quiet_hours_start || '22:00'}
                  onChange={(v) => setConfig({ ...config, quiet_hours_start: v })}
                  placeholder="22:00"
                  helper="When quiet hours begin"
                  info="Time in 24-hour format (HH:MM) when quiet hours start. Alerts below emergency severity will be held until quiet hours end."
                />
                <TextInput
                  label="End Time"
                  value={config.quiet_hours_end || '06:00'}
                  onChange={(v) => setConfig({ ...config, quiet_hours_end: v })}
                  placeholder="06:00"
                  helper="When quiet hours end"
                  info="Time in 24-hour format (HH:MM) when quiet hours end. Held alerts will be delivered at this time."
                />
              </div>
            </div>

            {/* Dedup Section */}
            <div className="space-y-3">
              <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                Deduplication
                <InfoButton info="Prevents alert spam. If the same condition fires multiple times within this window, only the first one is delivered." />
              </label>
              <NumberInput
                label="Dedup Window (seconds)"
                value={config.dedup_seconds || 600}
                onChange={(v) => setConfig({ ...config, dedup_seconds: v })}
                min={0}
                max={86400}
                helper="Don't re-send the same alert within this window"
                info="Prevents alert spam. If the same condition fires multiple times within this window, only the first one is delivered. Default is 600 seconds (10 minutes)."
              />
            </div>
          </>
        )}
      </div>
    </div>
  )
}
