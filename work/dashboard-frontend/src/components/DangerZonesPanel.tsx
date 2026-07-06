// DangerZonesPanel — fully isolated, additive feature.
// Loads/saves the standalone `danger_zones` config section via the generic
// /api/config helpers. Has its OWN state, fetch (on mount), and save. It is
// intentionally decoupled from the notifications config so it can never be
// tangled with the existing save logic.
// Extracted from Notifications.tsx — do NOT re-entangle.

import { useState, useEffect, useCallback } from 'react'
import {
  ChevronDown, ChevronRight, AlertTriangle, AlertCircle, Save, Check, Send,
  Activity, Cloud, Flame, Snowflake, Mountain,
} from 'lucide-react'
import { fetchConfig as apiFetchConfig, updateConfig as apiUpdateConfig } from '@/lib/api'
import { Toggle, NumberInput, TextInput, InfoButton } from '@/pages/Notifications'
import NodePicker from '@/components/NodePicker'
import ChannelPicker from '@/components/ChannelPicker'
import { KeyValueInput } from './KeyValueInput'

const DZ_MONITOR_ROLES = ['CLIENT_BASE', 'ROUTER', 'ROUTER_LATE'] as const

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
  tabled?: boolean
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
  min_acres: number
}

interface DangerZonesConfig {
  enabled: boolean
  dry_run: boolean
  monitor_roles: string[]
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
  return { enabled: false, buffer_mi: 5.0, min_acres: 0 }
}

// New-object default. NOTE: delivery_type defaults to mesh_dm (do NOT copy the
// page's mesh_broadcast new-rule default).
function dzDefault(): DangerZonesConfig {
  return {
    enabled: false,
    dry_run: true,
    monitor_roles: ['ROUTER', 'ROUTER_LATE', 'CLIENT_BASE'],
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
  meta: { key: string; label: string; description: string; Icon: typeof Activity; showAcres?: boolean; tabled?: boolean }
  cfg: DangerZoneHazardConfig
  onChange: (c: DangerZoneHazardConfig) => void
}) {
  const { Icon } = meta
  return (
    <div className={`border border-[#1e2a3a] p-3 space-y-2 ${meta.tabled ? 'opacity-50' : ''}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-start gap-2 flex-1">
          <Icon size={15} className="text-slate-400 mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <span className="text-sm text-slate-300">{meta.label}</span>
            <p className="text-xs text-slate-600">{meta.description}</p>
            {meta.tabled && (
              <span className="inline-block mt-1 px-2 py-0.5 text-[10px] uppercase tracking-wide rounded bg-slate-700 text-slate-300">
                Tabled — needs snowfall + elevation pipeline
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          disabled={meta.tabled}
          onClick={() => { if (!meta.tabled) onChange({ ...cfg, enabled: !cfg.enabled }) }}
          className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 ml-3 ${
            cfg.enabled ? 'bg-accent' : 'bg-[#1e2a3a]'
          } ${meta.tabled ? 'cursor-not-allowed' : ''}`}
        >
          <span
            className={`absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform ${
              cfg.enabled ? 'translate-x-5' : ''
            }`}
          />
        </button>
      </div>
      {cfg.enabled && !meta.tabled && (
        <div className={`grid gap-3 pt-2 border-t border-[#1e2a3a] ${meta.showAcres ? 'grid-cols-2' : 'grid-cols-1'}`}>
          <NumberInput
            label="Buffer (mi)"
            value={cfg.buffer_mi ?? 0}
            onChange={(v) => onChange({ ...cfg, buffer_mi: v })}
            min={0}
            step={0.5}
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

export default function DangerZonesPanel() {
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
                  <InfoButton info="Which Meshtastic node roles to correlate against hazards. Only nodes that have a GPS position are scanned." />
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
              <div className="grid grid-cols-2 gap-4">
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
                  <>
                    <TextInput
                      label="Webhook URL"
                      value={cfg.webhook_url || ''}
                      onChange={(v) => upd({ webhook_url: v })}
                      placeholder="https://discord.com/api/webhooks/..."
                      helper="POST alert as JSON"
                    />
                    <KeyValueInput
                      label="Webhook Headers"
                      value={cfg.webhook_headers || {}}
                      onChange={(v) => upd({ webhook_headers: v })}
                      helper="Custom HTTP headers sent with the danger-zone webhook"
                      keyPlaceholder="Header"
                      valuePlaceholder="Value"
                    />
                  </>
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
