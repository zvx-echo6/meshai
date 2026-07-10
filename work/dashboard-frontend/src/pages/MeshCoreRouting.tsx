import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { useDirty } from '@/context/DirtyContext'
import { Save, RotateCcw, RefreshCw, Check, MessageSquare, ExternalLink, Home, Hash } from 'lucide-react'
import {
  SeverityChannelMatrix,
  ListInput,
  Toggle,
  InfoButton,
  TOGGLE_FAMILY_META,
  MC_CHANNELS,
  type NotificationToggle,
  type NotificationsConfig,
  type RegionCell,
  type RegionRoutes,
} from './Notifications'
import { getMeshcoreRooms, type MeshcoreRoom } from '@/lib/api'

// Merge only the MeshCore-owned fields of `mine` into `fresh`, preserving every
// other (Meshtastic / Other-channels / general) field on the family. The
// severity matrix stores all channels in one dict per severity, so we keep the
// non-meshcore_* entries from the freshly-fetched config and overlay only the
// meshcore_* entries edited on this page.
function mergeMeshcoreFields(
  fresh: NotificationToggle | undefined,
  mine: NotificationToggle,
  key: string,
): NotificationToggle {
  const base: NotificationToggle = fresh ? { ...fresh } : { ...mine, name: key }
  base.name = base.name || key

  const freshSC = fresh?.severity_channels || {}
  const mineSC = mine.severity_channels || {}
  const severities = new Set([...Object.keys(freshSC), ...Object.keys(mineSC)])
  const mergedSC: Record<string, string[]> = {}
  severities.forEach((sev) => {
    const nonMeshcore = (freshSC[sev] || []).filter((c) => !c.startsWith('meshcore_'))
    const meshcore = (mineSC[sev] || []).filter((c) => c.startsWith('meshcore_'))
    mergedSC[sev] = [...nonMeshcore, ...meshcore]
  })
  base.severity_channels = mergedSC
  base.meshcore_channel = mine.meshcore_channel ?? null
  base.meshcore_dm_contacts = mine.meshcore_dm_contacts || []
  return base
}

// Merge region_routes, overlaying only the MC (.mc) field from mine and
// preserving the MT (.mt) field from fresh. Drops cells where both are null/empty.
function mergeRegionRoutesForMc(
  fresh: RegionRoutes | undefined,
  mine: RegionRoutes | undefined,
): RegionRoutes {
  // This page OWNS mc_enabled (from its own toggle); carry mt_enabled through
  // UNCHANGED from the freshly-fetched server value so we never clobber it.
  const mc_enabled = mine?.mc_enabled ?? false
  const mt_enabled = fresh?.mt_enabled ?? fresh?.enabled ?? false
  const freshCells = fresh?.cells || {}
  const mineCells = mine?.cells || {}

  const allFamilies = new Set([...Object.keys(freshCells), ...Object.keys(mineCells)])
  const newCells: Record<string, Record<string, RegionCell>> = {}

  for (const fam of allFamilies) {
    const freshFam = freshCells[fam] || {}
    const mineFam = mineCells[fam] || {}
    const allRegions = new Set([...Object.keys(freshFam), ...Object.keys(mineFam)])
    const newFam: Record<string, RegionCell> = {}

    for (const region of allRegions) {
      const freshCell = freshFam[region]
      const mineCell = mineFam[region]
      const rawMc = mineCell !== undefined ? (mineCell.mc || null) : (freshCell?.mc ?? null)
      const merged: RegionCell = {
        mt: freshCell?.mt ?? null,
        mc: rawMc,
        min_severity: mineCell?.min_severity ?? freshCell?.min_severity ?? 'routine',
        enabled: mineCell?.enabled ?? freshCell?.enabled ?? true,
      }
      const mcVal = merged.mc
      if (merged.mt !== null || (mcVal !== null && mcVal.trim() !== '')) {
        newFam[region] = merged
      }
    }

    if (Object.keys(newFam).length > 0) {
      newCells[fam] = newFam
    }
  }

  return { mt_enabled, mc_enabled, cells: newCells }
}

// TODO: room password UI needs a backend secrets endpoint. secrets_store.py
// has set/get/clear_room_password() keyed by room pubkey, but NO HTTP route
// exposes them (the generic /api/secrets endpoint is gated to a fixed
// SECRET_LABELS allowlist, so it cannot set a dynamic per-room password).
// Open rooms route fine without a password; password-protected rooms need
// that endpoint before a set/clear affordance can be added here.
export default function MeshCoreRouting() {
  const { setDirty } = useDirty()
  const [config, setConfig] = useState<NotificationsConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<NotificationsConfig | null>(null)
  const [regionNames, setRegionNames] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)
  // MeshCore room servers, for the channel-vs-room cell picker. Rooms are
  // targeted by writing the cell value as `room:<pubkey>`; channels stay bare.
  const [rooms, setRooms] = useState<MeshcoreRoom[]>([])
  const [roomsActive, setRoomsActive] = useState(false)

  // Local map: family key -> explicitly toggled on/off for region expand.
  const [regionExpandedMap, setRegionExpandedMap] = useState<Record<string, boolean | undefined>>({})

  const fetchConfig = useCallback(async () => {
    try {
      const [configRes, regionsRes] = await Promise.all([
        fetch('/api/config/notifications'),
        fetch('/api/notifications/regions'),
      ])
      if (!configRes.ok) throw new Error('Failed to fetch notifications config')
      const data: NotificationsConfig = await configRes.json()
      const regionsData: string[] = regionsRes.ok ? await regionsRes.json() : []
      setConfig(data)
      setOriginalConfig(JSON.parse(JSON.stringify(data)))
      setRegionNames(Array.isArray(regionsData) ? regionsData : [])
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'MeshCore Routing - MeshAI'
    fetchConfig()
  }, [fetchConfig])

  // Load MeshCore room servers once; degrade quietly if MeshCore is offline.
  const fetchRooms = useCallback(async () => {
    try {
      const res = await getMeshcoreRooms()
      setRooms(res.active ? res.rooms : [])
      setRoomsActive(res.active)
    } catch {
      setRooms([])
      setRoomsActive(false)
    }
  }, [])

  useEffect(() => {
    fetchRooms()
  }, [fetchRooms])

  useEffect(() => {
    if (config && originalConfig) {
      setHasChanges(JSON.stringify(config) !== JSON.stringify(originalConfig))
    }
  }, [config, originalConfig])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const upd = (fam: string, patch: Partial<NotificationToggle>) => {
    if (!config) return
    const toggles = config.toggles || {}
    setConfig({
      ...config,
      toggles: {
        ...toggles,
        [fam]: { ...(toggles[fam] || {}), name: fam, ...patch } as NotificationToggle,
      },
    })
  }

  // A cell value is either a bare channel NAME or `room:<pubkey>`.
  const ROOM_PREFIX = 'room:'
  const isRoomValue = (v: string | null | undefined): boolean =>
    typeof v === 'string' && v.startsWith(ROOM_PREFIX)
  const roomPubkeyOf = (v: string): string => v.slice(ROOM_PREFIX.length)
  const roomByPubkey = (pubkey: string): MeshcoreRoom | undefined =>
    rooms.find((r) => r.pubkey === pubkey)

  const setMcForRegion = (family: string, region: string, mc: string | null) => {
    if (!config) return
    const existing: RegionCell = config.region_routes?.cells?.[family]?.[region] ?? {
      mt: null, mc: null, min_severity: 'routine', enabled: true,
    }
    const newCells = {
      ...(config.region_routes?.cells || {}),
      [family]: {
        ...(config.region_routes?.cells?.[family] || {}),
        [region]: { ...existing, mc },
      },
    }
    setConfig({
      ...config,
      region_routes: {
        mt_enabled: config.region_routes?.mt_enabled ?? config.region_routes?.enabled ?? false,
        mc_enabled: config.region_routes?.mc_enabled ?? false,
        cells: newCells,
      },
    })
  }

  const clearMcForFamily = (family: string) => {
    if (!config) return
    const familyCells = config.region_routes?.cells?.[family] || {}
    const clearedFamily: Record<string, RegionCell> = {}
    for (const [r, c] of Object.entries(familyCells)) {
      clearedFamily[r] = { ...c, mc: null }
    }
    const newCells = { ...(config.region_routes?.cells || {}), [family]: clearedFamily }
    setConfig({
      ...config,
      region_routes: {
        mt_enabled: config.region_routes?.mt_enabled ?? config.region_routes?.enabled ?? false,
        mc_enabled: config.region_routes?.mc_enabled ?? false,
        cells: newCells,
      },
    })
  }

  const saveConfig = async () => {
    if (!config) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      // Re-fetch the live config and merge ONLY the MeshCore fields so we never
      // clobber concurrent edits made on the Meshtastic Routing page.
      const freshRes = await fetch('/api/config/notifications')
      if (!freshRes.ok) throw new Error('Failed to re-fetch notifications config')
      const fresh: NotificationsConfig = await freshRes.json()

      const merged: NotificationsConfig = {
        ...fresh,
        toggles: { ...(fresh.toggles || {}) },
        region_routes: mergeRegionRoutesForMc(fresh.region_routes, config.region_routes),
      }
      const myToggles = config.toggles || {}
      for (const { key } of TOGGLE_FAMILY_META) {
        const mine = myToggles[key]
        if (!mine) continue
        merged.toggles![key] = mergeMeshcoreFields((fresh.toggles || {})[key], mine, key)
      }

      const res = await fetch('/api/config/notifications', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(merged),
      })
      const result = await res.json()
      if (!res.ok) throw new Error((result as { detail?: string }).detail || 'Save failed')

      setConfig(merged)
      setOriginalConfig(JSON.parse(JSON.stringify(merged)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('MeshCore routing saved successfully')
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

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading MeshCore routing...</div>
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

  const toggles = config.toggles || {}

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">
            Per-family MeshCore delivery. Choose which channels fire at each severity, the
            MeshCore channel name, and DM contacts.
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

      {/* Cross-link note */}
      <div className="flex items-start gap-2 p-3 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-400">
        <ExternalLink size={16} className="text-accent mt-0.5 flex-shrink-0" />
        <div>
          Family gating (enable, severity threshold, freshness/cooldown) is on{' '}
          <Link to="/environment" className="text-accent hover:underline">
            Data Feeds
          </Link>
          . Meshtastic delivery is on{' '}
          <Link to="/meshtastic/routing" className="text-accent hover:underline">
            Meshtastic Routing
          </Link>
          . This page edits only the MeshCore delivery for each family.
        </div>
      </div>

      {/* Status messages */}
      {error && (
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{error}</div>
      )}
      {success && (
        <div className="p-3 text-sm bg-green-500/10 text-green-400 border border-green-500/20">
          <Check size={14} className="inline mr-2" />
          {success}
        </div>
      )}

      {/* Per-family MeshCore delivery */}
      <div className="bg-bg-card border border-border p-6 space-y-4">
        <div className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
          MeshCore Delivery
          <InfoButton info="For each notification family, choose which MeshCore channels fire at each severity, the MeshCore channel name to broadcast on, and the DM contacts to unicast to. Enabling a family and its severity threshold are configured on the Data Feeds page." />
        </div>
        <div className="border border-[#1e2a3a] p-3">
          <Toggle
            label="Enable MeshCore region routing"
            checked={config.region_routes?.mc_enabled ?? false}
            onChange={(on) =>
              setConfig({
                ...config,
                region_routes: {
                  mt_enabled: config.region_routes?.mt_enabled ?? config.region_routes?.enabled ?? false,
                  mc_enabled: on,
                  cells: config.region_routes?.cells || {},
                },
              })
            }
            helper="Master switch for per-region MeshCore channel routing. When off, families deliver only to their default MeshCore channels."
          />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {TOGGLE_FAMILY_META.map(({ key, label, Icon }) => {
            const t = toggles[key] || ({} as NotificationToggle)
            const familyCells = config.region_routes?.cells?.[key] || {}
            const hasAnyMc = regionNames.some(r => {
              const mc = familyCells[r]?.mc
              return mc !== null && mc !== undefined && mc.trim() !== ''
            })
            const localOverride = regionExpandedMap[key]
            const showRegion = localOverride !== undefined ? localOverride : hasAnyMc

            return (
              <div key={key} className="border border-[#1e2a3a] p-3 space-y-3">
                <div className="flex items-center gap-2 text-sm text-slate-200">
                  <Icon size={15} /> {label}
                </div>

                {/* MC delivery matrix */}
                <div className="space-y-3 p-3 bg-[#0a0e17] border border-[#1e2a3a]">
                  <div className="flex items-center gap-2 text-xs font-medium text-slate-300">
                    <MessageSquare size={13} />
                    MeshCore
                  </div>
                  <SeverityChannelMatrix
                    channels={MC_CHANNELS}
                    severityChannels={t.severity_channels || {}}
                    onChange={(sc) => upd(key, { severity_channels: sc })}
                  />
                  <div className="space-y-1">
                    <label className="text-xs text-slate-500 uppercase tracking-wide">
                      MeshCore channel name
                    </label>
                    <input
                      type="text"
                      value={t.meshcore_channel != null ? t.meshcore_channel : ''}
                      onChange={(e) =>
                        upd(key, { meshcore_channel: e.target.value === '' ? null : e.target.value })
                      }
                      placeholder="AIDA"
                      className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
                    />
                    <p className="text-xs text-slate-600">
                      Channel name on your MeshCore companion (e.g. AIDA). Blank = not broadcast on
                      MeshCore.
                    </p>
                  </div>
                  <ListInput
                    label="MeshCore DM contacts"
                    value={t.meshcore_dm_contacts || []}
                    onChange={(v) => upd(key, { meshcore_dm_contacts: v })}
                    placeholder="contact name or pubkey"
                    helper="MeshCore DM recipients (names or pubkeys)"
                    info="Contact names or pubkeys on the MeshCore companion. Used when meshcore_dm is enabled for a severity."
                  />
                </div>

                {/* Region-based routing expand */}
                <div className="border border-[#1e2a3a] p-3 space-y-2">
                  <Toggle
                    label="Region-based routing"
                    checked={showRegion}
                    onChange={(on) => {
                      setRegionExpandedMap(m => ({ ...m, [key]: on }))
                      if (!on) clearMcForFamily(key)
                    }}
                    helper="Route this family to different MC channels per region"
                  />
                  {showRegion && (
                    regionNames.length === 0 ? (
                      <p className="text-xs text-slate-500 italic">
                        No regions yet — add them on the{' '}
                        <Link to="/coverage" className="text-accent hover:underline">Coverage</Link> page.
                      </p>
                    ) : (
                      <div className="space-y-1.5 pt-1">
                        {regionNames.map(region => {
                          const cell: RegionCell = familyCells[region] ?? {
                            mt: null, mc: null, min_severity: 'routine', enabled: true,
                          }
                          const mcVal = cell.mc ?? ''
                          const targetsRoom = isRoomValue(mcVal)
                          const room = targetsRoom ? roomByPubkey(roomPubkeyOf(mcVal)) : undefined
                          return (
                            <div key={region} className="flex items-center gap-2">
                              <span className="text-xs text-slate-400 flex-1 min-w-0 truncate">{region}</span>
                              {/* Destination type: channel (bare name) vs room (room:<pubkey>) */}
                              <div className="flex border border-[#1e2a3a] rounded overflow-hidden">
                                <button
                                  type="button"
                                  title="Target a channel"
                                  onClick={() => {
                                    // Switching to channel: clear a room value, keep a channel value.
                                    if (targetsRoom) setMcForRegion(key, region, null)
                                  }}
                                  className={`px-1.5 py-1 flex items-center ${
                                    !targetsRoom ? 'bg-accent text-white' : 'text-slate-500 hover:text-slate-300'
                                  }`}
                                >
                                  <Hash size={12} />
                                </button>
                                <button
                                  type="button"
                                  title={roomsActive ? 'Target a room server' : 'MeshCore not connected'}
                                  disabled={!roomsActive && !targetsRoom}
                                  onClick={() => {
                                    // Switching to room: clear a channel value so the room <select>
                                    // starts from its placeholder.
                                    if (!targetsRoom) setMcForRegion(key, region, null)
                                  }}
                                  className={`px-1.5 py-1 flex items-center ${
                                    targetsRoom ? 'bg-accent text-white' : 'text-slate-500 hover:text-slate-300'
                                  } disabled:opacity-40 disabled:cursor-not-allowed`}
                                >
                                  <Home size={12} />
                                </button>
                              </div>
                              {targetsRoom ? (
                                roomsActive || room ? (
                                  <div className="flex items-center gap-1 w-40">
                                    <select
                                      value={room ? room.pubkey : ''}
                                      onChange={(e) => {
                                        const pk = e.target.value
                                        setMcForRegion(key, region, pk === '' ? null : ROOM_PREFIX + pk)
                                      }}
                                      className="flex-1 min-w-0 px-1.5 py-1 bg-[#0a0e17] border border-[#1e2a3a] rounded text-xs text-slate-200 focus:outline-none focus:border-accent"
                                    >
                                      <option value="">room…</option>
                                      {/* Keep an unknown/offline room's pubkey selectable so its value isn't silently dropped. */}
                                      {!room && targetsRoom && (
                                        <option value={roomPubkeyOf(mcVal)}>{roomPubkeyOf(mcVal).slice(0, 10)}…</option>
                                      )}
                                      {rooms.map((r) => (
                                        <option key={r.pubkey} value={r.pubkey}>
                                          {r.name || r.pubkey.slice(0, 10) + '…'}
                                        </option>
                                      ))}
                                    </select>
                                    {room && (
                                      <span
                                        title={room.path_established ? 'Path established' : 'No path yet — first send discovers it'}
                                        className={`text-[10px] ${room.path_established ? 'text-green-500' : 'text-slate-600'}`}
                                      >
                                        ●
                                      </span>
                                    )}
                                  </div>
                                ) : (
                                  <span className="w-40 text-xs text-slate-600 italic truncate">MeshCore not connected</span>
                                )
                              ) : (
                                <input
                                  type="text"
                                  value={mcVal}
                                  onChange={(e) => {
                                    const v = e.target.value
                                    setMcForRegion(key, region, v === '' ? null : v)
                                  }}
                                  placeholder="channel"
                                  className="w-40 px-2 py-1 bg-[#0a0e17] border border-[#1e2a3a] rounded text-xs text-slate-200 font-mono focus:outline-none focus:border-accent"
                                />
                              )}
                            </div>
                          )
                        })}
                      </div>
                    )
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
