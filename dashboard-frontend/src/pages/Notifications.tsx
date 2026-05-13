import { useState, useEffect, useCallback } from 'react'
import {
  Save, RotateCcw, RefreshCw, Plus, Trash2, ChevronDown, ChevronRight,
  Check, X, Eye as EyeIcon, EyeOff, Send, Clock, Zap,
  Calendar, AlertTriangle, Copy
} from 'lucide-react'
import ChannelPicker from '@/components/ChannelPicker'
import NodePicker from '@/components/NodePicker'

// Types
interface NotificationRuleConfig {
  name: string
  enabled: boolean
  // Trigger
  trigger_type: 'condition' | 'schedule'
  // Condition trigger
  categories: string[]
  min_severity: string
  // Schedule trigger
  schedule_frequency: 'daily' | 'twice_daily' | 'weekly' | 'custom'
  schedule_time: string
  schedule_time_2: string  // For twice_daily
  schedule_days: string[]  // For weekly
  schedule_cron: string    // For custom
  message_type: string
  custom_message: string
  // Delivery
  delivery_type: string
  broadcast_channel: number
  node_ids: string[]
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_password: string
  smtp_tls: boolean
  from_address: string
  recipients: string[]
  webhook_url: string
  webhook_headers: Record<string, string>
  // Behavior
  cooldown_minutes: number
  override_quiet: boolean
}

interface NotificationsConfig {
  enabled: boolean
  quiet_hours_start: string
  quiet_hours_end: string
  rules: NotificationRuleConfig[]
}

interface AlertCategory {
  id: string
  name: string
  description: string
  default_severity: string
}

// InfoButton component
function InfoButton({ info }: { info: string }) {
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

function TimeInput({ label, value, onChange, helper = '', info = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
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
        type="time"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
      />
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

function ListInput({ label, value, onChange, placeholder = 'Add item...', helper = '', info = '' }: {
  label: string
  value: string[]
  onChange: (v: string[]) => void
  placeholder?: string
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
          placeholder={placeholder}
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

// Notification Rule Card Component
function NotificationRuleCard({
  rule,
  categories,
  onChange,
  onDelete,
  onDuplicate,
  onTest,
}: {
  rule: NotificationRuleConfig
  categories: AlertCategory[]
  onChange: (r: NotificationRuleConfig) => void
  onDelete: () => void
  onDuplicate: () => void
  onTest: () => void
}) {
  const [expanded, setExpanded] = useState(!rule.name)
  const [testing, setTesting] = useState(false)

  const severityOptions = [
    { value: 'info', label: 'Info' },
    { value: 'advisory', label: 'Advisory' },
    { value: 'watch', label: 'Watch' },
    { value: 'warning', label: 'Warning' },
    { value: 'critical', label: 'Critical' },
    { value: 'emergency', label: 'Emergency' },
  ]

  const deliveryOptions = [
    { value: 'mesh_broadcast', label: 'Mesh Broadcast' },
    { value: 'mesh_dm', label: 'Mesh DM' },
    { value: 'email', label: 'Email' },
    { value: 'webhook', label: 'Webhook' },
  ]

  const frequencyOptions = [
    { value: 'daily', label: 'Once Daily' },
    { value: 'twice_daily', label: 'Twice Daily' },
    { value: 'weekly', label: 'Weekly' },
    { value: 'custom', label: 'Custom Cron' },
  ]

  const messageTypeOptions = [
    { value: 'mesh_health_summary', label: 'Mesh Health Summary' },
    { value: 'rf_propagation_report', label: 'RF Propagation Report' },
    { value: 'alerts_digest', label: 'Active Alerts Digest' },
    { value: 'environmental_conditions', label: 'Environmental Conditions' },
    { value: 'custom', label: 'Custom Message' },
  ]

  const dayOptions = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

  const toggleCategory = (catId: string) => {
    const current = rule.categories || []
    if (current.includes(catId)) {
      onChange({ ...rule, categories: current.filter(c => c !== catId) })
    } else {
      onChange({ ...rule, categories: [...current, catId] })
    }
  }

  const toggleDay = (day: string) => {
    const current = rule.schedule_days || []
    if (current.includes(day)) {
      onChange({ ...rule, schedule_days: current.filter(d => d !== day) })
    } else {
      onChange({ ...rule, schedule_days: [...current, day] })
    }
  }

  const handleTest = async () => {
    setTesting(true)
    await onTest()
    setTesting(false)
  }

  // Generate summary for collapsed view
  const getSummary = () => {
    const parts: string[] = []

    if (rule.trigger_type === 'schedule') {
      // Schedule summary
      const freq = frequencyOptions.find(f => f.value === rule.schedule_frequency)?.label || rule.schedule_frequency
      const msgType = messageTypeOptions.find(m => m.value === rule.message_type)?.label || rule.message_type
      parts.push(`${freq} at ${rule.schedule_time || '??:??'}`)
      parts.push(msgType)
    } else {
      // Condition summary
      const catCount = rule.categories?.length || 0
      const catText = catCount === 0 ? 'All categories' : `${catCount} categories`
      const severity = severityOptions.find(s => s.value === rule.min_severity)?.label || rule.min_severity
      parts.push(`${catText} at ${severity}+`)
    }

    // Delivery summary
    const delivery = deliveryOptions.find(d => d.value === rule.delivery_type)?.label || rule.delivery_type
    let target = ''
    if (rule.delivery_type === 'mesh_broadcast') {
      target = `Ch ${rule.broadcast_channel}`
    } else if (rule.delivery_type === 'mesh_dm') {
      target = `${rule.node_ids?.length || 0} nodes`
    } else if (rule.delivery_type === 'email') {
      target = rule.recipients?.length ? rule.recipients[0] + (rule.recipients.length > 1 ? ` +${rule.recipients.length - 1}` : '') : 'no recipients'
    } else if (rule.delivery_type === 'webhook') {
      try {
        const url = new URL(rule.webhook_url)
        target = url.hostname
      } catch {
        target = rule.webhook_url?.slice(0, 30) || 'no URL'
      }
    }
    parts.push(`${delivery}${target ? ` (${target})` : ''}`)

    return parts.join(' → ')
  }

  return (
    <div className={`border rounded-lg overflow-hidden ${rule.enabled ? 'border-[#1e2a3a]' : 'border-slate-700 opacity-60'}`}>
      {/* Header */}
      <div
        className="flex items-center justify-between p-3 bg-[#0a0e17] cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3 min-w-0 flex-1">
          {expanded ? <ChevronDown size={16} className="text-slate-500 flex-shrink-0" /> : <ChevronRight size={16} className="text-slate-500 flex-shrink-0" />}
          <button
            onClick={(e) => { e.stopPropagation(); onChange({ ...rule, enabled: !rule.enabled }) }}
            className={`w-2 h-2 rounded-full flex-shrink-0 ${rule.enabled ? 'bg-green-500' : 'bg-slate-500'}`}
            title={rule.enabled ? 'Enabled - click to disable' : 'Disabled - click to enable'}
          />
          {rule.trigger_type === 'schedule' ? (
            <Clock size={14} className="text-blue-400 flex-shrink-0" />
          ) : (
            <Zap size={14} className="text-yellow-400 flex-shrink-0" />
          )}
          <span className="font-medium text-slate-200 truncate">{rule.name || 'New Rule'}</span>
          {!expanded && (
            <span className="text-xs text-slate-500 truncate hidden sm:block">
              {getSummary()}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            onClick={(e) => { e.stopPropagation(); handleTest() }}
            disabled={testing || !rule.name}
            className="p-1.5 text-blue-400 hover:text-blue-300 hover:bg-blue-500/10 rounded disabled:opacity-50"
            title="Send test"
          >
            <Send size={14} />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onDuplicate() }}
            className="p-1.5 text-slate-400 hover:text-slate-200 hover:bg-slate-500/10 rounded"
            title="Duplicate rule"
          >
            <Copy size={14} />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onDelete() }}
            className="p-1.5 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded"
            title="Delete rule"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>

      {/* Expanded content */}
      {expanded && (
        <div className="p-4 space-y-6 border-t border-[#1e2a3a]">
          {/* Rule name */}
          <TextInput
            label="Rule Name"
            value={rule.name}
            onChange={(v) => onChange({ ...rule, name: v })}
            placeholder="e.g., Emergency Broadcast, Daily Health Report"
            helper="A descriptive name for this rule"
          />

          {/* Trigger type selector */}
          <div className="space-y-2">
            <label className="text-xs text-slate-500 uppercase tracking-wide">Trigger Type</label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => onChange({ ...rule, trigger_type: 'condition' })}
                className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-lg border transition-colors ${
                  rule.trigger_type !== 'schedule'
                    ? 'bg-accent/10 border-accent text-accent'
                    : 'bg-[#0a0e17] border-[#1e2a3a] text-slate-400 hover:text-slate-200'
                }`}
              >
                <Zap size={16} />
                <span>Condition</span>
              </button>
              <button
                type="button"
                onClick={() => onChange({ ...rule, trigger_type: 'schedule' })}
                className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-lg border transition-colors ${
                  rule.trigger_type === 'schedule'
                    ? 'bg-accent/10 border-accent text-accent'
                    : 'bg-[#0a0e17] border-[#1e2a3a] text-slate-400 hover:text-slate-200'
                }`}
              >
                <Clock size={16} />
                <span>Schedule</span>
              </button>
            </div>
            <p className="text-xs text-slate-600">
              {rule.trigger_type === 'schedule'
                ? 'Send messages on a schedule (daily reports, weekly digests)'
                : 'React to alert conditions (fires, outages, warnings)'}
            </p>
          </div>

          {/* WHEN section - Condition trigger */}
          {rule.trigger_type !== 'schedule' && (
            <div className="space-y-4 p-4 bg-[#0a0e17] rounded-lg border border-[#1e2a3a]">
              <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
                <AlertTriangle size={14} />
                WHEN (Condition)
              </div>

              <SelectInput
                label="Minimum Severity"
                value={rule.min_severity}
                onChange={(v) => onChange({ ...rule, min_severity: v })}
                options={severityOptions}
                helper="Only alerts at or above this level"
              />

              <div className="space-y-2">
                <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                  Alert Categories
                  <InfoButton info="Select which types of alerts trigger this rule. Leave all unchecked to match ALL categories." />
                </label>
                <div className="text-xs text-slate-500 mb-2">
                  {(rule.categories?.length || 0) === 0 ? 'All categories (none selected)' : `${rule.categories?.length} selected`}
                </div>
                <div className="max-h-48 overflow-y-auto border border-[#1e2a3a] rounded-lg p-2 space-y-1">
                  {categories.map((cat) => (
                    <label
                      key={cat.id}
                      onClick={() => toggleCategory(cat.id)}
                      className="flex items-start gap-2 p-2 rounded hover:bg-[#1e2a3a]/50 cursor-pointer"
                    >
                      <div className={`w-4 h-4 mt-0.5 rounded border flex items-center justify-center flex-shrink-0 ${
                        rule.categories?.includes(cat.id) ? 'bg-accent border-accent' : 'border-slate-600'
                      }`}>
                        {rule.categories?.includes(cat.id) && <Check size={12} className="text-white" />}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-slate-200">{cat.name}</div>
                        <div className="text-xs text-slate-500">{cat.description}</div>
                      </div>
                    </label>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* WHEN section - Schedule trigger */}
          {rule.trigger_type === 'schedule' && (
            <div className="space-y-4 p-4 bg-[#0a0e17] rounded-lg border border-[#1e2a3a]">
              <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
                <Calendar size={14} />
                WHEN (Schedule)
              </div>

              <SelectInput
                label="Frequency"
                value={rule.schedule_frequency || 'daily'}
                onChange={(v) => onChange({ ...rule, schedule_frequency: v as any })}
                options={frequencyOptions}
              />

              <div className="grid grid-cols-2 gap-4">
                <TimeInput
                  label="Time"
                  value={rule.schedule_time || '07:00'}
                  onChange={(v) => onChange({ ...rule, schedule_time: v })}
                  helper="24-hour format"
                />
                {rule.schedule_frequency === 'twice_daily' && (
                  <TimeInput
                    label="Second Time"
                    value={rule.schedule_time_2 || '19:00'}
                    onChange={(v) => onChange({ ...rule, schedule_time_2: v })}
                  />
                )}
              </div>

              {rule.schedule_frequency === 'weekly' && (
                <div className="space-y-2">
                  <label className="text-xs text-slate-500 uppercase tracking-wide">Days</label>
                  <div className="flex flex-wrap gap-2">
                    {dayOptions.map((day) => (
                      <button
                        key={day}
                        type="button"
                        onClick={() => toggleDay(day)}
                        className={`px-3 py-1.5 rounded text-sm capitalize transition-colors ${
                          rule.schedule_days?.includes(day)
                            ? 'bg-accent text-white'
                            : 'bg-[#1e2a3a] text-slate-400 hover:text-slate-200'
                        }`}
                      >
                        {day.slice(0, 3)}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {rule.schedule_frequency === 'custom' && (
                <TextInput
                  label="Cron Expression"
                  value={rule.schedule_cron || ''}
                  onChange={(v) => onChange({ ...rule, schedule_cron: v })}
                  placeholder="0 7 * * *"
                  helper="Standard cron format"
                  info="Five-field cron: minute hour day-of-month month day-of-week. Example: '0 7 * * 1' = 7:00 AM every Monday."
                />
              )}

              <SelectInput
                label="Message Type"
                value={rule.message_type || 'mesh_health_summary'}
                onChange={(v) => onChange({ ...rule, message_type: v })}
                options={messageTypeOptions}
                info="The type of report or message to send."
              />

              {rule.message_type === 'custom' && (
                <div className="space-y-1">
                  <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                    Custom Message
                    <InfoButton info="Use template tokens: {MESH_SCORE}, {NODE_COUNT}, {ACTIVE_ALERTS}, {KP}, {SFI}, {DATE}, {TIME}" />
                  </label>
                  <textarea
                    value={rule.custom_message || ''}
                    onChange={(e) => onChange({ ...rule, custom_message: e.target.value })}
                    rows={4}
                    placeholder="Good morning! Mesh health: {MESH_SCORE}/100 with {NODE_COUNT} nodes online."
                    className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
                  />
                </div>
              )}
            </div>
          )}

          {/* SEND VIA section */}
          <div className="space-y-4 p-4 bg-[#0a0e17] rounded-lg border border-[#1e2a3a]">
            <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
              <Send size={14} />
              SEND VIA
            </div>

            <SelectInput
              label="Delivery Method"
              value={rule.delivery_type || 'mesh_broadcast'}
              onChange={(v) => onChange({ ...rule, delivery_type: v })}
              options={deliveryOptions}
            />

            {/* Mesh Broadcast fields */}
            {rule.delivery_type === 'mesh_broadcast' && (
              <ChannelPicker
                label="Broadcast Channel"
                value={rule.broadcast_channel ?? 0}
                onChange={(v) => onChange({ ...rule, broadcast_channel: v })}
                helper="Select the mesh radio channel"
                mode="single"
              />
            )}

            {/* Mesh DM fields */}
            {rule.delivery_type === 'mesh_dm' && (
              <NodePicker
                label="Recipient Nodes"
                value={rule.node_ids || []}
                onChange={(v) => onChange({ ...rule, node_ids: v })}
                helper="Nodes that receive direct messages"
                valueType="node_id_hex"
              />
            )}

            {/* Email fields */}
            {rule.delivery_type === 'email' && (
              <div className="space-y-4">
                <ListInput
                  label="Recipients"
                  value={rule.recipients || []}
                  onChange={(v) => onChange({ ...rule, recipients: v })}
                  placeholder="email@example.com"
                  helper="Email addresses to receive alerts"
                />

                <details className="group">
                  <summary className="flex items-center gap-2 cursor-pointer text-sm text-slate-400 hover:text-slate-200">
                    <ChevronRight size={14} className="group-open:rotate-90 transition-transform" />
                    SMTP Configuration
                  </summary>
                  <div className="mt-4 space-y-4 pl-6 border-l border-[#1e2a3a]">
                    <div className="grid grid-cols-2 gap-4">
                      <TextInput
                        label="SMTP Host"
                        value={rule.smtp_host || ''}
                        onChange={(v) => onChange({ ...rule, smtp_host: v })}
                        placeholder="smtp.gmail.com"
                      />
                      <NumberInput
                        label="SMTP Port"
                        value={rule.smtp_port ?? 587}
                        onChange={(v) => onChange({ ...rule, smtp_port: v })}
                        min={1}
                        max={65535}
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <TextInput
                        label="Username"
                        value={rule.smtp_user || ''}
                        onChange={(v) => onChange({ ...rule, smtp_user: v })}
                      />
                      <TextInput
                        label="Password"
                        value={rule.smtp_password || ''}
                        onChange={(v) => onChange({ ...rule, smtp_password: v })}
                        type="password"
                        info="For Gmail, use an App Password from myaccount.google.com/apppasswords"
                      />
                    </div>
                    <Toggle
                      label="Use TLS"
                      checked={rule.smtp_tls ?? true}
                      onChange={(v) => onChange({ ...rule, smtp_tls: v })}
                    />
                    <TextInput
                      label="From Address"
                      value={rule.from_address || ''}
                      onChange={(v) => onChange({ ...rule, from_address: v })}
                      placeholder="alerts@yourdomain.com"
                    />
                  </div>
                </details>
              </div>
            )}

            {/* Webhook fields */}
            {rule.delivery_type === 'webhook' && (
              <div className="space-y-4">
                <TextInput
                  label="Webhook URL"
                  value={rule.webhook_url || ''}
                  onChange={(v) => onChange({ ...rule, webhook_url: v })}
                  placeholder="https://discord.com/api/webhooks/..."
                  helper="POST endpoint for alerts"
                  info="Works with Discord, Slack, ntfy.sh, Home Assistant, Pushover, or any HTTP POST endpoint."
                />

                <details className="group">
                  <summary className="flex items-center gap-2 cursor-pointer text-sm text-slate-400 hover:text-slate-200">
                    <ChevronRight size={14} className="group-open:rotate-90 transition-transform" />
                    Custom Headers (optional)
                  </summary>
                  <div className="mt-4 pl-6 border-l border-[#1e2a3a]">
                    <p className="text-xs text-slate-500 mb-2">
                      Headers are configured in config.yaml for security.
                    </p>
                  </div>
                </details>
              </div>
            )}
          </div>

          {/* Behavior section */}
          <div className="grid grid-cols-2 gap-4">
            <NumberInput
              label="Cooldown (minutes)"
              value={rule.cooldown_minutes ?? 10}
              onChange={(v) => onChange({ ...rule, cooldown_minutes: v })}
              min={0}
              helper="Min time between repeat sends"
              info="Prevents alert spam. Same condition won't re-trigger this rule within this window."
            />
            <div className="flex items-end pb-1">
              <Toggle
                label="Override Quiet Hours"
                checked={rule.override_quiet ?? false}
                onChange={(v) => onChange({ ...rule, override_quiet: v })}
                helper="Send during quiet hours"
              />
            </div>
          </div>
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

  const createDefaultRule = (): NotificationRuleConfig => ({
    name: '',
    enabled: true,
    trigger_type: 'condition',
    categories: [],
    min_severity: 'warning',
    schedule_frequency: 'daily',
    schedule_time: '07:00',
    schedule_time_2: '19:00',
    schedule_days: ['monday'],
    schedule_cron: '',
    message_type: 'mesh_health_summary',
    custom_message: '',
    delivery_type: 'mesh_broadcast',
    broadcast_channel: 0,
    node_ids: [],
    smtp_host: '',
    smtp_port: 587,
    smtp_user: '',
    smtp_password: '',
    smtp_tls: true,
    from_address: '',
    recipients: [],
    webhook_url: '',
    webhook_headers: {},
    cooldown_minutes: 10,
    override_quiet: false,
  })

  const addRule = () => {
    if (!config) return
    setConfig({ ...config, rules: [...(config.rules || []), createDefaultRule()] })
  }

  const duplicateRule = (index: number) => {
    if (!config) return
    const original = config.rules[index]
    const duplicate = { ...JSON.parse(JSON.stringify(original)), name: `${original.name} (copy)` }
    const newRules = [...config.rules]
    newRules.splice(index + 1, 0, duplicate)
    setConfig({ ...config, rules: newRules })
  }

  const testRule = async (index: number) => {
    try {
      const res = await fetch(`/api/notifications/rules/${index}/test`, { method: 'POST' })
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
            Configure notification rules for alerts and scheduled reports.
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
          info="When disabled, no alerts or scheduled messages will be delivered. The alert engine still runs and records alerts to history."
        />

        {config.enabled && (
          <>
            {/* Quiet Hours Section - at top */}
            <div className="space-y-3 p-4 bg-[#0a0e17] rounded-lg border border-[#1e2a3a]">
              <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                Quiet Hours
                <InfoButton info="Non-emergency alerts are held during these hours. Rules with 'Override Quiet Hours' enabled still deliver. Emergency and critical alerts always get through." />
              </label>
              <div className="grid grid-cols-2 gap-4">
                <TimeInput
                  label="Start Time"
                  value={config.quiet_hours_start || '22:00'}
                  onChange={(v) => setConfig({ ...config, quiet_hours_start: v })}
                  helper="When quiet hours begin"
                />
                <TimeInput
                  label="End Time"
                  value={config.quiet_hours_end || '06:00'}
                  onChange={(v) => setConfig({ ...config, quiet_hours_end: v })}
                  helper="When quiet hours end"
                />
              </div>
            </div>

            {/* Rules Section */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                  Notification Rules
                  <InfoButton info="Each rule is self-contained: define what triggers it (condition or schedule), where to send it (mesh, email, webhook), and behavior settings." />
                </label>
                <span className="text-xs text-slate-500">
                  {config.rules?.length || 0} rule{(config.rules?.length || 0) !== 1 ? 's' : ''}
                </span>
              </div>

              {(config.rules || []).map((rule, i) => (
                <NotificationRuleCard
                  key={i}
                  rule={rule}
                  categories={categories}
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
                  onDuplicate={() => duplicateRule(i)}
                  onTest={() => testRule(i)}
                />
              ))}

              <button
                onClick={addRule}
                className="w-full py-3 border border-dashed border-[#1e2a3a] rounded-lg text-slate-500 hover:text-slate-300 hover:border-accent flex items-center justify-center gap-2 transition-colors"
              >
                <Plus size={16} /> Add Rule
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
