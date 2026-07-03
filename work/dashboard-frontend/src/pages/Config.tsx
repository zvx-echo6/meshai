import { useState, useEffect, useCallback, useRef } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { notifyRestartRequired } from '@/components/RestartBanner'
import { useDirty } from '@/context/DirtyContext'
import NodePicker from '@/components/NodePicker'
import ChannelPicker from '@/components/ChannelPicker'
import {
  Settings, Bot, MessageSquare, Database, Brain, Eye,
  Terminal, Cpu, Cloud, BookOpen, Activity,
  LayoutDashboard, Save, RotateCcw, RefreshCw,
  Plus, Trash2, ChevronDown, ChevronRight, AlertTriangle,
  Check, X, Eye as EyeIcon, EyeOff, ExternalLink
} from 'lucide-react'

// Types for config sections
interface BotConfig {
  name: string
  owner: string
  respond_to_dms: boolean
  filter_bbs_protocols: boolean
}

export interface ConnectionConfig {
  type: string
  serial_port: string
  tcp_host: string
  tcp_port: number
  meshcore_host?: string
  meshcore_port?: number
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

export interface MeshMonitorConfig {
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

export interface MeshSourceConfig {
  name: string
  type: string
  url: string
  api_token: string
  refresh_interval: number
  polite_mode: boolean
  enabled: boolean
  host?: string
  port?: number
  username?: string
  password?: string
  topic_root?: string
  use_tls?: boolean
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

export interface MeshIntelligenceConfig {
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
  usgs: { enabled: boolean; tick_seconds: number; sites: string[] }
  traffic: { enabled: boolean; tick_seconds: number; api_key: string; corridors: { name: string; lat: number; lon: number }[] }
  roads511: { enabled: boolean; tick_seconds: number; api_key: string; base_url: string; endpoints: string[]; bbox: number[] }
  firms: { enabled: boolean; tick_seconds: number; map_key: string; source: string; bbox: number[]; day_range: number; confidence_min: string; proximity_km: number }
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
  { key: 'response', label: 'Response', icon: MessageSquare },
  { key: 'history', label: 'History', icon: Database },
  { key: 'memory', label: 'Memory', icon: Brain },
  { key: 'context', label: 'Context', icon: Eye },
  { key: 'commands', label: 'Commands', icon: Terminal },
  { key: 'llm', label: 'LLM', icon: Cpu },
  { key: 'weather', label: 'Weather', icon: Cloud },
  { key: 'knowledge', label: 'Knowledge', icon: BookOpen },
  { key: 'mesh_intelligence', label: 'Intelligence', icon: Activity },
  { key: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
]

// Section descriptions
const SECTION_DESCRIPTIONS: Record<SectionKey, string> = {
  bot: 'Identity and behavior settings for the bot on the mesh network.',
  connection: 'How MeshAI connects to your Meshtastic radio.',
  response: 'Controls how quickly and how much the bot responds on the mesh.',
  history: 'Conversation history storage and cleanup.',
  memory: 'Short-term conversation memory management. Controls how the bot maintains context within a conversation.',
  context: 'Passive channel monitoring. The bot listens to mesh channels and uses recent messages as context when responding.',
  commands: 'Mesh commands available via the configured prefix. Toggle individual commands on or off.',
  llm: 'AI model configuration. MeshAI uses an LLM to understand questions and generate responses.',
  weather: 'Weather data for the !weather command. This is separate from NWS environmental alerts.',
  meshmonitor: 'AIDA MeshMonitor integration. An additional data source for mesh network monitoring.',
  knowledge: 'Knowledge base for answering questions from stored documents. Connects to Qdrant vector database or local SQLite.',
  mesh_sources: 'Data sources for mesh network information. MeshAI can pull data from multiple sources simultaneously and merge them into a unified view.',
  mesh_intelligence: 'Advanced mesh analysis: health scoring, region management, and automated alerting. The intelligence engine monitors your mesh and detects problems automatically.',
  environmental: 'Where MeshAI gets live environmental data (weather, fires, quakes, gauges, traffic, space weather). Per-adapter knobs (API keys, regions, thresholds) live on the Environment page; the OR-not-AND architecture decision (Central vs native) is documented under Reference → OR-not-AND.',
  dashboard: "Web dashboard settings. You're looking at it right now.",
}

// Available commands with descriptions
const AVAILABLE_COMMANDS = [
  { name: 'help', description: 'Show available commands and usage' },
  { name: 'health', description: 'Mesh network health overview with status dots' },
  { name: 'status', description: 'Quick mesh status summary' },
  { name: 'region', description: 'List regions or get detailed region breakdown' },
  { name: 'neighbors', description: 'Show top infrastructure neighbors with signal quality' },
  { name: 'ping', description: 'Test bot responsiveness' },
  { name: 'clear', description: 'Clear your conversation history' },
  { name: 'reset', description: 'Reset conversation context' },
  { name: 'sub', description: 'Subscribe to scheduled reports or alerts' },
  { name: 'unsub', description: 'Remove a subscription' },
  { name: 'mysubs', description: 'List your active subscriptions' },
  { name: 'alerts', description: 'Active NWS weather alerts for mesh area' },
  { name: 'solar', description: 'Space weather and HF propagation conditions' },
  { name: 'hf', description: 'HF radio propagation (alias for !solar)' },
  { name: 'fire', description: 'Active wildfires near the mesh' },
  { name: 'avy', description: 'Avalanche advisories for configured zones' },
  { name: 'hotspots', description: 'NASA FIRMS satellite fire detections' },
  { name: 'streams', description: 'USGS stream gauge readings' },
  { name: 'roads', description: 'Road conditions and closures' },
  { name: 'traffic', description: 'Traffic flow on monitored corridors' },
]

// US States for dropdown
export const US_STATES = [
  { value: 'US-AL', label: 'Alabama' }, { value: 'US-AK', label: 'Alaska' },
  { value: 'US-AZ', label: 'Arizona' }, { value: 'US-AR', label: 'Arkansas' },
  { value: 'US-CA', label: 'California' }, { value: 'US-CO', label: 'Colorado' },
  { value: 'US-CT', label: 'Connecticut' }, { value: 'US-DE', label: 'Delaware' },
  { value: 'US-FL', label: 'Florida' }, { value: 'US-GA', label: 'Georgia' },
  { value: 'US-HI', label: 'Hawaii' }, { value: 'US-ID', label: 'Idaho' },
  { value: 'US-IL', label: 'Illinois' }, { value: 'US-IN', label: 'Indiana' },
  { value: 'US-IA', label: 'Iowa' }, { value: 'US-KS', label: 'Kansas' },
  { value: 'US-KY', label: 'Kentucky' }, { value: 'US-LA', label: 'Louisiana' },
  { value: 'US-ME', label: 'Maine' }, { value: 'US-MD', label: 'Maryland' },
  { value: 'US-MA', label: 'Massachusetts' }, { value: 'US-MI', label: 'Michigan' },
  { value: 'US-MN', label: 'Minnesota' }, { value: 'US-MS', label: 'Mississippi' },
  { value: 'US-MO', label: 'Missouri' }, { value: 'US-MT', label: 'Montana' },
  { value: 'US-NE', label: 'Nebraska' }, { value: 'US-NV', label: 'Nevada' },
  { value: 'US-NH', label: 'New Hampshire' }, { value: 'US-NJ', label: 'New Jersey' },
  { value: 'US-NM', label: 'New Mexico' }, { value: 'US-NY', label: 'New York' },
  { value: 'US-NC', label: 'North Carolina' }, { value: 'US-ND', label: 'North Dakota' },
  { value: 'US-OH', label: 'Ohio' }, { value: 'US-OK', label: 'Oklahoma' },
  { value: 'US-OR', label: 'Oregon' }, { value: 'US-PA', label: 'Pennsylvania' },
  { value: 'US-RI', label: 'Rhode Island' }, { value: 'US-SC', label: 'South Carolina' },
  { value: 'US-SD', label: 'South Dakota' }, { value: 'US-TN', label: 'Tennessee' },
  { value: 'US-TX', label: 'Texas' }, { value: 'US-UT', label: 'Utah' },
  { value: 'US-VT', label: 'Vermont' }, { value: 'US-VA', label: 'Virginia' },
  { value: 'US-WA', label: 'Washington' }, { value: 'US-WV', label: 'West Virginia' },
  { value: 'US-WI', label: 'Wisconsin' }, { value: 'US-WY', label: 'Wyoming' },
]

// InfoButton component with click-outside dismiss and X close button
function InfoButton({ info, link, linkText = 'Learn more' }: { info: string; link?: string; linkText?: string }) {
  const [open, setOpen] = useState(false)
  const popoverRef = useRef<HTMLDivElement>(null)

  // Close on click outside
  useEffect(() => {
    if (!open) return
    function handleClickOutside(e: MouseEvent) {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    const timer = setTimeout(() => document.addEventListener('mousedown', handleClickOutside), 0)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [open])

  return (
    <div className="relative inline-block" ref={popoverRef}>
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen(!open) }}
        className="ml-1.5 w-4 h-4 rounded-full bg-slate-700 hover:bg-slate-600 text-slate-400 hover:text-slate-200 inline-flex items-center justify-center text-xs transition-colors"
        title="More info"
      >
        ?
      </button>
      {open && (
        <div className="absolute left-0 top-6 z-50 w-72 p-3 bg-[#1a2332] border border-[#2a3a4a] shadow-xl text-xs text-slate-300 leading-relaxed">
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="absolute top-1 right-1 w-5 h-5 rounded hover:bg-slate-700 text-slate-500 hover:text-slate-300 inline-flex items-center justify-center transition-colors"
            aria-label="Close"
          >
            <X size={12} />
          </button>
          <div className="pr-4">{info}</div>
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
      )}
    </div>
  )
}

// Section description component
function SectionDescription({ text }: { text: string }) {
  return (
    <p className="text-sm text-slate-500 mb-6 pb-4 border-b border-[#1e2a3a]">{text}</p>
  )
}

// Form components
export function TextInput({ label, value, onChange, type = 'text', placeholder = '', helper = '', info = '', infoLink = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
  helper?: string
  info?: string
  infoLink?: string
}) {
  const [showPassword, setShowPassword] = useState(false)
  const isPassword = type === 'password'

  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} link={infoLink} />}
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

export function NumberInput({ label, value, onChange, min, max, step = 1, helper = '', info = '', infoLink = '' }: {
  label: string
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
  helper?: string
  info?: string
  infoLink?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} link={infoLink} />}
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

export function Toggle({ label, checked, onChange, helper = '', info = '', infoLink = '' }: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  helper?: string
  info?: string
  infoLink?: string
}) {
  return (
    <div className="flex items-center justify-between py-2">
      <div>
        <span className="flex items-center text-sm text-slate-300">
          {label}
          {info && <InfoButton info={info} link={infoLink} />}
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

export function SelectInput({ label, value, onChange, options, helper = '', info = '', infoLink = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
  helper?: string
  info?: string
  infoLink?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} link={infoLink} />}
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

function TextArea({ label, value, onChange, rows = 4, helper = '', info = '', infoLink = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  rows?: number
  helper?: string
  info?: string
  infoLink?: string
}) {
  return (
    <div className="space-y-1">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoButton info={info} link={infoLink} />}
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

export function ListInput({ label, value, onChange, helper = '', info = '', infoLink = '' }: {
  label: string
  value: string[]
  onChange: (v: string[]) => void
  helper?: string
  info?: string
  infoLink?: string
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
        {info && <InfoButton info={info} link={infoLink} />}
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

export function NumberListInput({ label, value, onChange, helper = '', info = '', infoLink = '' }: {
  label: string
  value: number[]
  onChange: (v: number[]) => void
  helper?: string
  info?: string
  infoLink?: string
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
        {info && <InfoButton info={info} link={infoLink} />}
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

// Alert rule with description component
function AlertRuleToggle({ label, description, checked, onChange, threshold, onThresholdChange, thresholdLabel, thresholdMin, thresholdMax, thresholdStep = 1, thresholdSuffix = '' }: {
  label: string
  description: string
  checked: boolean
  onChange: (v: boolean) => void
  threshold?: number
  onThresholdChange?: (v: number) => void
  thresholdLabel?: string
  thresholdMin?: number
  thresholdMax?: number
  thresholdStep?: number
  thresholdSuffix?: string
}) {
  return (
    <div className="border border-[#1e2a3a] p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex-1">
          <span className="text-sm text-slate-300">{label}</span>
          <p className="text-xs text-slate-600">{description}</p>
        </div>
        <button
          type="button"
          onClick={() => onChange(!checked)}
          className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 ml-3 ${
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
      {checked && threshold !== undefined && onThresholdChange && (
        <div className="flex items-center gap-2 pt-2 border-t border-[#1e2a3a]">
          <span className="text-xs text-slate-500">{thresholdLabel || 'Threshold'}:</span>
          <input
            type="number"
            value={threshold}
            onChange={(e) => onThresholdChange(Number(e.target.value))}
            min={thresholdMin}
            max={thresholdMax}
            step={thresholdStep}
            className="w-20 px-2 py-1 bg-[#0a0e17] border border-[#1e2a3a] rounded text-xs text-slate-200 font-mono"
          />
          {thresholdSuffix && <span className="text-xs text-slate-500">{thresholdSuffix}</span>}
        </div>
      )}
    </div>
  )
}

// Section renderers
function BotSection({ data, onChange }: { data: BotConfig; onChange: (d: BotConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.bot} />
      <div className="grid grid-cols-2 gap-4">
        <TextInput
          label="Bot Name"
          value={data.name}
          onChange={(v) => onChange({ ...data, name: v })}
          helper="Name the bot responds to on the mesh"
          info="When someone sends a message containing this name, the bot will respond. Also used as the sender name in broadcasts. Changing this requires a restart."
        />
        <TextInput
          label="Owner"
          value={data.owner}
          onChange={(v) => onChange({ ...data, owner: v })}
          helper="Your callsign or identifier"
          info="Identifies the bot operator. Shown in !help responses and used for admin-level commands."
        />
      </div>
      <Toggle
        label="Respond to DMs"
        checked={data.respond_to_dms}
        onChange={(v) => onChange({ ...data, respond_to_dms: v })}
        helper="Reply when someone sends a direct message"
        info="When enabled, the bot responds to direct messages from any node. When disabled, the bot only responds to channel messages that mention its name."
      />
      <Toggle
        label="Filter BBS Protocols"
        checked={data.filter_bbs_protocols}
        onChange={(v) => onChange({ ...data, filter_bbs_protocols: v })}
        helper="Ignore BBS bulletin board traffic"
        info="Filters out automated BBS protocol messages (advBBS, MAIL*, BOARD*) so the bot doesn't try to respond to machine-to-machine traffic."
      />
    </div>
  )
}

export function ConnectionSection({ data, onChange }: { data: ConnectionConfig; onChange: (d: ConnectionConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.connection} />
      <SelectInput
        label="Connection Type"
        value={data.type}
        onChange={(v) => onChange({ ...data, type: v })}
        options={[
          { value: 'serial', label: 'Serial (USB)' },
          { value: 'tcp', label: 'TCP (Network)' },
        ]}
        helper="Serial for USB-connected radios, TCP for network or meshtasticd"
        info="Serial: direct USB connection to a Meshtastic radio. TCP: connect over the network to a radio's IP or to meshtasticd running on another machine."
      />
      {data.type === 'serial' ? (
        <TextInput
          label="Serial Port"
          value={data.serial_port}
          onChange={(v) => onChange({ ...data, serial_port: v })}
          placeholder="/dev/ttyUSB0"
          helper="Device path for your USB radio"
          info="Usually /dev/ttyUSB0 on Linux or /dev/ttyACM0. Check with 'ls /dev/tty*' after plugging in your radio."
        />
      ) : (
        <div className="grid grid-cols-2 gap-4">
          <TextInput
            label="TCP Host"
            value={data.tcp_host}
            onChange={(v) => onChange({ ...data, tcp_host: v })}
            placeholder="192.168.1.100"
            helper="IP address or hostname of the radio/meshtasticd"
          />
          <NumberInput
            label="TCP Port"
            value={data.tcp_port}
            onChange={(v) => onChange({ ...data, tcp_port: v })}
            min={1}
            max={65535}
            helper="Default 4403 for meshtasticd"
          />
        </div>
      )}
      {/* MeshCore host/port live on their own first-class page
          (/meshcore/connection). Subtle cross-link only — no editable fields here. */}
      <div className="pt-2">
        <Link
          to="/meshcore/connection"
          className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-accent transition-colors"
        >
          &rarr; MeshCore connection
        </Link>
      </div>
    </div>
  )
}

function ResponseSection({ data, onChange }: { data: ResponseConfig; onChange: (d: ResponseConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.response} />
      <div className="grid grid-cols-2 gap-4">
        <NumberInput
          label="Delay Min (sec)"
          value={data.delay_min}
          onChange={(v) => onChange({ ...data, delay_min: v })}
          min={0}
          step={0.1}
          helper="Minimum wait before responding"
          info="Adds a random delay between min and max before the bot sends a response. Prevents the bot from appearing to respond instantly, which can feel unnatural on a radio network."
        />
        <NumberInput
          label="Delay Max (sec)"
          value={data.delay_max}
          onChange={(v) => onChange({ ...data, delay_max: v })}
          min={0}
          step={0.1}
          helper="Maximum wait before responding"
          info="Also prevents collisions with other traffic by staggering transmissions."
        />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <NumberInput
          label="Max Length"
          value={data.max_length}
          onChange={(v) => onChange({ ...data, max_length: v })}
          min={50}
          max={500}
          helper="Maximum characters per response message"
          info="Meshtastic packets have limited size. This caps how long each message chunk can be. The bot will split longer responses into multiple messages up to Max Messages."
        />
        <NumberInput
          label="Max Messages"
          value={data.max_messages}
          onChange={(v) => onChange({ ...data, max_messages: v })}
          min={1}
          max={10}
          helper="Maximum chunks per response"
          info="If a response is longer than Max Length, the bot splits it into this many chunks at most. Higher values = more complete answers but more airtime used."
        />
      </div>
    </div>
  )
}

function HistorySection({ data, onChange }: { data: HistoryConfig; onChange: (d: HistoryConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.history} />
      <TextInput
        label="Database Path"
        value={data.database}
        onChange={(v) => onChange({ ...data, database: v })}
        helper="SQLite file for storing conversation history"
        info="Path to the SQLite database file. Created automatically if it doesn't exist. Stores all conversation history for context."
      />
      <div className="grid grid-cols-2 gap-4">
        <NumberInput
          label="Max Messages Per User"
          value={data.max_messages_per_user}
          onChange={(v) => onChange({ ...data, max_messages_per_user: v })}
          min={0}
          helper="History limit per user (0 = unlimited)"
          info="Limits how many messages are stored per user. Older messages are pruned when the limit is reached. Set to 0 for no limit."
        />
        <NumberInput
          label="Conversation Timeout (sec)"
          value={data.conversation_timeout}
          onChange={(v) => onChange({ ...data, conversation_timeout: v })}
          min={0}
          helper="Seconds before context resets"
          info="If a user doesn't message for this long, their next message starts a new conversation context. The bot won't remember the previous topic."
        />
      </div>
      <Toggle
        label="Auto Cleanup"
        checked={data.auto_cleanup}
        onChange={(v) => onChange({ ...data, auto_cleanup: v })}
        helper="Automatically prune old conversations"
      />
      {data.auto_cleanup && (
        <div className="grid grid-cols-2 gap-4">
          <NumberInput
            label="Cleanup Interval (hours)"
            value={data.cleanup_interval_hours}
            onChange={(v) => onChange({ ...data, cleanup_interval_hours: v })}
            min={1}
            helper="Hours between cleanup runs"
          />
          <NumberInput
            label="Max Age (days)"
            value={data.max_age_days}
            onChange={(v) => onChange({ ...data, max_age_days: v })}
            min={1}
            helper="Delete conversations older than this"
          />
        </div>
      )}
    </div>
  )
}

function MemorySection({ data, onChange }: { data: MemoryConfig; onChange: (d: MemoryConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.memory} />
      <Toggle
        label="Enable Memory"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Keep conversation context between messages"
      />
      {data.enabled && (
        <div className="grid grid-cols-2 gap-4">
          <NumberInput
            label="Window Size"
            value={data.window_size}
            onChange={(v) => onChange({ ...data, window_size: v })}
            min={1}
            helper="Recent message pairs kept in full"
            info="The bot keeps this many recent exchanges (user message + bot response pairs) as full text in context. Older messages are summarized to save token space."
          />
          <NumberInput
            label="Summarize Threshold"
            value={data.summarize_threshold}
            onChange={(v) => onChange({ ...data, summarize_threshold: v })}
            min={1}
            helper="Messages before older context is summarized"
            info="When the conversation exceeds this many messages, older ones outside the window are compressed into a summary by the LLM."
          />
        </div>
      )}
    </div>
  )
}

function ContextSection({ data, onChange }: { data: ContextConfig; onChange: (d: ContextConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.context} />
      <Toggle
        label="Enable Passive Context"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Listen to channel traffic for context"
        info="When enabled, the bot monitors mesh channels and includes recent messages in its context. This lets the bot reference things other people said on the channel."
      />
      {data.enabled && (
        <>
          {/* Observe Channels + Ignore Nodes moved to the Meshtastic Connection
              page ("Bot behavior" section). max_age / max_context_items remain
              here as general context knobs. */}
          <div className="grid grid-cols-2 gap-4">
            <NumberInput
              label="Max Age (sec)"
              value={data.max_age}
              onChange={(v) => onChange({ ...data, max_age: v })}
              min={0}
              helper="Ignore messages older than this"
            />
            <NumberInput
              label="Max Context Items"
              value={data.max_context_items}
              onChange={(v) => onChange({ ...data, max_context_items: v })}
              min={1}
              helper="Maximum recent messages to include"
            />
          </div>
        </>
      )}
    </div>
  )
}

function CommandsSection({ data, onChange }: { data: CommandsConfig; onChange: (d: CommandsConfig) => void }) {
  const disabledSet = new Set(data.disabled_commands.map(c => c.toLowerCase()))

  const toggleCommand = (cmdName: string) => {
    const lowerName = cmdName.toLowerCase()
    if (disabledSet.has(lowerName)) {
      onChange({ ...data, disabled_commands: data.disabled_commands.filter(c => c.toLowerCase() !== lowerName) })
    } else {
      onChange({ ...data, disabled_commands: [...data.disabled_commands, cmdName] })
    }
  }

  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.commands} />
      <Toggle
        label="Enable Commands"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Allow !commands on the mesh"
      />
      {data.enabled && (
        <>
          <TextInput
            label="Command Prefix"
            value={data.prefix}
            onChange={(v) => onChange({ ...data, prefix: v })}
            helper="Character that triggers commands (e.g. ! for !help)"
            info="Users type this character followed by the command name. Only single characters recommended."
          />

          <div className="space-y-2">
            <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
              Available Commands
              <InfoButton info="Toggle commands on or off. Disabled commands won't respond when users invoke them." />
            </label>
            <div className="grid gap-1">
              {AVAILABLE_COMMANDS.map((cmd) => {
                const isEnabled = !disabledSet.has(cmd.name.toLowerCase())
                return (
                  <div
                    key={cmd.name}
                    className="flex items-center justify-between p-2 bg-[#0a0e17] border border-[#1e2a3a] rounded hover:border-[#2a3a4a] transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <code className="text-accent text-sm">!{cmd.name}</code>
                      <span className="text-xs text-slate-500">{cmd.description}</span>
                    </div>
                    <button
                      type="button"
                      onClick={() => toggleCommand(cmd.name)}
                      className={`relative w-9 h-5 rounded-full transition-colors ${
                        isEnabled ? 'bg-accent' : 'bg-[#1e2a3a]'
                      }`}
                    >
                      <span
                        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                          isEnabled ? 'translate-x-4' : ''
                        }`}
                      />
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function LLMSection({ data, onChange }: { data: LLMConfig; onChange: (d: LLMConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.llm} />
      <div className="grid grid-cols-2 gap-4">
        <SelectInput
          label="Backend"
          value={data.backend}
          onChange={(v) => onChange({ ...data, backend: v })}
          options={[
            { value: 'openai', label: 'OpenAI' },
            { value: 'anthropic', label: 'Anthropic' },
            { value: 'google', label: 'Google (Gemini)' },
          ]}
          helper="LLM provider to use"
          info="OpenAI: GPT models (gpt-4o, gpt-4o-mini). Anthropic: Claude models (claude-sonnet-4-20250514). Google: Gemini models. Can also point to compatible APIs like Ollama, LM Studio, or Open WebUI by changing the Base URL."
        />
        <TextInput
          label="Model"
          value={data.model}
          onChange={(v) => onChange({ ...data, model: v })}
          placeholder="gpt-4o-mini"
          helper="Specific model name"
          info="The specific model to use. Common choices: gpt-4o-mini (fast, cheap), gpt-4o (better, costs more), claude-sonnet-4-20250514 (Anthropic equivalent). For local models via Ollama, use the model name you pulled (e.g. llama3.1)."
        />
      </div>
      <TextInput
        label="API Key"
        value={data.api_key}
        onChange={(v) => onChange({ ...data, api_key: v })}
        type="password"
        helper="Supports ${ENV_VAR} syntax"
        info="Your API key from the provider. You can also use ${ENV_VAR} syntax to read from an environment variable instead of storing the key in the config file."
      />
      <TextInput
        label="Base URL"
        value={data.base_url}
        onChange={(v) => onChange({ ...data, base_url: v })}
        placeholder="https://api.openai.com/v1"
        helper="API endpoint (change for local LLMs)"
        info="Default API endpoint for the selected backend. Change this to point to a local LLM server (Ollama at http://localhost:11434/v1, Open WebUI, LM Studio, etc.) or a proxy."
      />
      <div className="grid grid-cols-2 gap-4">
        <NumberInput
          label="Timeout (sec)"
          value={data.timeout}
          onChange={(v) => onChange({ ...data, timeout: v })}
          min={5}
          max={120}
          helper="Maximum seconds to wait for response"
        />
        <NumberInput
          label="Max Response Tokens"
          value={data.max_response_tokens}
          onChange={(v) => onChange({ ...data, max_response_tokens: v })}
          min={100}
          helper="Token limit for LLM responses"
        />
      </div>
      <Toggle
        label="Use System Prompt"
        checked={data.use_system_prompt}
        onChange={(v) => onChange({ ...data, use_system_prompt: v })}
        helper="Enable custom system instructions"
      />
      {data.use_system_prompt && (
        <TextArea
          label="System Prompt"
          value={data.system_prompt}
          onChange={(v) => onChange({ ...data, system_prompt: v })}
          rows={6}
          helper="Instructions that shape the bot's personality"
          info="Instructions that shape the bot's personality and behavior. The bot always follows these instructions. MeshAI adds mesh health data and environmental context automatically â€” you don't need to include those here."
        />
      )}
      <Toggle
        label="Web Search"
        checked={data.web_search}
        onChange={(v) => onChange({ ...data, web_search: v })}
        helper="Enable web search tool (Open WebUI feature)"
      />
      <Toggle
        label="Google Grounding"
        checked={data.google_grounding}
        onChange={(v) => onChange({ ...data, google_grounding: v })}
        helper="Ground responses in web search (Gemini only)"
      />
    </div>
  )
}

function WeatherSection({ data, onChange }: { data: WeatherConfig; onChange: (d: WeatherConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.weather} />
      <div className="grid grid-cols-2 gap-4">
        <SelectInput
          label="Primary Provider"
          value={data.primary}
          onChange={(v) => onChange({ ...data, primary: v })}
          options={[
            { value: 'openmeteo', label: 'Open-Meteo' },
            { value: 'wttr', label: 'wttr.in' },
            { value: 'llm', label: 'LLM' },
          ]}
          helper="Main weather data source"
        />
        <SelectInput
          label="Fallback Provider"
          value={data.fallback}
          onChange={(v) => onChange({ ...data, fallback: v })}
          options={[
            { value: 'openmeteo', label: 'Open-Meteo' },
            { value: 'wttr', label: 'wttr.in' },
            { value: 'llm', label: 'LLM' },
            { value: 'none', label: 'None' },
          ]}
          helper="Backup if primary fails"
        />
      </div>
      <TextInput
        label="Default Location"
        value={data.default_location}
        onChange={(v) => onChange({ ...data, default_location: v })}
        placeholder="Your city, state"
        helper="Location when none specified"
      />
    </div>
  )
}

export function MeshMonitorSection({ data, onChange }: { data: MeshMonitorConfig; onChange: (d: MeshMonitorConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.meshmonitor} />
      <Toggle
        label="Enable MeshMonitor"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Connect to AIDA MeshMonitor instance"
        info="MeshMonitor by Yeraze provides node data, battery info, telemetry, and auto-responder patterns. MeshAI uses this as a data source and avoids duplicate responses."
      />
      {data.enabled && (
        <>
          <TextInput
            label="URL"
            value={data.url}
            onChange={(v) => onChange({ ...data, url: v })}
            placeholder="http://192.168.1.100:8080"
            helper="MeshMonitor API endpoint"
            info="Full URL to your MeshMonitor instance. Usually runs on port 8080."
          />
          <Toggle
            label="Inject Into Prompt"
            checked={data.inject_into_prompt}
            onChange={(v) => onChange({ ...data, inject_into_prompt: v })}
            helper="Tell LLM about MeshMonitor commands"
            info="Adds MeshMonitor's auto-responder patterns to the LLM context so it knows what commands MeshMonitor handles."
          />
          <NumberInput
            label="Refresh Interval (sec)"
            value={data.refresh_interval}
            onChange={(v) => onChange({ ...data, refresh_interval: v })}
            min={10}
            helper="How often to fetch patterns"
          />
          <Toggle
            label="Polite Mode"
            checked={data.polite_mode}
            onChange={(v) => onChange({ ...data, polite_mode: v })}
            helper="Reduce polling frequency"
            info="Reduces polling frequency for shared instances to be a good neighbor."
          />
        </>
      )}
    </div>
  )
}

function KnowledgeSection({ data, onChange }: { data: KnowledgeConfig; onChange: (d: KnowledgeConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.knowledge} />
      <Toggle
        label="Enable Knowledge Base"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Answer questions from stored documents"
        info="Uses RAG (Retrieval-Augmented Generation) to answer questions from a knowledge base. Supports Qdrant vector database or local SQLite with FTS5."
      />
      {data.enabled && (
        <>
          <SelectInput
            label="Backend"
            value={data.backend}
            onChange={(v) => onChange({ ...data, backend: v })}
            options={[
              { value: 'auto', label: 'Auto (Qdrant -> SQLite)' },
              { value: 'qdrant', label: 'Qdrant' },
              { value: 'sqlite', label: 'SQLite' },
            ]}
            helper="Knowledge storage backend"
            info="Auto tries Qdrant first, falls back to SQLite. Qdrant provides hybrid search with dense+sparse embeddings. SQLite uses FTS5 keyword search."
          />
          {(data.backend === 'qdrant' || data.backend === 'auto') && (
            <>
              <div className="grid grid-cols-2 gap-4">
                <TextInput
                  label="Qdrant Host"
                  value={data.qdrant_host}
                  onChange={(v) => onChange({ ...data, qdrant_host: v })}
                  helper="Qdrant server hostname"
                  info="IP or hostname of your Qdrant vector database server."
                />
                <NumberInput
                  label="Qdrant Port"
                  value={data.qdrant_port}
                  onChange={(v) => onChange({ ...data, qdrant_port: v })}
                  helper="Default 6333"
                />
              </div>
              <TextInput
                label="Collection"
                value={data.qdrant_collection}
                onChange={(v) => onChange({ ...data, qdrant_collection: v })}
                helper="Qdrant collection name"
              />
              <div className="grid grid-cols-2 gap-4">
                <TextInput
                  label="TEI Host"
                  value={data.tei_host}
                  onChange={(v) => onChange({ ...data, tei_host: v })}
                  helper="Text Embeddings Inference host"
                  info="TEI service for generating dense embeddings. Uses BAAI/bge-m3 model."
                />
                <NumberInput
                  label="TEI Port"
                  value={data.tei_port}
                  onChange={(v) => onChange({ ...data, tei_port: v })}
                  helper="Default 8090"
                />
              </div>
              <Toggle
                label="Use Sparse Embeddings"
                checked={data.use_sparse}
                onChange={(v) => onChange({ ...data, use_sparse: v })}
                helper="Enable hybrid search with sparse vectors"
                info="Combines dense embeddings with sparse (keyword-based) embeddings using Reciprocal Rank Fusion for better search results."
              />
            </>
          )}
          <TextInput
            label="SQLite DB Path"
            value={data.db_path}
            onChange={(v) => onChange({ ...data, db_path: v })}
            helper="Local knowledge database file"
          />
          <NumberInput
            label="Top K Results"
            value={data.top_k}
            onChange={(v) => onChange({ ...data, top_k: v })}
            min={1}
            max={20}
            helper="Number of documents to retrieve"
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

  const typeInfo: Record<string, string> = {
    meshview: 'Web-based mesh monitoring tool. Enter the full URL of a MeshView instance. No API key typically required.',
    meshmonitor: 'AIDA MeshMonitor API. Provides node data and network statistics. Requires API token.',
    mqtt: 'Subscribe directly to a Meshtastic MQTT broker for real-time packet data. This is push-based (instant) vs the polling approach of MeshView/MeshMonitor.',
  }

  return (
    <div className="border border-[#1e2a3a] overflow-hidden">
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
            <TextInput label="Name" value={source.name} onChange={(v) => onChange({ ...source, name: v })} helper="Friendly name for this source" />
            <SelectInput
              label="Type"
              value={source.type}
              onChange={(v) => onChange({ ...source, type: v })}
              options={[
                { value: 'meshview', label: 'MeshView' },
                { value: 'meshmonitor', label: 'MeshMonitor' },
                { value: 'mqtt', label: 'MQTT Broker' },
              ]}
              info={typeInfo[source.type] || ''}
            />
          </div>
          {source.type !== 'mqtt' && (
            <TextInput label="URL" value={source.url} onChange={(v) => onChange({ ...source, url: v })} helper="Full URL including protocol" />
          )}
          {source.type === 'meshmonitor' && (
            <TextInput label="API Token" value={source.api_token} onChange={(v) => onChange({ ...source, api_token: v })} type="password" helper="Bearer token for authentication" />
          )}
          {source.type === 'mqtt' && (
            <>
              <div className="grid grid-cols-2 gap-4">
                <TextInput label="Host" value={source.host || ''} onChange={(v) => onChange({ ...source, host: v })} helper="MQTT broker hostname" />
                <NumberInput label="Port" value={source.port || 1883} onChange={(v) => onChange({ ...source, port: v })} min={1} max={65535} helper="1883 plain, 8883 TLS" />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <TextInput label="Username" value={source.username || ''} onChange={(v) => onChange({ ...source, username: v })} />
                <TextInput label="Password" value={source.password || ''} onChange={(v) => onChange({ ...source, password: v })} type="password" />
              </div>
              <TextInput label="Topic Root" value={source.topic_root || 'msh/US'} onChange={(v) => onChange({ ...source, topic_root: v })} helper="Base topic to subscribe to" />
              <Toggle label="Use TLS" checked={source.use_tls || false} onChange={(v) => onChange({ ...source, use_tls: v })} helper="Encrypt MQTT connection" />
            </>
          )}
          <NumberInput label="Refresh Interval (sec)" value={source.refresh_interval} onChange={(v) => onChange({ ...source, refresh_interval: v })} min={10} helper="Polling frequency" />
          <Toggle label="Enabled" checked={source.enabled} onChange={(v) => onChange({ ...source, enabled: v })} />
          <Toggle label="Polite Mode" checked={source.polite_mode} onChange={(v) => onChange({ ...source, polite_mode: v })} helper="Reduce polling for shared instances" />
        </div>
      )}
    </div>
  )
}

export function MeshSourcesSection({ data, onChange }: { data: MeshSourceConfig[]; onChange: (d: MeshSourceConfig[]) => void }) {
  const addSource = () => {
    onChange([...data, {
      name: 'New Source',
      type: 'meshview',
      url: '',
      api_token: '',
      refresh_interval: 30,
      polite_mode: false,
      enabled: true,
      host: '',
      port: 1883,
      username: '',
      password: '',
      topic_root: 'msh/US',
      use_tls: false,
    }])
  }

  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.mesh_sources} />
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
        className="w-full py-2 border border-dashed border-[#1e2a3a] text-slate-500 hover:text-slate-300 hover:border-accent flex items-center justify-center gap-2 transition-colors"
      >
        <Plus size={16} /> Add Source
      </button>
    </div>
  )
}

export function MeshIntelligenceSection({ data, onChange }: { data: MeshIntelligenceConfig; onChange: (d: MeshIntelligenceConfig) => void }) {
  const [expandedRegion, setExpandedRegion] = useState<number | null>(null)

  return (
    <div className="space-y-6">
      <SectionDescription text={SECTION_DESCRIPTIONS.mesh_intelligence} />
      <Toggle
        label="Enable Mesh Intelligence"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Activate health scoring and alerting"
      />

      {data.enabled && (
        <>
          <div className="grid grid-cols-2 gap-4">
            <NumberInput
              label="Locality Radius (miles)"
              value={data.locality_radius_miles}
              onChange={(v) => onChange({ ...data, locality_radius_miles: v })}
              min={1}
              step={0.5}
              helper="Region assignment radius"
              info="Nodes within this distance of a region anchor point are assigned to that region."
            />
            <NumberInput
              label="Offline Threshold (hours)"
              value={data.offline_threshold_hours}
              onChange={(v) => onChange({ ...data, offline_threshold_hours: v })}
              min={1}
              helper="Time until node marked offline"
              info="A node is considered offline after not being heard for this many hours."
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <NumberInput
              label="Packet Threshold"
              value={data.packet_threshold}
              onChange={(v) => onChange({ ...data, packet_threshold: v })}
              min={0}
              helper="Min packets per 24h to flag"
              info="Minimum packets per 24 hours. Nodes below this are flagged as low activity."
            />
            <NumberInput
              label="Battery Warning %"
              value={data.battery_warning_percent}
              onChange={(v) => onChange({ ...data, battery_warning_percent: v })}
              min={1}
              max={100}
              helper="Global battery warning level"
            />
          </div>

          <NodePicker
            label="Critical Nodes"
            value={data.critical_nodes}
            onChange={(v) => onChange({ ...data, critical_nodes: v })}
            helper="Critical infrastructure nodes"
            info="Nodes that get priority alerting when they go offline."
            roleFilter="infrastructure"
          />

          <div className="grid grid-cols-2 gap-4">
            <ChannelPicker
              label="Alert Channel"
              value={data.alert_channel}
              onChange={(v) => onChange({ ...data, alert_channel: v })}
              helper="Channel for broadcast alerts"
              info="Meshtastic channel for broadcast alerts. Select Disabled to turn off channel broadcasting."
              mode="single"
              includeDisabled
            />
            <NumberInput
              label="Alert Cooldown (min)"
              value={data.alert_cooldown_minutes}
              onChange={(v) => onChange({ ...data, alert_cooldown_minutes: v })}
              min={1}
              helper="Min time between repeat alerts"
              info="Minimum minutes between repeated alerts for the same condition. Uses scaling cooldown (12h, 24h, 48h)."
            />
          </div>

          <div className="space-y-2">
            <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
              Regions
              <InfoButton info="Regions group mesh nodes by geographic area. Each region has an anchor point (lat/lon) and nodes within the region radius are automatically assigned. Regions enable localized reports, alerts, and health scoring." />
            </label>
            {data.regions.map((region, i) => (
              <div key={i} className="border border-[#1e2a3a] overflow-hidden">
                <div
                  className="flex items-center justify-between p-3 bg-[#0a0e17] cursor-pointer"
                  onClick={() => setExpandedRegion(expandedRegion === i ? null : i)}
                >
                  <div className="flex items-center gap-3">
                    {expandedRegion === i ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                    <span className="font-medium text-slate-200">{region.name || 'Unnamed Region'}</span>
                    <span className="text-xs text-slate-500">{region.local_name}</span>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      if (confirm(`Delete region "${region.name || 'Unnamed Region'}"?`)) {
                        const newRegions = data.regions.filter((_, j) => j !== i)
                        onChange({ ...data, regions: newRegions })
                      }
                    }}
                    className="p-1 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
                {expandedRegion === i && (
                  <div className="p-4 space-y-3 border-t border-[#1e2a3a]">
                    <div className="grid grid-cols-2 gap-4">
                      <TextInput label="Name" value={region.name} onChange={(v) => {
                        const newRegions = [...data.regions]
                        newRegions[i] = { ...region, name: v }
                        onChange({ ...data, regions: newRegions })
                      }} />
                      <TextInput label="Local Name" value={region.local_name} onChange={(v) => {
                        const newRegions = [...data.regions]
                        newRegions[i] = { ...region, local_name: v }
                        onChange({ ...data, regions: newRegions })
                      }} />
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <NumberInput label="Latitude" value={region.lat} onChange={(v) => {
                        const newRegions = [...data.regions]
                        newRegions[i] = { ...region, lat: v }
                        onChange({ ...data, regions: newRegions })
                      }} step={0.0001} />
                      <NumberInput label="Longitude" value={region.lon} onChange={(v) => {
                        const newRegions = [...data.regions]
                        newRegions[i] = { ...region, lon: v }
                        onChange({ ...data, regions: newRegions })
                      }} step={0.0001} />
                    </div>
                    <TextInput label="Description" value={region.description} onChange={(v) => {
                      const newRegions = [...data.regions]
                      newRegions[i] = { ...region, description: v }
                      onChange({ ...data, regions: newRegions })
                    }} />
                    <ListInput label="Aliases" value={region.aliases} onChange={(v) => {
                      const newRegions = [...data.regions]
                      newRegions[i] = { ...region, aliases: v }
                      onChange({ ...data, regions: newRegions })
                    }} />
                    <ListInput label="Cities" value={region.cities} onChange={(v) => {
                      const newRegions = [...data.regions]
                      newRegions[i] = { ...region, cities: v }
                      onChange({ ...data, regions: newRegions })
                    }} />
                  </div>
                )}
              </div>
            ))}
            <button
              onClick={() => {
                const newRegion: RegionAnchor = {
                  name: '',
                  local_name: '',
                  lat: 0,
                  lon: 0,
                  description: '',
                  aliases: [],
                  cities: [],
                }
                onChange({ ...data, regions: [...data.regions, newRegion] })
                setExpandedRegion(data.regions.length)
              }}
              className="w-full py-2 border border-dashed border-[#1e2a3a] text-slate-500 hover:text-slate-300 hover:border-accent flex items-center justify-center gap-2 transition-colors"
            >
              <Plus size={16} /> Add Region
            </button>
          </div>

          <div className="space-y-3">
            <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
              Alert Rules
              <InfoButton info="Configure which conditions trigger alerts. Each rule can have an optional threshold value." />
            </label>

            <div className="space-y-2">
              <h4 className="text-xs text-slate-400 font-medium">Infrastructure</h4>
              <AlertRuleToggle
                label="Infra Offline"
                description="Alert when an infrastructure node (router/repeater) goes offline"
                checked={data.alert_rules.infra_offline}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_offline: v } })}
              />
              <AlertRuleToggle
                label="Infra Recovery"
                description="Alert when an offline infrastructure node comes back online"
                checked={data.alert_rules.infra_recovery}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_recovery: v } })}
              />
              <AlertRuleToggle
                label="New Router"
                description="Alert when a new router/repeater appears on the mesh"
                checked={data.alert_rules.new_router}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, new_router: v } })}
              />
              <AlertRuleToggle
                label="Feeder Offline"
                description="Alert when a data source (MeshView/MeshMonitor) stops responding"
                checked={data.alert_rules.feeder_offline}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, feeder_offline: v } })}
              />
              <AlertRuleToggle
                label="Single Gateway"
                description="Alert when an infrastructure node has only one connection path"
                checked={data.alert_rules.infra_single_gateway}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_single_gateway: v } })}
              />
              <AlertRuleToggle
                label="Region Blackout"
                description="Alert when all infrastructure in a region goes offline"
                checked={data.alert_rules.region_total_blackout}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, region_total_blackout: v } })}
              />
            </div>

            <div className="space-y-2">
              <h4 className="text-xs text-slate-400 font-medium">Power</h4>
              <AlertRuleToggle
                label="Battery Warning"
                description="Alert when infra node battery drops below warning threshold"
                checked={data.alert_rules.battery_warning}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_warning: v } })}
                threshold={data.alert_rules.battery_warning_threshold}
                onThresholdChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_warning_threshold: v } })}
                thresholdLabel="Below"
                thresholdMin={10}
                thresholdMax={90}
                thresholdSuffix="%"
              />
              <AlertRuleToggle
                label="Battery Critical"
                description="Alert at critical battery level"
                checked={data.alert_rules.battery_critical}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_critical: v } })}
                threshold={data.alert_rules.battery_critical_threshold}
                onThresholdChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_critical_threshold: v } })}
                thresholdLabel="Below"
                thresholdMin={5}
                thresholdMax={50}
                thresholdSuffix="%"
              />
              <AlertRuleToggle
                label="Battery Emergency"
                description="Alert at emergency battery level"
                checked={data.alert_rules.battery_emergency}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_emergency: v } })}
                threshold={data.alert_rules.battery_emergency_threshold}
                onThresholdChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_emergency_threshold: v } })}
                thresholdLabel="Below"
                thresholdMin={1}
                thresholdMax={25}
                thresholdSuffix="%"
              />
              <AlertRuleToggle
                label="Battery Trend Declining"
                description="Alert when battery shows a declining trend over 7 days"
                checked={data.alert_rules.battery_trend_declining}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_trend_declining: v } })}
              />
              <AlertRuleToggle
                label="Power Source Change"
                description="Alert when a node switches between battery and USB power"
                checked={data.alert_rules.power_source_change}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, power_source_change: v } })}
              />
              <AlertRuleToggle
                label="Solar Not Charging"
                description="Alert when a solar-powered node isn't charging during daylight"
                checked={data.alert_rules.solar_not_charging}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, solar_not_charging: v } })}
              />
            </div>

            <div className="space-y-2">
              <h4 className="text-xs text-slate-400 font-medium">Utilization</h4>
              <AlertRuleToggle
                label="High Utilization"
                description="Alert when channel utilization stays high for extended periods"
                checked={data.alert_rules.sustained_high_util}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, sustained_high_util: v } })}
                threshold={data.alert_rules.high_util_threshold}
                onThresholdChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, high_util_threshold: v } })}
                thresholdLabel="Above"
                thresholdMin={5}
                thresholdMax={50}
                thresholdSuffix={`% for ${data.alert_rules.high_util_hours}h`}
              />
              <AlertRuleToggle
                label="Packet Flood"
                description="Alert when a single node sends excessive packets"
                checked={data.alert_rules.packet_flood}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, packet_flood: v } })}
                threshold={data.alert_rules.packet_flood_threshold}
                onThresholdChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, packet_flood_threshold: v } })}
                thresholdLabel="Over"
                thresholdMin={100}
                thresholdMax={2000}
                thresholdSuffix="pkts/24h"
              />
            </div>

            <div className="space-y-2">
              <h4 className="text-xs text-slate-400 font-medium">Health Scores</h4>
              <AlertRuleToggle
                label="Mesh Score Alert"
                description="Alert when overall mesh health score drops below threshold"
                checked={data.alert_rules.mesh_score_alert}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, mesh_score_alert: v } })}
                threshold={data.alert_rules.mesh_score_threshold}
                onThresholdChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, mesh_score_threshold: v } })}
                thresholdLabel="Below"
                thresholdMin={30}
                thresholdMax={90}
                thresholdSuffix="/100"
              />
              <AlertRuleToggle
                label="Region Score Alert"
                description="Alert when a region's health score drops below threshold"
                checked={data.alert_rules.region_score_alert}
                onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, region_score_alert: v } })}
                threshold={data.alert_rules.region_score_threshold}
                onThresholdChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, region_score_threshold: v } })}
                thresholdLabel="Below"
                thresholdMin={30}
                thresholdMax={90}
                thresholdSuffix="/100"
              />
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function DashboardSection({ data, onChange }: { data: DashboardConfig; onChange: (d: DashboardConfig) => void }) {
  return (
    <div className="space-y-4">
      <SectionDescription text={SECTION_DESCRIPTIONS.dashboard} />
      <Toggle
        label="Enable Dashboard"
        checked={data.enabled}
        onChange={(v) => onChange({ ...data, enabled: v })}
        helper="Run the web dashboard"
      />
      {data.enabled && (
        <div className="grid grid-cols-2 gap-4">
          <TextInput
            label="Host"
            value={data.host}
            onChange={(v) => onChange({ ...data, host: v })}
            placeholder="0.0.0.0"
            helper="Network bind address"
            info="0.0.0.0 = accessible from any device on the network. 127.0.0.1 = only accessible from this machine."
          />
          <NumberInput
            label="Port"
            value={data.port}
            onChange={(v) => onChange({ ...data, port: v })}
            min={1}
            max={65535}
            helper="Dashboard URL port"
            info="Port number for the web dashboard URL. You access the dashboard at http://your-ip:port"
          />
        </div>
      )}
    </div>
  )
}

export default function Config() {
  const { setDirty } = useDirty()
  const [config, setConfig] = useState<FullConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<FullConfig | null>(null)
  const [activeSection, setActiveSection] = useState<SectionKey>('bot')
  const [searchParams] = useSearchParams()

  // Deep-link support: nav items like /config?section=connection pre-select a
  // section. Runs on mount and whenever the query param changes.
  useEffect(() => {
    const section = searchParams.get('section')
    if (section && SECTIONS.some((s) => s.key === section)) {
      setActiveSection(section as SectionKey)
    }
  }, [searchParams])
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
    document.title = 'Config — MeshAI'
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
      setDirty(false)

      if (result.restart_required) {
        setRestartRequired(true)
        // v0.6-tail-3: surface the cross-page banner with the changed-key list.
        notifyRestartRequired(Array.isArray(result.changed_keys) ? result.changed_keys : [])
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
      case 'response': return <ResponseSection data={config.response} onChange={(d) => updateSection('response', d)} />
      case 'history': return <HistorySection data={config.history} onChange={(d) => updateSection('history', d)} />
      case 'memory': return <MemorySection data={config.memory} onChange={(d) => updateSection('memory', d)} />
      case 'context': return <ContextSection data={config.context} onChange={(d) => updateSection('context', d)} />
      case 'commands': return <CommandsSection data={config.commands} onChange={(d) => updateSection('commands', d)} />
      case 'llm': return <LLMSection data={config.llm} onChange={(d) => updateSection('llm', d)} />
      case 'weather': return <WeatherSection data={config.weather} onChange={(d) => updateSection('weather', d)} />
      case 'knowledge': return <KnowledgeSection data={config.knowledge} onChange={(d) => updateSection('knowledge', d)} />
      case 'mesh_intelligence': return <MeshIntelligenceSection data={config.mesh_intelligence} onChange={(d) => updateSection('mesh_intelligence', d)} />
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
          <div className="flex items-center justify-between p-3 mb-4 bg-amber-500/10 border border-amber-500/30">
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
          <div className="flex items-center gap-2 p-3 mb-4 bg-red-500/10 border border-red-500/30 text-red-400">
            <X size={16} />
            <span className="text-sm">{error}</span>
          </div>
        )}

        {success && (
          <div className="flex items-center gap-2 p-3 mb-4 bg-green-500/10 border border-green-500/30 text-green-400">
            <Check size={16} />
            <span className="text-sm">{success}</span>
          </div>
        )}

        <div className="flex-1 overflow-y-auto pr-2">
          <div className="bg-bg-card border border-border p-6">
            {renderSection()}
          </div>
        </div>
      </div>
    </div>
  )
}
