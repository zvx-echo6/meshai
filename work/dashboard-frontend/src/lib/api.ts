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
  // Only populated by the REST /api/health response; the websocket
  // health_update push includes neither of these (see mesh_routes.py).
  recommendations?: string[]
  // false when the recommendations engine couldn't run (unwired reporter or
  // an exception) — distinct from a genuinely empty `recommendations` list.
  // Must NOT be treated the same as "mesh is healthy".
  recommendations_available?: boolean
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

export interface SerialPort {
  device: string
  by_id?: string | null
  by_path?: string | null
  stable_path: string
  description?: string
  vid?: number | null
  pid?: number | null
  serial_number?: string | null
  manufacturer?: string | null
  product?: string | null
  likely_radio: boolean
}

export interface SerialPortsResponse {
  ports: SerialPort[]
  note: string
}

export async function getSerialPorts(): Promise<SerialPortsResponse> {
  return fetchJson<SerialPortsResponse>('/api/serial-ports')
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

// ---- Generic (no-code) data sources ------------------------------------

export interface FieldMapping {
  source_path: string
  dest_key: string
}

export interface GenericSource {
  name: string
  enabled: boolean
  url: string
  items_path: string
  id_path: string
  lat_path?: string
  lon_path?: string
  geometry_path?: string
  title_path?: string
  time_path?: string
  category: string
  poll_seconds: number
  severity: string
  field_mappings: FieldMapping[]
  summary_template?: string
  emoji?: string
  // Optional custom request headers (e.g. User-Agent override or Authorization).
  // Empty/absent = the default browser UA. Sent on both poll and preview.
  headers?: Record<string, string>
}

export interface GenericSourcePreview {
  ok: boolean
  status?: number
  error?: string
  sample?: string
  item_count?: number | null
  first_item?: string
  items_path_note?: string
}

export async function fetchGenericSources(): Promise<GenericSource[]> {
  return fetchJson<GenericSource[]>('/api/config/generic_sources')
}

export async function saveGenericSources(
  sources: GenericSource[]
): Promise<{ saved: boolean; restart_required: boolean; changed_keys?: string[] }> {
  const response = await fetch('/api/config/generic_sources', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(sources),
  })
  const result = await response.json()
  if (!response.ok) {
    throw new Error((result as { detail?: string }).detail || `Save failed (${response.status})`)
  }
  return result
}

export async function previewGenericSource(
  url: string,
  items_path?: string,
  headers?: Record<string, string>
): Promise<GenericSourcePreview> {
  const response = await fetch('/api/generic-sources/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, items_path, headers }),
  })
  // The endpoint never raises; it returns {ok:false,error} on failure too.
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

export async function fetchActivity(
  limit = 100,
  transport?: string,
  category?: string,
): Promise<ActivityEntry[]> {
  const params = new URLSearchParams()
  params.set('limit', limit.toString())
  if (transport && transport !== 'all') params.set('transport', transport)
  if (category && category !== 'all') params.set('category', category)
  return fetchJson<ActivityEntry[]>(`/api/activity?${params.toString()}`)
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

// MeshCore channels with on-air hash + PSK key (routes by channel NAME, no slot/index).
export interface MeshcoreChannelDetail { name: string; hash: string | null; key: string | null }
export interface MeshcoreChannelsDetail {
  active: boolean
  channels: MeshcoreChannelDetail[]
}

export async function getMeshcoreChannelsDetail(): Promise<MeshcoreChannelsDetail> {
  return fetchJson<MeshcoreChannelsDetail>('/api/meshcore/channels/detail')
}

// Provision a new MeshCore channel on the companion. `key` is a 32-char hex
// PSK (16 bytes); omit it for a public channel (name must start with "#").
// Throws (with the backend's `detail` message) on failure — e.g. duplicate
// name, no free slot, bad key, or MeshCore not connected.
export async function addMeshcoreChannel(
  name: string,
  key?: string,
): Promise<MeshcoreChannels> {
  const response = await fetch('/api/meshcore/channels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, key: key || undefined }),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || `API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}

// Remove a provisioned MeshCore channel from the companion by name.
export async function removeMeshcoreChannel(name: string): Promise<MeshcoreChannels> {
  const response = await fetch(`/api/meshcore/channels/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || `API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}

// MeshCore room servers (type-3 contacts). A routing cell targets a room with
// the value ``room:<pubkey>`` (vs a bare channel name for channel targets).
// ``active:false`` / [] when MeshCore is not connected.
export interface MeshcoreRoom {
  name: string | null
  pubkey: string
  prefix: string
  path_established: boolean
  password_set?: boolean
}
export interface MeshcoreRooms {
  active: boolean
  rooms: MeshcoreRoom[]
}

export async function getMeshcoreRooms(): Promise<MeshcoreRooms> {
  return fetchJson<MeshcoreRooms>('/api/meshcore/rooms')
}

// Set (or clear, if password is empty) a MeshCore room server's login password.
// The value is never read back — only a boolean status is returned.
export async function setRoomPassword(
  pubkey: string,
  password: string,
): Promise<{ ok: boolean; password_set: boolean }> {
  const response = await fetch(`/api/meshcore/room-password/${encodeURIComponent(pubkey)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value: password }),
  })
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}

// Clear a MeshCore room server's stored login password.
export async function clearRoomPassword(
  pubkey: string,
): Promise<{ ok: boolean; password_set: boolean }> {
  const response = await fetch(`/api/meshcore/room-password/${encodeURIComponent(pubkey)}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}

export interface MeshcoreContact {
  name: string | null
  pubkey: string
  type: number | null
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
  last_advert_sent?: number | null  // epoch seconds; null/absent = never advertised
}

export async function fetchMeshcoreContacts(): Promise<MeshcoreContacts> {
  return fetchJson<MeshcoreContacts>('/api/meshcore/contacts')
}
export async function fetchMeshcoreSelf(): Promise<MeshcoreSelf> {
  return fetchJson<MeshcoreSelf>('/api/meshcore/self')
}

export async function sendMeshcoreAdvert(): Promise<TestSendResult> {
  const response = await fetch('/api/meshcore/advert', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }
  return response.json()
}

// --- MeshCore telemetry ---

export interface MeshcoreTelemetryData {
  voltage?: number; temperature?: number; humidity?: number; battery_pct?: number;
  current?: number; illuminance?: number; barometer?: number; power?: number;
  altitude?: number; distance?: number; gps?: unknown;
  raw?: unknown[];
  [key: string]: unknown;
}
export interface MeshcoreTelemetryEntry {
  contact: string;
  data: MeshcoreTelemetryData | null;
  polled_at: string | null;
  available: boolean;
}
export interface MeshcoreTelemetry { active: boolean; entries: MeshcoreTelemetryEntry[]; }
export interface MeshcorePollResult { available: boolean; contact: string; data?: MeshcoreTelemetryData; detail?: string; }

// Connection config subset the telemetry UI reads/writes. The rest of the
// connection object is preserved verbatim via the index signature so PUTs can
// send the WHOLE object back (the backend coerces the body into the full
// ConnectionConfig dataclass — a partial PUT would reset omitted fields).
export interface ConnectionConfig {
  meshcore_telemetry_contacts?: string[]
  meshcore_telemetry_interval_seconds?: number
  [key: string]: unknown
}

export async function fetchConnectionConfig(): Promise<ConnectionConfig> {
  return fetchJson<ConnectionConfig>('/api/config/connection')
}

export async function fetchMeshcoreTelemetry(): Promise<MeshcoreTelemetry> {
  return fetchJson<MeshcoreTelemetry>('/api/meshcore/telemetry')
}

export async function pollMeshcoreContact(contact: string): Promise<MeshcorePollResult> {
  const response = await fetch('/api/meshcore/telemetry/poll', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contact }),
  })
  if (!response.ok) throw new Error(`API error: ${response.status} ${response.statusText}`)
  return response.json()
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
