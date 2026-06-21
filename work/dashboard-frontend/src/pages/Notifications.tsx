import { useState, useEffect, useCallback } from 'react'
import {
  Save, RotateCcw, RefreshCw, Plus, Trash2, ChevronDown, ChevronRight,
  Check, X, Eye as EyeIcon, EyeOff, Send, Clock, Zap,
  Calendar, AlertTriangle, Copy, AlertCircle, Layers,
  Wifi, WifiOff, Mail, Globe, Radio, MessageSquare,
  Activity, Cloud, Flame, Car, Snowflake, Mountain, MapPin, Satellite
} from 'lucide-react'
import ChannelPicker from '@/components/ChannelPicker'
import NodePicker from '@/components/NodePicker'
import { fetchConfig as apiFetchConfig, updateConfig as apiUpdateConfig } from '@/lib/api'

// Types
interface NotificationRuleConfig {
  name: string
  enabled: boolean
  trigger_type: 'condition' | 'schedule'
  categories: string[]
  min_severity: string
  schedule_frequency: 'daily' | 'twice_daily' | 'weekly'
  schedule_time: string
  schedule_time_2: string
  schedule_days: string[]
  message_type: string
  custom_message: string
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
  cooldown_minutes: number
  region_scope: string[]
}

interface NotificationToggle {
  name: string
  enabled: boolean
  min_severity: string
  regions: string[]
  severity_channels: Record<string, string[]>
  broadcast_channel: number | null
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
}

interface NotificationsConfig {
  enabled: boolean
  cold_start_grace_seconds?: number
  band_conditions_enabled?: boolean
  band_conditions_schedule?: string[]
  rules: NotificationRuleConfig[]
  toggles?: Record<string, NotificationToggle>
}

interface AlertCategory {
  id: string
  name: string
  description: string
  default_severity: string
  example_message: string
  toggle?: string
}

interface RegionInfo {
  name: string
  local_name?: string
}

interface RuleStats {
  last_fired: number | null
  last_test: number | null
  fire_count: number
}

interface SourceHealth {
  [category: string]: {
    enabled: boolean
    active_events: number
    source: string
    status: 'ok' | 'disabled' | 'no_data'
  }
}

interface TestResult {
  live_data_summary?: string[]
  conditions_matched?: number
  preview_messages?: string[]
  is_example?: boolean
  conditions_below_threshold?: number
  below_threshold_summary?: string
  below_threshold_events?: { headline: string; severity: string }[]
  suggestion?: string
  delivered?: boolean
  delivery_method?: string
  delivery_result?: string
  delivery_error?: string
  can_send_live?: boolean
  source_health?: SourceHealth
  rule_stats?: RuleStats
  // Legacy
  success?: boolean
  message?: string
}

interface ChannelTestResult {
  success: boolean
  message: string
  error: string
  details: Record<string, unknown>
}

// Severity levels with descriptions
const SEVERITY_OPTIONS = [
  { value: 'routine', label: 'Routine', description: 'Informational, no time pressure (ducting, new node, weather advisory, battery declining)' },
  { value: 'priority', label: 'Priority', description: 'Needs attention soon (severe weather, fire nearby, node offline, HF blackout)' },
  { value: 'immediate', label: 'Immediate', description: 'Act now, drop everything (fire at infrastructure, extreme weather, region blackout)' },
]

// Notification rule templates
const RULE_TEMPLATES = [
  {
    id: "mesh_health",
    name: "Mesh Health Monitoring",
    description: "Infrastructure problems - offline nodes, low battery, channel congestion",
    rule: {
      name: "Mesh Health Monitoring",
      enabled: true,
      trigger_type: "condition" as const,
      categories: ["infra_offline", "critical_node_down", "infra_recovery", "battery_warning", "battery_critical", "battery_emergency", "high_utilization", "packet_flood", "mesh_score_low"],
      min_severity: "routine",
      delivery_type: "mesh_broadcast",
      broadcast_channel: 0,
      cooldown_minutes: 30,
      schedule_frequency: "daily" as const,
      schedule_time: "07:00",
      schedule_time_2: "",
      schedule_days: [] as string[],
      message_type: "",
      custom_message: "",
      node_ids: [] as string[],
      smtp_host: "",
      smtp_port: 587,
      smtp_user: "",
      smtp_password: "",
      smtp_tls: true,
      from_address: "",
      recipients: [] as string[],
      webhook_url: "",
      webhook_headers: {} as Record<string, string>,
    }
  },
  {
    id: "weather_fire",
    name: "Weather & Fire Alerts",
    description: "Environmental threats - severe weather, nearby wildfires, new ignitions, flooding",
    rule: {
      name: "Weather & Fire Alerts",
      enabled: true,
      trigger_type: "condition" as const,
      categories: ["weather_warning", "fire_proximity", "new_ignition", "stream_flood_warning"],
      min_severity: "priority",
      delivery_type: "mesh_broadcast",
      broadcast_channel: 0,
      cooldown_minutes: 15,
      schedule_frequency: "daily" as const,
      schedule_time: "07:00",
      schedule_time_2: "",
      schedule_days: [] as string[],
      message_type: "",
      custom_message: "",
      node_ids: [] as string[],
      smtp_host: "",
      smtp_port: 587,
      smtp_user: "",
      smtp_password: "",
      smtp_tls: true,
      from_address: "",
      recipients: [] as string[],
      webhook_url: "",
      webhook_headers: {} as Record<string, string>,
    }
  },
  {
    id: "rf_conditions",
    name: "RF Conditions",
    description: "Propagation changes - solar events, HF blackouts, tropospheric ducting",
    rule: {
      name: "RF Conditions",
      enabled: true,
      trigger_type: "condition" as const,
      categories: ["hf_blackout", "tropospheric_ducting", "geomagnetic_storm"],
      min_severity: "routine",
      delivery_type: "mesh_broadcast",
      broadcast_channel: 0,
      cooldown_minutes: 60,
      schedule_frequency: "daily" as const,
      schedule_time: "07:00",
      schedule_time_2: "",
      schedule_days: [] as string[],
      message_type: "",
      custom_message: "",
      node_ids: [] as string[],
      smtp_host: "",
      smtp_port: 587,
      smtp_user: "",
      smtp_password: "",
      smtp_tls: true,
      from_address: "",
      recipients: [] as string[],
      webhook_url: "",
      webhook_headers: {} as Record<string, string>,
    }
  },
  {
    id: "road_traffic",
    name: "Road & Traffic",
    description: "Road closures and severe congestion",
    rule: {
      name: "Road & Traffic",
      enabled: true,
      trigger_type: "condition" as const,
      categories: ["road_closure", "traffic_congestion"],
      min_severity: "routine",
      delivery_type: "mesh_broadcast",
      broadcast_channel: 0,
      cooldown_minutes: 30,
      schedule_frequency: "daily" as const,
      schedule_time: "07:00",
      schedule_time_2: "",
      schedule_days: [] as string[],
      message_type: "",
      custom_message: "",
      node_ids: [] as string[],
      smtp_host: "",
      smtp_port: 587,
      smtp_user: "",
      smtp_password: "",
      smtp_tls: true,
      from_address: "",
      recipients: [] as string[],
      webhook_url: "",
      webhook_headers: {} as Record<string, string>,
    }
  },
  {
    id: "everything_critical",
    name: "Everything Critical",
    description: "All emergency-level events regardless of type",
    rule: {
      name: "Everything Critical",
      enabled: true,
      trigger_type: "condition" as const,
      categories: [] as string[],
      min_severity: "immediate",
      delivery_type: "mesh_broadcast",
      broadcast_channel: 0,
      cooldown_minutes: 5,
      schedule_frequency: "daily" as const,
      schedule_time: "07:00",
      schedule_time_2: "",
      schedule_days: [] as string[],
      message_type: "",
      custom_message: "",
      node_ids: [] as string[],
      smtp_host: "",
      smtp_port: 587,
      smtp_user: "",
      smtp_password: "",
      smtp_tls: true,
      from_address: "",
      recipients: [] as string[],
      webhook_url: "",
      webhook_headers: {} as Record<string, string>,
    }
  },
  {
    id: "morning_briefing",
    name: "Morning Briefing",
    description: "Daily health and conditions summary at 7am",
    rule: {
      name: "Morning Briefing",
      enabled: true,
      trigger_type: "schedule" as const,
      categories: [] as string[],
      min_severity: "routine",
      schedule_frequency: "daily" as const,
      schedule_time: "07:00",
      schedule_time_2: "",
      schedule_days: [] as string[],
      message_type: "mesh_health_summary",
      custom_message: "",
      delivery_type: "mesh_broadcast",
      broadcast_channel: 0,
      cooldown_minutes: 0,
      node_ids: [] as string[],
      smtp_host: "",
      smtp_port: 587,
      smtp_user: "",
      smtp_password: "",
      smtp_tls: true,
      from_address: "",
      recipients: [] as string[],
      webhook_url: "",
      webhook_headers: {} as Record<string, string>,
    }
  },
]

// Helper to format relative time
function formatRelativeTime(timestamp: number | null): string {
  if (!timestamp) return 'Never'
  const now = Date.now() / 1000
  const diff = now - timestamp
  if (diff < 60) return 'Just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`
  return new Date(timestamp * 1000).toLocaleDateString()
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
          <div className="absolute left-0 top-6 z-50 w-72 p-3 bg-[#1a2332] border border-[#2a3a4a] shadow-xl text-xs text-slate-300 leading-relaxed">
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

// Severity selector with descriptions
function SeveritySelector({ value, onChange }: {
  value: string
  onChange: (v: string) => void
}) {
  const [isOpen, setIsOpen] = useState(false)
  const selected = SEVERITY_OPTIONS.find(s => s.value === value) || SEVERITY_OPTIONS[0]

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        Severity Threshold
        <InfoButton info="Only alerts at or above this severity trigger this rule. ROUTINE = informational, PRIORITY = needs attention, IMMEDIATE = act now." />
      </label>
      <div className="relative">
        <button
          type="button"
          onClick={() => setIsOpen(!isOpen)}
          className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-left flex items-center justify-between hover:border-accent transition-colors"
        >
          <div>
            <span className="text-slate-200">{selected.label}</span>
            <span className="text-slate-500 ml-2">- {selected.description}</span>
          </div>
          <ChevronDown size={16} className={`text-slate-500 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
        </button>
        {isOpen && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setIsOpen(false)} />
            <div className="absolute left-0 right-0 top-full mt-1 z-50 bg-[#0a0e17] border border-[#1e2a3a] shadow-xl overflow-hidden">
              {SEVERITY_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => { onChange(opt.value); setIsOpen(false) }}
                  className={`w-full px-3 py-2.5 text-left text-sm hover:bg-[#1e2a3a] transition-colors ${
                    value === opt.value ? 'bg-accent/10' : ''
                  }`}
                >
                  <div className="font-medium text-slate-200">{opt.label}</div>
                  <div className="text-xs text-slate-500">{opt.description}</div>
                </button>
              ))}
            </div>
          </>
        )}
      </div>
      <p className="text-xs text-slate-600">Lower = more notifications. "Warning" recommended for most rules.</p>
    </div>
  )
}

// Channel Test Button Component
function ChannelTestButton({ rule }: {
  rule: NotificationRuleConfig
}) {
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<ChannelTestResult | null>(null)

  const handleTest = async () => {
    setTesting(true)
    setResult(null)

    try {
      // Build channel config from rule
      let channelConfig: Record<string, unknown> = { type: rule.delivery_type }

      if (rule.delivery_type === 'mesh_broadcast') {
        channelConfig.channel_index = rule.broadcast_channel
      } else if (rule.delivery_type === 'mesh_dm') {
        channelConfig.node_ids = rule.node_ids
      } else if (rule.delivery_type === 'email') {
        channelConfig = {
          type: 'email',
          smtp_host: rule.smtp_host,
          smtp_port: rule.smtp_port,
          smtp_user: rule.smtp_user,
          smtp_password: rule.smtp_password,
          smtp_tls: rule.smtp_tls,
          from_address: rule.from_address,
          recipients: rule.recipients,
        }
      } else if (rule.delivery_type === 'webhook') {
        channelConfig = {
          type: 'webhook',
          url: rule.webhook_url,
          headers: rule.webhook_headers,
        }
      }

      const res = await fetch('/api/notifications/channels/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(channelConfig),
      })
      const data = await res.json()
      setResult(data)
    } catch (err) {
      setResult({
        success: false,
        message: 'Test failed',
        error: err instanceof Error ? err.message : 'Unknown error',
        details: {}
      })
    } finally {
      setTesting(false)
    }
  }

  if (!rule.delivery_type) return null

  const icon = {
    mesh_broadcast: <Radio size={14} />,
    mesh_dm: <MessageSquare size={14} />,
    email: <Mail size={14} />,
    webhook: <Globe size={14} />,
  }[rule.delivery_type] || <Wifi size={14} />

  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={handleTest}
        disabled={testing}
        className="flex items-center gap-2 px-3 py-1.5 bg-slate-700 hover:bg-slate-600 rounded text-sm disabled:opacity-50"
      >
        {testing ? (
          <>
            <RefreshCw size={14} className="animate-spin" />
            Testing...
          </>
        ) : (
          <>
            {icon}
            Test Channel
          </>
        )}
      </button>

      {result && (
        <div className={`p-2 rounded text-xs ${
          result.success
            ? 'bg-green-500/10 border border-green-500/30 text-green-400'
            : 'bg-red-500/10 border border-red-500/30 text-red-400'
        }`}>
          <div className="flex items-start gap-2">
            {result.success ? <Check size={14} className="mt-0.5 flex-shrink-0" /> : <X size={14} className="mt-0.5 flex-shrink-0" />}
            <div>
              <div className="font-medium">{result.message}</div>
              {result.error && <div className="mt-1 text-red-300">{result.error}</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// Notification Rule Card Component
function NotificationRuleCard({
  rule,
  ruleIndex,
  categories,
  regions,
  onChange,
  onDelete,
  onDuplicate,
  onTest,
}: {
  rule: NotificationRuleConfig
  ruleIndex: number
  categories: AlertCategory[]
  regions: RegionInfo[]
  onChange: (r: NotificationRuleConfig) => void
  onDelete: () => void
  onDuplicate: () => void
  onTest: () => void
}) {
  const [expanded, setExpanded] = useState(!rule.name)
  const [testing, setTesting] = useState(false)
  const [ruleStats, setRuleStats] = useState<RuleStats | null>(null)
  const [sourceHealth, setSourceHealth] = useState<SourceHealth | null>(null)

  // Fetch rule stats on mount
  useEffect(() => {
    if (rule.name && ruleIndex >= 0) {
      fetch(`/api/notifications/rules/${ruleIndex}/stats`)
        .then(res => res.json())
        .then(data => setRuleStats(data))
        .catch(() => {})

      if (rule.categories?.length) {
        fetch('/api/notifications/rules/sources', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ categories: rule.categories })
        })
          .then(res => res.json())
          .then(data => setSourceHealth(data))
          .catch(() => {})
      }
    }
  }, [rule.name, ruleIndex, rule.categories])

  const deliveryOptions = [
    { value: '', label: '(None)', description: 'Rule matches but does not deliver' },
    { value: 'mesh_broadcast', label: 'Mesh Broadcast', description: 'Send to a mesh radio channel' },
    { value: 'mesh_dm', label: 'Mesh DM', description: 'Direct message to specific nodes' },
    { value: 'email', label: 'Email', description: 'Send via SMTP' },
    { value: 'webhook', label: 'Webhook', description: 'POST to any URL' },
  ]

  const frequencyOptions = [
    { value: 'daily', label: 'Daily' },
    { value: 'twice_daily', label: 'Twice Daily' },
    { value: 'weekly', label: 'Weekly' },
  ]

  const messageTypeOptions = [
    { value: 'mesh_health_summary', label: 'Mesh Health Summary', description: 'Current health score, pillar breakdown, problem nodes' },
    { value: 'rf_propagation_report', label: 'RF Propagation Report', description: 'Solar indices, Kp, ducting conditions' },
    { value: 'alerts_digest', label: 'Active Alerts Digest', description: 'Summary of all active environmental alerts' },
    { value: 'environmental_conditions', label: 'Environmental Conditions', description: 'Full conditions: weather, fire, streams, roads' },
    { value: 'custom', label: 'Custom Message', description: 'Write your own with template tokens' },
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

  const selectManyCategories = (catIds: string[], action: 'add' | 'remove') => {
    const current = rule.categories || []
    if (action === 'add') {
      const merged = Array.from(new Set([...current, ...catIds]))
      onChange({ ...rule, categories: merged })
    } else {
      const drop = new Set(catIds)
      onChange({ ...rule, categories: current.filter(c => !drop.has(c)) })
    }
  }

  const toggleRegion = (regionName: string) => {
    const current = rule.region_scope || []
    if (current.includes(regionName)) {
      onChange({ ...rule, region_scope: current.filter(r => r !== regionName) })
    } else {
      onChange({ ...rule, region_scope: [...current, regionName] })
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

  // Get example message for display
  const getExampleMessage = (): string => {
    if (rule.trigger_type === 'schedule') {
      return '[Scheduled report preview would appear here]'
    }
    const ruleCats = rule.categories || []
    if (ruleCats.length === 0 && categories.length > 0) {
      return categories[0].example_message || 'Alert notification'
    }
    const firstCat = categories.find(c => ruleCats.includes(c.id))
    return firstCat?.example_message || 'Alert notification'
  }

  // Generate summary for collapsed view
  const getSummary = () => {
    const parts: string[] = []

    if (rule.trigger_type === 'schedule') {
      const freq = frequencyOptions.find(f => f.value === rule.schedule_frequency)?.label || rule.schedule_frequency
      const msgType = messageTypeOptions.find(m => m.value === rule.message_type)?.label || rule.message_type
      parts.push(`${freq} at ${rule.schedule_time || '??:??'}`)
      parts.push(msgType)
    } else {
      const catCount = rule.categories?.length || 0
      const catText = catCount === 0 ? 'All' : categories.filter(c => rule.categories?.includes(c.id)).map(c => c.name).slice(0, 2).join(', ') + (catCount > 2 ? ` +${catCount - 2}` : '')
      const severity = SEVERITY_OPTIONS.find(s => s.value === rule.min_severity)?.label || rule.min_severity
      parts.push(`${catText} at ${severity}+`)
    }

    // Delivery summary
    if (!rule.delivery_type) {
      parts.push('No delivery')
    } else {
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
          target = rule.webhook_url?.slice(0, 20) || 'no URL'
        }
      }
      parts.push(`${delivery}${target ? ` (${target})` : ''}`)
    }

    return parts.join(' -> ')
  }

  // Get source status indicators
  const getSourceIndicators = () => {
    if (!sourceHealth || !rule.categories?.length) return null

    const sources = new Map<string, { enabled: boolean; events: number }>()
    for (const [, health] of Object.entries(sourceHealth)) {
      const existing = sources.get(health.source)
      if (existing) {
        existing.events += health.active_events
        existing.enabled = existing.enabled && health.enabled
      } else {
        sources.set(health.source, { enabled: health.enabled, events: health.active_events })
      }
    }

    return Array.from(sources.entries()).map(([source, { enabled, events }]) => (
      <span
        key={source}
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs ${
          enabled
            ? 'bg-green-500/10 text-green-400'
            : 'bg-red-500/10 text-red-400'
        }`}
        title={enabled ? `${events} active` : 'Not enabled'}
      >
        {enabled ? <Wifi size={10} /> : <WifiOff size={10} />}
        {source.toUpperCase()}
        {enabled && events > 0 && ` (${events})`}
      </span>
    ))
  }

  return (
    <div className={`border overflow-hidden ${rule.enabled ? 'border-[#1e2a3a]' : 'border-slate-700 opacity-60'}`}>
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
            title={rule.enabled ? 'Enabled' : 'Disabled'}
          />
          {rule.trigger_type === 'schedule' ? (
            <Clock size={14} className="text-[#f59e0b] flex-shrink-0" />
          ) : (
            <Zap size={14} className="text-yellow-400 flex-shrink-0" />
          )}
          <span className="font-medium text-slate-200 truncate" title={rule.name || undefined}>{rule.name || 'New Rule'}</span>
          {!expanded && (
            <span className={`text-xs truncate hidden sm:block ${!rule.delivery_type ? 'text-amber-400' : 'text-slate-500'}`}>
              {getSummary()}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {/* Activity badge */}
          {!expanded && (() => {
            const badgeBase = 'hidden sm:inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs mr-2'
            if (!rule.enabled) {
              return <span className={`${badgeBase} bg-slate-800 text-slate-500`}>Disabled</span>
            }
            if (!ruleStats) return null
            const count = ruleStats.fire_count || 0
            const last = ruleStats.last_fired
            const sevenDaysAgo = (Date.now() / 1000) - 7 * 86400
            if (count > 0 && last && last >= sevenDaysAgo) {
              return (
                <span className={`${badgeBase} bg-green-500/10 text-green-400`} title={`Last fired ${formatRelativeTime(last)}`}>
                  Active
                </span>
              )
            }
            if (count > 0 && last) {
              return (
                <span className={`${badgeBase} bg-yellow-500/10 text-yellow-400`} title={`Last fired ${formatRelativeTime(last)}`}>
                  Idle (no recent activity)
                </span>
              )
            }
            return <span className={`${badgeBase} bg-slate-800 text-slate-400`}>No activity yet</span>
          })()}
          {/* Source indicators */}
          {!expanded && (
            <div className="hidden md:flex items-center gap-1 mr-2">
              {getSourceIndicators()}
            </div>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); handleTest() }}
            disabled={testing || !rule.name}
            className="p-1.5 text-[#f59e0b] hover:text-[#d97706] hover:bg-[#f59e0b]/10 rounded disabled:opacity-50"
            title="Test rule"
          >
            <Send size={14} />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onDuplicate() }}
            className="p-1.5 text-slate-400 hover:text-slate-200 hover:bg-slate-500/10 rounded"
            title="Duplicate"
          >
            <Copy size={14} />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onDelete() }}
            className="p-1.5 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded"
            title="Delete"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>

      {/* Status line (collapsed view) */}
      {!expanded && rule.name && (
        <div className="px-3 pb-2 pt-0 bg-[#0a0e17] flex items-center gap-2 flex-wrap text-xs">
          {!rule.delivery_type && (
            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-amber-500/10 text-amber-400 rounded">
              <AlertCircle size={10} />
              No delivery method
            </span>
          )}
          {ruleStats?.fire_count !== undefined && ruleStats.fire_count > 0 && (
            <span className="text-slate-500">
              Fired {ruleStats.fire_count}x
            </span>
          )}
        </div>
      )}

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

          {/* Trigger type toggle */}
          <div className="space-y-2">
            <label className="text-xs text-slate-500 uppercase tracking-wide">Trigger Type</label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => onChange({ ...rule, trigger_type: 'condition' })}
                className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 border transition-colors ${
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
                className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 border transition-colors ${
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
                ? 'Send reports on a schedule (daily briefings, weekly digests)'
                : 'React to alert conditions (fires, outages, weather warnings)'}
            </p>
          </div>

          {/* WHEN section - Condition trigger */}
          {rule.trigger_type !== 'schedule' && (
            <div className="space-y-4 p-4 bg-[#0a0e17] border border-[#1e2a3a]">
              <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
                <AlertTriangle size={14} />
                WHEN (Condition)
              </div>

              <SeveritySelector
                value={rule.min_severity}
                onChange={(v) => onChange({ ...rule, min_severity: v })}
              />

              <div className="space-y-2">
                <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                  Alert Categories
                  <InfoButton info="Select which types of alerts trigger this rule. Leave all unchecked to match ALL categories. Categories are grouped by family — use the 'All' / 'Clear' buttons in each header to bulk-toggle." />
                </label>
                <div className="text-xs text-slate-500 mb-2">
                  {(rule.categories?.length || 0) === 0 ? 'All categories (none selected)' : `${rule.categories?.length} selected`}
                </div>
                <GroupedCategoryPicker
                  categories={categories}
                  selected={rule.categories || []}
                  onToggle={toggleCategory}
                  onSelectMany={selectManyCategories}
                />
              </div>

              {/* Source health display */}
              {sourceHealth && Object.keys(sourceHealth).length > 0 && (
                <div className="space-y-2">
                  <label className="text-xs text-slate-500 uppercase tracking-wide">Data Sources</label>
                  <div className="flex flex-wrap gap-2">
                    {getSourceIndicators()}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* WHEN section - Schedule trigger */}
          {rule.trigger_type === 'schedule' && (
            <div className="space-y-4 p-4 bg-[#0a0e17] border border-[#1e2a3a]">
              <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
                <Calendar size={14} />
                WHEN (Schedule)
              </div>

              <div className="space-y-1">
                <label className="text-xs text-slate-500 uppercase tracking-wide">Frequency</label>
                <select
                  value={rule.schedule_frequency || 'daily'}
                  onChange={(e) => onChange({ ...rule, schedule_frequency: e.target.value as 'daily' | 'twice_daily' | 'weekly' })}
                  className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
                >
                  {frequencyOptions.map(opt => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <TimeInput
                  label="Time"
                  value={rule.schedule_time || '07:00'}
                  onChange={(v) => onChange({ ...rule, schedule_time: v })}
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

              <div className="space-y-1">
                <label className="text-xs text-slate-500 uppercase tracking-wide">Report Type</label>
                <select
                  value={rule.message_type || 'mesh_health_summary'}
                  onChange={(e) => onChange({ ...rule, message_type: e.target.value })}
                  className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
                >
                  {messageTypeOptions.map(opt => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
                <p className="text-xs text-slate-600">
                  {messageTypeOptions.find(m => m.value === rule.message_type)?.description}
                </p>
              </div>

              {rule.message_type === 'custom' && (
                <div className="space-y-1">
                  <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                    Custom Message
                    <InfoButton info="Available tokens: {MESH_SCORE}, {NODE_COUNT}, {NODES_ONLINE}, {ACTIVE_ALERTS}, {KP}, {SFI}, {DATE}, {TIME}" />
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

          {/* REGIONS section — scope rule to specific regions; empty = all regions */}
          <div className="space-y-2 p-4 bg-[#0a0e17] border border-[#1e2a3a]">
            <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
              <MapPin size={14} />
              REGIONS
              <InfoButton info="Limit this rule to alerts from specific regions. Empty selection = all regions (backward compatible). Region names come from /api/regions." />
            </div>
            <div className="text-xs text-slate-500">
              {(rule.region_scope?.length || 0) === 0
                ? 'All regions (none selected)'
                : `${rule.region_scope.length} of ${regions.length} selected`}
            </div>
            {regions.length === 0 ? (
              <div className="text-xs text-slate-600 italic">No regions configured.</div>
            ) : (
              <div className="flex flex-wrap gap-2">
                {regions.map(r => {
                  const on = (rule.region_scope || []).includes(r.name)
                  return (
                    <button
                      key={r.name}
                      type="button"
                      onClick={() => toggleRegion(r.name)}
                      className={`px-3 py-1.5 rounded text-sm transition-colors ${
                        on
                          ? 'bg-accent text-white'
                          : 'bg-[#1e2a3a] text-slate-400 hover:text-slate-200'
                      }`}
                      title={r.local_name || r.name}
                    >
                      {r.local_name || r.name}
                    </button>
                  )
                })}
              </div>
            )}
          </div>

          {/* SEND VIA section */}
          <div className="space-y-4 p-4 bg-[#0a0e17] border border-[#1e2a3a]">
            <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
              <Send size={14} />
              SEND VIA
            </div>

            <div className="space-y-1">
              <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                Delivery Method
                <InfoButton info="Where this notification gets delivered. Select (None) to save the rule without delivery - it will match conditions but won't send until you configure a delivery method." />
              </label>
              <select
                value={rule.delivery_type || ''}
                onChange={(e) => onChange({ ...rule, delivery_type: e.target.value })}
                className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
              >
                {deliveryOptions.map(opt => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
              <p className="text-xs text-slate-600">
                {deliveryOptions.find(d => d.value === (rule.delivery_type || ''))?.description}
              </p>
            </div>

            {/* No delivery warning */}
            {!rule.delivery_type && (
              <div className="flex items-start gap-2 p-3 bg-amber-500/10 border border-amber-500/20">
                <AlertCircle size={16} className="text-amber-400 mt-0.5 flex-shrink-0" />
                <div className="text-sm text-amber-300">
                  Rule will log matches but not deliver until a delivery method is configured.
                </div>
              </div>
            )}

            {/* Mesh Broadcast fields */}
            {rule.delivery_type === 'mesh_broadcast' && (
              <>
                <ChannelPicker
                  label="Broadcast Channel"
                  value={rule.broadcast_channel ?? 0}
                  onChange={(v) => onChange({ ...rule, broadcast_channel: v })}
                  helper="Select the mesh radio channel"
                  mode="single"
                />
                <ChannelTestButton rule={rule} />
              </>
            )}

            {/* Mesh DM fields */}
            {rule.delivery_type === 'mesh_dm' && (
              <>
                <NodePicker
                  label="Recipient Nodes"
                  value={rule.node_ids || []}
                  onChange={(v) => onChange({ ...rule, node_ids: v })}
                  helper="Nodes that receive direct messages"
                  valueType="node_id_hex"
                />
                <ChannelTestButton rule={rule} />
              </>
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
                        info="Gmail users: use an App Password from myaccount.google.com/apppasswords"
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

                <ChannelTestButton rule={rule} />
              </div>
            )}

            {/* Webhook fields */}
            {rule.delivery_type === 'webhook' && (
              <>
                <TextInput
                  label="Webhook URL"
                  value={rule.webhook_url || ''}
                  onChange={(v) => onChange({ ...rule, webhook_url: v })}
                  placeholder="https://discord.com/api/webhooks/..."
                  helper="POST alert as JSON"
                  info="Works with Discord webhooks, ntfy.sh, Slack, Home Assistant, Pushover, or any HTTP POST endpoint."
                />
                <ChannelTestButton rule={rule} />
              </>
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
            />          </div>

          {/* Rule statistics */}
          {ruleStats && (
            <div className="flex items-center gap-4 text-xs text-slate-500">
              <span>Last fired: {formatRelativeTime(ruleStats.last_fired)}</span>
              <span>Last tested: {formatRelativeTime(ruleStats.last_test)}</span>
              <span>Total fires: {ruleStats.fire_count}</span>
            </div>
          )}

          {/* Example message */}
          {rule.trigger_type !== 'schedule' && (
            <div className="space-y-2">
              <label className="text-xs text-slate-500 uppercase tracking-wide">Example Message</label>
              <div className="p-3 bg-[#1e2a3a]/50 border border-[#1e2a3a]">
                <p className="text-sm text-slate-300 font-mono">{getExampleMessage()}</p>
              </div>
              <p className="text-xs text-slate-600">This is an example of what this rule would send.</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Main Notifications Page Component
const TOGGLE_FAMILY_META: { key: string; label: string; Icon: typeof Activity }[] = [
  { key: 'mesh_health', label: 'Mesh Health', Icon: Activity },
  { key: 'weather', label: 'Weather', Icon: Cloud },
  { key: 'fire', label: 'Fire', Icon: Flame },
  { key: 'rf_propagation', label: 'RF Propagation', Icon: Radio },
  { key: 'roads', label: 'Roads', Icon: Car },
  { key: 'avalanche', label: 'Avalanche', Icon: Snowflake },
  { key: 'satpass', label: 'Satellite Passes', Icon: Satellite },
  { key: 'seismic', label: 'Seismic', Icon: Mountain },
  { key: 'tracking', label: 'Tracking', Icon: MapPin },
]

// Grouped category picker — shows categories family-by-family using TOGGLE_FAMILY_META
// for icons/labels/order. Each family has a Select-all / Clear control and a count.
// Categories whose `toggle` field doesn't match a known family fall into "Other".
function GroupedCategoryPicker({
  categories,
  selected,
  onToggle,
  onSelectMany,
}: {
  categories: AlertCategory[]
  selected: string[]
  onToggle: (catId: string) => void
  onSelectMany: (catIds: string[], action: 'add' | 'remove') => void
}) {
  const FAMILY_KEYS = new Set(TOGGLE_FAMILY_META.map(f => f.key))
  // Group by toggle, preserving family order; collect unknowns into "other".
  const byFamily = new Map<string, AlertCategory[]>()
  TOGGLE_FAMILY_META.forEach(f => byFamily.set(f.key, []))
  const other: AlertCategory[] = []
  for (const cat of categories) {
    const fam = cat.toggle
    if (fam && FAMILY_KEYS.has(fam)) byFamily.get(fam)!.push(cat)
    else other.push(cat)
  }

  // Track which families are expanded. Default: any family that has selected items
  // starts expanded so users can see what's checked.
  const initialOpen = new Set<string>()
  for (const [fam, cats] of byFamily) {
    if (cats.some(c => selected.includes(c.id))) initialOpen.add(fam)
  }
  if (other.some(c => selected.includes(c.id))) initialOpen.add('other')
  const [openFamilies, setOpenFamilies] = useState<Set<string>>(initialOpen)
  const toggleOpen = (key: string) => {
    setOpenFamilies(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })
  }

  const renderGroup = (key: string, label: string, Icon: typeof Activity | null, cats: AlertCategory[]) => {
    if (!cats.length) return null
    const isOpen = openFamilies.has(key)
    const ids = cats.map(c => c.id)
    const selectedInFamily = ids.filter(id => selected.includes(id)).length
    return (
      <div key={key} className="border border-[#1e2a3a] rounded">
        <div className="flex items-center justify-between px-2 py-1.5 bg-[#0d1420]">
          <button
            type="button"
            onClick={() => toggleOpen(key)}
            className="flex items-center gap-2 text-sm text-slate-200 flex-1 min-w-0"
          >
            {isOpen ? <ChevronDown size={14} className="text-slate-500 flex-shrink-0" /> : <ChevronRight size={14} className="text-slate-500 flex-shrink-0" />}
            {Icon && <Icon size={14} className="text-slate-400 flex-shrink-0" />}
            <span className="truncate">{label} ({cats.length})</span>
            {selectedInFamily > 0 && (
              <span className="ml-1 text-xs text-accent">{selectedInFamily} selected</span>
            )}
          </button>
          <div className="flex items-center gap-1 flex-shrink-0">
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onSelectMany(ids, 'add') }}
              className="text-xs px-2 py-0.5 rounded text-slate-400 hover:text-accent hover:bg-accent/10"
              title="Select all in family"
            >
              All
            </button>
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onSelectMany(ids, 'remove') }}
              className="text-xs px-2 py-0.5 rounded text-slate-400 hover:text-red-400 hover:bg-red-500/10"
              title="Clear family"
            >
              Clear
            </button>
          </div>
        </div>
        {isOpen && (
          <div className="p-1 space-y-1">
            {cats.map(cat => (
              <label
                key={cat.id}
                onClick={() => onToggle(cat.id)}
                className="flex items-start gap-2 p-2 rounded hover:bg-[#1e2a3a]/50 cursor-pointer"
              >
                <div className={`w-4 h-4 mt-0.5 rounded border flex items-center justify-center flex-shrink-0 ${
                  selected.includes(cat.id) ? 'bg-accent border-accent' : 'border-slate-600'
                }`}>
                  {selected.includes(cat.id) && <Check size={12} className="text-white" />}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-slate-200">{cat.name}</div>
                  <div className="text-xs text-slate-500">{cat.description}</div>
                </div>
              </label>
            ))}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="max-h-96 overflow-y-auto border border-[#1e2a3a] p-2 space-y-2">
      {TOGGLE_FAMILY_META.map(f => renderGroup(f.key, f.label, f.Icon, byFamily.get(f.key) || []))}
      {renderGroup('other', 'Other', null, other)}
    </div>
  )
}
const TOGGLE_CHANNELS = ['digest', 'mesh_broadcast', 'mesh_dm', 'email', 'webhook']
const TOGGLE_SEVERITIES = ['routine', 'priority', 'immediate']

function MasterToggles({ toggles, onChange }: {
  toggles: Record<string, NotificationToggle>
  onChange: (t: Record<string, NotificationToggle>) => void
}) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const upd = (fam: string, patch: Partial<NotificationToggle>) =>
    onChange({ ...toggles, [fam]: { ...(toggles[fam] || {}), name: fam, ...patch } as NotificationToggle })
  return (
    <div className="space-y-3 mb-8">
      <div className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        Master Toggles
        <InfoButton info="Per-family notification policy: enable a family, set its severity threshold, choose which channels fire at each severity, and scope to regions (PagerDuty/Grafana-style)." />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {TOGGLE_FAMILY_META.map(({ key, label, Icon }) => {
          const t = (toggles[key] || ({} as NotificationToggle))
          const isOpen = expanded === key
          const chanCount = Object.values(t.severity_channels || {}).reduce((n, arr) => n + ((arr as string[])?.length || 0), 0)
          const regionCount = (t.regions || []).length
          return (
            <div key={key} className="border border-[#1e2a3a] p-3">
              <div className="flex items-center justify-between">
                <button type="button" onClick={() => setExpanded(isOpen ? null : key)}
                        className="flex items-center gap-2 text-sm text-slate-200">
                  <Icon size={15} /> {label}
                  {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
                <Toggle label="" checked={!!t.enabled} onChange={(v) => upd(key, { enabled: v })} />
              </div>
              {!isOpen && (
                <div className="text-xs text-slate-600 mt-1">
                  {t.enabled
                    ? `${regionCount || 'all'} region${regionCount === 1 ? '' : 's'}, ${chanCount} channel${chanCount === 1 ? '' : 's'} at ${t.min_severity || 'priority'}+`
                    : 'OFF'}
                </div>
              )}
              {isOpen && (
                <div className={`mt-3 space-y-3 ${t.enabled ? '' : 'opacity-40 pointer-events-none select-none'}`}>
                  <SeveritySelector value={t.min_severity || 'priority'} onChange={(v) => upd(key, { min_severity: v })} />
                  <div className="text-xs text-slate-500">Severity &rarr; channels</div>
                  <table className="text-xs w-full">
                    <thead>
                      <tr><th></th>{TOGGLE_CHANNELS.map((c) => <th key={c} className="text-slate-500 font-normal px-1">{c.replace('_', ' ')}</th>)}</tr>
                    </thead>
                    <tbody>
                      {TOGGLE_SEVERITIES.map((sev) => (
                        <tr key={sev}>
                          <td className="text-slate-400 pr-2">{sev}</td>
                          {TOGGLE_CHANNELS.map((ch) => {
                            const on = (t.severity_channels?.[sev] || []).includes(ch)
                            return (
                              <td key={ch} className="text-center">
                                <input type="checkbox" checked={on} onChange={(e) => {
                                  const cur: Record<string, string[]> = { ...(t.severity_channels || {}) }
                                  const arr = new Set(cur[sev] || [])
                                  if (e.target.checked) arr.add(ch); else arr.delete(ch)
                                  cur[sev] = Array.from(arr)
                                  upd(key, { severity_channels: cur })
                                }} />
                              </td>
                            )
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <ListInput label="Regions (empty = all)" value={t.regions || []} onChange={(v) => upd(key, { regions: v })} placeholder="Add region..." />                  <div className="text-xs text-slate-500 pt-1">Channel config</div>
                  <NumberInput label="Broadcast channel" value={t.broadcast_channel ?? 0} onChange={(v) => upd(key, { broadcast_channel: v })} />
                  <ListInput label="DM node IDs" value={t.node_ids || []} onChange={(v) => upd(key, { node_ids: v })} placeholder="!nodeid" />
                  <ListInput label="Email recipients" value={t.recipients || []} onChange={(v) => upd(key, { recipients: v })} placeholder="ops@example.com" />
                  <TextInput label="SMTP host" value={t.smtp_host || ''} onChange={(v) => upd(key, { smtp_host: v })} placeholder="smtp.example.com" />
                  <NumberInput label="SMTP port" value={t.smtp_port ?? 587} onChange={(v) => upd(key, { smtp_port: v })} />
                  <TextInput label="Webhook URL" value={t.webhook_url || ''} onChange={(v) => upd(key, { webhook_url: v })} placeholder="https://..." />
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}


export default function Notifications() {
  const [config, setConfig] = useState<NotificationsConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<NotificationsConfig | null>(null)
  const [categories, setCategories] = useState<AlertCategory[]>([])
  const [regions, setRegions] = useState<RegionInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<TestResult | null>(null)
  const [testDialog, setTestDialog] = useState<{ open: boolean; ruleIndex: number; loading: boolean; action: string }>({ open: false, ruleIndex: -1, loading: false, action: '' })
  const [showTemplates, setShowTemplates] = useState(false)
  const [hasChanges, setHasChanges] = useState(false)

  const fetchConfig = useCallback(async () => {
    try {
      const [configRes, categoriesRes, regionsRes] = await Promise.all([
        fetch('/api/config/notifications'),
        fetch('/api/notifications/categories'),
        fetch('/api/regions'),
      ])
      if (!configRes.ok) throw new Error('Failed to fetch notifications config')
      const configData = await configRes.json()
      const categoriesData = await categoriesRes.json()
      // /api/regions is best-effort — don't block the page if it fails.
      const regionsData: RegionInfo[] = regionsRes.ok ? await regionsRes.json() : []
      setConfig(configData)
      setOriginalConfig(JSON.parse(JSON.stringify(configData)))
      setCategories(categoriesData)
      setRegions(Array.isArray(regionsData) ? regionsData : [])
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'Notifications - MeshAI'
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
    min_severity: 'routine',
    schedule_frequency: 'daily',
    schedule_time: '07:00',
    schedule_time_2: '19:00',
    schedule_days: ['monday'],
    message_type: 'mesh_health_summary',
    custom_message: '',
    delivery_type: '',
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
    region_scope: [],
  })

  const addRule = () => {
    if (!config) return
    setConfig({ ...config, rules: [...(config.rules || []), createDefaultRule()] })
  }

  const addFromTemplate = (templateId: string) => {
    if (!config) return
    const template = RULE_TEMPLATES.find(t => t.id === templateId)
    if (!template) return
    // Merge over defaults so future NotificationRuleConfig fields (e.g., region_scope)
    // don't have to be back-filled into every literal template.
    setConfig({ ...config, rules: [...(config.rules || []), { ...createDefaultRule(), ...template.rule }] })
    setShowTemplates(false)
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
    setTestDialog({ open: true, ruleIndex: index, loading: true, action: '' })
    try {
      const res = await fetch(`/api/notifications/rules/${index}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'preview' })
      })
      const result = await res.json()
      setTestResult(result)
      setTestDialog(d => ({ ...d, loading: false }))
    } catch {
      setTestResult({ success: false, message: 'Failed to get preview' })
      setTestDialog(d => ({ ...d, loading: false }))
    }
  }

  const sendTestAction = async (action: string) => {
    const index = testDialog.ruleIndex
    setTestDialog(d => ({ ...d, loading: true, action }))
    try {
      const res = await fetch(`/api/notifications/rules/${index}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action })
      })
      const result = await res.json()
      setTestResult(result)
      setTestDialog(d => ({ ...d, loading: false }))
    } catch {
      setTestResult({ success: false, message: `Failed to ${action}` })
      setTestDialog(d => ({ ...d, loading: false }))
    }
  }

  const closeTestDialog = () => {
    setTestDialog({ open: false, ruleIndex: -1, loading: false, action: '' })
    setTestResult(null)
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
      {/* Test Dialog */}
      {testDialog.open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="bg-[#1a2332] border border-[#2a3a4a] shadow-xl max-w-2xl w-full mx-4 max-h-[85vh] overflow-auto">
            <div className="p-4 border-b border-[#2a3a4a] flex items-center justify-between sticky top-0 bg-[#1a2332]">
              <h3 className="text-lg font-semibold">Test Notification Rule</h3>
              <button onClick={closeTestDialog} className="text-slate-500 hover:text-slate-300">
                <X size={20} />
              </button>
            </div>
            <div className="p-4 space-y-4">
              {testDialog.loading ? (
                <div className="flex items-center justify-center py-8">
                  <RefreshCw size={20} className="animate-spin text-slate-400 mr-2" />
                  <div className="text-slate-400">
                    {testDialog.action ? `${testDialog.action.replace('_', ' ').replace('send ', 'Sending ')}...` : 'Loading current data...'}
                  </div>
                </div>
              ) : testResult ? (
                <>
                  {/* Section 1: Current Data */}
                  <div className="space-y-2">
                    <div className="text-sm font-medium text-slate-400 uppercase tracking-wide">Current Data</div>
                    {testResult.live_data_summary && testResult.live_data_summary.length > 0 ? (
                      <div className="p-3 bg-slate-800/50 rounded space-y-1">
                        {testResult.live_data_summary.map((line, i) => (
                          <div
                            key={i}
                            className={`text-sm font-mono ${line.startsWith('[!]') ? 'text-amber-400' : ''}`}
                          >
                            {line}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="p-3 bg-slate-800/50 rounded text-sm text-slate-500">
                        No live data available for this rule's categories
                      </div>
                    )}
                  </div>

                  {/* Section 2: Alert Conditions */}
                  <div className="space-y-2">
                    <div className="text-sm font-medium text-slate-400 uppercase tracking-wide">Rule Matching</div>
                    <div className="flex items-center gap-2 flex-wrap">
                      {testResult.conditions_matched && testResult.conditions_matched > 0 ? (
                        <span className="px-2 py-1 bg-green-500/20 text-green-400 rounded text-sm">
                          {testResult.conditions_matched} condition{testResult.conditions_matched !== 1 ? 's' : ''} match - this rule WOULD fire
                        </span>
                      ) : (
                        <span className="px-2 py-1 bg-slate-700 text-slate-400 rounded text-sm">
                          No conditions trigger this rule right now
                        </span>
                      )}
                      {testResult.conditions_below_threshold && testResult.conditions_below_threshold > 0 && (
                        <span className="px-2 py-1 bg-yellow-500/20 text-yellow-400 rounded text-sm">
                          {testResult.conditions_below_threshold} below threshold
                        </span>
                      )}
                    </div>

                    {/* Near-miss explanation */}
                    {testResult.conditions_below_threshold && testResult.conditions_below_threshold > 0 && (
                      <div className="p-3 bg-yellow-500/10 border border-yellow-500/30 rounded text-sm space-y-2">
                        <div className="text-yellow-300">{testResult.below_threshold_summary}</div>
                        {testResult.below_threshold_events && testResult.below_threshold_events.length > 0 && (
                          <div className="space-y-1 text-yellow-200/80">
                            {testResult.below_threshold_events.slice(0, 3).map((evt, i) => (
                              <div key={i} className="flex items-center gap-2">
                                <span className="text-xs px-1.5 py-0.5 bg-yellow-500/20 rounded">{evt.severity}</span>
                                <span>{evt.headline}</span>
                              </div>
                            ))}
                          </div>
                        )}
                        {testResult.suggestion && (
                          <div className="text-yellow-400 text-xs mt-2">Tip: {testResult.suggestion}</div>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Section 3: Preview Messages */}
                  <div className="space-y-2">
                    <div className="text-sm font-medium text-slate-400 uppercase tracking-wide">
                      {testResult.is_example ? 'Example Messages' : 'Messages That Would Fire'}
                    </div>
                    {testResult.preview_messages?.map((msg, i) => (
                      <div key={i} className="p-3 bg-slate-800 rounded text-sm font-mono break-words">
                        {msg}
                      </div>
                    ))}
                  </div>

                  {/* Delivery result */}
                  {testResult.delivered !== undefined && testResult.delivery_result && (
                    <div className={`p-3 rounded text-sm ${
                      testResult.delivered
                        ? 'bg-green-500/10 border border-green-500/30 text-green-400'
                        : 'bg-red-500/10 border border-red-500/30 text-red-400'
                    }`}>
                      <div className="flex items-start gap-2">
                        {testResult.delivered ? <Check size={16} className="mt-0.5" /> : <X size={16} className="mt-0.5" />}
                        <div>
                          <div>{testResult.delivery_result}</div>
                          {testResult.delivery_error && (
                            <div className="mt-1 text-red-300">{testResult.delivery_error}</div>
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Legacy format support */}
                  {testResult.message && !testResult.preview_messages && (
                    <div className={`p-3 rounded text-sm ${testResult.success ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'}`}>
                      {testResult.message}
                    </div>
                  )}
                </>
              ) : null}
            </div>
            <div className="p-4 border-t border-[#2a3a4a] flex justify-between sticky bottom-0 bg-[#1a2332]">
              <button
                onClick={closeTestDialog}
                className="px-4 py-2 text-slate-400 hover:text-slate-200"
              >
                Close
              </button>
              {testResult && !testResult.delivered && (
                <div className="flex gap-2">
                  {!testResult.delivery_method ? (
                    <span className="px-3 py-2 text-amber-400 text-sm">
                      Configure a delivery method to send test messages
                    </span>
                  ) : (
                    <>
                      {testResult.live_data_summary && testResult.live_data_summary.length > 0 && (
                        <button
                          onClick={() => sendTestAction('send_status')}
                          disabled={testDialog.loading}
                          className="px-3 py-2 bg-slate-700 hover:bg-slate-600 rounded text-sm disabled:opacity-50"
                          title="Send current conditions summary"
                        >
                          Send Current Conditions
                        </button>
                      )}
                      <button
                        onClick={() => sendTestAction('send_test')}
                        disabled={testDialog.loading}
                        className="px-3 py-2 bg-slate-700 hover:bg-slate-600 rounded text-sm disabled:opacity-50"
                        title="Send example alert message"
                      >
                        Send Example Alert
                      </button>
                      {testResult.can_send_live && (
                        <button
                          onClick={() => sendTestAction('send_live')}
                          disabled={testDialog.loading}
                          className="px-3 py-2 bg-accent hover:bg-accent/80 rounded text-sm disabled:opacity-50"
                          title="Send actual live alert"
                        >
                          Send Live Alert
                        </button>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">
            Alert delivery and scheduled reports. Rules define what triggers a notification and where it gets sent.
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
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">
          {error}
        </div>
      )}
      {success && (
        <div className="p-3 text-sm bg-green-500/10 text-green-400 border border-green-500/20">
          <Check size={14} className="inline mr-2" />
          {success}
        </div>
      )}

      {/* Main content */}
      <div className="bg-bg-card border border-border p-6 space-y-6">
        <Toggle
          label="Enable Notifications"
          checked={config.enabled}
          onChange={(v) => setConfig({ ...config, enabled: v })}
          helper="Master switch for all notification delivery"
          info="When disabled, no alerts or scheduled messages will be delivered. Alerts still get recorded to history."
        />

        {config.enabled && (
          <>            {/* Cold-start grace -- v0.5.8b */}
            <div className="space-y-3 p-4 bg-[#0a0e17] border border-[#1e2a3a]">
              <div className="flex items-center gap-2">
                <label className="text-xs text-slate-500 uppercase tracking-wide">Cold-start grace</label>
              </div>
              <NumberInput
                label="Grace period (seconds)"
                value={config.cold_start_grace_seconds ?? 60}
                onChange={(v) => setConfig({ ...config, cold_start_grace_seconds: v })}
                min={0}
                max={600}
                helper="Suppress broadcasts for this many seconds after the first event arrives"
                info="When meshai starts seeing events for the first time, suppress mesh broadcasts for this many seconds to absorb any JetStream backlog. Persistence rows still get written; only broadcasts are suppressed."
              />
            </div>

            {/* Band Conditions -- v0.5.11 */}
            <div className="space-y-3 p-4 bg-[#0a0e17] border border-[#1e2a3a]">
              <div className="flex items-center gap-2">
                <label className="text-xs text-slate-500 uppercase tracking-wide">Band Conditions (HF propagation)</label>
              </div>
              <Toggle
                label="Enable scheduled band-conditions broadcasts"
                checked={config.band_conditions_enabled ?? true}
                onChange={(v) => setConfig({ ...config, band_conditions_enabled: v })}
                helper="3x/day HF propagation summary (Day/Night ratings per band group). The daily fire digest (twice-daily LLM summary of active fires + the last 24h of growth/spotting) is configured separately under Adapter Config -> fires.digest_*. See Reference -> Fire Tracker (Fusion) and Reference -> Broadcast Types for the New/Update/Active prefix system."
                info="Source priority: (1) recent SWPC readings persisted locally; (2) HamQSL.com fallback; (3) silent skip if both fail. Persistence rows are written either way for an audit trail."
              />
              {(config.band_conditions_enabled ?? true) && (
                <div className="grid grid-cols-3 gap-3">
                  <TimeInput
                    label="Slot 1"
                    value={(config.band_conditions_schedule ?? ['06:00','14:00','22:00'])[0] || '06:00'}
                    onChange={(v) => {
                      const s = [...(config.band_conditions_schedule ?? ['06:00','14:00','22:00'])]
                      s[0] = v
                      setConfig({ ...config, band_conditions_schedule: s })
                    }}
                    helper="Morning (default 06:00 MT)"
                  />
                  <TimeInput
                    label="Slot 2"
                    value={(config.band_conditions_schedule ?? ['06:00','14:00','22:00'])[1] || '14:00'}
                    onChange={(v) => {
                      const s = [...(config.band_conditions_schedule ?? ['06:00','14:00','22:00'])]
                      s[1] = v
                      setConfig({ ...config, band_conditions_schedule: s })
                    }}
                    helper="Afternoon (default 14:00 MT)"
                  />
                  <TimeInput
                    label="Slot 3"
                    value={(config.band_conditions_schedule ?? ['06:00','14:00','22:00'])[2] || '22:00'}
                    onChange={(v) => {
                      const s = [...(config.band_conditions_schedule ?? ['06:00','14:00','22:00'])]
                      s[2] = v
                      setConfig({ ...config, band_conditions_schedule: s })
                    }}
                    helper="Night (default 22:00 MT)"
                  />
                </div>
              )}
              <p className="text-xs text-slate-600">All times are Mountain Time (America/Boise). DST handled automatically.</p>
            </div>

            {/* Master Toggles */}
            {config.toggles && (
              <MasterToggles
                toggles={config.toggles}
                onChange={(t) => setConfig({ ...config, toggles: t })}
              />
            )}

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
                  ruleIndex={i}
                  categories={categories}
                  regions={regions}                  onChange={(r) => {
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

              <div className="flex gap-2">
                <button
                  onClick={addRule}
                  className="flex-1 py-3 border border-dashed border-[#1e2a3a] text-slate-500 hover:text-slate-300 hover:border-accent flex items-center justify-center gap-2 transition-colors"
                >
                  <Plus size={16} /> Add Rule
                </button>
                <div className="relative">
                  <button
                    onClick={() => setShowTemplates(!showTemplates)}
                    className="py-3 px-4 border border-dashed border-[#1e2a3a] text-slate-500 hover:text-slate-300 hover:border-accent flex items-center gap-2 transition-colors"
                  >
                    <Layers size={16} /> Add from Template
                  </button>
                  {showTemplates && (
                    <>
                      <div className="fixed inset-0 z-40" onClick={() => setShowTemplates(false)} />
                      <div className="absolute right-0 top-full mt-2 z-50 w-80 bg-[#1a2332] border border-[#2a3a4a] shadow-xl overflow-hidden">
                        <div className="p-2 border-b border-[#2a3a4a] text-xs text-slate-500 uppercase">Rule Templates</div>
                        {RULE_TEMPLATES.map((t) => (
                          <button
                            key={t.id}
                            onClick={() => addFromTemplate(t.id)}
                            className="w-full p-3 text-left hover:bg-[#2a3a4a] transition-colors"
                          >
                            <div className="font-medium text-slate-200">{t.name}</div>
                            <div className="text-xs text-slate-500 mt-0.5">{t.description}</div>
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Danger Zones — self-contained panel (own GET/PUT for the isolated
          `danger_zones` config section; never entangled with the notifications
          save above). Additive only. */}
      <DangerZonesPanel />
    </div>
  )
}

// ============================================================================
// Danger Zones panel — fully isolated, additive feature.
// Loads/saves the standalone `danger_zones` config section via the generic
// /api/config helpers. Has its OWN state, fetch (on mount), and save. It is
// intentionally decoupled from the page's `notifications` config so it can
// never be tangled with the existing save logic.
// ============================================================================

const DZ_MONITOR_ROLES = ['CLIENT_BASE', 'ROUTER', 'ROUTER_LATE'] as const

const DZ_SEVERITY_OPTIONS = [
  { value: 'routine', label: 'Routine' },
  { value: 'priority', label: 'Priority' },
  { value: 'immediate', label: 'Immediate' },
]

const DZ_DELIVERY_OPTIONS = [
  { value: 'mesh_dm', label: 'Mesh DM (unicast to nodes)' },
  { value: 'mesh_broadcast', label: 'Mesh Broadcast (channel)' },
  { value: 'email', label: 'Email' },
  { value: 'webhook', label: 'Webhook' },
  { value: 'none', label: '(None / log only)' },
]

// Per-family rows. snow is a sub-gate of weather; flood a sub-gate of seismic.
const DZ_FAMILIES: {
  key: string
  label: string
  description: string
  Icon: typeof Activity
  showAcres?: boolean
}[] = [
  { key: 'fire', label: 'Fire', description: 'Active wildfires (radius from fire perimeter).', Icon: Flame, showAcres: true },
  { key: 'weather', label: 'Weather', description: 'Severe weather warnings near a node.', Icon: Cloud },
  { key: 'snow', label: 'Snow (sub-gate of Weather)', description: 'Snow-category weather events.', Icon: Snowflake },
  { key: 'flood', label: 'Flood (sub-gate of Seismic)', description: 'Stream/flood gauge events.', Icon: Activity },
  { key: 'avalanche', label: 'Avalanche', description: 'Avalanche advisories near a node.', Icon: Mountain },
  { key: 'seismic', label: 'Seismic', description: 'Earthquakes and seismic events near a node.', Icon: Mountain },
]

interface DangerZoneHazardConfig {
  enabled: boolean
  buffer_mi: number
  min_severity: string
  min_acres: number
}

interface DangerZonesConfig {
  enabled: boolean
  dry_run: boolean
  monitor_roles: string[]
  position_max_age_hours: number
  default_buffer_mi: number
  cooldown_minutes: number
  fire: DangerZoneHazardConfig
  weather: DangerZoneHazardConfig
  snow: DangerZoneHazardConfig
  flood: DangerZoneHazardConfig
  avalanche: DangerZoneHazardConfig
  seismic: DangerZoneHazardConfig
  delivery_type: string
  node_ids: string[]
  broadcast_channel: number | null
  webhook_url: string
  webhook_headers: Record<string, string>
}

function dzDefaultHazard(): DangerZoneHazardConfig {
  return { enabled: false, buffer_mi: 5.0, min_severity: 'priority', min_acres: 0 }
}

// New-object default. NOTE: delivery_type defaults to mesh_dm (do NOT copy the
// page's mesh_broadcast new-rule default).
function dzDefault(): DangerZonesConfig {
  return {
    enabled: false,
    dry_run: true,
    monitor_roles: ['ROUTER', 'ROUTER_LATE', 'CLIENT_BASE'],
    position_max_age_hours: 72,
    default_buffer_mi: 5.0,
    cooldown_minutes: 360,
    fire: dzDefaultHazard(),
    weather: dzDefaultHazard(),
    snow: dzDefaultHazard(),
    flood: dzDefaultHazard(),
    avalanche: dzDefaultHazard(),
    seismic: dzDefaultHazard(),
    delivery_type: 'mesh_dm',
    node_ids: [],
    broadcast_channel: null,
    webhook_url: '',
    webhook_headers: {},
  }
}

// Minimal local select — SelectInput lives in Config.tsx (which we must not
// touch/entangle), so this panel keeps its own tiny equivalent.
function DZSelect({ label, value, onChange, options, info = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
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
    </div>
  )
}

// Per-family row — mirrors the AlertRuleToggle (toggle + thresholds) pattern.
// AlertRuleToggle itself lives in Config.tsx and is NOT exported, so this is a
// local equivalent purpose-built for the per-family hazard config.
function DZFamilyRow({ meta, cfg, onChange }: {
  meta: { key: string; label: string; description: string; Icon: typeof Activity; showAcres?: boolean }
  cfg: DangerZoneHazardConfig
  onChange: (c: DangerZoneHazardConfig) => void
}) {
  const { Icon } = meta
  return (
    <div className="border border-[#1e2a3a] p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-start gap-2 flex-1">
          <Icon size={15} className="text-slate-400 mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <span className="text-sm text-slate-300">{meta.label}</span>
            <p className="text-xs text-slate-600">{meta.description}</p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => onChange({ ...cfg, enabled: !cfg.enabled })}
          className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 ml-3 ${
            cfg.enabled ? 'bg-accent' : 'bg-[#1e2a3a]'
          }`}
        >
          <span
            className={`absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform ${
              cfg.enabled ? 'translate-x-5' : ''
            }`}
          />
        </button>
      </div>
      {cfg.enabled && (
        <div className={`grid gap-3 pt-2 border-t border-[#1e2a3a] ${meta.showAcres ? 'grid-cols-3' : 'grid-cols-2'}`}>
          <NumberInput
            label="Buffer (mi)"
            value={cfg.buffer_mi ?? 0}
            onChange={(v) => onChange({ ...cfg, buffer_mi: v })}
            min={0}
            step={0.5}
          />
          <DZSelect
            label="Min Severity"
            value={cfg.min_severity || 'priority'}
            onChange={(v) => onChange({ ...cfg, min_severity: v })}
            options={DZ_SEVERITY_OPTIONS}
          />
          {meta.showAcres && (
            <NumberInput
              label="Min Acres"
              value={cfg.min_acres ?? 0}
              onChange={(v) => onChange({ ...cfg, min_acres: v })}
              min={0}
              step={1}
            />
          )}
        </div>
      )}
    </div>
  )
}

function DangerZonesPanel() {
  const [expanded, setExpanded] = useState(false)
  const [cfg, setCfg] = useState<DangerZonesConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const raw = (await apiFetchConfig('danger_zones')) as Partial<DangerZonesConfig>
      // Merge over defaults so missing/new fields are always present.
      const d = dzDefault()
      setCfg({
        ...d,
        ...raw,
        fire: { ...d.fire, ...(raw.fire || {}) },
        weather: { ...d.weather, ...(raw.weather || {}) },
        snow: { ...d.snow, ...(raw.snow || {}) },
        flood: { ...d.flood, ...(raw.flood || {}) },
        avalanche: { ...d.avalanche, ...(raw.avalanche || {}) },
        seismic: { ...d.seismic, ...(raw.seismic || {}) },
        monitor_roles: raw.monitor_roles ?? d.monitor_roles,
        node_ids: raw.node_ids ?? d.node_ids,
        webhook_headers: raw.webhook_headers ?? d.webhook_headers,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load danger zones config')
      setCfg(dzDefault())
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const save = async () => {
    if (!cfg) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      await apiUpdateConfig('danger_zones', cfg)
      setSuccess('Danger Zones config saved')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const upd = (patch: Partial<DangerZonesConfig>) => setCfg(c => (c ? { ...c, ...patch } : c))

  const toggleRole = (role: string) => {
    if (!cfg) return
    const cur = cfg.monitor_roles || []
    upd({ monitor_roles: cur.includes(role) ? cur.filter(r => r !== role) : [...cur, role] })
  }

  return (
    <div className="bg-bg-card border border-border">
      {/* Collapsible header */}
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center justify-between p-4 text-left"
      >
        <div className="flex items-center gap-3">
          <AlertTriangle size={18} className="text-amber-400" />
          <div>
            <div className="text-sm font-medium text-slate-200">Danger Zones</div>
            <div className="text-xs text-slate-500">
              Alert when monitored infrastructure nodes are in/near a hazard
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {cfg && (
            <span className={`text-xs px-2 py-0.5 rounded ${
              cfg.enabled
                ? (cfg.dry_run ? 'bg-yellow-500/10 text-yellow-400' : 'bg-green-500/10 text-green-400')
                : 'bg-slate-800 text-slate-500'
            }`}>
              {cfg.enabled ? (cfg.dry_run ? 'Dry-run' : 'Live') : 'Disabled'}
            </span>
          )}
          {expanded ? <ChevronDown size={18} className="text-slate-500" /> : <ChevronRight size={18} className="text-slate-500" />}
        </div>
      </button>

      {expanded && (
        <div className="p-6 pt-0 space-y-6">
          {/* Safety copy */}
          <div className="flex items-start gap-2 p-3 bg-amber-500/10 border border-amber-500/20">
            <AlertCircle size={16} className="text-amber-400 mt-0.5 flex-shrink-0" />
            <div className="text-xs text-amber-200/90 leading-relaxed">
              Ships disabled; when enabled, defaults to dry-run / log-only — no mesh traffic
              until you turn dry-run off. Requires <span className="font-medium">Enable Notifications</span> (above)
              and environmental feeds to be on, since hazard events only flow when those are active.
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

          {loading || !cfg ? (
            <div className="text-sm text-slate-500">Loading danger zones config...</div>
          ) : (
            <>
              <Toggle
                label="Enable Danger Zones"
                checked={cfg.enabled}
                onChange={(v) => upd({ enabled: v })}
                helper="Master switch for the infrastructure danger-zone correlator"
              />
              <Toggle
                label="Dry-run (log only)"
                checked={cfg.dry_run}
                onChange={(v) => upd({ dry_run: v })}
                helper="When on, matches are logged but nothing is sent to the mesh. Turn off only after verifying dry-run output."
              />

              {/* Monitored roles */}
              <div className="space-y-2">
                <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                  Monitored Roles
                  <InfoButton info="Which Meshtastic node roles to correlate against hazards. Only nodes with a fresh GPS position (see Position Max Age) are scanned." />
                </label>
                <div className="flex flex-wrap gap-2">
                  {DZ_MONITOR_ROLES.map(role => {
                    const on = (cfg.monitor_roles || []).includes(role)
                    return (
                      <button
                        key={role}
                        type="button"
                        onClick={() => toggleRole(role)}
                        className={`px-3 py-1.5 rounded text-sm transition-colors ${
                          on ? 'bg-accent text-white' : 'bg-[#1e2a3a] text-slate-400 hover:text-slate-200'
                        }`}
                      >
                        {role}
                      </button>
                    )
                  })}
                </div>
              </div>

              {/* Global numeric settings */}
              <div className="grid grid-cols-3 gap-4">
                <NumberInput
                  label="Position Max Age (hrs)"
                  value={cfg.position_max_age_hours}
                  onChange={(v) => upd({ position_max_age_hours: v })}
                  min={1}
                  helper="Skip nodes whose last position is older than this"
                />
                <NumberInput
                  label="Default Buffer (mi)"
                  value={cfg.default_buffer_mi}
                  onChange={(v) => upd({ default_buffer_mi: v })}
                  min={0}
                  step={0.5}
                  helper="Buffer used when a family has none set"
                />
                <NumberInput
                  label="Cooldown (min)"
                  value={cfg.cooldown_minutes}
                  onChange={(v) => upd({ cooldown_minutes: v })}
                  min={0}
                  helper="Min time between repeat alerts per node+family"
                />
              </div>

              {/* Per-family hazard config */}
              <div className="space-y-3">
                <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
                  Hazard Families
                  <InfoButton info="Enable each hazard family to monitor, with its own buffer distance and severity threshold. Snow is a sub-gate of Weather; Flood a sub-gate of Seismic." />
                </label>
                {DZ_FAMILIES.map(meta => (
                  <DZFamilyRow
                    key={meta.key}
                    meta={meta}
                    cfg={cfg[meta.key as keyof DangerZonesConfig] as DangerZoneHazardConfig}
                    onChange={(c) => upd({ [meta.key]: c } as Partial<DangerZonesConfig>)}
                  />
                ))}
              </div>

              {/* Delivery */}
              <div className="space-y-4 p-4 bg-[#0a0e17] border border-[#1e2a3a]">
                <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
                  <Send size={14} />
                  DELIVERY
                </div>
                <DZSelect
                  label="Delivery Method"
                  value={cfg.delivery_type || 'mesh_dm'}
                  onChange={(v) => upd({ delivery_type: v })}
                  options={DZ_DELIVERY_OPTIONS}
                  info="Where danger-zone alerts get delivered. Mesh DM unicasts to specific nodes; broadcast sends to a channel. Has no effect while dry-run is on."
                />

                {cfg.delivery_type === 'mesh_dm' && (
                  <NodePicker
                    label="Recipient Nodes"
                    value={cfg.node_ids || []}
                    onChange={(v) => upd({ node_ids: v })}
                    helper="Nodes that receive direct messages"
                    valueType="node_id_hex"
                  />
                )}

                {cfg.delivery_type === 'mesh_broadcast' && (
                  <ChannelPicker
                    label="Broadcast Channel"
                    value={cfg.broadcast_channel ?? 0}
                    onChange={(v) => upd({ broadcast_channel: v })}
                    helper="Select the mesh radio channel"
                    mode="single"
                  />
                )}

                {cfg.delivery_type === 'webhook' && (
                  <TextInput
                    label="Webhook URL"
                    value={cfg.webhook_url || ''}
                    onChange={(v) => upd({ webhook_url: v })}
                    placeholder="https://discord.com/api/webhooks/..."
                    helper="POST alert as JSON"
                  />
                )}

                {cfg.delivery_type === 'email' && (
                  <p className="text-xs text-slate-600">
                    Email delivery uses the SMTP settings configured for notification rules.
                  </p>
                )}
              </div>

              {/* Save */}
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={save}
                  disabled={saving}
                  className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent/80 disabled:bg-slate-700 disabled:cursor-not-allowed rounded text-white transition-colors"
                >
                  <Save size={16} />
                  {saving ? 'Saving...' : 'Save Danger Zones'}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
