import { useState, useEffect } from 'react'
import GaugeSites from './GaugeSites'
import TownAnchors from './TownAnchors'

const TABS = [
  { key: 'gauge-sites', label: 'Gauge Sites' },
  { key: 'town-anchors', label: 'Town Anchors' },
] as const

type TabKey = typeof TABS[number]['key']

export default function Places() {
  const [activeTab, setActiveTab] = useState<TabKey>('gauge-sites')

  useEffect(() => {
    document.title = 'Places - MeshAI'
  }, [])

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
      {activeTab === 'gauge-sites' && <GaugeSites />}
      {activeTab === 'town-anchors' && <TownAnchors />}
    </div>
  )
}
