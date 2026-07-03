import { useState, useEffect, useCallback } from 'react'
import { Save, RotateCcw, RefreshCw, Check } from 'lucide-react'
import {
  MeshMonitorSection,
  MeshSourcesSection,
  type MeshMonitorConfig,
  type MeshSourceConfig,
} from './Config'
import { notifyRestartRequired } from '@/components/RestartBanner'
import { fetchConfig as apiFetchConfig, updateConfig as apiUpdateConfig } from '@/lib/api'
import { useDirty } from '@/context/DirtyContext'

export default function MeshtasticSources() {
  const { setDirty } = useDirty()
  const [meshmonitor, setMeshmonitor] = useState<MeshMonitorConfig | null>(null)
  const [originalMeshmonitor, setOriginalMeshmonitor] = useState<MeshMonitorConfig | null>(null)
  const [meshSources, setMeshSources] = useState<MeshSourceConfig[] | null>(null)
  const [originalMeshSources, setOriginalMeshSources] = useState<MeshSourceConfig[] | null>(null)

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const [mm, ms] = await Promise.all([
        apiFetchConfig('meshmonitor') as Promise<MeshMonitorConfig>,
        apiFetchConfig('mesh_sources') as Promise<MeshSourceConfig[]>,
      ])
      setMeshmonitor(mm)
      setOriginalMeshmonitor(JSON.parse(JSON.stringify(mm)))
      setMeshSources(ms)
      setOriginalMeshSources(JSON.parse(JSON.stringify(ms)))
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'Meshtastic Sources - MeshAI'
    fetchData()
  }, [fetchData])

  useEffect(() => {
    if (meshmonitor && originalMeshmonitor && meshSources && originalMeshSources) {
      const mmChanged = JSON.stringify(meshmonitor) !== JSON.stringify(originalMeshmonitor)
      const msChanged = JSON.stringify(meshSources) !== JSON.stringify(originalMeshSources)
      setHasChanges(mmChanged || msChanged)
    }
  }, [meshmonitor, originalMeshmonitor, meshSources, originalMeshSources])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const saveConfig = async () => {
    if (!meshmonitor || !meshSources) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const [mmResult, msResult] = await Promise.all([
        apiUpdateConfig('meshmonitor', meshmonitor),
        apiUpdateConfig('mesh_sources', meshSources),
      ])
      setOriginalMeshmonitor(JSON.parse(JSON.stringify(meshmonitor)))
      setOriginalMeshSources(JSON.parse(JSON.stringify(meshSources)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('Meshtastic sources saved successfully')
      if (mmResult.restart_required || msResult.restart_required) {
        notifyRestartRequired([])
      }
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const discardChanges = () => {
    if (originalMeshmonitor) setMeshmonitor(JSON.parse(JSON.stringify(originalMeshmonitor)))
    if (originalMeshSources) setMeshSources(JSON.parse(JSON.stringify(originalMeshSources)))
    setHasChanges(false)
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading Meshtastic sources...</div>
      </div>
    )
  }

  if (!meshmonitor || !meshSources) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Failed to load sources config</div>
      </div>
    )
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">
            MeshMonitor integration and mesh awareness data sources.
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
          <Check size={14} className="inline mr-2" />
          {success}
        </div>
      )}

      {/* MeshMonitor card */}
      <div className="bg-bg-card border border-border p-6">
        <MeshMonitorSection data={meshmonitor} onChange={setMeshmonitor} />
      </div>

      {/* Mesh Sources card */}
      <div className="bg-bg-card border border-border p-6">
        <MeshSourcesSection data={meshSources} onChange={setMeshSources} />
      </div>
    </div>
  )
}
