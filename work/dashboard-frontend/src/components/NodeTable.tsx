import { useState, useMemo } from 'react'
import { ChevronUp, ChevronDown, Search, Filter } from 'lucide-react'
import type { NodeInfo } from '@/lib/api'

interface NodeTableProps {
  nodes: NodeInfo[]
  selectedNodeId: number | null
  onSelectNode: (nodeId: number) => void
}

type SortField = 'short_name' | 'role' | 'battery_level' | 'last_heard' | 'hardware'
type SortDir = 'asc' | 'desc'
type QuickFilter = 'all' | 'infra' | 'online'

const INFRA_ROLES = ['ROUTER', 'ROUTER_LATE', 'REPEATER', 'TRACKER']

function getStatusColor(lastHeard: string | null): string {
  if (!lastHeard) return 'bg-slate-500'
  const date = new Date(lastHeard)
  const now = new Date()
  const diffHours = (now.getTime() - date.getTime()) / 3600000
  if (diffHours < 1) return 'bg-green-500'
  if (diffHours < 24) return 'bg-amber-500'
  return 'bg-slate-500'
}

function formatLastHeard(lastHeard: string | null): string {
  if (!lastHeard) return '—'
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

function formatBattery(node: NodeInfo): string {
  if (node.battery_level === null) return '—'
  if (node.battery_level > 100 || (node.voltage && node.voltage > 4.1)) {
    return 'USB ⚡'
  }
  return `${node.battery_level.toFixed(0)}%`
}

function getRegionName(lat: number | null): string {
  if (lat === null) return '—'
  if (lat > 46) return 'Northern'
  if (lat > 44.5) return 'Central'
  if (lat > 43) return 'SW Idaho'
  return 'SC Idaho'
}

export default function NodeTable({
  nodes,
  selectedNodeId,
  onSelectNode,
}: NodeTableProps) {
  const [searchTerm, setSearchTerm] = useState('')
  const [sortField, setSortField] = useState<SortField>('short_name')
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  const [quickFilter, setQuickFilter] = useState<QuickFilter>('all')

  // Filter and sort nodes
  const filteredNodes = useMemo(() => {
    let result = [...nodes]

    // Quick filter
    if (quickFilter === 'infra') {
      result = result.filter((n) => INFRA_ROLES.includes(n.role))
    } else if (quickFilter === 'online') {
      result = result.filter((n) => {
        if (!n.last_heard) return false
        const date = new Date(n.last_heard)
        const now = new Date()
        const diffHours = (now.getTime() - date.getTime()) / 3600000
        return diffHours < 1
      })
    }

    // Search filter
    if (searchTerm) {
      const term = searchTerm.toLowerCase()
      result = result.filter((n) =>
        n.short_name.toLowerCase().includes(term) ||
        n.long_name.toLowerCase().includes(term) ||
        n.role.toLowerCase().includes(term) ||
        getRegionName(n.latitude).toLowerCase().includes(term)
      )
    }

    // Sort
    result.sort((a, b) => {
      let aVal: string | number = ''
      let bVal: string | number = ''

      switch (sortField) {
        case 'short_name':
          aVal = a.short_name.toLowerCase()
          bVal = b.short_name.toLowerCase()
          break
        case 'role':
          aVal = a.role
          bVal = b.role
          break
        case 'battery_level':
          aVal = a.battery_level ?? -1
          bVal = b.battery_level ?? -1
          break
        case 'last_heard':
          aVal = a.last_heard ? new Date(a.last_heard).getTime() : 0
          bVal = b.last_heard ? new Date(b.last_heard).getTime() : 0
          break
        case 'hardware':
          aVal = a.hardware.toLowerCase()
          bVal = b.hardware.toLowerCase()
          break
      }

      if (aVal < bVal) return sortDir === 'asc' ? -1 : 1
      if (aVal > bVal) return sortDir === 'asc' ? 1 : -1
      return 0
    })

    return result
  }, [nodes, searchTerm, sortField, sortDir, quickFilter])

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDir('asc')
    }
  }

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null
    return sortDir === 'asc' ? (
      <ChevronUp size={14} className="inline ml-1" />
    ) : (
      <ChevronDown size={14} className="inline ml-1" />
    )
  }

  return (
    <div className="bg-bg-card border border-border overflow-hidden">
      {/* Filter bar */}
      <div className="p-3 border-b border-border flex items-center gap-3">
        {/* Search */}
        <div className="relative flex-1 max-w-xs">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            placeholder="Search nodes..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full pl-9 pr-3 py-1.5 bg-bg-hover border border-border rounded text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-accent"
          />
        </div>

        {/* Quick filters */}
        <div className="flex items-center gap-1">
          <Filter size={14} className="text-slate-500 mr-1" />
          {(['all', 'infra', 'online'] as QuickFilter[]).map((filter) => (
            <button
              key={filter}
              onClick={() => setQuickFilter(filter)}
              className={`px-2 py-1 text-xs rounded transition-colors ${
                quickFilter === filter
                  ? 'bg-accent text-white'
                  : 'bg-bg-hover text-slate-400 hover:text-slate-200'
              }`}
            >
              {filter === 'all' ? 'All' : filter === 'infra' ? 'Infra' : 'Online'}
            </button>
          ))}
        </div>

        {/* Count */}
        <div className="text-xs text-slate-500 ml-auto">
          {filteredNodes.length} of {nodes.length} nodes
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-bg-hover text-slate-400 text-xs">
              <th className="w-8 px-3 py-2"></th>
              <th
                className="px-3 py-2 text-left cursor-pointer hover:text-slate-200"
                onClick={() => handleSort('short_name')}
              >
                Name <SortIcon field="short_name" />
              </th>
              <th
                className="px-3 py-2 text-left cursor-pointer hover:text-slate-200"
                onClick={() => handleSort('role')}
              >
                Role <SortIcon field="role" />
              </th>
              <th className="px-3 py-2 text-left">Region</th>
              <th
                className="px-3 py-2 text-left cursor-pointer hover:text-slate-200"
                onClick={() => handleSort('battery_level')}
              >
                <span title="Battery percent (4.20V = 100%, 3.60V ~ 30% warning, 3.30V ~ 3% critical). USB ⚡ = USB-powered (>100% or >4.1V); no battery management applies.">Battery</span> <SortIcon field="battery_level" />
              </th>
              <th
                className="px-3 py-2 text-left cursor-pointer hover:text-slate-200"
                onClick={() => handleSort('last_heard')}
              >
                <span title="Status dot: green = heard in the last hour; amber = within 24h; slate = offline (past the configured threshold). See Reference → Mesh Health for thresholds by node type.">Last Heard</span> <SortIcon field="last_heard" />
              </th>
              <th
                className="px-3 py-2 text-left cursor-pointer hover:text-slate-200"
                onClick={() => handleSort('hardware')}
              >
                Hardware <SortIcon field="hardware" />
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {filteredNodes.slice(0, 100).map((node) => {
              const isInfra = INFRA_ROLES.includes(node.role)
              const isSelected = node.node_num === selectedNodeId

              return (
                <tr
                  key={node.node_num}
                  onClick={() => onSelectNode(node.node_num)}
                  className={`cursor-pointer transition-colors ${
                    isSelected
                      ? 'bg-accent/10'
                      : 'hover:bg-bg-hover'
                  }`}
                >
                  <td className="px-3 py-2">
                    <div className={`w-2 h-2 rounded-full ${getStatusColor(node.last_heard)}`} />
                  </td>
                  <td className="px-3 py-2">
                    <div className="font-mono text-slate-200">{node.short_name}</div>
                    <div className="text-xs text-slate-500 truncate max-w-[200px]">
                      {node.long_name}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${
                        isInfra
                          ? 'bg-cyan-500/20 text-accent'
                          : 'bg-slate-500/20 text-slate-400'
                      }`}
                    >
                      {node.role}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-slate-400">
                    {getRegionName(node.latitude)}
                  </td>
                  <td className="px-3 py-2 font-mono text-slate-300">
                    {formatBattery(node)}
                  </td>
                  <td className="px-3 py-2 text-slate-400">
                    {formatLastHeard(node.last_heard)}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-400 truncate max-w-[150px]">
                    {node.hardware || '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>

        {filteredNodes.length > 100 && (
          <div className="px-3 py-2 text-xs text-slate-500 text-center border-t border-border">
            Showing first 100 of {filteredNodes.length} nodes
          </div>
        )}

        {filteredNodes.length === 0 && (
          <div className="px-3 py-8 text-sm text-slate-500 text-center">
            No nodes match your filters
          </div>
        )}
      </div>
    </div>
  )
}
