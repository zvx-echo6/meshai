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
    coverage: number
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

export interface AlertHistoryItem {
  id?: number
  type: string
  severity: string
  message: string
  timestamp: string
  duration?: number
  scope_type?: string
  scope_value?: string
  resolved_at?: string
}

export interface AlertHistoryResponse {
  items: AlertHistoryItem[]
  total: number
}

export interface ActivityEntry {
  id: number
  sent_at: number | string | null   // epoch seconds (int) on new rows
  recipient: string | null
  channel: string | number | null
  text: string | null
  source_event_table: string | null
  source_event_pk: string | number | null
  bytes_sent: number | null
  ack_received: number | null
  transport: string | null   // 'meshtastic' | 'meshcore' | null (legacy)
  success: number | null     // 1 sent, 0 skip/fail, null legacy
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

// Kp history entry for charting
export interface KpHistoryEntry {
  time: string
  value: number
}

// SFI history entry for charting
export interface SfiHistoryEntry {
  time: string
  value: number
}

// Refractivity profile entry
export interface ProfileEntry {
  level_hPa: number
  height_m: number
  N: number
  M: number
  T_C: number
  RH: number
}

// Gradient entry
export interface GradientEntry {
  from_level: number
  to_level: number
  from_height_m: number
  to_height_m: number
  gradient: number
}

export interface BandConditionsStatus {
  enabled: boolean
  ratings?: {
    "80-40m"?: string
    "30-20m"?: string
    "17-15m"?: string
    "12-10m"?: string
  }
  slot_label?: string
  sent_at?: number
  source?: string
}

// Kept for backward compat references
export type SWPCStatus = BandConditionsStatus

export interface DuctingStatus {
  enabled: boolean
  condition?: string
  min_gradient?: number
  duct_thickness_m?: number | null
  duct_base_m?: number | null
  last_update?: string
  profile?: ProfileEntry[]
  gradients?: GradientEntry[]
  assessment?: string
  location?: { lat: number; lon: number }
}

export interface RFPropagation {
  hf: {
    kp_current?: number
    sfi?: number
    r_scale?: number
    s_scale?: number
    g_scale?: number
    active_warnings?: string[]
    kp_history?: KpHistoryEntry[]
  }
  uhf_ducting: {
    condition?: string
    min_gradient?: number
    duct_thickness_m?: number | null
    profile?: ProfileEntry[]
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

export async function fetchAlertHistory(
  limit: number = 50,
  offset: number = 0,
  type?: string,
  severity?: string
): Promise<AlertHistoryResponse | AlertHistoryItem[]> {
  const params = new URLSearchParams()
  params.set('limit', limit.toString())
  params.set('offset', offset.toString())
  if (type && type !== 'all') params.set('type', type)
  if (severity && severity !== 'all') params.set('severity', severity)
  return fetchJson<AlertHistoryResponse | AlertHistoryItem[]>(`/api/alerts/history?${params.toString()}`)
}

export async function fetchActivity(limit = 100): Promise<ActivityEntry[]> {
  return fetchJson<ActivityEntry[]>(`/api/activity?limit=${limit}`)
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

export async function fetchSWPC(): Promise<BandConditionsStatus> {
  return fetchJson<BandConditionsStatus>('/api/env/swpc')
}

export async function fetchDucting(): Promise<DuctingStatus> {
  return fetchJson<DuctingStatus>('/api/env/ducting')
}

export interface FireEvent {
  source: string
  event_id: string
  event_type: string
  severity: string
  headline: string
  name: string
  acres: number
  pct_contained: number
  lat: number | null
  lon: number | null
  distance_km: number | null
  nearest_anchor: string | null
  state: string
  expires: number
  fetched_at: number
  polygon?: number[][][]
}

export interface AvalancheEvent {
  source: string
  event_id: string
  event_type: string
  severity: string
  headline: string
  zone_name: string
  center: string
  center_id: string
  center_link: string
  forecast_link: string
  danger: string
  danger_level: number
  danger_name: string
  travel_advice: string
  state: string
  lat: number | null
  lon: number | null
  expires: number
  fetched_at: number
}

export interface StreamGaugeEvent {
  source: string
  event_id: string
  event_type: string
  headline: string
  severity: string
  lat?: number
  lon?: number
  expires: number
  fetched_at: number
  properties: {
    site_id: string
    site_name: string
    parameter: string
    value: number
    unit: string
    timestamp: string
  }
}

export interface TrafficEvent {
  source: string
  event_id: string
  event_type: string
  headline: string
  severity: string
  lat?: number
  lon?: number
  expires: number
  fetched_at: number
  properties: {
    corridor: string
    currentSpeed: number
    freeFlowSpeed: number
    speedRatio: number
    currentTravelTime: number
    freeFlowTravelTime: number
    confidence: number
    roadClosure: boolean
  }
}

export interface RoadEvent {
  source: string
  event_id: string
  event_type: string
  headline: string
  description?: string
  severity: string
  lat?: number
  lon?: number
  expires: number
  fetched_at: number
  properties: {
    roadway: string
    is_closure: boolean
    last_updated?: string
  }
}

export interface HotspotEvent {
  source: string
  event_id: string
  event_type: string
  headline: string
  severity: string
  lat?: number
  lon?: number
  expires: number
  fetched_at: number
  properties: {
    new_ignition: boolean
    confidence: string
    frp?: number
    brightness?: number
    acq_date: string
    acq_time: string
    near_fire?: string
    distance_to_fire_km?: number
    distance_km?: number
    nearest_anchor?: string
  }
}

export interface HotspotsResponse {
  enabled: boolean
  hotspots: HotspotEvent[]
  new_ignitions: number
}

export interface AvalancheResponse {
  off_season: boolean
  advisories: AvalancheEvent[]
}

export async function fetchFires(): Promise<FireEvent[]> {
  return fetchJson<FireEvent[]>('/api/env/fires')
}

export async function fetchAvalanche(): Promise<AvalancheResponse> {
  return fetchJson<AvalancheResponse>('/api/env/avalanche')
}

export async function fetchStreams(): Promise<StreamGaugeEvent[]> {
  return fetchJson<StreamGaugeEvent[]>('/api/env/streams')
}

export async function fetchTraffic(): Promise<TrafficEvent[]> {
  return fetchJson<TrafficEvent[]>('/api/env/traffic')
}

export async function fetchRoads(): Promise<RoadEvent[]> {
  return fetchJson<RoadEvent[]>('/api/env/roads')
}

export async function fetchHotspots(): Promise<HotspotsResponse> {
  return fetchJson<HotspotsResponse>('/api/env/hotspots')
}

export async function fetchRegions(): Promise<RegionInfo[]> {
  return fetchJson<RegionInfo[]>('/api/regions')
}

export interface MeshcoreChannels { active: boolean; channels: string[] }
export interface TestSendResult { sent: boolean; detail: string }

export async function getMeshcoreChannels(): Promise<MeshcoreChannels> {
  return fetchJson<MeshcoreChannels>('/api/meshcore/channels')
}

export interface MeshcoreContact {
  name: string | null
  pubkey: string
  type: string | null
  last_advert: number | null
  lat: number | null
  lon: number | null
  out_path_len: number | null
}
export interface MeshcoreContacts {
  active: boolean
  contacts: MeshcoreContact[]
}
export interface MeshcoreSelf {
  name?: string | null
  pubkey?: string | null
  connected: boolean
  host?: string
  port?: number
  channel_count?: number
}

export async function fetchMeshcoreContacts(): Promise<MeshcoreContacts> {
  return fetchJson<MeshcoreContacts>('/api/meshcore/contacts')
}
export async function fetchMeshcoreSelf(): Promise<MeshcoreSelf> {
  return fetchJson<MeshcoreSelf>('/api/meshcore/self')
}

export async function sendTestMessage(body: {
  transport: 'meshtastic' | 'meshcore'
  channel: string | number
  text?: string
}): Promise<TestSendResult> {
  const response = await fetch('/api/mesh/test-send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}
