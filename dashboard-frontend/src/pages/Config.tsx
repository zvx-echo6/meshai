import { useState, useEffect, useCallback } from 'react'
import {
  Settings, Bot, Wifi, MessageSquare, Database, Brain, Eye,
  Terminal, Cpu, Cloud, Radio, BookOpen, Layers, Activity,
  Thermometer, LayoutDashboard, Save, RotateCcw, RefreshCw,
  Plus, Trash2, ChevronDown, ChevronRight, AlertTriangle,
  Check, X, Eye as EyeIcon, EyeOff
} from 'lucide-react'

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
  // MQTT-specific fields
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
function TextInput({ label, value, onChange, type = 'text', placeholder = '', helper = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
  helper?: string
}) {
  const [showPassword, setShowPassword] = useState(false)
  const isPassword = type === 'password'

  return (
    <div className="space-y-1">
      <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
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

function NumberInput({ label, value, onChange, min, max, step = 1, helper = '' }: {
  label: string
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
  helper?: string
}) {
  return (
    <div className="space-y-1">
      <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
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

function Toggle({ label, checked, onChange, helper = '' }: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  helper?: string
}) {
  return (
    <div className="flex items-center justify-between py-2">
      <div>
        <span className="text-sm text-slate-300">{label}</span>
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

function SelectInput({ label, value, onChange, options, helper = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
  helper?: string
}) {
  return (
    <div className="space-y-1">
      <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
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

function TextArea({ label, value, onChange, rows = 4, helper = '' }: {
  label: string
  value: string
  onChange: (v: string) => void
  rows?: number
  helper?: string
}) {
  return (
    <div className="space-y-1">
      <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
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

function ListInput({ label, value, onChange, helper = '' }: {
  label: string
  value: string[]
  onChange: (v: string[]) => void
  helper?: string
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
      <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
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

function NumberListInput({ label, value, onChange, helper = '' }: {
  label: string
  value: number[]
  onChange: (v: number[]) => void
  helper?: string
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
      <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
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

// Section renderers
function BotSection({ data, onChange }: { data: BotConfig; onChange: (d: BotConfig) => void }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <TextInput label="Bot Name" value={data.name} onChange={(v) => onChange({ ...data, name: v })} />
        <TextInput label="Owner" value={data.owner} onChange={(v) => onChange({ ...data, owner: v })} />
      </div>
      <Toggle label="Respond to DMs" checked={data.respond_to_dms} onChange={(v) => onChange({ ...data, respond_to_dms: v })} />
      <Toggle label="Filter BBS Protocols" checked={data.filter_bbs_protocols} onChange={(v) => onChange({ ...data, filter_bbs_protocols: v })} />
    </div>
  )
}

function ConnectionSection({ data, onChange }: { data: ConnectionConfig; onChange: (d: ConnectionConfig) => void }) {
  return (
    <div className="space-y-4">
      <SelectInput
        label="Connection Type"
        value={data.type}
        onChange={(v) => onChange({ ...data, type: v })}
        options={[
          { value: 'serial', label: 'Serial' },
          { value: 'tcp', label: 'TCP' },
        ]}
      />
      {data.type === 'serial' ? (
        <TextInput label="Serial Port" value={data.serial_port} onChange={(v) => onChange({ ...data, serial_port: v })} placeholder="/dev/ttyUSB0" />
      ) : (
        <div className="grid grid-cols-2 gap-4">
          <TextInput label="TCP Host" value={data.tcp_host} onChange={(v) => onChange({ ...data, tcp_host: v })} placeholder="192.168.1.100" />
          <NumberInput label="TCP Port" value={data.tcp_port} onChange={(v) => onChange({ ...data, tcp_port: v })} min={1} max={65535} />
        </div>
      )}
    </div>
  )
}

function ResponseSection({ data, onChange }: { data: ResponseConfig; onChange: (d: ResponseConfig) => void }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <NumberInput label="Delay Min (sec)" value={data.delay_min} onChange={(v) => onChange({ ...data, delay_min: v })} min={0} step={0.1} />
        <NumberInput label="Delay Max (sec)" value={data.delay_max} onChange={(v) => onChange({ ...data, delay_max: v })} min={0} step={0.1} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <NumberInput label="Max Length" value={data.max_length} onChange={(v) => onChange({ ...data, max_length: v })} min={50} max={500} />
        <NumberInput label="Max Messages" value={data.max_messages} onChange={(v) => onChange({ ...data, max_messages: v })} min={1} max={10} />
      </div>
    </div>
  )
}

function HistorySection({ data, onChange }: { data: HistoryConfig; onChange: (d: HistoryConfig) => void }) {
  return (
    <div className="space-y-4">
      <TextInput label="Database Path" value={data.database} onChange={(v) => onChange({ ...data, database: v })} />
      <div className="grid grid-cols-2 gap-4">
        <NumberInput label="Max Messages Per User" value={data.max_messages_per_user} onChange={(v) => onChange({ ...data, max_messages_per_user: v })} min={0} helper="0 = unlimited" />
        <NumberInput label="Conversation Timeout (sec)" value={data.conversation_timeout} onChange={(v) => onChange({ ...data, conversation_timeout: v })} min={0} />
      </div>
      <Toggle label="Auto Cleanup" checked={data.auto_cleanup} onChange={(v) => onChange({ ...data, auto_cleanup: v })} />
      {data.auto_cleanup && (
        <div className="grid grid-cols-2 gap-4">
          <NumberInput label="Cleanup Interval (hours)" value={data.cleanup_interval_hours} onChange={(v) => onChange({ ...data, cleanup_interval_hours: v })} min={1} />
          <NumberInput label="Max Age (days)" value={data.max_age_days} onChange={(v) => onChange({ ...data, max_age_days: v })} min={1} />
        </div>
      )}
    </div>
  )
}

function MemorySection({ data, onChange }: { data: MemoryConfig; onChange: (d: MemoryConfig) => void }) {
  return (
    <div className="space-y-4">
      <Toggle label="Enable Memory Optimization" checked={data.enabled} onChange={(v) => onChange({ ...data, enabled: v })} />
      {data.enabled && (
        <div className="grid grid-cols-2 gap-4">
          <NumberInput label="Window Size" value={data.window_size} onChange={(v) => onChange({ ...data, window_size: v })} min={1} helper="Recent message pairs to keep in full" />
          <NumberInput label="Summarize Threshold" value={data.summarize_threshold} onChange={(v) => onChange({ ...data, summarize_threshold: v })} min={1} helper="Messages before re-summarizing" />
        </div>
      )}
    </div>
  )
}

function ContextSection({ data, onChange }: { data: ContextConfig; onChange: (d: ContextConfig) => void }) {
  return (
    <div className="space-y-4">
      <Toggle label="Enable Passive Context" checked={data.enabled} onChange={(v) => onChange({ ...data, enabled: v })} />
      {data.enabled && (
        <>
          <NumberListInput label="Observe Channels" value={data.observe_channels} onChange={(v) => onChange({ ...data, observe_channels: v })} helper="Empty = all channels" />
          <ListInput label="Ignore Nodes" value={data.ignore_nodes} onChange={(v) => onChange({ ...data, ignore_nodes: v })} helper="Node IDs to ignore" />
          <div className="grid grid-cols-2 gap-4">
            <NumberInput label="Max Age (sec)" value={data.max_age} onChange={(v) => onChange({ ...data, max_age: v })} min={0} />
            <NumberInput label="Max Context Items" value={data.max_context_items} onChange={(v) => onChange({ ...data, max_context_items: v })} min={1} />
          </div>
        </>
      )}
    </div>
  )
}

function CommandsSection({ data, onChange }: { data: CommandsConfig; onChange: (d: CommandsConfig) => void }) {
  return (
    <div className="space-y-4">
      <Toggle label="Enable Commands" checked={data.enabled} onChange={(v) => onChange({ ...data, enabled: v })} />
      {data.enabled && (
        <>
          <TextInput label="Command Prefix" value={data.prefix} onChange={(v) => onChange({ ...data, prefix: v })} />
          <ListInput label="Disabled Commands" value={data.disabled_commands} onChange={(v) => onChange({ ...data, disabled_commands: v })} helper="Commands to disable" />
        </>
      )}
    </div>
  )
}

function LLMSection({ data, onChange }: { data: LLMConfig; onChange: (d: LLMConfig) => void }) {
  return (
    <div className="space-y-4">
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
        />
        <TextInput label="Model" value={data.model} onChange={(v) => onChange({ ...data, model: v })} placeholder="gpt-4o-mini" />
      </div>
      <TextInput label="API Key" value={data.api_key} onChange={(v) => onChange({ ...data, api_key: v })} type="password" helper="Supports ${ENV_VAR} syntax" />
      <TextInput label="Base URL" value={data.base_url} onChange={(v) => onChange({ ...data, base_url: v })} placeholder="https://api.openai.com/v1" />
      <div className="grid grid-cols-2 gap-4">
        <NumberInput label="Timeout (sec)" value={data.timeout} onChange={(v) => onChange({ ...data, timeout: v })} min={5} max={120} />
        <NumberInput label="Max Response Tokens" value={data.max_response_tokens} onChange={(v) => onChange({ ...data, max_response_tokens: v })} min={100} />
      </div>
      <Toggle label="Use System Prompt" checked={data.use_system_prompt} onChange={(v) => onChange({ ...data, use_system_prompt: v })} />
      {data.use_system_prompt && (
        <TextArea label="System Prompt" value={data.system_prompt} onChange={(v) => onChange({ ...data, system_prompt: v })} rows={6} />
      )}
      <Toggle label="Web Search" checked={data.web_search} onChange={(v) => onChange({ ...data, web_search: v })} helper="Open WebUI feature" />
      <Toggle label="Google Grounding" checked={data.google_grounding} onChange={(v) => onChange({ ...data, google_grounding: v })} helper="Gemini only" />
    </div>
  )
}

function WeatherSection({ data, onChange }: { data: WeatherConfig; onChange: (d: WeatherConfig) => void }) {
  return (
    <div className="space-y-4">
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
        />
      </div>
      <TextInput label="Default Location" value={data.default_location} onChange={(v) => onChange({ ...data, default_location: v })} placeholder="Twin Falls, ID" />
    </div>
  )
}

function MeshMonitorSection({ data, onChange }: { data: MeshMonitorConfig; onChange: (d: MeshMonitorConfig) => void }) {
  return (
    <div className="space-y-4">
      <Toggle label="Enable MeshMonitor" checked={data.enabled} onChange={(v) => onChange({ ...data, enabled: v })} />
      {data.enabled && (
        <>
          <TextInput label="URL" value={data.url} onChange={(v) => onChange({ ...data, url: v })} placeholder="http://192.168.1.100:8080" />
          <Toggle label="Inject Into Prompt" checked={data.inject_into_prompt} onChange={(v) => onChange({ ...data, inject_into_prompt: v })} helper="Tell LLM about MeshMonitor commands" />
          <NumberInput label="Refresh Interval (sec)" value={data.refresh_interval} onChange={(v) => onChange({ ...data, refresh_interval: v })} min={10} />
          <Toggle label="Polite Mode" checked={data.polite_mode} onChange={(v) => onChange({ ...data, polite_mode: v })} helper="Reduces polling frequency for shared instances" />
        </>
      )}
    </div>
  )
}

function KnowledgeSection({ data, onChange }: { data: KnowledgeConfig; onChange: (d: KnowledgeConfig) => void }) {
  return (
    <div className="space-y-4">
      <Toggle label="Enable Knowledge Base" checked={data.enabled} onChange={(v) => onChange({ ...data, enabled: v })} />
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
          />
          {(data.backend === 'qdrant' || data.backend === 'auto') && (
            <>
              <div className="grid grid-cols-2 gap-4">
                <TextInput label="Qdrant Host" value={data.qdrant_host} onChange={(v) => onChange({ ...data, qdrant_host: v })} />
                <NumberInput label="Qdrant Port" value={data.qdrant_port} onChange={(v) => onChange({ ...data, qdrant_port: v })} />
              </div>
              <TextInput label="Collection" value={data.qdrant_collection} onChange={(v) => onChange({ ...data, qdrant_collection: v })} />
              <Toggle label="Use Sparse Embeddings" checked={data.use_sparse} onChange={(v) => onChange({ ...data, use_sparse: v })} />
            </>
          )}
          <TextInput label="SQLite DB Path" value={data.db_path} onChange={(v) => onChange({ ...data, db_path: v })} />
          <NumberInput label="Top K Results" value={data.top_k} onChange={(v) => onChange({ ...data, top_k: v })} min={1} max={20} />
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
            <TextInput label="Name" value={source.name} onChange={(v) => onChange({ ...source, name: v })} />
            <SelectInput
              label="Type"
              value={source.type}
              onChange={(v) => onChange({ ...source, type: v })}
              options={[
                { value: 'meshview', label: 'MeshView' },
                { value: 'meshmonitor', label: 'MeshMonitor' },
                { value: 'mqtt', label: 'MQTT Broker' },
              ]}
            />
          </div>
          {source.type !== 'mqtt' && (
            <TextInput label="URL" value={source.url} onChange={(v) => onChange({ ...source, url: v })} />
          )}
          {source.type === 'meshmonitor' && (
            <TextInput label="API Token" value={source.api_token} onChange={(v) => onChange({ ...source, api_token: v })} type="password" />
          )}
          {source.type === 'mqtt' && (
            <>
              <div className="grid grid-cols-2 gap-4">
                <TextInput label="Host" value={source.host || ''} onChange={(v) => onChange({ ...source, host: v })} />
                <NumberInput label="Port" value={source.port || 1883} onChange={(v) => onChange({ ...source, port: v })} min={1} max={65535} />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <TextInput label="Username" value={source.username || ''} onChange={(v) => onChange({ ...source, username: v })} />
                <TextInput label="Password" value={source.password || ''} onChange={(v) => onChange({ ...source, password: v })} type="password" />
              </div>
              <TextInput label="Topic Root" value={source.topic_root || 'msh/US'} onChange={(v) => onChange({ ...source, topic_root: v })} />
              <Toggle label="Use TLS" checked={source.use_tls || false} onChange={(v) => onChange({ ...source, use_tls: v })} />
            </>
          )}
          <NumberInput label="Refresh Interval (sec)" value={source.refresh_interval} onChange={(v) => onChange({ ...source, refresh_interval: v })} min={10} />
          <Toggle label="Enabled" checked={source.enabled} onChange={(v) => onChange({ ...source, enabled: v })} />
          <Toggle label="Polite Mode" checked={source.polite_mode} onChange={(v) => onChange({ ...source, polite_mode: v })} />
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
      <Toggle label="Enable Mesh Intelligence" checked={data.enabled} onChange={(v) => onChange({ ...data, enabled: v })} />

      {data.enabled && (
        <>
          <div className="grid grid-cols-2 gap-4">
            <NumberInput label="Locality Radius (miles)" value={data.locality_radius_miles} onChange={(v) => onChange({ ...data, locality_radius_miles: v })} min={1} step={0.5} />
            <NumberInput label="Offline Threshold (hours)" value={data.offline_threshold_hours} onChange={(v) => onChange({ ...data, offline_threshold_hours: v })} min={1} />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <NumberInput label="Packet Threshold" value={data.packet_threshold} onChange={(v) => onChange({ ...data, packet_threshold: v })} min={0} helper="Per 24h to flag" />
            <NumberInput label="Battery Warning %" value={data.battery_warning_percent} onChange={(v) => onChange({ ...data, battery_warning_percent: v })} min={1} max={100} />
          </div>

          <ListInput label="Critical Nodes" value={data.critical_nodes} onChange={(v) => onChange({ ...data, critical_nodes: v })} helper="Short names of critical nodes (e.g., MHR, HPR)" />

          <div className="grid grid-cols-2 gap-4">
            <NumberInput label="Alert Channel" value={data.alert_channel} onChange={(v) => onChange({ ...data, alert_channel: v })} min={-1} helper="-1 = disabled" />
            <NumberInput label="Alert Cooldown (min)" value={data.alert_cooldown_minutes} onChange={(v) => onChange({ ...data, alert_cooldown_minutes: v })} min={1} />
          </div>

          <div className="space-y-2">
            <label className="block text-xs text-slate-500 uppercase tracking-wide">Regions</label>
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
          </div>

          <div className="space-y-2">
            <label className="block text-xs text-slate-500 uppercase tracking-wide mb-3">Alert Rules</label>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1">
              <Toggle label="Infra Offline" checked={data.alert_rules.infra_offline} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_offline: v } })} />
              <Toggle label="Infra Recovery" checked={data.alert_rules.infra_recovery} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_recovery: v } })} />
              <Toggle label="New Router" checked={data.alert_rules.new_router} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, new_router: v } })} />
              <Toggle label="Battery Warning" checked={data.alert_rules.battery_warning} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_warning: v } })} />
              <Toggle label="Battery Critical" checked={data.alert_rules.battery_critical} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_critical: v } })} />
              <Toggle label="Battery Emergency" checked={data.alert_rules.battery_emergency} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, battery_emergency: v } })} />
              <Toggle label="Power Source Change" checked={data.alert_rules.power_source_change} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, power_source_change: v } })} />
              <Toggle label="Solar Not Charging" checked={data.alert_rules.solar_not_charging} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, solar_not_charging: v } })} />
              <Toggle label="High Utilization" checked={data.alert_rules.sustained_high_util} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, sustained_high_util: v } })} />
              <Toggle label="Packet Flood" checked={data.alert_rules.packet_flood} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, packet_flood: v } })} />
              <Toggle label="Single Gateway" checked={data.alert_rules.infra_single_gateway} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, infra_single_gateway: v } })} />
              <Toggle label="Region Blackout" checked={data.alert_rules.region_total_blackout} onChange={(v) => onChange({ ...data, alert_rules: { ...data.alert_rules, region_total_blackout: v } })} />
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
      <Toggle label="Enable Environmental Feeds" checked={data.enabled} onChange={(v) => onChange({ ...data, enabled: v })} />

      {data.enabled && (
        <>
          <ListInput label="NWS Zones" value={data.nws_zones} onChange={(v) => onChange({ ...data, nws_zones: v })} helper="Zone IDs like IDZ016, IDZ030" />

          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">NWS Weather Alerts</span>
              <Toggle label="" checked={data.nws.enabled} onChange={(v) => onChange({ ...data, nws: { ...data.nws, enabled: v } })} />
            </div>
            {data.nws.enabled && (
              <>
                <TextInput label="User Agent" value={data.nws.user_agent} onChange={(v) => onChange({ ...data, nws: { ...data.nws, user_agent: v } })} helper="Required format: (app_name, contact_email)" />
                <div className="grid grid-cols-2 gap-4">
                  <NumberInput label="Tick Seconds" value={data.nws.tick_seconds} onChange={(v) => onChange({ ...data, nws: { ...data.nws, tick_seconds: v } })} min={30} />
                  <SelectInput
                    label="Min Severity"
                    value={data.nws.severity_min}
                    onChange={(v) => onChange({ ...data, nws: { ...data.nws, severity_min: v } })}
                    options={[
                      { value: 'minor', label: 'Minor' },
                      { value: 'moderate', label: 'Moderate' },
                      { value: 'severe', label: 'Severe' },
                      { value: 'extreme', label: 'Extreme' },
                    ]}
                  />
                </div>
              </>
            )}
          </div>

          <div className="border border-[#1e2a3a] rounded-lg p-4">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">NOAA Space Weather (SWPC)</span>
              <Toggle label="" checked={data.swpc.enabled} onChange={(v) => onChange({ ...data, swpc: { ...data.swpc, enabled: v } })} />
            </div>
          </div>

          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">Tropospheric Ducting</span>
              <Toggle label="" checked={data.ducting.enabled} onChange={(v) => onChange({ ...data, ducting: { ...data.ducting, enabled: v } })} />
            </div>
            {data.ducting.enabled && (
              <div className="grid grid-cols-3 gap-4">
                <NumberInput label="Tick Seconds" value={data.ducting.tick_seconds} onChange={(v) => onChange({ ...data, ducting: { ...data.ducting, tick_seconds: v } })} min={60} />
                <NumberInput label="Latitude" value={data.ducting.latitude} onChange={(v) => onChange({ ...data, ducting: { ...data.ducting, latitude: v } })} step={0.01} />
                <NumberInput label="Longitude" value={data.ducting.longitude} onChange={(v) => onChange({ ...data, ducting: { ...data.ducting, longitude: v } })} step={0.01} />
              </div>
            )}
          </div>

          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">NIFC Fire Perimeters</span>
              <Toggle label="" checked={data.fires.enabled} onChange={(v) => onChange({ ...data, fires: { ...data.fires, enabled: v } })} />
            </div>
            {data.fires.enabled && (
              <div className="grid grid-cols-2 gap-4">
                <NumberInput label="Tick Seconds" value={data.fires.tick_seconds} onChange={(v) => onChange({ ...data, fires: { ...data.fires, tick_seconds: v } })} min={60} />
                <TextInput label="State" value={data.fires.state} onChange={(v) => onChange({ ...data, fires: { ...data.fires, state: v } })} placeholder="US-ID" />
              </div>
            )}
          </div>

          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">Avalanche Advisories</span>
              <Toggle label="" checked={data.avalanche.enabled} onChange={(v) => onChange({ ...data, avalanche: { ...data.avalanche, enabled: v } })} />
            </div>
            {data.avalanche.enabled && (
              <>
                <NumberInput label="Tick Seconds" value={data.avalanche.tick_seconds} onChange={(v) => onChange({ ...data, avalanche: { ...data.avalanche, tick_seconds: v } })} min={60} />
                <ListInput label="Center IDs" value={data.avalanche.center_ids} onChange={(v) => onChange({ ...data, avalanche: { ...data.avalanche, center_ids: v } })} />
                <NumberListInput label="Season Months" value={data.avalanche.season_months} onChange={(v) => onChange({ ...data, avalanche: { ...data.avalanche, season_months: v } })} helper="e.g., 12, 1, 2, 3, 4" />
              </>
            )}
          </div>

          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">USGS Stream Gauges</span>
              <Toggle label="" checked={data.usgs?.enabled || false} onChange={(v) => onChange({ ...data, usgs: { ...data.usgs, enabled: v, tick_seconds: data.usgs?.tick_seconds || 900, sites: data.usgs?.sites || [] } })} />
            </div>
            {data.usgs?.enabled && (
              <>
                <NumberInput label="Tick Seconds" value={data.usgs.tick_seconds} onChange={(v) => onChange({ ...data, usgs: { ...data.usgs, tick_seconds: v } })} min={900} />
                <ListInput label="Site IDs" value={data.usgs.sites} onChange={(v) => onChange({ ...data, usgs: { ...data.usgs, sites: v } })} helper="Find IDs at waterdata.usgs.gov/nwis" />
              </>
            )}
          </div>

          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">TomTom Traffic</span>
              <Toggle label="" checked={data.traffic?.enabled || false} onChange={(v) => onChange({ ...data, traffic: { ...data.traffic, enabled: v, tick_seconds: data.traffic?.tick_seconds || 300, api_key: data.traffic?.api_key || '', corridors: data.traffic?.corridors || [] } })} />
            </div>
            {data.traffic?.enabled && (
              <>
                <TextInput label="API Key" value={data.traffic.api_key} onChange={(v) => onChange({ ...data, traffic: { ...data.traffic, api_key: v } })} type="password" helper="Get key at developer.tomtom.com" />
                <NumberInput label="Tick Seconds" value={data.traffic.tick_seconds} onChange={(v) => onChange({ ...data, traffic: { ...data.traffic, tick_seconds: v } })} min={60} />
                <div className="text-xs text-slate-500 mt-2">Corridors (each with name, lat, lon):</div>
                {(data.traffic.corridors || []).map((c, i) => (
                  <div key={i} className="grid grid-cols-4 gap-2 items-end">
                    <TextInput label="Name" value={c.name} onChange={(v) => {
                      const newCorridors = [...data.traffic.corridors]
                      newCorridors[i] = { ...c, name: v }
                      onChange({ ...data, traffic: { ...data.traffic, corridors: newCorridors } })
                    }} />
                    <NumberInput label="Lat" value={c.lat} onChange={(v) => {
                      const newCorridors = [...data.traffic.corridors]
                      newCorridors[i] = { ...c, lat: v }
                      onChange({ ...data, traffic: { ...data.traffic, corridors: newCorridors } })
                    }} step={0.01} />
                    <NumberInput label="Lon" value={c.lon} onChange={(v) => {
                      const newCorridors = [...data.traffic.corridors]
                      newCorridors[i] = { ...c, lon: v }
                      onChange({ ...data, traffic: { ...data.traffic, corridors: newCorridors } })
                    }} step={0.01} />
                    <button
                      onClick={() => onChange({ ...data, traffic: { ...data.traffic, corridors: data.traffic.corridors.filter((_, j) => j !== i) } })}
                      className="px-2 py-1 text-xs text-red-400 hover:text-red-300"
                    >Remove</button>
                  </div>
                ))}
                <button
                  onClick={() => onChange({ ...data, traffic: { ...data.traffic, corridors: [...(data.traffic.corridors || []), { name: '', lat: 0, lon: 0 }] } })}
                  className="text-xs text-accent hover:underline"
                >+ Add Corridor</button>
              </>
            )}
          </div>

          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">511 Road Conditions</span>
              <Toggle label="" checked={data.roads511?.enabled || false} onChange={(v) => onChange({ ...data, roads511: { ...data.roads511, enabled: v, tick_seconds: data.roads511?.tick_seconds || 300, api_key: data.roads511?.api_key || '', base_url: data.roads511?.base_url || '', endpoints: data.roads511?.endpoints || ['/get/event'], bbox: data.roads511?.bbox || [] } })} />
            </div>
            {data.roads511?.enabled && (
              <>
                <TextInput label="Base URL" value={data.roads511.base_url} onChange={(v) => onChange({ ...data, roads511: { ...data.roads511, base_url: v } })} placeholder="https://511.yourstate.gov/api/v2" />
                <TextInput label="API Key" value={data.roads511.api_key} onChange={(v) => onChange({ ...data, roads511: { ...data.roads511, api_key: v } })} type="password" helper="Leave empty if not required" />
                <NumberInput label="Tick Seconds" value={data.roads511.tick_seconds} onChange={(v) => onChange({ ...data, roads511: { ...data.roads511, tick_seconds: v } })} min={60} />
                <ListInput label="Endpoints" value={data.roads511.endpoints} onChange={(v) => onChange({ ...data, roads511: { ...data.roads511, endpoints: v } })} helper="e.g., /get/event, /get/mountainpasses" />
                <div className="grid grid-cols-4 gap-2">
                  <NumberInput label="West" value={data.roads511.bbox?.[0] || 0} onChange={(v) => {
                    const bbox = [...(data.roads511.bbox || [0, 0, 0, 0])]
                    bbox[0] = v
                    onChange({ ...data, roads511: { ...data.roads511, bbox } })
                  }} step={0.01} />
                  <NumberInput label="South" value={data.roads511.bbox?.[1] || 0} onChange={(v) => {
                    const bbox = [...(data.roads511.bbox || [0, 0, 0, 0])]
                    bbox[1] = v
                    onChange({ ...data, roads511: { ...data.roads511, bbox } })
                  }} step={0.01} />
                  <NumberInput label="East" value={data.roads511.bbox?.[2] || 0} onChange={(v) => {
                    const bbox = [...(data.roads511.bbox || [0, 0, 0, 0])]
                    bbox[2] = v
                    onChange({ ...data, roads511: { ...data.roads511, bbox } })
                  }} step={0.01} />
                  <NumberInput label="North" value={data.roads511.bbox?.[3] || 0} onChange={(v) => {
                    const bbox = [...(data.roads511.bbox || [0, 0, 0, 0])]
                    bbox[3] = v
                    onChange({ ...data, roads511: { ...data.roads511, bbox } })
                  }} step={0.01} />
                </div>
                <div className="text-xs text-slate-500">Bounding box filter (leave all 0 to disable)</div>
              </>
            )}
          </div>

          <div className="border border-[#1e2a3a] rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">NASA FIRMS Satellite Fire Detection</span>
              <Toggle label="" checked={data.firms?.enabled || false} onChange={(v) => onChange({ ...data, firms: { ...data.firms, enabled: v, tick_seconds: data.firms?.tick_seconds || 1800, map_key: data.firms?.map_key || '', source: data.firms?.source || 'VIIRS_SNPP_NRT', bbox: data.firms?.bbox || [], day_range: data.firms?.day_range || 1, confidence_min: data.firms?.confidence_min || 'nominal', proximity_km: data.firms?.proximity_km || 10 } })} />
            </div>
            {data.firms?.enabled && (
              <>
                <TextInput label="MAP Key" value={data.firms.map_key} onChange={(v) => onChange({ ...data, firms: { ...data.firms, map_key: v } })} type="password" helper="Get key at firms.modaps.eosdis.nasa.gov/api/area/" />
                <NumberInput label="Tick Seconds" value={data.firms.tick_seconds} onChange={(v) => onChange({ ...data, firms: { ...data.firms, tick_seconds: v } })} min={300} />
                <SelectInput
                  label="Satellite Source"
                  value={data.firms.source}
                  onChange={(v) => onChange({ ...data, firms: { ...data.firms, source: v } })}
                  options={[
                    { value: 'VIIRS_SNPP_NRT', label: 'VIIRS SNPP (Near Real-Time)' },
                    { value: 'VIIRS_NOAA20_NRT', label: 'VIIRS NOAA-20 (Near Real-Time)' },
                    { value: 'MODIS_NRT', label: 'MODIS (Near Real-Time)' },
                  ]}
                />
                <NumberInput label="Day Range" value={data.firms.day_range} onChange={(v) => onChange({ ...data, firms: { ...data.firms, day_range: v } })} min={1} max={10} helper="1-10 days of data" />
                <SelectInput
                  label="Minimum Confidence"
                  value={data.firms.confidence_min}
                  onChange={(v) => onChange({ ...data, firms: { ...data.firms, confidence_min: v } })}
                  options={[
                    { value: 'low', label: 'Low' },
                    { value: 'nominal', label: 'Nominal' },
                    { value: 'high', label: 'High' },
                  ]}
                />
                <NumberInput label="Proximity (km)" value={data.firms.proximity_km} onChange={(v) => onChange({ ...data, firms: { ...data.firms, proximity_km: v } })} step={0.5} helper="Distance to match known fires" />
                <div className="grid grid-cols-4 gap-2">
                  <NumberInput label="West" value={data.firms.bbox?.[0] || 0} onChange={(v) => {
                    const bbox = [...(data.firms.bbox || [0, 0, 0, 0])]
                    bbox[0] = v
                    onChange({ ...data, firms: { ...data.firms, bbox } })
                  }} step={0.01} />
                  <NumberInput label="South" value={data.firms.bbox?.[1] || 0} onChange={(v) => {
                    const bbox = [...(data.firms.bbox || [0, 0, 0, 0])]
                    bbox[1] = v
                    onChange({ ...data, firms: { ...data.firms, bbox } })
                  }} step={0.01} />
                  <NumberInput label="East" value={data.firms.bbox?.[2] || 0} onChange={(v) => {
                    const bbox = [...(data.firms.bbox || [0, 0, 0, 0])]
                    bbox[2] = v
                    onChange({ ...data, firms: { ...data.firms, bbox } })
                  }} step={0.01} />
                  <NumberInput label="North" value={data.firms.bbox?.[3] || 0} onChange={(v) => {
                    const bbox = [...(data.firms.bbox || [0, 0, 0, 0])]
                    bbox[3] = v
                    onChange({ ...data, firms: { ...data.firms, bbox } })
                  }} step={0.01} />
                </div>
                <div className="text-xs text-slate-500">Bounding box for monitoring area (required)</div>
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
      <Toggle label="Enable Dashboard" checked={data.enabled} onChange={(v) => onChange({ ...data, enabled: v })} />
      {data.enabled && (
        <div className="grid grid-cols-2 gap-4">
          <TextInput label="Host" value={data.host} onChange={(v) => onChange({ ...data, host: v })} placeholder="0.0.0.0" />
          <NumberInput label="Port" value={data.port} onChange={(v) => onChange({ ...data, port: v })} min={1} max={65535} />
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
