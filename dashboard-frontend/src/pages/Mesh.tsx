import { useEffect, useState, useCallback, useMemo } from 'react'
import { Map, Network } from 'lucide-react'
import {
  fetchNodes,
  fetchEdges,
  fetchRegions,
  type NodeInfo,
  type EdgeInfo,
  type RegionInfo,
} from '@/lib/api'
import TopologyGraph from '@/components/TopologyGraph'
import GeoMap from '@/components/GeoMap'
import NodeDetail from '@/components/NodeDetail'
import NodeTable from '@/components/NodeTable'

type ViewMode = 'topo' | 'geo'

export default function Mesh() {
  const [nodes, setNodes] = useState<NodeInfo[]>([])
  const [edges, setEdges] = useState<EdgeInfo[]>([])
  const [_regions, setRegions] = useState<RegionInfo[]>([])
  const [selectedNodeId, setSelectedNodeId] = useState<number | null>(null)
  const [viewMode, setViewMode] = useState<ViewMode>('topo')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Fetch data on mount
  useEffect(() => {
    document.title = 'Mesh — MeshAI'
    Promise.all([fetchNodes(), fetchEdges(), fetchRegions()])
      .then(([n, e, r]) => {
        setNodes(n)
        setEdges(e)
        setRegions(r)
        setLoading(false)
      })
      .catch((err) => {
        setError(err.message)
        setLoading(false)
      })
  }, [])

  // Get selected node
  const selectedNode = useMemo(
    () => nodes.find((n) => n.node_num === selectedNodeId) || null,
    [nodes, selectedNodeId]
  )

  // Handle node selection
  const handleSelectNode = useCallback((nodeId: number | null) => {
    setSelectedNodeId(nodeId)
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading mesh data...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Error: {error}</div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header with view toggle */}
      <div className="flex items-center justify-between">
        <div className="text-sm text-slate-400">
          {nodes.length} nodes • {edges.length} edges
        </div>

        {/* View toggle */}
        <div className="flex items-center bg-bg-card border border-border rounded-lg p-1">
          <button
            onClick={() => setViewMode('topo')}
            className={`flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors ${
              viewMode === 'topo'
                ? 'bg-accent text-white'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Network size={14} />
            <span title="Force-directed graph of nodes + neighbor links. Edge weight reflects SNR; node color reflects status (green = active, amber = stale, slate = offline).">Topology</span>
          </button>
          <button
            onClick={() => setViewMode('geo')}
            className={`flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors ${
              viewMode === 'geo'
                ? 'bg-accent text-white'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Map size={14} />
            <span title="Nodes plotted by lat/lon on a basemap. Nodes without a reported position are clustered at the top edge.">Geographic</span>
          </button>
        </div>
      </div>

      {/* Main view area */}
      <div className="flex gap-0">
        {/* Graph/Map */}
        <div className="flex-1 min-w-0">
          {viewMode === 'topo' ? (
            <TopologyGraph
              nodes={nodes}
              edges={edges}
              selectedNodeId={selectedNodeId}
              onSelectNode={handleSelectNode}
            />
          ) : (
            <GeoMap
              nodes={nodes}
              edges={edges}
              selectedNodeId={selectedNodeId}
              onSelectNode={handleSelectNode}
            />
          )}
        </div>

        {/* Detail panel */}
        <NodeDetail
          node={selectedNode}
          edges={edges}
          nodes={nodes}
          onSelectNode={handleSelectNode}
        />
      </div>

      {/* Node table */}
      <NodeTable
        nodes={nodes}
        selectedNodeId={selectedNodeId}
        onSelectNode={handleSelectNode}
      />
    </div>
  )
}
