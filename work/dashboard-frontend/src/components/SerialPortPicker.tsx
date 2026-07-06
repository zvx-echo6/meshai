import { useState } from 'react'
import { RefreshCw, Radio, Check } from 'lucide-react'
import { getSerialPorts, type SerialPort } from '@/lib/api'

// Reusable USB serial-port picker. Manual text entry is always available; the
// "Detect USB devices" button lists ports from GET /api/serial-ports and, when a
// port is picked, sets the STABLE by-id path (port.stable_path) — that path
// survives ttyACM* enumeration hops, which is the whole point.
export default function SerialPortPicker({
  value,
  onChange,
  label = 'Serial Port',
  helper = 'Device path for your USB radio — click Detect to auto-fill a stable by-id path',
}: {
  value: string
  onChange: (path: string) => void
  label?: string
  helper?: string
}) {
  const [ports, setPorts] = useState<SerialPort[] | null>(null)
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const detect = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await getSerialPorts()
      setPorts(res.ports)
      setNote(res.note || '')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to list serial ports')
      setPorts([])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-2">
      <div className="space-y-1">
        <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
        <div className="flex gap-2">
          <input
            type="text"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder="/dev/serial/by-id/usb-...  (or /dev/ttyACM0)"
            className="flex-1 px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
          />
          <button
            type="button"
            onClick={detect}
            disabled={loading}
            className="flex items-center gap-2 px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] hover:border-accent disabled:opacity-50 disabled:cursor-not-allowed rounded text-sm text-slate-300 whitespace-nowrap transition-colors"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            {loading ? 'Detecting...' : 'Detect USB devices'}
          </button>
        </div>
        {helper && <p className="text-xs text-slate-600">{helper}</p>}
      </div>

      {error && (
        <div className="p-3 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{error}</div>
      )}

      {ports !== null && !error && (
        ports.length === 0 ? (
          <div className="text-sm text-slate-500 p-3 border border-[#1e2a3a] rounded">
            No USB serial devices found — is the device passed through to the container?
          </div>
        ) : (
          <div className="border border-[#1e2a3a] rounded p-2 space-y-1">
            {ports.map((p) => {
              const selected = value === p.stable_path
              const title = p.product || p.description || p.device
              return (
                <button
                  type="button"
                  key={p.stable_path + p.device}
                  onClick={() => onChange(p.stable_path)}
                  className={`w-full text-left flex items-start gap-2 p-2 rounded hover:bg-[#0a0e17] transition-colors ${
                    selected ? 'bg-[#0a0e17] ring-1 ring-accent' : ''
                  }`}
                >
                  <div
                    className={`mt-0.5 w-4 h-4 rounded-full border flex items-center justify-center flex-shrink-0 ${
                      selected ? 'bg-accent border-accent' : 'border-slate-600'
                    }`}
                  >
                    {selected && <Check size={12} className="text-white" />}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-slate-200 truncate">{title}</span>
                      {p.likely_radio && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide bg-accent/15 text-accent border border-accent/30 flex-shrink-0">
                          <Radio size={10} /> likely radio
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-slate-500 font-mono truncate">{p.stable_path}</div>
                    {p.manufacturer && <div className="text-xs text-slate-600 truncate">{p.manufacturer}</div>}
                  </div>
                </button>
              )
            })}
          </div>
        )
      )}

      {note && <p className="text-xs text-slate-600 italic">{note}</p>}
    </div>
  )
}
