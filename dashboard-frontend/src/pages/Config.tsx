import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Settings, Bot, Wifi, MessageSquare, Database, Brain, Eye,
  Terminal, Cpu, Cloud, Radio, BookOpen, Layers, Activity,
  Thermometer, LayoutDashboard, Save, RotateCcw, RefreshCw,
  Plus, Trash2, ChevronDown, ChevronRight, AlertTriangle,
  Check, X, Eye as EyeIcon, EyeOff, HelpCircle
} from 'lucide-react'

// Voltage lookup for Li-ion battery percentages
const VOLTAGE_MAP: Record<number, string> = {
  100: '4.20V',
  90: '4.10V',
  80: '4.00V',
  70: '3.90V',
  60: '3.80V',
  50: '3.70V',
  40: '3.65V',
  30: '3.60V',
  20: '3.55V',
  15: '3.50V',
  10: '3.45V',
  7: '3.40V',
  5: '3.38V',
  0: '3.30V',
}

function getVoltageApprox(percent: number): string {
  const keys = Object.keys(VOLTAGE_MAP).map(Number).sort((a, b) => b - a)
  for (const key of keys) {
    if (percent >= key) return VOLTAGE_MAP[key]
  }
  return '3.30V'
}

// Section descriptions
const SECTION_DESCRIPTIONS: Record<string, string> = {
  bot: 'Configure the bot identity and basic behavior settings for the Meshtastic AI assistant.',
  connection: 'Set up how the bot connects to your Meshtastic device — via serial port or TCP network connection.',
  response: 'Control message timing and length limits. Delays help avoid channel congestion; length limits fit LoRa constraints.',
  history: 'Manage conversation history storage. Messages are stored in SQLite for context and analytics.',
  memory: 'Memory optimization summarizes old conversations to reduce token usage while preserving context.',
  context: 'Passive context lets the bot observe channel traffic to understand ongoing conversations without being directly addressed.',
  commands: 'Configure slash commands that users can send to trigger specific bot actions.',
  llm: 'Configure the LLM backend (OpenAI, Anthropic, Google) and model settings for AI responses.',
  weather: 'Set up weather providers for the !wx command. Open-Meteo is free; wttr.in has rate limits.',
  meshmonitor: 'Connect to MeshMonitor for real-time mesh network telemetry and node information.',
  knowledge: 'RAG (Retrieval-Augmented Generation) knowledge base for answering questions from your documents.',
  mesh_sources: 'Connect to mesh visualization tools (MeshView, MeshMonitor) to aggregate node data.',
  mesh_intelligence: 'Mesh Intelligence monitors network health, detects outages, and generates alerts.',
  environmental: 'Environmental data feeds for weather alerts, space weather, fires, and avalanche conditions.',
  dashboard: 'Configure the web dashboard server settings.',
}

// Info button component with popover
function InfoButton({ info }: { info: string }) {
  const [show, setShow] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setShow(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        type="button"
        onClick={() => setShow(!show)}
        className="ml-1 text-slate-500 hover:text-slate-300 transition-colors"
        aria-label="More information"
      >
        <HelpCircle size={14} />
      </button>
      {show && (
        <div className="absolute z-50 left-0 mt-1 w-64 p-3 bg-[#1a1f2e] border border-[#2a3548] rounded-lg shadow-xl text-xs text-slate-300 leading-relaxed">
          {info}
        </div>
      )}
    </div>
  )
}

// Types for config sections
interface BotConfig {
  name: string
  owner: string
  respond_to_dms: boolean
  filter_bbs_protocols: boolean
}

interface ConnectionConfig {
  type: string
  serial_port: string
  tcp_host: string
  tcp_port: number
}

interface ResponseConfig {
  delay_min: number
  delay_max: number
  max_length: number
  max_messages: number
}

interface HistoryConfig {
  database: string
  max_messages_per_user: number
  conversation_timeout: number
  auto_cleanup: boolean
  cleanup_interval_hours: number
  max_age_days: number
}

interface MemoryConfig {
  enabled: boolean
  window_size: number
  summarize_threshold: number
}

interface ContextConfig {
  enabled: boolean
  observe_channels: number[]
  ignore_nodes: string[]
  max_age: number
  max_context_items: number
}

interface CommandsConfig {
  enabled: boolean
  prefix: string
  disabled_commands: string[]
  custom_commands: Record<string, string>
}

interface LLMConfig {
  backend: string
  api_key: string
  base_url: string
  model: string
  timeout: number
  max_response_tokens: number
  system_prompt: string
  use_system_prompt: boolean
  web_search: boolean
  google_grounding: boolean
}

interface WeatherConfig {
  primary: string
  fallback: string
  default_location: string
  openmeteo: { url: string }
  wttr: { url: string }
}

interface MeshMonitorConfig {
  enabled: boolean
  url: string
  inject_into_prompt: boolean
  refresh_interval: number
  polite_mode: boolean
}

interface KnowledgeConfig {
  enabled: boolean
  backend: string
  qdrant_host: string
  qdrant_port: number
  qdrant_collection: string
  tei_host: string
  tei_port: number
  sparse_host: string
  sparse_port: number
  use_sparse: boolean
  db_path: string
  top_k: number
}

interface MeshSourceConfig {
  name: string
  type: string
  url: string
  api_token: string
  refresh_interval: number
  polite_mode: boolean
  enabled: boolean
}

interface RegionAnchor {
  name: string
  lat: number
  lon: number
  local_name: string
  description: string
  aliases: string[]
  cities: string[]
}

interface AlertRulesConfig {
  infra_offline: boolean
  infra_recovery: boolean
  new_router: boolean
  battery_trend_declining: boolean
  battery_warning: boolean
  battery_critical: boolean
  battery_emergency: boolean
  battery_warning_threshold: number
  battery_critical_threshold: number
  battery_emergency_threshold: number
  power_source_change: boolean
  solar_not_charging: boolean
  sustained_high_util: boolean
  high_util_threshold: number
  high_util_hours: number
  packet_flood: boolean
  packet_flood_threshold: number
  infra_single_gateway: boolean
  feeder_offline: boolean
  region_total_blackout: boolean
  mesh_score_alert: boolean
  mesh_score_threshold: number
  region_score_alert: boolean
  region_score_threshold: number
}

interface MeshIntelligenceConfig {
  enabled: boolean
  regions: RegionAnchor[]
  locality_radius_miles: number
  offline_threshold_hours: number
  packet_threshold: number
  battery_warning_percent: number
  critical_nodes: string[]
  alert_channel: number
  alert_cooldown_minutes: number
  alert_rules: AlertRulesConfig
}

interface NWSConfig {
  enabled: boolean
  tick_seconds: number
  areas: string[]
  severity_min: string
  user_agent: string
}

interface EnvironmentalConfig {
  enabled: boolean
  nws_zones: string[]
  nws: NWSConfig
  swpc: { enabled: boolean }
  ducting: { enabled: boolean; tick_seconds: number; latitude: number; longitude: number }
  fires: { enabled: boolean; tick_seconds: number; state: string }
  avalanche: { enabled: boolean; tick_seconds: number; center_ids: string[]; season_months: number[] }
}

interface DashboardConfig {
  enabled: boolean
  port: number
  host: string
}

interface FullConfig {
  bot: BotConfig
  connection: ConnectionConfig
  response: ResponseConfig
  history: HistoryConfig
  memory: MemoryConfig
  context: ContextConfig
  commands: CommandsConfig
  llm: LLMConfig
  weather: WeatherConfig
  meshmonitor: MeshMonitorConfig
  knowledge: KnowledgeConfig
  mesh_sources: MeshSourceConfig[]
  mesh_intelligence: MeshIntelligenceConfig
  environmental: EnvironmentalConfig
  dashboard: DashboardConfig
}

type SectionKey = keyof FullConfig

const SECTIONS: { key: SectionKey; label: string; icon: typeof Settings }[] = [
  { key: 'bot', label: 'Bot', icon: Bot },
  { key: 'connection', label: 'Connection', icon: Wifi },
  { key: 'response', label: 'Response', icon: MessageSquare },
  { key: 'history', label: 'History', icon: Database },
  { key: 'memory', label: 'Memory', icon: Brain },
  { key: 'context', label: 'Context', icon: Eye },
  { key: 'commands', label: 'Commands', icon: Terminal },
  { key: 'llm', label: 'LLM', icon: Cpu },
  { key: 'weather', label: 'Weather', icon: Cloud },
  { key: 'meshmonitor', label: 'MeshMonitor', icon: Radio },
  { key: 'knowledge', label: 'Knowledge', icon: BookOpen },
  { key: 'mesh_sources', label: 'Mesh Sources', icon: Layers },
  { key: 'mesh_intelligence', label: 'Intelligence', icon: Activity },
  { key: 'environmental', label: 'Environmental', icon: Thermometer },
  { key: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
]

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

function NumberInput({ label, value, onChange, min, max, step = 1, helper = '', info = '', suffix = '' }: {
  label: string
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
  helper?: string
  info?: string
  suffix?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}{suffix && <span className="ml-1 text-slate-400 normal-case">({suffix})</span>}
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
  options: { value: string; label: string; description?: string }[]
  helper?: string
  info?: string
}) {
  const selectedOption = options.find(o => o.value === value)
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
      {selectedOption?.description && (
        <p className="text-xs text-slate-500 italic">{selectedOption.description}</p>
      )}
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

function TextArea({ label, value, onChange, rows = 4, helper = '', info = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  rows?: number
  helper?: string
  info?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent resize-y"
      />
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
  const [text, setText] = useState(value.join(', '))

  useEffect(() => {
    setText(value.join(', '))
  }, [value])

  const handleBlur = () => {
    const items = text.split(',').map(s => s.trim()).filter(Boolean)
    onChange(items)
  }

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={handleBlur}
        placeholder="item1, item2, item3"
        className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
      />
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

function NumberListInput({ label, value, onChange, helper = '', info = '' }: {
  label: string
  value: number[]
  onChange: (v: number[]) => void
  helper?: string
  info?: string
}) {
  const [text, setText] = useState(value.join(', '))

  useEffect(() => {
    setText(value.join(', '))
  }, [value])

  const handleBlur = () => {
    const items = text.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n))
    onChange(items)
  }

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} />}
      </label>
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={handleBlur}
        placeholder="0, 1, 2"
        className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
      />
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}

// Section header with description
function SectionHeader({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-6 pb-4 border-b border-[#1e2a3a]">
      <p className="text-sm text-slate-400">{description}</p>
    </div>
  )
}

// Section renderers
function BotSection({ data, onChange }: { data: BotConfig; onChange: (d: BotConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Bot" description={SECTION_DESCRIPTIONS.bot} />
      <div className="grid grid-cols-2 gap-4">
        <TextInput
          label="Bot Name"
          value={data.name}
          onChange={(v) => onChange({ ...data, name: v })}
          helper="Displayed name in mesh messages"
          info="The name shown when the bot sends messages. Keep it short for LoRa efficiency. This appears in the 'From' field of Meshtastic messages."
        />
        <TextInput
          label="Owner"
          value={data.owner}
          onChange={(v) => onChange({ ...data, owner: v })}
          helper="Short name of the bot owner"
          info="Used for accountability and contact. The bot can mention this when asked who operates it."
        />
      </div>
      <Toggle
        label="Respond to DMs"
        checked={data.respond_to_dms}
        onChange={(v) => onChange({ ...data, respond_to_dms: v })}
        helper="Reply to direct messages to this node"
        info="When enabled, the bot will respond to private/direct messages sent to its node ID, not just channel broadcasts."
      />
      <Toggle
        label="Filter BBS Protocols"
        checked={data.filter_bbs_protocols}
        onChange={(v) => onChange({ ...data, filter_bbs_protocols: v })}
        helper="Ignore BBS mailbox traffic (recommended)"
        info="Filters out protocol messages from Meshtastic BBS systems like MailTastic. Prevents the bot from responding to automated bulletin board traffic."
      />
    </div>
  )
}

function ConnectionSection({ data, onChange }: { data: ConnectionConfig; onChange: (d: ConnectionConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Connection" description={SECTION_DESCRIPTIONS.connection} />
      <SelectInput
        label="Connection Type"
        value={data.type}
        onChange={(v) => onChange({ ...data, type: v })}
        options={[
          { value: 'serial', label: 'Serial', description: 'USB cable directly to the Meshtastic device' },
          { value: 'tcp', label: 'TCP', description: 'Network connection to device\'s WiFi API' },
        ]}
        info="Serial is more reliable; TCP allows remote connection to a WiFi-enabled device."
      />
      {data.type === 'serial' ? (
        <TextInput
          label="Serial Port"
          value={data.serial_port}
          onChange={(v) => onChange({ ...data, serial_port: v })}
          placeholder="/dev/ttyUSB0"
          helper="Linux: /dev/ttyUSB0 or /dev/ttyACM0 • Windows: COM3"
          info="The USB serial port where your Meshtastic device is connected. On Linux, use 'ls /dev/tty*' to find it. On Windows, check Device Manager for COM port number."
        />
      ) : (
        <div className="grid grid-cols-2 gap-4">
          <TextInput
            label="TCP Host"
            value={data.tcp_host}
            onChange={(v) => onChange({ ...data, tcp_host: v })}
            placeholder="192.168.1.100"
            helper="IP address of the Meshtastic device"
            info="The IP address of your WiFi-enabled Meshtastic device. Find this in your router's DHCP table or in the device's WiFi settings."
          />
          <NumberInput
            label="TCP Port"
            value={data.tcp_port}
            onChange={(v) => onChange({ ...data, tcp_port: v })}
            min={1}
            max={65535}
            helper="Default: 4403"
            info="TCP port for the Meshtastic API. The default is 4403. Only change if you've modified the device configuration."
          />
        </div>
      )}
    </div>
  )
}

function ResponseSection({ data, onChange }: { data: ResponseConfig; onChange: (d: ResponseConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Response" description={SECTION_DESCRIPTIONS.response} />
      <div className="grid grid-cols-2 gap-4">
        <NumberInput
          label="Delay Min"
          value={data.delay_min}
          onChange={(v) => onChange({ ...data, delay_min: v })}
          min={0}
          step={0.1}
          suffix="sec"
          helper="Minimum wait before responding"
          info="Random delay between min and max before the bot responds. Prevents rapid-fire responses that can congest the channel. LoRa has ~1-2 second transmission times."
        />
        <NumberInput
          label="Delay Max"
          value={data.delay_max}
          onChange={(v) => onChange({ ...data, delay_max: v })}
          min={0}
          step={0.1}
          suffix="sec"
          helper="Maximum wait before responding"
          info="Upper bound for response delay. Higher values give humans time to respond first and reduce channel congestion."
        />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <NumberInput
          label="Max Length"
          value={data.max_length}
          onChange={(v) => onChange({ ...data, max_length: v })}
          min={50}
          max={500}
          suffix="chars"
          helper="Maximum characters per message (LoRa limit: ~230)"
          info="Maximum characters per message. LoRa packets have limited payload (~230 chars). Longer messages are split. Keep under 200 for best reliability."
        />
        <NumberInput
          label="Max Messages"
          value={data.max_messages}
          onChange={(v) => onChange({ ...data, max_messages: v })}
          min={1}
          max={10}
          helper="Maximum message chunks per response"
          info="If a response exceeds max_length, it's split into multiple messages. This limits how many messages can be sent. Keep low (2-3) to avoid flooding."
        />
      </div>
    </div>
  )
}

function HistorySection({ data, onChange }: { data: HistoryConfig; onChange: (d: HistoryConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="History" description={SECTION_DESCRIPTIONS.history} />
      <TextInput
        label="Database Path"
        value={data.database}
        onChange={(v) => onChange({ ...data, database: v })}
        helper="SQLite database file for conversation history"
        info="Path to the SQLite database file. Use an absolute path for Docker deployments. The file is created automatically if it doesn't exist."
      />
      <div className="grid grid-cols-2 gap-4">
        <NumberInput
          label="Max Messages Per User"
          value={data.max_messages_per_user}
          onChange={(v) => onChange({ ...data, max_messages_per_user: v })}
          min={0}
          helper="0 = unlimited"
          info="Limits stored messages per user to manage database size. Set to 0 for unlimited. Recommended: 100-500 for active meshes."
        />
        <NumberInput
          label="Conversation Timeout"
          value={data.conversation_timeout}
          onChange={(v) => onChange({ ...data, conversation_timeout: v })}
          min={0}
          suffix="sec"
          helper="Time before conversation resets (0 = never)"
          info="After this many seconds of inactivity, the conversation context resets. Set to 0 to keep conversations indefinitely. Recommended: 3600 (1 hour)."
        />
      </div>
      <Toggle
        label="Auto Cleanup"
        checked={data.auto_cleanup}
        onChange={(v) => onChange({ ...data, auto_cleanup: v })}
        helper="Automatically delete old messages"
        info="Periodically removes old conversation history to manage database size. Recommended for production to prevent unbounded growth."
      />
      {data.auto_cleanup && (
        <div className="grid grid-cols-2 gap-4">
          <NumberInput
            label="Cleanup Interval"
            value={data.cleanup_interval_hours}
            onChange={(v) => onChange({ ...data, cleanup_interval_hours: v })}
            min={1}
            suffix="hours"
            helper="How often to run cleanup"
            info="Frequency of the cleanup job. Higher values reduce database overhead but allow more accumulation. Recommended: 24 hours."
          />
          <NumberInput
            label="Max Age"
            value={data.max_age_days}
            onChange={(v) => onChange({ ...data, max_age_days: v })}
            min={1}
            suffix="days"
            helper="Delete messages older than this"
            info="Messages older than this are deleted during cleanup. Recommended: 30-90 days depending on storage constraints."
          />
        </div>
      )}
    </div>
  )
}

function MemorySection({ data, onChange }: { data: MemoryConfig; onChange: (d: MemoryConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Memory" description={SECTION_DESCRIPTIONS.memory} />
      <Toggle
        label="Enable Memory Optimization"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Summarize old conversations to reduce token usage"
        info="When enabled, older conversation turns are summarized instead of included verbatim. This reduces LLM token costs while preserving context. Useful for long conversations."
      />
      {data.enabled && (
        <div className="grid grid-cols-2 gap-4">
          <NumberInput
            label="Window Size"
            value={data.window_size}
            onChange={(v) => onChange({ ...data, window_size: v })}
            min={1}
            helper="Recent message pairs to keep in full"
            info="The N most recent message pairs (user + bot) are kept verbatim. Older messages are summarized. Recommended: 3-5 for typical LoRa conversations."
          />
          <NumberInput
            label="Summarize Threshold"
            value={data.summarize_threshold}
            onChange={(v) => onChange({ ...data, summarize_threshold: v })}
            min={1}
            helper="Messages before re-summarizing"
            info="After this many new messages, the summary is regenerated to include recent context. Lower values keep summaries fresh but cost more tokens."
          />
        </div>
      )}
    </div>
  )
}

function ContextSection({ data, onChange }: { data: ContextConfig; onChange: (d: ContextConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Context" description={SECTION_DESCRIPTIONS.context} />
      <Toggle
        label="Enable Passive Context"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Observe channel messages for conversation awareness"
        info="When enabled, the bot passively listens to channel traffic to understand ongoing conversations. This helps it respond more contextually when addressed."
      />
      {data.enabled && (
        <>
          <NumberListInput
            label="Observe Channels"
            value={data.observe_channels}
            onChange={(v) => onChange({ ...data, observe_channels: v })}
            helper="Empty = all channels"
            info="Channel indices to observe (0 = primary, 1 = secondary, etc.). Leave empty to observe all channels. Separate multiple with commas: 0, 1, 2"
          />
          <ListInput
            label="Ignore Nodes"
            value={data.ignore_nodes}
            onChange={(v) => onChange({ ...data, ignore_nodes: v })}
            helper="Node IDs to ignore"
            info="Short names or IDs of nodes to ignore when building context. Useful for filtering out noisy nodes or other bots. Example: BOT1, RELAY2"
          />
          <div className="grid grid-cols-2 gap-4">
            <NumberInput
              label="Max Age"
              value={data.max_age}
              onChange={(v) => onChange({ ...data, max_age: v })}
              min={0}
              suffix="sec"
              helper="Ignore messages older than this"
              info="Context messages older than this are discarded. Keeps context relevant to current conversation. Recommended: 300-600 seconds (5-10 minutes)."
            />
            <NumberInput
              label="Max Context Items"
              value={data.max_context_items}
              onChange={(v) => onChange({ ...data, max_context_items: v })}
              min={1}
              helper="Max recent messages to include"
              info="Maximum number of observed messages to include in context. Higher values give more context but increase token usage. Recommended: 5-10."
            />
          </div>
        </>
      )}
    </div>
  )
}

function CommandsSection({ data, onChange }: { data: CommandsConfig; onChange: (d: CommandsConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Commands" description={SECTION_DESCRIPTIONS.commands} />
      <Toggle
        label="Enable Commands"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Allow users to trigger commands with a prefix"
        info="When enabled, messages starting with the command prefix trigger specific bot actions instead of AI responses. Example: !wx for weather."
      />
      {data.enabled && (
        <>
          <TextInput
            label="Command Prefix"
            value={data.prefix}
            onChange={(v) => onChange({ ...data, prefix: v })}
            helper="Character(s) that trigger commands"
            info="The prefix character(s) that indicate a command. Common choices: ! or / or . — Example: !wx, /help"
          />
          <ListInput
            label="Disabled Commands"
            value={data.disabled_commands}
            onChange={(v) => onChange({ ...data, disabled_commands: v })}
            helper="Commands to disable (e.g., help, ping)"
            info="List of command names to disable. Useful for removing commands you don't want users to access. Separate with commas: help, restart, debug"
          />
        </>
      )}
    </div>
  )
}

function LLMSection({ data, onChange }: { data: LLMConfig; onChange: (d: LLMConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="LLM" description={SECTION_DESCRIPTIONS.llm} />
      <div className="grid grid-cols-2 gap-4">
        <SelectInput
          label="Backend"
          value={data.backend}
          onChange={(v) => onChange({ ...data, backend: v })}
          options={[
            { value: 'openai', label: 'OpenAI', description: 'GPT-4, GPT-4o, GPT-4o-mini — best compatibility' },
            { value: 'anthropic', label: 'Anthropic', description: 'Claude 3.5 Sonnet, Claude 3 Haiku — excellent for long context' },
            { value: 'google', label: 'Google (Gemini)', description: 'Gemini 1.5 Pro/Flash — supports grounding' },
          ]}
          info="The LLM provider. Each has different pricing and capabilities. OpenAI has widest model selection, Anthropic excels at instructions, Gemini offers free tier."
        />
        <TextInput
          label="Model"
          value={data.model}
          onChange={(v) => onChange({ ...data, model: v })}
          placeholder="gpt-4o-mini"
          helper="Model name for the selected backend"
          info="Examples: gpt-4o-mini (fast/cheap), gpt-4o (capable), claude-3-haiku-20240307 (fast), claude-3-5-sonnet-20240620 (capable), gemini-1.5-flash (fast)"
        />
      </div>
      <TextInput
        label="API Key"
        value={data.api_key}
        onChange={(v) => onChange({ ...data, api_key: v })}
        type="password"
        helper="Supports ${ENV_VAR} syntax for environment variables"
        info="Your API key for the selected provider. Use ${OPENAI_API_KEY} syntax to read from environment variables instead of storing in config file."
      />
      <TextInput
        label="Base URL"
        value={data.base_url}
        onChange={(v) => onChange({ ...data, base_url: v })}
        placeholder="https://api.openai.com/v1"
        helper="API endpoint (leave empty for default)"
        info="Override the API endpoint. Useful for local LLMs (ollama, llama.cpp), Azure OpenAI, or proxy services like Open WebUI. Leave empty to use provider defaults."
      />
      <div className="grid grid-cols-2 gap-4">
        <NumberInput
          label="Timeout"
          value={data.timeout}
          onChange={(v) => onChange({ ...data, timeout: v })}
          min={5}
          max={120}
          suffix="sec"
          helper="Max wait time for LLM response"
          info="How long to wait for the LLM to respond before timing out. Larger models or complex prompts may need more time. Recommended: 30-60 seconds."
        />
        <NumberInput
          label="Max Response Tokens"
          value={data.max_response_tokens}
          onChange={(v) => onChange({ ...data, max_response_tokens: v })}
          min={100}
          helper="Token limit for responses"
          info="Maximum tokens in the LLM response. LoRa messages are ~230 chars so 150-300 tokens is usually sufficient. Higher values cost more and may exceed message limits."
        />
      </div>
      <Toggle
        label="Use System Prompt"
        checked={data.use_system_prompt}
        onChange={(v) => onChange({ ...data, use_system_prompt: v })}
        helper="Set a custom system prompt for the LLM"
        info="Enable to use a custom system prompt that sets the bot's personality, knowledge base, and behavior guidelines."
      />
      {data.use_system_prompt && (
        <TextArea
          label="System Prompt"
          value={data.system_prompt}
          onChange={(v) => onChange({ ...data, system_prompt: v })}
          rows={6}
          helper="Instructions that shape the bot's behavior"
          info="The system prompt defines the bot's identity and constraints. Include: who it is, what it knows, response style preferences, and any safety guidelines."
        />
      )}
      <Toggle
        label="Web Search"
        checked={data.web_search}
        onChange={(v) => onChange({ ...data, web_search: v })}
        helper="Enable web search via Open WebUI"
        info="If using Open WebUI as the base URL, this enables web search capabilities. The bot can search the internet to answer current events questions."
      />
      <Toggle
        label="Google Grounding"
        checked={data.google_grounding}
        onChange={(v) => onChange({ ...data, google_grounding: v })}
        helper="Gemini only — ground responses in Google Search"
        info="Gemini-specific feature that grounds responses in real Google Search results. Improves factual accuracy but increases latency and cost."
      />
    </div>
  )
}

function WeatherSection({ data, onChange }: { data: WeatherConfig; onChange: (d: WeatherConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Weather" description={SECTION_DESCRIPTIONS.weather} />
      <div className="grid grid-cols-2 gap-4">
        <SelectInput
          label="Primary Provider"
          value={data.primary}
          onChange={(v) => onChange({ ...data, primary: v })}
          options={[
            { value: 'openmeteo', label: 'Open-Meteo', description: 'Free, no API key, reliable. Recommended.' },
            { value: 'wttr', label: 'wttr.in', description: 'Free but rate limited. Simpler output.' },
            { value: 'llm', label: 'LLM', description: 'Let the LLM generate weather (needs grounding or web search)' },
          ]}
          info="The weather provider used for the !wx command. Open-Meteo is recommended as it's free with no rate limits."
        />
        <SelectInput
          label="Fallback Provider"
          value={data.fallback}
          onChange={(v) => onChange({ ...data, fallback: v })}
          options={[
            { value: 'openmeteo', label: 'Open-Meteo', description: 'Free, no API key, reliable' },
            { value: 'wttr', label: 'wttr.in', description: 'Free but rate limited' },
            { value: 'llm', label: 'LLM', description: 'Use LLM as fallback' },
            { value: 'none', label: 'None', description: 'No fallback — fail if primary fails' },
          ]}
          info="Used if the primary provider fails. Having a fallback improves reliability."
        />
      </div>
      <TextInput
        label="Default Location"
        value={data.default_location}
        onChange={(v) => onChange({ ...data, default_location: v })}
        placeholder="Twin Falls, ID"
        helper="Used when user doesn't specify a location"
        info="The default location for weather queries. Users can override by specifying a location: !wx Boise or !wx 43.6,-116.2"
      />
    </div>
  )
}

function MeshMonitorSection({ data, onChange }: { data: MeshMonitorConfig; onChange: (d: MeshMonitorConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="MeshMonitor" description={SECTION_DESCRIPTIONS.meshmonitor} />
      <Toggle
        label="Enable MeshMonitor"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Connect to MeshMonitor for mesh telemetry"
        info="MeshMonitor is a companion tool that provides real-time mesh statistics. When enabled, the bot can answer questions about network health."
      />
      {data.enabled && (
        <>
          <TextInput
            label="URL"
            value={data.url}
            onChange={(v) => onChange({ ...data, url: v })}
            placeholder="http://192.168.1.100:8080"
            helper="MeshMonitor web interface URL"
            info="The URL of your MeshMonitor instance. This is typically running on the same machine as your Meshtastic gateway."
          />
          <Toggle
            label="Inject Into Prompt"
            checked={data.inject_into_prompt}
            onChange={(v) => onChange({ ...data, inject_into_prompt: v })}
            helper="Tell LLM about MeshMonitor commands"
            info="When enabled, the system prompt includes information about !mesh commands. The LLM can then help users with mesh queries."
          />
          <NumberInput
            label="Refresh Interval"
            value={data.refresh_interval}
            onChange={(v) => onChange({ ...data, refresh_interval: v })}
            min={10}
            suffix="sec"
            helper="How often to fetch mesh data"
            info="Frequency of mesh data refresh. Lower values give fresher data but increase load. Recommended: 30-60 seconds."
          />
          <Toggle
            label="Polite Mode"
            checked={data.polite_mode}
            onChange={(v) => onChange({ ...data, polite_mode: v })}
            helper="Reduce polling for shared instances"
            info="Reduces polling frequency when multiple clients share a MeshMonitor instance. Recommended for public/shared setups to reduce server load."
          />
        </>
      )}
    </div>
  )
}

function KnowledgeSection({ data, onChange }: { data: KnowledgeConfig; onChange: (d: KnowledgeConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Knowledge" description={SECTION_DESCRIPTIONS.knowledge} />
      <Toggle
        label="Enable Knowledge Base"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Use RAG to answer questions from your documents"
        info="Retrieval-Augmented Generation (RAG) lets the bot answer questions from your documents. Requires embeddings and a vector database."
      />
      {data.enabled && (
        <>
          <SelectInput
            label="Backend"
            value={data.backend}
            onChange={(v) => onChange({ ...data, backend: v })}
            options={[
              { value: 'auto', label: 'Auto (Qdrant -> SQLite)', description: 'Try Qdrant first, fall back to SQLite' },
              { value: 'qdrant', label: 'Qdrant', description: 'High-performance vector DB, requires server' },
              { value: 'sqlite', label: 'SQLite', description: 'Simple file-based storage, no server needed' },
            ]}
            info="Qdrant provides better performance for large document collections. SQLite is simpler but slower for large datasets."
          />
          {(data.backend === 'qdrant' || data.backend === 'auto') && (
            <>
              <div className="grid grid-cols-2 gap-4">
                <TextInput
                  label="Qdrant Host"
                  value={data.qdrant_host}
                  onChange={(v) => onChange({ ...data, qdrant_host: v })}
                  helper="Qdrant server hostname"
                  info="Hostname or IP of your Qdrant server. Use 'localhost' for local installs or the Docker container name."
                />
                <NumberInput
                  label="Qdrant Port"
                  value={data.qdrant_port}
                  onChange={(v) => onChange({ ...data, qdrant_port: v })}
                  helper="Default: 6333"
                  info="Qdrant gRPC port. The default is 6333. REST API is typically on 6334."
                />
              </div>
              <TextInput
                label="Collection"
                value={data.qdrant_collection}
                onChange={(v) => onChange({ ...data, qdrant_collection: v })}
                helper="Qdrant collection name"
                info="The name of the Qdrant collection storing your document embeddings. Created automatically if it doesn't exist."
              />
              <Toggle
                label="Use Sparse Embeddings"
                checked={data.use_sparse}
                onChange={(v) => onChange({ ...data, use_sparse: v })}
                helper="Hybrid search with BM25-style sparse vectors"
                info="Combines dense embeddings with sparse (keyword-based) vectors for hybrid search. Improves retrieval quality but requires more resources."
              />
            </>
          )}
          <TextInput
            label="SQLite DB Path"
            value={data.db_path}
            onChange={(v) => onChange({ ...data, db_path: v })}
            helper="Path to SQLite vector database"
            info="File path for the SQLite vector database. Used as primary storage in SQLite mode or as fallback in Auto mode."
          />
          <NumberInput
            label="Top K Results"
            value={data.top_k}
            onChange={(v) => onChange({ ...data, top_k: v })}
            min={1}
            max={20}
            helper="Number of document chunks to retrieve"
            info="How many relevant document chunks to include in the prompt. More chunks = more context but higher token cost. Recommended: 3-5."
          />
        </>
      )}
    </div>
  )
}

function MeshSourceCard({ source, onChange, onDelete }: {
  source: MeshSourceConfig
  onChange: (s: MeshSourceConfig) => void
  onDelete: () => void
}) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="border border-[#1e2a3a] rounded-lg overflow-hidden">
      <div
        className="flex items-center justify-between p-3 bg-[#0a0e17] cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          <div className={`w-2 h-2 rounded-full ${source.enabled ? 'bg-green-500' : 'bg-slate-500'}`} />
          <span className="font-mono text-sm text-slate-200">{source.name || 'Unnamed Source'}</span>
          <span className="text-xs text-slate-500 bg-[#1e2a3a] px-2 py-0.5 rounded">{source.type}</span>
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onDelete(); }}
          className="p-1 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded"
        >
          <Trash2 size={14} />
        </button>
      </div>
      {expanded && (
        <div className="p-4 space-y-4 border-t border-[#1e2a3a]">
          <div className="grid grid-cols-2 gap-4">
            <TextInput
              label="Name"
              value={source.name}
              onChange={(v) => onChange({ ...source, name: v })}
              helper="Display name for this source"
              info="A friendly name to identify this mesh data source in the dashboard and logs."
            />
            <SelectInput
              label="Type"
              value={source.type}
              onChange={(v) => onChange({ ...source, type: v })}
              options={[
                { value: 'meshview', label: 'MeshView', description: 'meshview.idahosat.org or similar public mesh viewer' },
                { value: 'meshmonitor', label: 'MeshMonitor', description: 'Self-hosted MeshMonitor instance with API' },
              ]}
              info="MeshView provides public mesh data. MeshMonitor is self-hosted and may require an API token."
            />
          </div>
          <TextInput
            label="URL"
            value={source.url}
            onChange={(v) => onChange({ ...source, url: v })}
            helper="API endpoint URL"
            info="The URL of the mesh visualization API. For MeshView: https://meshview.idahosat.org/api. For MeshMonitor: http://localhost:8080/api"
          />
          {source.type === 'meshmonitor' && (
            <TextInput
              label="API Token"
              value={source.api_token}
              onChange={(v) => onChange({ ...source, api_token: v })}
              type="password"
              helper="Authentication token if required"
              info="Some MeshMonitor instances require an API token for access. Leave blank if your instance doesn't require authentication."
            />
          )}
          <NumberInput
            label="Refresh Interval"
            value={source.refresh_interval}
            onChange={(v) => onChange({ ...source, refresh_interval: v })}
            min={10}
            suffix="sec"
            helper="How often to fetch data"
            info="Frequency of data refresh. Public servers may rate limit aggressive polling. Recommended: 30-60 seconds."
          />
          <Toggle
            label="Enabled"
            checked={source.enabled}
            onChange={(v) => onChange({ ...source, enabled: v })}
            helper="Include this source in mesh data aggregation"
          />
          <Toggle
            label="Polite Mode"
            checked={source.polite_mode}
            onChange={(v) => onChange({ ...source, polite_mode: v })}
            helper="Reduce polling for shared/public servers"
            info="Reduces polling frequency to be kind to shared or public servers. Recommended for public MeshView instances."
          />
        </div>
      )}
    </div>
  )
}

function MeshSourcesSection({ data, onChange }: { data: MeshSourceConfig[]; onChange: (d: MeshSourceConfig[]) => void }) {
  const addSource = () => {
    onChange([...data, {
      name: 'New Source',
      type: 'meshview',
      url: '',
      api_token: '',
      refresh_interval: 30,
      polite_mode: false,
      enabled: true,
    }])
  }

  return (
    <div className="space-y-4">
      <SectionHeader title="Mesh Sources" description={SECTION_DESCRIPTIONS.mesh_sources} />
      {data.map((source, i) => (
        <MeshSourceCard
          key={i}
          source={source}
          onChange={(s) => {
            const newData = [...data]
            newData[i] = s
            onChange(newData)
          }}
          onDelete={() => {
            if (confirm(`Delete source "${source.name}"?`)) {
              onChange(data.filter((_, j) => j !== i))
            }
          }}
        />
      ))}
      <button
        onClick={addSource}
        className="w-full py-2 border border-dashed border-[#1e2a3a] rounded-lg text-slate-500 hover:text-slate-300 hover:border-accent flex items-center justify-center gap-2 transition-colors"
      >
        <Plus size={16} /> Add Source
      </button>
    </div>
  )
}

function MeshIntelligenceSection({ data, onChange }: { data: MeshIntelligenceConfig; onChange: (d: MeshIntelligenceConfig) => void }) {
  const [expandedRegion, setExpandedRegion] = useState<number | null>(null)

  return (
    <div className="space-y-6">
      <SectionHeader title="Mesh Intelligence" description={SECTION_DESCRIPTIONS.mesh_intelligence} />
      <Toggle
        label="Enable Mesh Intelligence"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Monitor mesh health and generate alerts"
        info="Mesh Intelligence analyzes network data to detect outages, low batteries, high utilization, and other issues. Generates alerts for operators."
      />

      {data.enabled && (
        <>
          <div className="grid grid-cols-2 gap-4">
            <NumberInput
              label="Locality Radius"
              value={data.locality_radius_miles}
              onChange={(v) => onChange({ ...data, locality_radius_miles: v })}
              min={1}
              step={0.5}
              suffix="miles"
              helper="Max distance to assign node to region"
              info="Nodes within this distance of a region anchor are assigned to that region. Used for regional health scoring and alerts."
            />
            <NumberInput
              label="Offline Threshold"
              value={data.offline_threshold_hours}
              onChange={(v) => onChange({ ...data, offline_threshold_hours: v })}
              min={1}
              suffix="hours"
              helper="Hours without packets = offline"
              info="A node is considered offline if no packets received for this many hours. Rule of thumb: 4x the beacon interval. Default 2 hours for fixed infrastructure."
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <NumberInput
              label="Packet Threshold"
              value={data.packet_threshold}
              onChange={(v) => onChange({ ...data, packet_threshold: v })}
              min={0}
              helper="Min packets/24h before flagging low activity"
              info="Nodes below this packet count per day may be experiencing issues. Infrastructure nodes should typically have 50+ packets/day. Set to 0 to disable."
            />
            <NumberInput
              label="Battery Warning %"
              value={data.battery_warning_percent}
              onChange={(v) => onChange({ ...data, battery_warning_percent: v })}
              min={1}
              max={100}
              suffix={`~${getVoltageApprox(data.battery_warning_percent)}`}
              helper="Alert when battery falls below this"
              info="Li-ion voltage curve: 100%=4.20V, 60%=3.80V, 30%=3.60V, 15%=3.50V, 5%=3.38V. Cells may report inaccurate percentages; voltage is more reliable."
            />
          </div>

          <ListInput
            label="Critical Nodes"
            value={data.critical_nodes}
            onChange={(v) => onChange({ ...data, critical_nodes: v })}
            helper="Short names of critical infrastructure (e.g., MHR, HPR)"
            info="Nodes marked critical get priority alerting. When these go offline, alerts are sent immediately rather than waiting for the full threshold. List short names."
          />

          <div className="grid grid-cols-2 gap-4">
            <NumberInput
              label="Alert Channel"
              value={data.alert_channel}
              onChange={(v) => onChange({ ...data, alert_channel: v })}
              min={-1}
              helper="-1 = disabled, 0 = primary, 1 = secondary"
              info="Channel index to send mesh alerts on. Set to -1 to disable mesh alerts (dashboard only). 0 = primary channel, 1 = secondary, etc."
            />
            <NumberInput
              label="Alert Cooldown"
              value={data.alert_cooldown_minutes}
              onChange={(v) => onChange({ ...data, alert_cooldown_minutes: v })}
              min={1}
              suffix="min"
              helper="Min time between duplicate alerts"
              info="Prevents alert storms. The same alert won't be sent again until this cooldown expires. Recommended: 30-60 minutes."
            />
          </div>

          <div className="space-y-2">
            <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
              Regions
              <InfoButton info="Define geographic regions to group nodes. Each region has an anchor point (lat/lon) and nodes within locality_radius are assigned to it. Regional health scores aggregate node status." />
            </label>
            {data.regions.map((region, i) => (
              <div key={i} className="border border-[#1e2a3a] rounded-lg overflow-hidden">
                <div
                  className="flex items-center justify-between p-3 bg-[#0a0e17] cursor-pointer"
                  onClick={() => setExpandedRegion(expandedRegion === i ? null : i)}
                >
                  <div className="flex items-center gap-3">
                    {expandedRegion === i ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                    <span className="font-medium text-slate-200">{region.name}</span>
                    <span className="text-xs text-slate-500">{region.local_name}</span>
                  </div>
                </div>
                {expandedRegion === i && (
                  <div className="p-4 space-y-3 border-t border-[#1e2a3a]">
                    <div className="grid grid-cols-2 gap-4">
                      <TextInput
                        label="Name"
                        value={region.name}
                        onChange={(v) => {
                          const newRegions = [...data.regions]
                          newRegions[i] = { ...region, name: v }
                          onChange({ ...data, regions: newRegions })
                        }}
                        helper="Region identifier"
                      />
                      <TextInput
                        label="Local Name"
                        value={region.local_name}
                        onChange={(v) => {
                          const newRegions = [...data.regions]
                          newRegions[i] = { ...region, local_name: v }
                          onChange({ ...data, regions: newRegions })
                        }}
                        helper="Friendly display name"
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <NumberInput
                        label="Latitude"
                        value={region.lat}
                        onChange={(v) => {
                          const newRegions = [...data.regions]
                          newRegions[i] = { ...region, lat: v }
                          onChange({ ...data, regions: newRegions })
                        }}
                        step={0.0001}
                        helper="Region center latitude"
                      />
                      <NumberInput
                        label="Longitude"
                        value={region.lon}
                        onChange={(v) => {
                          const newRegions = [...data.regions]
                          newRegions[i] = { ...region, lon: v }
                          onChange({ ...data, regions: newRegions })
                        }}
                        step={0.0001}
                        helper="Region center longitude"
                      />
                    </div>
                    <TextInput
                      label="Description"
                      value={region.description}
                      onChange={(v) => {
                        const newRegions = [...data.regions]
                        newRegions[i] = { ...region, description: v }
                        onChange({ ...data, regions: newRegions })
                      }}
                      helper="Human-readable description"
                    />
                    <ListInput
                      label="Aliases"
                      value={region.aliases}
                      onChange={(v) => {
                        const newRegions = [...data.regions]
                        newRegions[i] = { ...region, aliases: v }
                        onChange({ ...data, regions: newRegions })
                      }}
                      helper="Alternative names for search"
                    />
                    <ListInput
                      label="Cities"
                      value={region.cities}
                      onChange={(v) => {
                        const newRegions = [...data.regions]
                        newRegions[i] = { ...region, cities: v }
                        onChange({ ...data, regions: newRegions })
                      }}
                      helper="Cities/towns in this region"
                    />
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Alert Rules with detailed descriptions */}
          <div className="space-y-4">
            <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
              Alert Rules
              <InfoButton info="Toggle which conditions generate alerts. Each rule monitors specific aspects of mesh health. Thresholds can be adjusted in the fields above." />
            </label>

            {/* Infrastructure Alerts */}
            <div className="bg-[#0d1117] border border-[#1e2a3a] rounded-lg p-4 space-y-2">
              <div className="text-xs text-slate-400 uppercase tracking-wide mb-2">Infrastructure</div>
              <Toggle
                label="Infra Offline"
                checked={data.alert_rules.infra_offline}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_offline: v } })}
                helper="Alert when routers/repeaters stop responding"
                info="Triggers when an infrastructure node (router, repeater) hasn't been heard for the offline threshold period. Example: 'MHR — Mountain Harrison Rptr has not been heard for 2 hours'"
              />
              <Toggle
                label="Infra Recovery"
                checked={data.alert_rules.infra_recovery}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_recovery: v } })}
                helper="Alert when offline infra comes back online"
                info="Sends a recovery notification when a previously offline infrastructure node comes back online. Example: 'MHR — Mountain Harrison Rptr back online after 2h outage'"
              />
              <Toggle
                label="New Router"
                checked={data.alert_rules.new_router}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, new_router: v } })}
                helper="Alert when a new router appears on the mesh"
                info="Detects when a new ROUTER or ROUTER_CLIENT role node appears. Useful for tracking mesh expansion. Example: 'Snake River Relay appeared in Wood River Valley'"
              />
            </div>

            {/* Power Alerts */}
            <div className="bg-[#0d1117] border border-[#1e2a3a] rounded-lg p-4 space-y-2">
              <div className="text-xs text-slate-400 uppercase tracking-wide mb-2">Power & Battery</div>
              <Toggle
                label={`Battery Warning (${data.alert_rules.battery_warning_threshold}% ≈ ${getVoltageApprox(data.alert_rules.battery_warning_threshold)})`}
                checked={data.alert_rules.battery_warning}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_warning: v } })}
                helper="Alert when battery below warning threshold"
                info="Triggers at 30% (~3.60V). Solar panels should recharge before critical level. Example: 'BLD-MTN at 28% (3.58V), solar not charging'"
              />
              <Toggle
                label={`Battery Critical (${data.alert_rules.battery_critical_threshold}% ≈ ${getVoltageApprox(data.alert_rules.battery_critical_threshold)})`}
                checked={data.alert_rules.battery_critical}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_critical: v } })}
                helper="Alert when battery critically low"
                info="Triggers at 15% (~3.50V). Node may shut down within hours if not charged. Example: 'BLD-MTN at 12% (3.48V) — shutdown in hours'"
              />
              <Toggle
                label={`Battery Emergency (${data.alert_rules.battery_emergency_threshold}% ≈ ${getVoltageApprox(data.alert_rules.battery_emergency_threshold)})`}
                checked={data.alert_rules.battery_emergency}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_emergency: v } })}
                helper="Alert when battery near shutdown"
                info="Triggers at 5% (~3.38V). Node will shut down imminently to protect the battery. Example: 'BLD-MTN at 4% (3.38V) — shutdown imminent'"
              />
              <Toggle
                label="Power Source Change"
                checked={data.alert_rules.power_source_change}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, power_source_change: v } })}
                helper="Alert when node switches from USB to battery"
                info="Detects when a node loses external power and switches to battery. May indicate site power outage. Example: 'MHR switched from USB to battery — possible outage'"
              />
              <Toggle
                label="Solar Not Charging"
                checked={data.alert_rules.solar_not_charging}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, solar_not_charging: v } })}
                helper="Alert when solar panel not charging during daylight"
                info="Triggers if a solar-powered node isn't charging during daylight hours. May indicate panel obstruction or failure. Example: 'BLD-MTN not charging during daylight (12:00 MDT)'"
              />
            </div>

            {/* Utilization Alerts */}
            <div className="bg-[#0d1117] border border-[#1e2a3a] rounded-lg p-4 space-y-2">
              <div className="text-xs text-slate-400 uppercase tracking-wide mb-2">Channel Utilization</div>
              <Toggle
                label={`High Utilization (>${data.alert_rules.high_util_threshold}%)`}
                checked={data.alert_rules.sustained_high_util}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, sustained_high_util: v } })}
                helper="Alert when channel airtime exceeds threshold"
                info="LoRa channel utilization. Firmware throttles GPS at 25%, severe issues at 50%, meltdown at 65%. Default threshold: 40%. Example: '47% utilization (threshold: 40%). Reliability may degrade.'"
              />
              <Toggle
                label={`Packet Flood (>${data.alert_rules.packet_flood_threshold}/min)`}
                checked={data.alert_rules.packet_flood}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, packet_flood: v } })}
                helper="Alert when a node sends excessive packets"
                info="Detects RADIO packets/min from ONE node. Normal: 1-5/min. Above 10 = suspicious. This is RADIO packets, not water flooding. May indicate firmware bug. Example: 'Node BKBS transmitting 42 packets/min (threshold: 10/min)'"
              />
            </div>

            {/* Coverage Alerts */}
            <div className="bg-[#0d1117] border border-[#1e2a3a] rounded-lg p-4 space-y-2">
              <div className="text-xs text-slate-400 uppercase tracking-wide mb-2">Coverage</div>
              <Toggle
                label="Single Gateway"
                checked={data.alert_rules.infra_single_gateway}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_single_gateway: v } })}
                helper="Alert when node has only one uplink path"
                info="Infrastructure nodes should have multiple gateway paths for redundancy. Triggers when a node drops to single gateway. Example: 'HPR dropped to single gateway. Previously had 3 paths.'"
              />
              <Toggle
                label="Feeder Offline"
                checked={data.alert_rules.feeder_offline}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, feeder_offline: v } })}
                helper="Alert when a gateway feeder goes offline"
                info="Feeder gateways provide uplink for multiple nodes. When one fails, multiple nodes may lose connectivity. Example: 'AIDA-N2 gateway not responding. 5 nodes may lose uplink.'"
              />
              <Toggle
                label="Region Blackout"
                checked={data.alert_rules.region_total_blackout}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, region_total_blackout: v } })}
                helper="Alert when all infra in a region is offline"
                info="Critical alert when an entire region loses all infrastructure. Example: 'REGION BLACKOUT: All infrastructure in Magic Valley offline!'"
              />
            </div>

            {/* Health Score Alerts */}
            <div className="bg-[#0d1117] border border-[#1e2a3a] rounded-lg p-4 space-y-2">
              <div className="text-xs text-slate-400 uppercase tracking-wide mb-2">Health Scores</div>
              <Toggle
                label={`Mesh Score Low (<${data.alert_rules.mesh_score_threshold})`}
                checked={data.alert_rules.mesh_score_alert}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, mesh_score_alert: v } })}
                helper="Alert when overall mesh health degrades"
                info="Composite health score (0-100) based on node availability, battery levels, and connectivity. Default threshold: 65. Example: 'Score 62/100 (threshold: 65). Infrastructure: 71, Connectivity: 58.'"
              />
              <Toggle
                label={`Region Score Low (<${data.alert_rules.region_score_threshold})`}
                checked={data.alert_rules.region_score_alert}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, region_score_alert: v } })}
                helper="Alert when a region's health degrades"
                info="Per-region health score. Useful for detecting localized issues. Default threshold: 60. Example: 'Magic Valley at 55/100 (threshold: 60). 2 nodes offline.'"
              />
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function EnvironmentalSection({ data, onChange }: { data: EnvironmentalConfig; onChange: (d: EnvironmentalConfig) => void }) {
  return (
    <div className="space-y-6">
      <SectionHeader title="Environmental" description={SECTION_DESCRIPTIONS.environmental} />
      <Toggle
        label="Enable Environmental Feeds"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Activate all environmental data sources"
        info="Master toggle for environmental data. When enabled, the system fetches weather alerts, space weather, fire data, and other feeds based on individual settings below."
      />

      {data.enabled && (
        <>
          <ListInput
            label="NWS Zones"
            value={data.nws_zones}
            onChange={(v) => onChange({ ...data, nws_zones: v })}
            helper="Zone IDs like IDZ016, IDZ030"
            info="NWS forecast zones for your mesh area. Find zones at weather.gov or search 'NWS zone finder'. Example: IDZ016 (Magic Valley), IDZ030 (Sawtooth)."
          />

          {/* NWS Weather Alerts */}
          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-300">NWS Weather Alerts</span>
                <InfoButton info="National Weather Service alerts including watches, warnings, and advisories. Covers severe weather, winter storms, fire weather, and more." />
              </div>
              <Toggle label="" checked={data.nws.enabled} onChange={(v) => onChange({ ...data, nws: { ...data.nws, enabled: v } })} />
            </div>
            {data.nws.enabled && (
              <>
                <TextInput
                  label="User Agent"
                  value={data.nws.user_agent}
                  onChange={(v) => onChange({ ...data, nws: { ...data.nws, user_agent: v } })}
                  helper="Required by NWS: (app_name, contact_email)"
                  info="NWS API requires a User-Agent header identifying your application. Format: (MeshAI, your@email.com). They may contact you if there are issues."
                />
                <div className="grid grid-cols-2 gap-4">
                  <NumberInput
                    label="Tick Seconds"
                    value={data.nws.tick_seconds}
                    onChange={(v) => onChange({ ...data, nws: { ...data.nws, tick_seconds: v } })}
                    min={30}
                    helper="Poll interval (min 30 sec)"
                    info="How often to check for new alerts. NWS recommends no more than once per minute. Default: 60 seconds."
                  />
                  <SelectInput
                    label="Min Severity"
                    value={data.nws.severity_min}
                    onChange={(v) => onChange({ ...data, nws: { ...data.nws, severity_min: v } })}
                    options={[
                      { value: 'minor', label: 'Minor', description: 'All alerts including minor advisories' },
                      { value: 'moderate', label: 'Moderate', description: 'Watches, Warnings, Advisories (recommended)' },
                      { value: 'severe', label: 'Severe', description: 'Only Severe Warnings' },
                      { value: 'extreme', label: 'Extreme', description: 'Only Extreme/Life-threatening events' },
                    ]}
                    info="Filter alerts by NWS severity. 'Moderate' captures most actionable alerts without minor weather statements."
                  />
                </div>
              </>
            )}
          </div>

          {/* SWPC Space Weather */}
          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-300">NOAA Space Weather (SWPC)</span>
                <InfoButton info="Space Weather Prediction Center data: Solar Flux Index (SFI), Kp geomagnetic index, R/S/G scales for radio blackouts, solar radiation, and geomagnetic storms. Affects HF propagation and can cause radio blackouts." />
              </div>
              <Toggle label="" checked={data.swpc.enabled} onChange={(v) => onChange({ ...data, swpc: { ...data.swpc, enabled: v } })} />
            </div>
          </div>

          {/* Tropospheric Ducting */}
          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-300">Tropospheric Ducting</span>
                <InfoButton info="Atmospheric ducting analysis using NOAA RAP soundings. Detects conditions where VHF/UHF signals can travel much further than normal due to temperature inversions. Measured in M-units/km refractivity gradient." />
              </div>
              <Toggle label="" checked={data.ducting.enabled} onChange={(v) => onChange({ ...data, ducting: { ...data.ducting, enabled: v } })} />
            </div>
            {data.ducting.enabled && (
              <div className="grid grid-cols-3 gap-4">
                <NumberInput
                  label="Tick Seconds"
                  value={data.ducting.tick_seconds}
                  onChange={(v) => onChange({ ...data, ducting: { ...data.ducting, tick_seconds: v } })}
                  min={60}
                  helper="Default: 3 hours"
                  info="Atmospheric soundings are only available every few hours. Polling more frequently than hourly is usually unnecessary."
                />
                <NumberInput
                  label="Latitude"
                  value={data.ducting.latitude}
                  onChange={(v) => onChange({ ...data, ducting: { ...data.ducting, latitude: v } })}
                  step={0.01}
                  helper="Center of your mesh"
                  info="Latitude for atmospheric profile lookup. Use the center of your mesh coverage area."
                />
                <NumberInput
                  label="Longitude"
                  value={data.ducting.longitude}
                  onChange={(v) => onChange({ ...data, ducting: { ...data.ducting, longitude: v } })}
                  step={0.01}
                  helper="Center of your mesh"
                  info="Longitude for atmospheric profile lookup. Use the center of your mesh coverage area."
                />
              </div>
            )}
          </div>

          {/* NIFC Fire Perimeters */}
          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-300">NIFC Fire Perimeters</span>
                <InfoButton info="National Interagency Fire Center wildfire perimeter data. Tracks active fires with acreage, containment percentage, and proximity to your mesh infrastructure." />
              </div>
              <Toggle label="" checked={data.fires.enabled} onChange={(v) => onChange({ ...data, fires: { ...data.fires, enabled: v } })} />
            </div>
            {data.fires.enabled && (
              <div className="grid grid-cols-2 gap-4">
                <NumberInput
                  label="Tick Seconds"
                  value={data.fires.tick_seconds}
                  onChange={(v) => onChange({ ...data, fires: { ...data.fires, tick_seconds: v } })}
                  min={60}
                  helper="Default: 10 minutes"
                  info="Fire perimeters update a few times daily. Polling every 10-30 minutes is sufficient."
                />
                <TextInput
                  label="State"
                  value={data.fires.state}
                  onChange={(v) => onChange({ ...data, fires: { ...data.fires, state: v } })}
                  placeholder="US-ID"
                  helper="ISO 3166-2 code (e.g., US-ID, US-MT)"
                  info="Filter fires to a specific state using ISO 3166-2 codes. US-ID = Idaho, US-MT = Montana, US-CA = California, etc."
                />
              </div>
            )}
          </div>

          {/* Avalanche Advisories */}
          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-300">Avalanche Advisories</span>
                <InfoButton info="Avalanche center forecasts and danger levels. Danger scale: 1=Low, 2=Moderate, 3=Considerable, 4=High, 5=Extreme. Most fatalities occur at level 3 (Considerable)." />
              </div>
              <Toggle label="" checked={data.avalanche.enabled} onChange={(v) => onChange({ ...data, avalanche: { ...data.avalanche, enabled: v } })} />
            </div>
            {data.avalanche.enabled && (
              <>
                <NumberInput
                  label="Tick Seconds"
                  value={data.avalanche.tick_seconds}
                  onChange={(v) => onChange({ ...data, avalanche: { ...data.avalanche, tick_seconds: v } })}
                  min={60}
                  helper="Default: 30 minutes"
                  info="Avalanche forecasts are typically updated once or twice daily. Polling every 30-60 minutes is sufficient."
                />
                <ListInput
                  label="Center IDs"
                  value={data.avalanche.center_ids}
                  onChange={(v) => onChange({ ...data, avalanche: { ...data.avalanche, center_ids: v } })}
                  helper="e.g., SNFAC (Sawtooth), BTAC (Bridger-Teton)"
                  info="Avalanche center identifiers. SNFAC = Sawtooth (ID), BTAC = Bridger-Teton (WY/ID), GNFAC = Gallatin (MT), CAIC = Colorado, NWAC = NW (WA/OR)."
                />
                <NumberListInput
                  label="Season Months"
                  value={data.avalanche.season_months}
                  onChange={(v) => onChange({ ...data, avalanche: { ...data.avalanche, season_months: v } })}
                  helper="1=Jan, 12=Dec — typically Dec-Apr"
                  info="Months when avalanche forecasts are active. Most centers operate December through April. Outside these months, data shows 'off season'."
                />
              </>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function DashboardSection({ data, onChange }: { data: DashboardConfig; onChange: (d: DashboardConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionHeader title="Dashboard" description={SECTION_DESCRIPTIONS.dashboard} />
      <Toggle
        label="Enable Dashboard"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Run the web dashboard server"
        info="Enables the web-based dashboard for monitoring mesh health, viewing alerts, and configuring settings through a browser."
      />
      {data.enabled && (
        <div className="grid grid-cols-2 gap-4">
          <TextInput
            label="Host"
            value={data.host}
            onChange={(v) => onChange({ ...data, host: v })}
            placeholder="0.0.0.0"
            helper="0.0.0.0 = all interfaces, 127.0.0.1 = localhost only"
            info="Network interface to bind. Use 0.0.0.0 to accept connections from any IP (needed for Docker or remote access). Use 127.0.0.1 for local-only access."
          />
          <NumberInput
            label="Port"
            value={data.port}
            onChange={(v) => onChange({ ...data, port: v })}
            min={1}
            max={65535}
            helper="Default: 8080"
            info="TCP port for the dashboard. Choose a port not used by other services. Common alternatives: 3000, 5000, 8000, 8888."
          />
        </div>
      )}
    </div>
  )
}

export default function Config() {
  const [config, setConfig] = useState<FullConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<FullConfig | null>(null)
  const [activeSection, setActiveSection] = useState<SectionKey>('bot')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [restartRequired, setRestartRequired] = useState(false)
  const [hasChanges, setHasChanges] = useState(false)

  const fetchConfig = useCallback(async () => {
    try {
      const res = await fetch('/api/config')
      if (!res.ok) throw new Error('Failed to fetch config')
      const data = await res.json()
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
    fetchConfig()
  }, [fetchConfig])

  useEffect(() => {
    if (config && originalConfig) {
      setHasChanges(JSON.stringify(config) !== JSON.stringify(originalConfig))
    }
  }, [config, originalConfig])

  const saveSection = async () => {
    if (!config) return

    setSaving(true)
    setError(null)
    setSuccess(null)

    try {
      const sectionData = config[activeSection]
      const res = await fetch(`/api/config/${activeSection}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sectionData),
      })

      const result = await res.json()

      if (!res.ok) {
        throw new Error(result.detail || 'Save failed')
      }

      setSuccess(`${activeSection} saved successfully`)
      setOriginalConfig(JSON.parse(JSON.stringify(config)))
      setHasChanges(false)

      if (result.restart_required) {
        setRestartRequired(true)
      }

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

  const restartService = async () => {
    try {
      await fetch('/api/restart', { method: 'POST' })
      setRestartRequired(false)
      setSuccess('Restart initiated')
    } catch {
      setError('Restart failed')
    }
  }

  const updateSection = <K extends SectionKey>(section: K, data: FullConfig[K]) => {
    if (!config) return
    setConfig({ ...config, [section]: data })
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading configuration...</div>
      </div>
    )
  }

  if (!config) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Failed to load configuration</div>
      </div>
    )
  }

  const renderSection = () => {
    switch (activeSection) {
      case 'bot': return <BotSection data={config.bot} onChange={(d) => updateSection('bot', d)} />
      case 'connection': return <ConnectionSection data={config.connection} onChange={(d) => updateSection('connection', d)} />
      case 'response': return <ResponseSection data={config.response} onChange={(d) => updateSection('response', d)} />
      case 'history': return <HistorySection data={config.history} onChange={(d) => updateSection('history', d)} />
      case 'memory': return <MemorySection data={config.memory} onChange={(d) => updateSection('memory', d)} />
      case 'context': return <ContextSection data={config.context} onChange={(d) => updateSection('context', d)} />
      case 'commands': return <CommandsSection data={config.commands} onChange={(d) => updateSection('commands', d)} />
      case 'llm': return <LLMSection data={config.llm} onChange={(d) => updateSection('llm', d)} />
      case 'weather': return <WeatherSection data={config.weather} onChange={(d) => updateSection('weather', d)} />
      case 'meshmonitor': return <MeshMonitorSection data={config.meshmonitor} onChange={(d) => updateSection('meshmonitor', d)} />
      case 'knowledge': return <KnowledgeSection data={config.knowledge} onChange={(d) => updateSection('knowledge', d)} />
      case 'mesh_sources': return <MeshSourcesSection data={config.mesh_sources} onChange={(d) => updateSection('mesh_sources', d)} />
      case 'mesh_intelligence': return <MeshIntelligenceSection data={config.mesh_intelligence} onChange={(d) => updateSection('mesh_intelligence', d)} />
      case 'environmental': return <EnvironmentalSection data={config.environmental} onChange={(d) => updateSection('environmental', d)} />
      case 'dashboard': return <DashboardSection data={config.dashboard} onChange={(d) => updateSection('dashboard', d)} />
      default: return null
    }
  }

  const activeLabel = SECTIONS.find(s => s.key === activeSection)?.label || activeSection

  return (
    <div className="flex gap-6 h-[calc(100vh-8rem)]">
      <div className="w-48 flex-shrink-0 space-y-1">
        {SECTIONS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setActiveSection(key)}
            className={`w-full flex items-center gap-2 px-3 py-2 rounded text-sm transition-colors ${
              activeSection === key
                ? 'bg-accent text-white'
                : 'text-slate-400 hover:text-slate-200 hover:bg-bg-hover'
            }`}
          >
            <Icon size={16} />
            <span>{label}</span>
            {hasChanges && activeSection === key && (
              <span className="ml-auto w-2 h-2 bg-amber-500 rounded-full" />
            )}
          </button>
        ))}
      </div>

      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <Settings size={20} className="text-slate-500" />
            <h2 className="text-lg font-semibold text-slate-200">{activeLabel}</h2>
          </div>
          <div className="flex items-center gap-2">
            {hasChanges && (
              <button
                onClick={discardChanges}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-slate-400 hover:text-slate-200 bg-bg-hover rounded transition-colors"
              >
                <RotateCcw size={14} />
                Discard
              </button>
            )}
            <button
              onClick={saveSection}
              disabled={saving || !hasChanges}
              className="flex items-center gap-1.5 px-4 py-1.5 text-sm bg-accent text-white rounded hover:bg-accent/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {saving ? <RefreshCw size={14} className="animate-spin" /> : <Save size={14} />}
              Save
            </button>
          </div>
        </div>

        {restartRequired && (
          <div className="flex items-center justify-between p-3 mb-4 bg-amber-500/10 border border-amber-500/30 rounded-lg">
            <div className="flex items-center gap-2 text-amber-400">
              <AlertTriangle size={16} />
              <span className="text-sm">Restart required for changes to take effect</span>
            </div>
            <button
              onClick={restartService}
              className="px-3 py-1 text-sm bg-amber-500 text-white rounded hover:bg-amber-600 transition-colors"
            >
              Restart Now
            </button>
          </div>
        )}

        {error && (
          <div className="flex items-center gap-2 p-3 mb-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">
            <X size={16} />
            <span className="text-sm">{error}</span>
          </div>
        )}

        {success && (
          <div className="flex items-center gap-2 p-3 mb-4 bg-green-500/10 border border-green-500/30 rounded-lg text-green-400">
            <Check size={16} />
            <span className="text-sm">{success}</span>
          </div>
        )}

        <div className="flex-1 overflow-y-auto pr-2">
          <div className="bg-bg-card border border-border rounded-lg p-6">
            {renderSection()}
          </div>
        </div>
      </div>
    </div>
  )
}
