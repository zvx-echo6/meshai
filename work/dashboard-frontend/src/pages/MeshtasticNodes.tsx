import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw, Lightbulb, CheckCircle, AlertTriangle, ExternalLink } from 'lucide-react'
import Mesh from './Mesh'
import MeshtasticSources from './MeshtasticSources'
import { fetchHealth, type MeshHealth } from '@/lib/api'

const TABS = [
  { key: 'nodes', label: 'Nodes' },
  { key: 'sources', label: 'Sources' },
  { key: 'health', label: 'Health' },
] as const

type TabKey = typeof TABS[number]['key']

export default function MeshtasticNodes() {
  const [activeTab, setActiveTab] = useState<TabKey>('nodes')

  // Health tab state
  const [health, setHealth] = useState<MeshHealth | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    document.title = 'Nodes & Health - MeshAI'
  }, [])

  const fetchData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchHealth()
      setHealth(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load mesh health')
    } finally {
      setLoading(false)
    }
  }, [])

  // Load when Health tab becomes active
  useEffect(() => {
    if (activeTab === 'health' && health === null && !loading) {
      fetchData()
    }
  }, [activeTab, health, loading, fetchData])

  return (
    <div className="space-y-4">
      {/* Tabs */}
      <div className="flex gap-1 border-b border-border">
        {TABS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setActiveTab(key)}
            className={`px-4 py-2 text-sm whitespace-nowrap border-b-2 -mb-px transition-colors ${
              activeTab === key
                ? 'border-accent text-accent'
                : 'border-transparent text-[#777] hover:text-white'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === 'nodes' && <Mesh />}
      {activeTab === 'sources' && <MeshtasticSources />}
      {activeTab === 'health' && (
        <div className="max-w-2xl mx-auto space-y-4">
          {/* Header */}
          <div className="flex items-center justify-between">
            <p className="text-sm text-slate-500">
              Optimization recommendations generated from current mesh health.
            </p>
            <button
              onClick={fetchData}
              className="p-2 text-slate-400 hover:text-slate-200 hover:bg-bg-hover rounded transition-colors"
              title="Refresh"
            >
              <RefreshCw size={18} />
            </button>
          </div>

          {/* Status messages */}
          {error && (
            <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{error}</div>
          )}

          {loading ? (
            <div className="flex items-center justify-center h-32">
              <div className="text-slate-400">Loading...</div>
            </div>
          ) : health ? (
            <div className="bg-bg-card border border-border p-4">
              <h2 className="text-[10px] font-sans uppercase tracking-widest text-[#666] mb-3">
                Optimization Recommendations
              </h2>
              {health.recommendations && health.recommendations.length > 0 ? (
                <div className="space-y-2">
                  {health.recommendations.map((rec, i) => (
                    <div
                      key={i}
                      className="p-3 bg-accent/5 border-l-2 border-accent flex items-start gap-3"
                    >
                      <Lightbulb size={16} className="text-accent flex-shrink-0 mt-0.5" />
                      <span className="text-sm font-sans text-[#e0e0e0]">{rec}</span>
                    </div>
                  ))}
                </div>
              ) : health.recommendations_available === false ? (
                <div className="flex items-center gap-2 text-amber-400 py-4">
                  <AlertTriangle size={16} />
                  <span className="font-sans">
                    Couldn't compute recommendations right now. Check server logs.
                  </span>
                </div>
              ) : (
                <div className="flex items-center gap-2 text-[#777] py-4">
                  <CheckCircle size={16} className="text-green-500" />
                  <span className="font-sans">No recommendations — mesh is healthy.</span>
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-center justify-center h-32">
              <div className="text-red-400">Failed to load mesh health</div>
            </div>
          )}

          {/* Cross-link note */}
          <div className="flex items-start gap-2 p-3 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-400">
            <ExternalLink size={16} className="text-accent mt-0.5 flex-shrink-0" />
            <div>
              Health scoring, region management, and alerting thresholds are configured on{' '}
              <Link to="/config?section=mesh_intelligence" className="text-accent hover:underline">
                Settings &rarr; Intelligence
              </Link>
              .
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
