import { useState, useEffect, useCallback } from 'react'
import { Save, RotateCcw, RefreshCw, Check } from 'lucide-react'
import { ConnectionSection, type ConnectionConfig } from './Config'
import { notifyRestartRequired } from '@/components/RestartBanner'
import { fetchConfig as apiFetchConfig, updateConfig as apiUpdateConfig } from '@/lib/api'
import { useDirty } from '@/context/DirtyContext'

export default function MeshtasticConnection() {
  const { setDirty } = useDirty()
  const [config, setConfig] = useState<ConnectionConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<ConnectionConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  const fetchConfig = useCallback(async () => {
    setLoading(true)
    try {
      const data = (await apiFetchConfig('connection')) as ConnectionConfig
      setConfig(data)
      setOriginalConfig(JSON.parse(JSON.stringify(data)))
      setHasChanges(false)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    document.title = 'Meshtastic Connection - MeshAI'
    fetchConfig()
  }, [fetchConfig])

  useEffect(() => {
    if (config && originalConfig) {
      setHasChanges(JSON.stringify(config) !== JSON.stringify(originalConfig))
    }
  }, [config, originalConfig])

  useEffect(() => {
    setDirty(hasChanges)
    return () => setDirty(false)
  }, [hasChanges, setDirty])

  const saveConfig = async () => {
    if (!config) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      // PUT the whole connection object so MeshCore fields aren't clobbered.
      const result = await apiUpdateConfig('connection', config)
      setOriginalConfig(JSON.parse(JSON.stringify(config)))
      setHasChanges(false)
      setDirty(false)
      setSuccess('Meshtastic connection saved successfully')
      if (result.restart_required) {
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
    if (originalConfig) {
      setConfig(JSON.parse(JSON.stringify(originalConfig)))
      setHasChanges(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-slate-400">Loading Meshtastic connection...</div>
      </div>
    )
  }

  if (!config) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">Failed to load connection config</div>
      </div>
    )
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500">
            Connection to your Meshtastic radio (serial or TCP).
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchConfig}
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

      {/* Form */}
      <div className="bg-bg-card border border-border p-6">
        <ConnectionSection data={config} onChange={setConfig} />
      </div>
    </div>
  )
}
