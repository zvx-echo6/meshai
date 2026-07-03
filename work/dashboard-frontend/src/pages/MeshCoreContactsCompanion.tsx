import { useState, useEffect } from 'react'
import MeshCoreContacts from './MeshCoreContacts'
import MeshCoreCompanion from './MeshCoreCompanion'

const TABS = [
  { key: 'contacts', label: 'Contacts' },
  { key: 'companion', label: 'Companion' },
] as const

type TabKey = typeof TABS[number]['key']

export default function MeshCoreContactsCompanion() {
  const [activeTab, setActiveTab] = useState<TabKey>('contacts')

  useEffect(() => {
    document.title = 'Contacts & Companion - MeshAI'
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
      {activeTab === 'contacts' && <MeshCoreContacts />}
      {activeTab === 'companion' && <MeshCoreCompanion />}
    </div>
  )
}
