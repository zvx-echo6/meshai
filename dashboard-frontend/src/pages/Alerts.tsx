import { Bell } from 'lucide-react'

export default function Alerts() {
  return (
    <div className="flex flex-col items-center justify-center h-[60vh] text-center">
      <div className="w-16 h-16 rounded-full bg-bg-card border border-border flex items-center justify-center mb-6">
        <Bell size={32} className="text-slate-500" />
      </div>
      <h2 className="text-xl font-semibold text-slate-300 mb-2">Alerts</h2>
      <p className="text-slate-500 max-w-md">
        Alert history and subscriptions coming in Phase 11
      </p>
    </div>
  )
}
