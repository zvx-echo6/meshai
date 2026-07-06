import { useState, useEffect } from 'react'
import { Plus, Trash2 } from 'lucide-react'

// Reusable key -> value dict editor for a Record<string,string> (e.g. HTTP
// headers). Styling/behavior mirrors the inline custom-command editor in
// pages/Config.tsx (CommandsSection): ordered rows kept in local state so a
// half-typed (blank-key) row isn't dropped mid-edit; the blank-filtered dict is
// committed up to the parent on every change.

// Self-contained "?" info popover so this component doesn't depend on any page.
function InfoBadge({ info }: { info: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="relative inline-block">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen(!open) }}
        className="ml-1.5 w-4 h-4 rounded-full bg-slate-700 hover:bg-slate-600 text-slate-400 hover:text-slate-200 inline-flex items-center justify-center text-xs transition-colors"
        title="More info"
      >
        ?
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-6 z-50 w-72 p-3 bg-[#1a2332] border border-[#2a3a4a] shadow-xl text-xs text-slate-300 leading-relaxed">
            {info}
          </div>
        </>
      )}
    </div>
  )
}

export function KeyValueInput({
  label,
  value,
  onChange,
  helper,
  info,
  keyPlaceholder = 'Key',
  valuePlaceholder = 'Value',
}: {
  label: string
  value: Record<string, string>
  onChange: (v: Record<string, string>) => void
  helper?: string
  info?: string
  keyPlaceholder?: string
  valuePlaceholder?: string
}) {
  const [rows, setRows] = useState<[string, string][]>(() =>
    Object.entries(value || {})
  )

  // Re-sync local rows if the parent value changes out from under us (reset,
  // fresh fetch, family switch) — but only when the committed view differs, so
  // an in-progress blank-key row isn't clobbered by our own onChange echo.
  useEffect(() => {
    const committed: Record<string, string> = {}
    for (const [k, v] of rows) {
      if (k.trim()) committed[k.trim()] = v
    }
    if (JSON.stringify(committed) !== JSON.stringify(value || {})) {
      setRows(Object.entries(value || {}))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  const commit = (next: [string, string][]) => {
    setRows(next)
    onChange(Object.fromEntries(next.filter(([k]) => k.trim())))
  }

  return (
    <div className="space-y-2">
      <label className="flex items-center text-xs text-slate-500 uppercase tracking-wide">
        {label}
        {info && <InfoBadge info={info} />}
      </label>
      {rows.map(([k, v], i) => (
        <div key={i} className="flex items-start gap-2">
          <input
            type="text"
            value={k}
            onChange={(e) => commit(rows.map((r, j) => (j === i ? [e.target.value, r[1]] as [string, string] : r)))}
            placeholder={keyPlaceholder}
            className="w-40 flex-shrink-0 px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
          />
          <input
            type="text"
            value={v}
            onChange={(e) => commit(rows.map((r, j) => (j === i ? [r[0], e.target.value] as [string, string] : r)))}
            placeholder={valuePlaceholder}
            className="flex-1 px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent placeholder-slate-600"
          />
          <button
            type="button"
            onClick={() => commit(rows.filter((_, j) => j !== i))}
            className="p-2 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded flex-shrink-0"
            aria-label="Remove header"
          >
            <Trash2 size={14} />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() => commit([...rows, ['', '']])}
        className="w-full py-2 border border-dashed border-[#1e2a3a] text-slate-500 hover:text-slate-300 hover:border-accent flex items-center justify-center gap-2 transition-colors"
      >
        <Plus size={16} /> Add Header
      </button>
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}
