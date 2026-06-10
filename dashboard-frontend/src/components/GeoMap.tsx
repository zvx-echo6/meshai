import { useEffect, useMemo } from 'react'
import { MapContainer, TileLayer, CircleMarker, Polyline, Popup, Tooltip, useMap } from 'react-leaflet'
import type { LatLngBoundsExpression, LatLngTuple } from 'leaflet'
import 'leaflet/dist/leaflet.css'
import type { NodeInfo, EdgeInfo } from '@/lib/api'
import { ExternalLink, MapPin } from 'lucide-react'

// Fix Leaflet default marker icon issue with Vite
import L from 'leaflet'
import markerIcon from 'leaflet/dist/images/marker-icon.png'
import markerIcon2x from 'leaflet/dist/images/marker-icon-2x.png'
import markerShadow from 'leaflet/dist/images/marker-shadow.png'

// @ts-expect-error - Leaflet icon fix
delete L.Icon.Default.prototype._getIconUrl
L.Icon.Default.mergeOptions({
  iconUrl: markerIcon,
  iconRetinaUrl: markerIcon2x,
  shadowUrl: markerShadow,
})

interface GeoMapProps {
  nodes: NodeInfo[]
  edges: EdgeInfo[]
  selectedNodeId: number | null
  onSelectNode: (nodeId: number | null) => void
}

const REGION_COLORS = ['#3b82f6', '#a78bfa', '#06b6d4', '#f59e0b', '#22c55e', '#ec4899', '#8b5cf6', '#14b8a6']
const INFRA_ROLES = ['ROUTER', 'ROUTER_LATE', 'REPEATER', 'TRACKER']

function getQualityColor(snr: number): string {
  if (snr > 12) return '#22c55e'
  if (snr > 8) return '#4ade80'
  if (snr > 5) return '#f59e0b'
  if (snr > 3) return '#f97316'
  return '#ef4444'
}

function getRegionIndex(lat: number | null): number {
  if (lat === null) return 0
  if (lat > 46) return 0
  if (lat > 44.5) return 1
  if (lat > 43) return 2
  return 3
}

function formatLastHeard(lastHeard: string | null): string {
  if (!lastHeard) return 'Unknown'
  const date = new Date(lastHeard)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)

  if (diffMins < 1) return 'Just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  return `${diffDays}d ago`
}

// Component to fit bounds on mount
function FitBounds({ bounds }: { bounds: LatLngBoundsExpression | null }) {
  const map = useMap()

  useEffect(() => {
    if (bounds) {
      map.fitBounds(bounds, { padding: [50, 50] })
    }
  }, [map, bounds])

  return null
}

interface NodePopupProps {
  node: NodeInfo
}

function NodePopup({ node }: NodePopupProps) {
  const hasCoords = node.latitude !== null && node.longitude !== null
  const batteryText = node.battery_level !== null
    ? (node.battery_level > 100 || (node.voltage && node.voltage > 4.1) ? 'USB ⚡' : `${node.battery_level.toFixed(0)}%`)
    : 'Unknown'

  return (
    <div className="min-w-[200px]">
      <div className="font-semibold text-slate-800">{node.short_name}</div>
      <div className="text-xs text-slate-600 mb-2">{node.long_name}</div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <div className="text-slate-500">Role</div>
        <div className="text-slate-700 font-medium">{node.role}</div>

        <div className="text-slate-500">Hardware</div>
        <div className="text-slate-700">{node.hardware || 'Unknown'}</div>

        <div className="text-slate-500">Battery</div>
        <div className="text-slate-700">{batteryText}</div>

        <div className="text-slate-500">Last Heard</div>
        <div className="text-slate-700">{formatLastHeard(node.last_heard)}</div>
      </div>

      {hasCoords && (
        <div className="mt-3 pt-2 border-t border-slate-200 flex gap-2">
          <a
            href={`https://www.google.com/maps?q=${node.latitude},${node.longitude}`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800"
          >
            <ExternalLink size={10} />
            Google Maps
          </a>
          <a
            href={`https://www.openstreetmap.org/?mlat=${node.latitude}&mlon=${node.longitude}&zoom=14`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800"
          >
            <ExternalLink size={10} />
            OSM
          </a>
        </div>
      )}
    </div>
  )
}

export default function GeoMap({
  nodes,
  edges,
  selectedNodeId,
  onSelectNode,
}: GeoMapProps) {
  // Filter nodes with valid coordinates
  const geoNodes = useMemo(() =>
    nodes.filter((n) => n.latitude !== null && n.longitude !== null),
    [nodes]
  )

  const nodesWithoutCoords = nodes.length - geoNodes.length

  // Create node map for edge lookup
  const nodeMap = useMemo(() =>
    new Map(geoNodes.map((n) => [n.node_num, n])),
    [geoNodes]
  )

  // Filter edges where both nodes have coordinates
  const geoEdges = useMemo(() =>
    edges.filter((e) => nodeMap.has(e.from_node) && nodeMap.has(e.to_node)),
    [edges, nodeMap]
  )

  // Calculate bounds
  const bounds = useMemo((): LatLngBoundsExpression | null => {
    if (geoNodes.length === 0) return null
    const lats = geoNodes.map((n) => n.latitude!)
    const lons = geoNodes.map((n) => n.longitude!)
    return [
      [Math.min(...lats), Math.min(...lons)],
      [Math.max(...lats), Math.max(...lons)],
    ]
  }, [geoNodes])

  // Default center (Idaho)
  const defaultCenter: LatLngTuple = [43.6, -114.4]

  // Get neighbors of selected node
  const selectedNeighbors = useMemo(() => {
    const neighbors = new Set<number>()
    if (selectedNodeId !== null) {
      edges.forEach((e) => {
        if (e.from_node === selectedNodeId) neighbors.add(e.to_node)
        if (e.to_node === selectedNodeId) neighbors.add(e.from_node)
      })
    }
    return neighbors
  }, [selectedNodeId, edges])

  return (
    <div className="relative bg-bg-card rounded-lg border border-border overflow-hidden">
      <MapContainer
        center={defaultCenter}
        zoom={7}
        style={{ width: '100%', height: '540px' }}
        className="z-0"
      >
        <TileLayer
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>, &copy; <a href="https://carto.com/attributions">CARTO</a>'
        />

        <FitBounds bounds={bounds} />

        {/* Edges */}
        {geoEdges.map((edge, i) => {
          const fromNode = nodeMap.get(edge.from_node)!
          const toNode = nodeMap.get(edge.to_node)!
          const isRelated = selectedNodeId === null ||
            edge.from_node === selectedNodeId ||
            edge.to_node === selectedNodeId

          return (
            <Polyline
              key={i}
              positions={[
                [fromNode.latitude!, fromNode.longitude!],
                [toNode.latitude!, toNode.longitude!],
              ]}
              color={getQualityColor(edge.snr)}
              weight={isRelated && selectedNodeId !== null ? 2.5 : 1.5}
              opacity={selectedNodeId === null ? 0.3 : (isRelated ? 0.6 : 0.08)}
            />
          )
        })}

        {/* Nodes */}
        {geoNodes.map((node) => {
          const isSelected = node.node_num === selectedNodeId
          const isNeighbor = selectedNeighbors.has(node.node_num)
          const isRelated = selectedNodeId === null || isSelected || isNeighbor
          const isInfra = INFRA_ROLES.includes(node.role)
          const regionIndex = getRegionIndex(node.latitude)
          const color = REGION_COLORS[regionIndex % REGION_COLORS.length]

          return (
            <CircleMarker
              key={node.node_num}
              center={[node.latitude!, node.longitude!]}
              radius={isInfra ? 8 : 5}
              fillColor={isInfra ? color : '#111827'}
              fillOpacity={isRelated ? 0.9 : 0.2}
              stroke={true}
              color={isSelected ? '#ffffff' : color}
              weight={isSelected ? 3 : isInfra ? 0 : 2}
              opacity={isRelated ? 1 : 0.3}
              eventHandlers={{
                click: () => onSelectNode(isSelected ? null : node.node_num),
              }}
            >
              <Tooltip direction="top" offset={[0, -8]}>
                <span className="font-mono text-xs">{node.short_name}</span>
              </Tooltip>
              <Popup>
                <NodePopup node={node} />
              </Popup>
            </CircleMarker>
          )
        })}
      </MapContainer>

      {/* Stats overlay */}
      <div className="absolute bottom-4 left-4 bg-bg-card/90 backdrop-blur-sm border border-border rounded px-3 py-2 text-xs text-slate-400 flex items-center gap-2">
        <MapPin size={12} />
        <span>
          Showing {geoNodes.length} of {nodes.length} nodes
          {nodesWithoutCoords > 0 && (
            <span className="text-slate-500"> ({nodesWithoutCoords} without coordinates)</span>
          )}
        </span>
      </div>
    </div>
  )
}
