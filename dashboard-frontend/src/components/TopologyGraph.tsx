import { useEffect, useRef, useMemo, useState, useCallback } from 'react'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import { Filter } from 'lucide-react'
import type { NodeInfo, EdgeInfo } from '@/lib/api'

interface TopologyGraphProps {
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

function getNodeSize(role: string): number {
  if (role === 'ROUTER' || role === 'ROUTER_LATE') return 30
  if (role === 'REPEATER' || role === 'TRACKER') return 25
  if (role === 'CLIENT_MUTE') return 7
  if (role === 'CLIENT_BASE') return 12
  return 15 // CLIENT and others
}

type FilterMode = 'all' | 'infra' | 'connected'

export default function TopologyGraph({
  nodes,
  edges,
  selectedNodeId,
  onSelectNode,
}: TopologyGraphProps) {
  const chartRef = useRef<ReactECharts>(null)
  const [filterMode, setFilterMode] = useState<FilterMode>('connected')

  // Build set of node IDs that have at least one edge
  const connectedNodeIds = useMemo(() => {
    const ids = new Set<number>()
    edges.forEach((e) => {
      ids.add(e.from_node)
      ids.add(e.to_node)
    })
    return ids
  }, [edges])

  // Filter nodes based on mode
  const filteredNodes = useMemo(() => {
    let result = nodes

    if (filterMode === 'connected') {
      // Only nodes with edges (like Meshview)
      result = result.filter((n) => connectedNodeIds.has(n.node_num))
    } else if (filterMode === 'infra') {
      // Only infrastructure nodes
      result = result.filter((n) => INFRA_ROLES.includes(n.role))
    }

    return result
  }, [nodes, filterMode, connectedNodeIds])

  // Build node map for quick lookup
  const nodeMap = useMemo(() => {
    return new Map(filteredNodes.map((n) => [n.node_num, n]))
  }, [filteredNodes])

  // Filter edges to only include those between filtered nodes
  const filteredEdges = useMemo(() => {
    return edges.filter((e) => nodeMap.has(e.from_node) && nodeMap.has(e.to_node))
  }, [edges, nodeMap])

  // Get neighbors of selected node
  const selectedNeighbors = useMemo(() => {
    const neighbors = new Set<number>()
    if (selectedNodeId !== null) {
      filteredEdges.forEach((e) => {
        if (e.from_node === selectedNodeId) neighbors.add(e.to_node)
        if (e.to_node === selectedNodeId) neighbors.add(e.from_node)
      })
    }
    return neighbors
  }, [selectedNodeId, filteredEdges])

  // Build ECharts data
  const chartData = useMemo(() => {
    const graphNodes = filteredNodes.map((n) => {
      const regionIndex = getRegionIndex(n.latitude)
      const color = REGION_COLORS[regionIndex % REGION_COLORS.length]
      const isInfra = INFRA_ROLES.includes(n.role)
      const isSelected = n.node_num === selectedNodeId
      const isNeighbor = selectedNeighbors.has(n.node_num)
      const isRelated = selectedNodeId === null || isSelected || isNeighbor

      return {
        id: String(n.node_num),
        name: n.short_name,
        value: n.node_num,
        symbolSize: getNodeSize(n.role),
        itemStyle: {
          color: isInfra ? color : '#111827',
          borderColor: color,
          borderWidth: isInfra ? 0 : 2,
          opacity: isRelated ? 1 : 0.15,
        },
        label: {
          show: true,
          position: 'bottom' as const,
          distance: 5,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          color: isRelated ? '#94a3b8' : '#94a3b820',
        },
        // Store extra data for click handler
        nodeNum: n.node_num,
        longName: n.long_name,
        role: n.role,
      }
    })

    const graphLinks = filteredEdges.map((e) => {
      const isRelated = selectedNodeId === null ||
        e.from_node === selectedNodeId ||
        e.to_node === selectedNodeId

      return {
        source: String(e.from_node),
        target: String(e.to_node),
        value: e.snr,
        lineStyle: {
          color: getQualityColor(e.snr),
          width: isRelated && selectedNodeId !== null ? 2 : 1,
          opacity: selectedNodeId === null ? 0.4 : (isRelated ? 0.6 : 0.04),
        },
      }
    })

    return { nodes: graphNodes, links: graphLinks }
  }, [filteredNodes, filteredEdges, selectedNodeId, selectedNeighbors])

  // ECharts option
  const option: EChartsOption = useMemo(() => ({
    backgroundColor: '#111827',
    tooltip: {
      trigger: 'item',
      backgroundColor: '#1e293b',
      borderColor: '#334155',
      textStyle: {
        color: '#e2e8f0',
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 11,
      },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      formatter: (params: any) => {
        if (params.data && params.data.longName) {
          const d = params.data
          return `<strong>${d.name}</strong><br/>${d.longName}<br/>Role: ${d.role}`
        }
        return ''
      },
    },
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        animation: false,
        data: chartData.nodes,
        links: chartData.links,
        force: {
          repulsion: 200,
          edgeLength: [80, 120],
          gravity: 0.1,
        },
        emphasis: {
          focus: 'adjacency',
          blurScope: 'coordinateSystem',
          lineStyle: {
            width: 3,
          },
        },
        label: {
          show: true,
          position: 'bottom',
          distance: 5,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
        },
        edgeLabel: {
          show: false,
        },
        // Selection styling handled via data opacity
      },
    ],
  }), [chartData])

  // Handle chart events
  const onChartClick = useCallback((params: { data?: { nodeNum?: number } }) => {
    if (params.data && 'nodeNum' in params.data) {
      const nodeNum = params.data.nodeNum
      onSelectNode(selectedNodeId === nodeNum ? null : nodeNum ?? null)
    }
  }, [selectedNodeId, onSelectNode])

  const onChartEvents = useMemo(() => ({
    click: onChartClick,
  }), [onChartClick])

  // Update chart when selection changes
  useEffect(() => {
    const chart = chartRef.current?.getEchartsInstance()
    if (chart) {
      chart.setOption(option, { notMerge: false, lazyUpdate: true })
    }
  }, [option])

  return (
    <div className="relative bg-bg-card rounded-lg border border-border overflow-hidden">
      <ReactECharts
        ref={chartRef}
        option={option}
        style={{ height: '540px', width: '100%' }}
        onEvents={onChartEvents}
        opts={{ renderer: 'canvas' }}
      />

      {/* Filter controls */}
      <div className="absolute top-4 left-4 flex items-center gap-2 bg-bg-card/90 backdrop-blur-sm border border-border rounded px-3 py-2">
        <Filter size={14} className="text-slate-500" />
        <div className="flex gap-1">
          {([
            { key: 'connected', label: 'Connected' },
            { key: 'infra', label: 'Infra' },
            { key: 'all', label: 'All' },
          ] as { key: FilterMode; label: string }[]).map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setFilterMode(key)}
              className={`px-2 py-1 text-xs rounded transition-colors ${
                filterMode === key
                  ? 'bg-accent text-white'
                  : 'bg-bg-hover text-slate-400 hover:text-slate-200'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="text-xs text-slate-500 ml-2">
          {filteredNodes.length} nodes • {filteredEdges.length} edges
        </span>
      </div>

      {/* Legend */}
      <div className="absolute bottom-4 left-4 bg-bg-card/90 backdrop-blur-sm border border-border rounded p-3">
        <div className="text-xs text-slate-400 font-medium mb-2">Edge Quality (SNR)</div>
        <div className="space-y-1">
          {[
            { label: 'Excellent (>12)', color: '#22c55e' },
            { label: 'Good (8-12)', color: '#4ade80' },
            { label: 'Fair (5-8)', color: '#f59e0b' },
            { label: 'Marginal (3-5)', color: '#f97316' },
            { label: 'Poor (<3)', color: '#ef4444' },
          ].map((item) => (
            <div key={item.label} className="flex items-center gap-2">
              <div className="w-4 h-0.5" style={{ backgroundColor: item.color }} />
              <span className="text-xs text-slate-500">{item.label}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Node type legend */}
      <div className="absolute bottom-4 right-4 bg-bg-card/90 backdrop-blur-sm border border-border rounded p-3">
        <div className="text-xs text-slate-400 font-medium mb-2">Node Type</div>
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <div className="w-3 h-3 rounded-full bg-blue-500" />
            <span className="text-xs text-slate-500">Infrastructure</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-3 h-3 rounded-full bg-gray-900 border-2 border-blue-500" />
            <span className="text-xs text-slate-500">Client</span>
          </div>
        </div>
      </div>
    </div>
  )
}
