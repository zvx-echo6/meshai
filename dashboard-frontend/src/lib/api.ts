// API types matching actual backend responses

export interface SystemStatus {
  version: string
  uptime_seconds: number
  bot_name: string
  connection_type: string
  connection_target: string
  connected: boolean
  node_count: number
  source_count: number
  env_feeds_enabled: boolean
  dashboard_port: number
}

export interface MeshHealth {
  score: number
  tier: string
  pillars: {
    infrastructure: number
    utilization: number
    behavior: number
    power: number
  }
  infra_online: number
  infra_total: number
  util_percent: number
  flagged_nodes: number
  battery_warnings: number
  total_nodes: number
  total_regions: number
  unlocated_count: number
  last_computed: string
  recommendations: string[]
}

export interface NodeInfo {
  node_num: number
  node_id_hex: string
  short_name: string
  long_name: string
  role: string
  latitude: number | null
  longitude: number | null
  last_heard: string | null
  battery_level: number | null
  voltage: number | null
  snr: number | null
  firmware: string
  hardware: string
  uptime: number | null
  sources: string[]
}

export interface EdgeInfo {
  from_node: number
  to_node: number
  snr: number
  quality: string
}

export interface RegionInfo {
  name: string
  local_name: string
  node_count: number
  infra_count: number
  infra_online: number
  online_count: number
  score: number
  tier: string
  center_lat: number
  center_lon: number
}

export interface SourceHealth {
  name: string
  type: string
  url: string
  is_loaded: boolean
  last_error: string | null
  consecutive_errors: number
  response_time_ms: number | null
  tick_count: number
  node_count: number
}

export interface Alert {
  type: string
  severity: string
  message: string
  timestamp: string
  scope_type?: string
  scope_value?: string
}

export interface EnvStatus {
  enabled: boolean
  feeds: EnvFeedHealth[]
}

export interface EnvFeedHealth {
  source: string
  is_loaded: boolean
  last_error: string | null
  consecutive_errors: number
  event_count: number
  last_fetch: number
}

export interface EnvEvent {
  source: string
  event_id: string
  event_type: string
  severity: string
  headline: string
  description?: string
  expires?: number
  fetched_at: number
  [key: string]: unknown
}

export interface SWPCStatus {
  enabled: boolean
  kp_current?: number
  kp_timestamp?: string
  sfi?: number
  r_scale?: number
  s_scale?: number
  g_scale?: number
  band_assessment?: string
  band_detail?: string
  active_warnings?: string[]
}

export interface DuctingStatus {
  enabled: boolean
  condition?: string
  min_gradient?: number
  duct_thickness_m?: number | null
  duct_base_m?: number | null
  assessment?: string
  last_update?: string
}

export interface RFPropagation {
  hf: {
    kp_current?: number
    sfi?: number
    r_scale?: number
    s_scale?: number
    g_scale?: number
    band_assessment?: string
    band_detail?: string
    active_warnings?: string[]
  }
  uhf_ducting: {
    condition?: string
    min_gradient?: number
    duct_thickness_m?: number | null
    assessment?: string
  }
}

// API fetch helpers

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}

export async function fetchStatus(): Promise<SystemStatus> {
  return fetchJson<SystemStatus>('/api/status')
}

export async function fetchHealth(): Promise<MeshHealth> {
  return fetchJson<MeshHealth>('/api/health')
}

export async function fetchNodes(): Promise<NodeInfo[]> {
  return fetchJson<NodeInfo[]>('/api/nodes')
}

export async function fetchEdges(): Promise<EdgeInfo[]> {
  return fetchJson<EdgeInfo[]>('/api/edges')
}

export async function fetchSources(): Promise<SourceHealth[]> {
  return fetchJson<SourceHealth[]>('/api/sources')
}

export async function fetchConfig(section?: string): Promise<unknown> {
  const url = section ? `/api/config/${section}` : '/api/config'
  return fetchJson(url)
}

export async function updateConfig(
  section: string,
  data: unknown
): Promise<{ saved: boolean; restart_required: boolean }> {
  const response = await fetch(`/api/config/${section}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}

export async function fetchAlerts(): Promise<Alert[]> {
  return fetchJson<Alert[]>('/api/alerts/active')
}

export async function fetchEnvStatus(): Promise<EnvStatus> {
  return fetchJson<EnvStatus>('/api/env/status')
}

export async function fetchEnvActive(): Promise<EnvEvent[]> {
  return fetchJson<EnvEvent[]>('/api/env/active')
}

export async function fetchRFPropagation(): Promise<RFPropagation> {
  return fetchJson<RFPropagation>('/api/env/propagation')
}

export async function fetchSWPC(): Promise<SWPCStatus> {
  return fetchJson<SWPCStatus>('/api/env/swpc')
}

export async function fetchDucting(): Promise<DuctingStatus> {
  return fetchJson<DuctingStatus>('/api/env/ducting')
}

export async function fetchRegions(): Promise<RegionInfo[]> {
  return fetchJson<RegionInfo[]>('/api/regions')
}
