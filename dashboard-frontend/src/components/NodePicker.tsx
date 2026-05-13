import { useState, useEffect, useMemo } from 'react'
import { Search, X, Check } from 'lucide-react'

interface Node {
  node_num: number
  node_id_hex: string
  short_name: string
  long_name: string
  role: string
  is_infrastructure?: boolean
}

interface NodePickerProps {
  label: string
  value: string[]
  onChange: (value: string[]) => void
  helper?: string
  info?: string
  roleFilter?: string  // e.g., "ROUTER" to show only infrastructure
  valueType?: 'short_name' | 'node_num' | 'node_id_hex'  // What to store in value
}

export default function NodePicker({
  label,
  value,
  onChange,
  helper,
  info: _info,
  roleFilter,
  valueType = 'short_name',
}: NodePickerProps) {
  const [nodes, setNodes] = useState<Node[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [isOpen, setIsOpen] = useState(false)

  useEffect(() => {
    fetch('/api/nodes')
      .then(res => res.json())
      .then(data => {
        setNodes(data)
        setLoading(false)
      })
      .catch(() => {
        setNodes([])
        setLoading(false)
      })
  }, [])

  const filteredNodes = useMemo(() => {
    let result = nodes

    // Filter by role if specified
    if (roleFilter) {
      result = result.filter(n => {
        if (roleFilter === 'ROUTER' || roleFilter === 'infrastructure') {
          return n.is_infrastructure ||
                 n.role === 'ROUTER' ||
                 n.role === 'ROUTER_CLIENT' ||
                 n.role === 'REPEATER'
        }
        return n.role === roleFilter
      })
    }

    // Filter by search
    if (search.trim()) {
      const s = search.toLowerCase()
      result = result.filter(n =>
        n.short_name?.toLowerCase().includes(s) ||
        n.long_name?.toLowerCase().includes(s) ||
        n.role?.toLowerCase().includes(s) ||
        n.node_id_hex?.toLowerCase().includes(s)
      )
    }

    return result.sort((a, b) => (a.short_name || '').localeCompare(b.short_name || ''))
  }, [nodes, search, roleFilter])

  const getNodeValue = (node: Node): string => {
    switch (valueType) {
      case 'node_num':
        return String(node.node_num)
      case 'node_id_hex':
        return node.node_id_hex
      default:
        return node.short_name || String(node.node_num)
    }
  }

  const isSelected = (node: Node): boolean => {
    const nodeVal = getNodeValue(node)
    return value.includes(nodeVal)
  }

  const toggleNode = (node: Node) => {
    const nodeVal = getNodeValue(node)
    if (value.includes(nodeVal)) {
      onChange(value.filter(v => v !== nodeVal))
    } else {
      onChange([...value, nodeVal])
    }
  }

  const formatNodeDisplay = (node: Node): string => {
    const parts = [node.short_name]
    if (node.long_name && node.long_name !== node.short_name) {
      parts.push(`— ${node.long_name}`)
    }
    if (node.role) {
      parts.push(`(${node.role})`)
    }
    return parts.join(' ')
  }

  // Fallback to text input if no nodes loaded
  if (!loading && nodes.length === 0) {
    return (
      <div className="space-y-1">
        <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
        <input
          type="text"
          value={value.join(', ')}
          onChange={(e) => onChange(e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
          placeholder="Enter node IDs separated by commas"
          className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
        />
        {helper && <p className="text-xs text-slate-600">{helper}</p>}
      </div>
    )
  }

  return (
    <div className="space-y-1">
      <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>

      {/* Selected nodes display */}
      {value.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-2">
          {value.map((v) => {
            const node = nodes.find(n => getNodeValue(n) === v)
            return (
              <span
                key={v}
                className="inline-flex items-center gap-1 px-2 py-1 bg-accent/20 text-accent rounded text-sm"
              >
                {node ? node.short_name : v}
                <button
                  type="button"
                  onClick={() => onChange(value.filter(val => val !== v))}
                  className="hover:text-white"
                >
                  <X size={14} />
                </button>
              </span>
            )
          })}
        </div>
      )}

      {/* Search and dropdown */}
      <div className="relative">
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onFocus={() => setIsOpen(true)}
            placeholder={loading ? "Loading nodes..." : "Search nodes..."}
            className="w-full pl-9 pr-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
          />
        </div>

        {isOpen && !loading && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setIsOpen(false)} />
            <div className="absolute left-0 right-0 top-full mt-1 z-50 max-h-64 overflow-y-auto bg-[#0a0e17] border border-[#1e2a3a] rounded-lg shadow-xl">
              {filteredNodes.length === 0 ? (
                <div className="p-3 text-sm text-slate-500 text-center">
                  No nodes found
                </div>
              ) : (
                filteredNodes.map((node) => (
                  <button
                    key={node.node_num}
                    type="button"
                    onClick={() => toggleNode(node)}
                    className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-[#1e2a3a] ${
                      isSelected(node) ? 'bg-accent/10' : ''
                    }`}
                  >
                    <div className={`w-4 h-4 rounded border flex items-center justify-center ${
                      isSelected(node) ? 'bg-accent border-accent' : 'border-slate-600'
                    }`}>
                      {isSelected(node) && <Check size={12} className="text-white" />}
                    </div>
                    <span className="text-slate-200">{formatNodeDisplay(node)}</span>
                  </button>
                ))
              )}
            </div>
          </>
        )}
      </div>

      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}
