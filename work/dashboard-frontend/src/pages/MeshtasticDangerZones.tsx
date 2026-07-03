import { useEffect } from 'react'
import DangerZonesPanel from '@/components/DangerZonesPanel'

export default function MeshtasticDangerZones() {
  useEffect(() => {
    document.title = 'Danger Zones - MeshAI'
  }, [])

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <p className="text-sm text-slate-500">
        Alert infrastructure nodes when they are within a configurable buffer distance of an active hazard.
      </p>
      <DangerZonesPanel />
    </div>
  )
}
