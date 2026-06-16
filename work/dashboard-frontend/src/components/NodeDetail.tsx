import { useMemo } from 'react'
import { ExternalLink, Radio, Zap } from 'lucide-react'
import type { NodeInfo, EdgeInfo } from '@/lib/api'

interface NodeDetailProps {
  node: NodeInfo | null
  edges: EdgeInfo[]
  nodes: NodeInfo[]
  onSelectNode: (nodeId: number) => void
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

function getQualityLabel(snr: number): string {
  if (snr > 12) return 'excellent'
  if (snr > 8) return 'good'
  if (snr > 5) return 'fair'
  if (snr > 3) return 'marginal'
  return 'poor'
}

function getRegionIndex(lat: number | null): number {
  if (lat === null) return 0
  if (lat > 46) return 0
  if (lat > 44.5) return 1
  if (lat > 43) return 2
  return 3
}

function getRegionName(index: number): string {
  const names = ['Northern ID', 'Central ID', 'SW Idaho', 'SC Idaho']
  return names[index] || 'Unknown'
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

function getStatusColor(lastHeard: string | null): string {
  if (!lastHeard) return 'bg-slate-500'
  const date = new Date(lastHeard)
  const now = new Date()
  const diffHours = (now.getTime() - date.getTime()) / 3600000
  if (diffHours < 1) return 'bg-green-500'
  if (diffHours < 24) return 'bg-amber-500'
  return 'bg-slate-500'
}

export default function NodeDetail({
  node,
  edges,
  nodes,
  onSelectNode,
}: NodeDetailProps) {
  // Get neighbors with edge info
  const neighbors = useMemo(() => {
    if (!node) return []

    const nodeMap = new Map(nodes.map((n) => [n.node_num, n]))
    const neighborData: Array<{
      node: NodeInfo
      snr: number
      quality: string
    }> = []

    edges.forEach((e) => {
      if (e.from_node === node.node_num) {
        const neighbor = nodeMap.get(e.to_node)
        if (neighbor) {
          neighborData.push({ node: neighbor, snr: e.snr, quality: e.quality })
        }
      } else if (e.to_node === node.node_num) {
        const neighbor = nodeMap.get(e.from_node)
        if (neighbor) {
          neighborData.push({ node: neighbor, snr: e.snr, quality: e.quality })
        }
      }
    })

    // SNR quality bands (also the legend behind the colored quality dots):
  //   >12 excellent — reliable mesh hop
  //   8-12 good
  //   5-8 fair — works in clear conditions
  //   3-5 marginal — will drop under load
  //   <3 poor — intermittent
  // Sort by SNR descending
    return neighborData.sort((a, b) => b.snr - a.snr)
  }, [node, edges, nodes])

  if (!node) {
    return (
      <div className="w-[250px] flex-shrink-0 bg-bg-card border-l border-border p-4 flex flex-col items-center justify-center h-[540px]">
        <div className="w-12 h-12 rounded-full bg-bg-hover border border-border flex items-center justify-center mb-3">
          <Radio size={24} className="text-slate-500" />
        </div>
        <p className="text-sm text-slate-500 text-center">
          Click a node to inspect
        </p>
      </div>
    )
  }

  const isInfra = INFRA_ROLES.includes(node.role)
  const regionIndex = getRegionIndex(node.latitude)
  const regionColor = REGION_COLORS[regionIndex % REGION_COLORS.length]
  const hasCoords = node.latitude !== null && node.longitude !== null
  const batteryText = node.battery_level !== null
    ? (node.battery_level > 100 || (node.voltage && node.voltage > 4.1) ? 'USB' : `${node.battery_level.toFixed(0)}%`)
    : '—'
  const isPowered = node.battery_level !== null && (node.battery_level > 100 || (node.voltage && node.voltage > 4.1))

  return (
    <div className="w-[250px] flex-shrink-0 bg-bg-card border-l border-border flex flex-col h-[540px] overflow-hidden">
      {/* Header */}
      <div className="p-4 border-b border-border">
        {/* Node ID badge */}
        <div
          className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono mb-2"
          style={{ backgroundColor: `${regionColor}20`, color: regionColor }}
        >
          {node.node_id_hex}
        </div>

        {/* Name */}
        <div className="font-mono text-lg text-slate-100">{node.short_name}</div>
        <div className="text-xs text-slate-500 truncate">{node.long_name}</div>
      </div>

      {/* Info grid */}
      <div className="p-4 border-b border-border grid grid-cols-2 gap-3">
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Role</div>
          <div className={`text-sm font-medium ${isInfra ? 'text-accent' : 'text-slate-300'}`}>
            {node.role}
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Region</div>
          <div className="text-sm text-slate-300">{getRegionName(regionIndex)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Battery</div>
          <div className="text-sm text-slate-300 flex items-center gap-1">
            {isPowered && <Zap size={12} className="text-amber-400" />}
            {batteryText}
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500 mb-0.5">Status</div>
          <div className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${getStatusColor(node.last_heard)}`} />
            <span className="text-sm text-slate-300">{formatLastHeard(node.last_heard)}</span>
          </div>
        </div>
        <div className="col-span-2">
          <div className="text-xs text-slate-500 mb-0.5">Hardware</div>
          <div className="text-sm text-slate-300 font-mono truncate">
            {node.hardware || 'Unknown'}
          </div>
        </div>
      </div>

      {/* External links */}
      {hasCoords && (
        <div className="px-4 py-3 border-b border-border flex gap-3">
          <a
            href={`https://www.google.com/maps?q=${node.latitude},${node.longitude}`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300"
          >
            <ExternalLink size={10} />
            Google Maps
          </a>
          <a
            href={`https://www.openstreetmap.org/?mlat=${node.latitude}&mlon=${node.longitude}&zoom=14`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300"
          >
            <ExternalLink size={10} />
            OSM
          </a>
        </div>
      )}

      {/* Neighbors */}
      <div className="flex-1 overflow-y-auto">
        <div className="px-4 py-2 text-xs text-slate-500 font-medium sticky top-0 bg-bg-card border-b border-border">
          Neighbors ({neighbors.length})
        </div>
        {neighbors.length > 0 ? (
          <div className="divide-y divide-border">
            {neighbors.map((n) => (
              <button
                key={n.node.node_num}
                onClick={() => onSelectNode(n.node.node_num)}
                className="w-full px-4 py-2 text-left hover:bg-bg-hover transition-colors flex items-center gap-2"
                style={{ borderLeftWidth: 3, borderLeftColor: getQualityColor(n.snr) }}
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-slate-200 font-mono truncate">
                    {n.node.short_name}
                  </div>
                  <div className="text-xs text-slate-500 truncate">
                    {n.node.long_name}
                  </div>
                </div>
                <div className="text-right flex-shrink-0">
                  <div className="text-xs font-mono" style={{ color: getQualityColor(n.snr) }}>
                    {n.snr.toFixed(1)} dB
                  </div>
                  <div className="text-xs text-slate-500">
                    {getQualityLabel(n.snr)}
                  </div>
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div className="px-4 py-6 text-center text-sm text-slate-500">
            No known neighbors
          </div>
        )}
      </div>
    </div>
  )
}
