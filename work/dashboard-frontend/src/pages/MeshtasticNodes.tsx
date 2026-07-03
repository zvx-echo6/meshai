import { useState, useEffect, useCallback } from 'react'
import { Save, RotateCcw, RefreshCw, Check } from 'lucide-react'
import Mesh from './Mesh'
import MeshtasticSources from './MeshtasticSources'
import { MeshIntelligenceSection, type MeshIntelligenceConfig } from './Config'
import { fetchConfig as apiFetchConfig, updateConfig as apiUpdateConfig } from '@/lib/api'
import { useDirty } from '@/context/DirtyContext'
import { notifyRestartRequired } from '@/components/RestartBanner'

const TABS = [
  { key: 'nodes', label: 'Nodes' },
  { key: 'sources', label: 'Sources' },
  { key: 'health', label: 'Health' },
] as const

type TabKey = typeof TABS[number]['key']

export default function MeshtasticNodes() {
  const [activeTab, setActiveTab] = useState<TabKey>('nodes')

  // Health tab state
  const { setDirty } = useDirty()
  const [intelligence, setIntelligence] = useState<MeshIntelligenceConfig | null>(null)
  const [originalIntelligence, setOriginalIntelligence] = useState<MeshIntelligenceConfig | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  useEffect(() => {
    document.title = 'Nodes & Health - MeshAI'
  }, [])

  const fetchData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = (await apiFetchConfig('mesh_intelligence')) as MeshIntelligenceConfig
      setIntelligence(data)
      setOriginalIntelligence(JSON.parse(JSON.stringify(data)))
      setHasChanges(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load mesh intelligence config')
    } finally {
      setLoading(false)
    }
  }, [])

  // Load when Health tab becomes active
  useEffect(() => {
    if (activeTab === 'health' && intelligence === null && !loading) {
      fetchData()
    }
  }, [activeTab, intelligence, loading, fetchData])

  useEffect(() => {
    if (intelligence && originalIntelligence) {
      setHasChanges(JSON.stringify(intelligence) !== JSON.stringify(originalIntelligence))
    }
  }, [intelligence, originalIntelligence])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const saveConfig = async () => {
    if (!intelligence) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const result = await apiUpdateConfig('mesh_intelligence', intelligence)
      setOriginalIntelligence(JSON.parse(JSON.stringify(intelligence)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('Mesh intelligence saved successfully')
      if (result.restart_required) notifyRestartRequired([])
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const discardChanges = () => {
    if (originalIntelligence) setIntelligence(JSON.parse(JSON.stringify(originalIntelligence)))
    setHasChanges(false)
  }

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
        <div className="max-w-2xl mx-auto space-y-6">
          {/* Save bar */}
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-slate-500">
                Mesh health scoring, region management, and automated alerting.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={fetchData}
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

          {/* Status messages */}
          {error && (
            <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{error}</div>
          )}
          {success && (
            <div className="p-3 text-sm bg-green-500/10 text-green-400 border border-green-500/20">
              <Check size={14} className="inline mr-2" />{success}
            </div>
          )}

          {loading ? (
            <div className="flex items-center justify-center h-32">
              <div className="text-slate-400">Loading...</div>
            </div>
          ) : intelligence ? (
            <div className="bg-bg-card border border-border p-6">
              <MeshIntelligenceSection data={intelligence} onChange={setIntelligence} />
            </div>
          ) : (
            <div className="flex items-center justify-center h-32">
              <div className="text-red-400">Failed to load config</div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
