import { useEffect, useRef, useCallback, useState } from 'react'
import * as d3 from 'd3'
import type { NodeInfo, EdgeInfo } from '@/lib/api'

interface TopologyGraphProps {
  nodes: NodeInfo[]
  edges: EdgeInfo[]
  selectedNodeId: number | null
  onSelectNode: (nodeId: number | null) => void
}

interface SimNode extends d3.SimulationNodeDatum {
  id: number
  shortName: string
  longName: string
  role: string
  isInfra: boolean
  regionIndex: number
  x?: number
  y?: number
  fx?: number | null
  fy?: number | null
}

interface SimLink extends d3.SimulationLinkDatum<SimNode> {
  source: SimNode | number
  target: SimNode | number
  snr: number
  quality: string
}

interface Particle {
  edgeIndex: number
  t: number
  speed: number
  size: number
}

const REGION_COLORS = ['#3b82f6', '#a78bfa', '#06b6d4', '#f59e0b', '#22c55e', '#ec4899', '#8b5cf6', '#14b8a6']

function getQualityColor(snr: number): string {
  if (snr > 12) return '#22c55e'  // excellent - green
  if (snr > 8) return '#4ade80'   // good - light green
  if (snr > 5) return '#f59e0b'   // fair - amber
  if (snr > 3) return '#f97316'   // marginal - orange
  return '#ef4444'                 // poor - red
}

const INFRA_ROLES = ['ROUTER', 'ROUTER_LATE', 'REPEATER', 'TRACKER']

export default function TopologyGraph({
  nodes,
  edges,
  selectedNodeId,
  onSelectNode,
}: TopologyGraphProps) {
  const svgRef = useRef<SVGSVGElement>(null)
  const simulationRef = useRef<d3.Simulation<SimNode, SimLink> | null>(null)
  const particlesRef = useRef<Particle[]>([])
  const animationRef = useRef<number>(0)
  const dragNodeRef = useRef<SimNode | null>(null)

  const [simNodes, setSimNodes] = useState<SimNode[]>([])
  const [simLinks, setSimLinks] = useState<SimLink[]>([])
  const [dimensions, setDimensions] = useState({ width: 800, height: 540 })

  // Build region index map
  const regionIndexMap = useCallback((lat: number | null) => {
    // Simple region assignment based on latitude bands for Idaho
    if (lat === null) return 0
    if (lat > 46) return 0  // Northern
    if (lat > 44.5) return 1  // Central
    if (lat > 43) return 2  // SW
    return 3  // SC
  }, [])

  // Initialize simulation
  useEffect(() => {
    if (!nodes.length) return

    const width = dimensions.width
    const height = dimensions.height

    // Create simulation nodes
    const simNodesData: SimNode[] = nodes.map((n) => ({
      id: n.node_num,
      shortName: n.short_name,
      longName: n.long_name,
      role: n.role,
      isInfra: INFRA_ROLES.includes(n.role),
      regionIndex: regionIndexMap(n.latitude),
      x: width / 2 + (Math.random() - 0.5) * 200,
      y: height / 2 + (Math.random() - 0.5) * 200,
    }))

    const nodeMap = new Map(simNodesData.map((n) => [n.id, n]))

    // Create simulation links
    const simLinksData: SimLink[] = edges
      .filter((e) => nodeMap.has(e.from_node) && nodeMap.has(e.to_node))
      .map((e) => ({
        source: e.from_node,
        target: e.to_node,
        snr: e.snr,
        quality: e.quality,
      }))

    // Create D3 force simulation
    const simulation = d3.forceSimulation<SimNode>(simNodesData)
      .alphaDecay(0.008)
      .velocityDecay(0.35)
      .force('charge', d3.forceManyBody<SimNode>()
        .strength((n) => n.isInfra ? -250 : -120))
      .force('link', d3.forceLink<SimNode, SimLink>(simLinksData)
        .id((d) => d.id)
        .distance((d) => {
          const snr = d.snr || 0
          if (snr > 12) return 80
          if (snr > 8) return 100
          if (snr > 5) return 125
          return 155
        })
        .strength(0.7))
      .force('center', d3.forceCenter(width / 2, height / 2).strength(0.05))
      .force('collide', d3.forceCollide<SimNode>((d) => d.isInfra ? 28 : 16).strength(0.5))

    simulationRef.current = simulation
    setSimNodes(simNodesData)
    setSimLinks(simLinksData as SimLink[])

    // Initialize particles
    const particles: Particle[] = []
    simLinksData.forEach((_, i) => {
      const numParticles = 2 + Math.floor(Math.random() * 2)
      for (let p = 0; p < numParticles; p++) {
        particles.push({
          edgeIndex: i,
          t: Math.random(),
          speed: 0.002 + Math.random() * 0.003,
          size: 1.5 + Math.random() * 1.5,
        })
      }
    })
    particlesRef.current = particles

    // Animation loop
    let lastTime = 0
    const animate = (time: number) => {
      const dt = Math.min((time - lastTime) / 16.67, 2) // Cap at 2x speed
      lastTime = time

      // Update particle positions
      particlesRef.current.forEach((p) => {
        p.t += p.speed * dt
        if (p.t > 1) p.t -= 1
      })

      // Read simulation positions
      setSimNodes([...simNodesData])

      animationRef.current = requestAnimationFrame(animate)
    }
    animationRef.current = requestAnimationFrame(animate)

    return () => {
      cancelAnimationFrame(animationRef.current)
      simulation.stop()
    }
  }, [nodes, edges, dimensions, regionIndexMap])

  // Handle resize
  useEffect(() => {
    const handleResize = () => {
      if (svgRef.current) {
        const rect = svgRef.current.parentElement?.getBoundingClientRect()
        if (rect) {
          setDimensions({ width: rect.width, height: 540 })
        }
      }
    }
    handleResize()
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // Get neighbors of selected node
  const selectedNeighbors = new Set<number>()
  if (selectedNodeId !== null) {
    edges.forEach((e) => {
      if (e.from_node === selectedNodeId) selectedNeighbors.add(e.to_node)
      if (e.to_node === selectedNodeId) selectedNeighbors.add(e.from_node)
    })
  }

  // Drag handlers
  const handlePointerDown = useCallback((e: React.PointerEvent, node: SimNode) => {
    e.preventDefault()
    e.stopPropagation()
    const svg = svgRef.current
    if (!svg || !simulationRef.current) return

    svg.setPointerCapture(e.pointerId)
    dragNodeRef.current = node
    node.fx = node.x
    node.fy = node.y
    simulationRef.current.alphaTarget(0.3).restart()
  }, [])

  const handlePointerMove = useCallback((e: React.PointerEvent) => {
    if (!dragNodeRef.current || !svgRef.current) return

    const svg = svgRef.current
    const rect = svg.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top

    dragNodeRef.current.fx = x
    dragNodeRef.current.fy = y
  }, [])

  const handlePointerUp = useCallback((e: React.PointerEvent) => {
    if (!dragNodeRef.current || !simulationRef.current) return

    svgRef.current?.releasePointerCapture(e.pointerId)
    dragNodeRef.current.fx = null
    dragNodeRef.current.fy = null
    dragNodeRef.current = null
    simulationRef.current.alphaTarget(0)
  }, [])

  const handleNodeClick = useCallback((nodeId: number) => {
    onSelectNode(selectedNodeId === nodeId ? null : nodeId)
  }, [selectedNodeId, onSelectNode])

  const handleBackgroundClick = useCallback(() => {
    onSelectNode(null)
  }, [onSelectNode])

  return (
    <div className="relative bg-bg-card rounded-lg border border-border overflow-hidden">
      <svg
        ref={svgRef}
        width={dimensions.width}
        height={dimensions.height}
        className="cursor-grab active:cursor-grabbing"
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerLeave={handlePointerUp}
        onClick={handleBackgroundClick}
      >
        {/* Background */}
        <rect width="100%" height="100%" fill="#111827" />

        {/* Edges */}
        <g>
          {simLinks.map((link, i) => {
            const source = typeof link.source === 'object' ? link.source : simNodes.find(n => n.id === link.source)
            const target = typeof link.target === 'object' ? link.target : simNodes.find(n => n.id === link.target)
            if (!source?.x || !source?.y || !target?.x || !target?.y) return null

            const isRelated = selectedNodeId === null ||
              source.id === selectedNodeId ||
              target.id === selectedNodeId

            const opacity = selectedNodeId === null ? 0.4 : (isRelated ? 0.6 : 0.04)
            const color = getQualityColor(link.snr)

            const sx = source.x
            const sy = source.y
            const tx = target.x
            const ty = target.y

            return (
              <g key={i}>
                <line
                  x1={sx}
                  y1={sy}
                  x2={tx}
                  y2={ty}
                  stroke={color}
                  strokeWidth={isRelated && selectedNodeId !== null ? 2 : 1}
                  opacity={opacity}
                />
                {/* SNR label when node selected */}
                {selectedNodeId !== null && isRelated && (
                  <text
                    x={(sx + tx) / 2}
                    y={(sy + ty) / 2 - 4}
                    fill={color}
                    fontSize="9"
                    fontFamily="JetBrains Mono, monospace"
                    textAnchor="middle"
                    opacity={0.9}
                  >
                    {link.snr.toFixed(1)} dB
                  </text>
                )}
              </g>
            )
          })}
        </g>

        {/* Particles */}
        <g>
          {particlesRef.current.map((particle, i) => {
            const link = simLinks[particle.edgeIndex]
            if (!link) return null

            const source = typeof link.source === 'object' ? link.source : simNodes.find(n => n.id === link.source)
            const target = typeof link.target === 'object' ? link.target : simNodes.find(n => n.id === link.target)
            if (!source?.x || !source?.y || !target?.x || !target?.y) return null

            const isRelated = selectedNodeId === null ||
              source.id === selectedNodeId ||
              target.id === selectedNodeId

            const sx = source.x
            const sy = source.y
            const tx = target.x
            const ty = target.y
            const x = sx + (tx - sx) * particle.t
            const y = sy + (ty - sy) * particle.t
            const color = getQualityColor(link.snr)

            return (
              <circle
                key={i}
                cx={x}
                cy={y}
                r={particle.size}
                fill={color}
                opacity={selectedNodeId === null ? 0.7 : (isRelated ? 0.8 : 0.05)}
              />
            )
          })}
        </g>

        {/* Nodes */}
        <g>
          {simNodes.map((node) => {
            if (node.x === undefined || node.y === undefined) return null

            const isSelected = node.id === selectedNodeId
            const isNeighbor = selectedNeighbors.has(node.id)
            const isRelated = selectedNodeId === null || isSelected || isNeighbor
            const opacity = isRelated ? 1 : 0.1
            const color = REGION_COLORS[node.regionIndex % REGION_COLORS.length]
            const radius = node.isInfra ? 14 : 8

            return (
              <g
                key={node.id}
                transform={`translate(${node.x}, ${node.y})`}
                opacity={opacity}
                className="cursor-pointer"
                onPointerDown={(e) => handlePointerDown(e, node)}
                onClick={(e) => {
                  e.stopPropagation()
                  handleNodeClick(node.id)
                }}
              >
                {/* Selection ring */}
                {isSelected && (
                  <circle
                    r={radius + 4}
                    fill="none"
                    stroke="white"
                    strokeWidth={2}
                    strokeDasharray="4 2"
                    opacity={0.8}
                  />
                )}
                {/* Node circle */}
                <circle
                  r={radius}
                  fill={node.isInfra ? color : '#111827'}
                  stroke={color}
                  strokeWidth={node.isInfra ? 0 : 2}
                />
                {/* Label */}
                <text
                  y={radius + 12}
                  textAnchor="middle"
                  fill="#94a3b8"
                  fontSize="10"
                  fontFamily="JetBrains Mono, monospace"
                >
                  {node.shortName}
                </text>
              </g>
            )
          })}
        </g>

        {/* Legend */}
        <g transform={`translate(16, ${dimensions.height - 100})`}>
          <rect x={-8} y={-8} width={140} height={96} fill="#0a0e17" fillOpacity={0.8} rx={4} />
          <text fill="#94a3b8" fontSize="10" fontWeight="500" y={4}>Edge Quality</text>
          {[
            { label: 'Excellent (>12)', color: '#22c55e' },
            { label: 'Good (8-12)', color: '#4ade80' },
            { label: 'Fair (5-8)', color: '#f59e0b' },
            { label: 'Marginal (3-5)', color: '#f97316' },
            { label: 'Poor (<3)', color: '#ef4444' },
          ].map((item, i) => (
            <g key={item.label} transform={`translate(0, ${16 + i * 14})`}>
              <line x1={0} y1={0} x2={16} y2={0} stroke={item.color} strokeWidth={2} />
              <text x={22} y={3} fill="#64748b" fontSize="9">{item.label}</text>
            </g>
          ))}
        </g>

        {/* Node type legend */}
        <g transform={`translate(${dimensions.width - 130}, ${dimensions.height - 50})`}>
          <rect x={-8} y={-8} width={120} height={44} fill="#0a0e17" fillOpacity={0.8} rx={4} />
          <g>
            <circle cx={8} cy={6} r={6} fill="#3b82f6" />
            <text x={20} y={9} fill="#64748b" fontSize="9">Infrastructure</text>
          </g>
          <g transform="translate(0, 18)">
            <circle cx={8} cy={6} r={5} fill="#111827" stroke="#3b82f6" strokeWidth={1.5} />
            <text x={20} y={9} fill="#64748b" fontSize="9">Client</text>
          </g>
        </g>
      </svg>
    </div>
  )
}
