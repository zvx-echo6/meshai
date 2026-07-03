import { useEffect } from 'react'
import { Bot } from 'lucide-react'

export default function MeshCoreCompanion() {
  useEffect(() => {
    document.title = 'Companion & Channels - MeshAI'
  }, [])

  return (
    <div className="max-w-3xl mx-auto">
      <div className="bg-bg-card border border-border p-8">
        <div className="flex items-start gap-4">
          <div className="flex-shrink-0 w-12 h-12 rounded-lg bg-[#0a0e17] border border-[#1e2a3a] flex items-center justify-center">
            <Bot size={24} className="text-accent" />
          </div>
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <h2 className="text-xl font-semibold text-slate-100">Companion &amp; Channels</h2>
              <span className="px-2 py-0.5 text-[10px] uppercase tracking-wide rounded bg-slate-700 text-slate-300">
                Coming soon
              </span>
            </div>
            <p className="text-sm text-slate-400 leading-relaxed max-w-prose">
              This page will show live status for the AIDA MeshCore companion &mdash; its connection
              health and the list of channels it is currently joined to. Once the companion status
              API is available, you'll be able to monitor the companion here and see which channels
              are reachable for broadcast delivery.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
