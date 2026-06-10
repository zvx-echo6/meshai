import { useState, useEffect } from 'react'
import { Check } from 'lucide-react'

interface Channel {
  index: number
  name: string
  role: string
  enabled: boolean
}

interface ChannelPickerSingleProps {
  label: string
  value: number
  onChange: (value: number) => void
  helper?: string
  info?: string
  mode: 'single'
  includeDisabled?: boolean  // Include a "Disabled (-1)" option
}

interface ChannelPickerMultiProps {
  label: string
  value: number[]
  onChange: (value: number[]) => void
  helper?: string
  info?: string
  mode: 'multi'
}

type ChannelPickerProps = ChannelPickerSingleProps | ChannelPickerMultiProps

export default function ChannelPicker(props: ChannelPickerProps) {
  const [channels, setChannels] = useState<Channel[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/api/channels')
      .then(res => res.json())
      .then(data => {
        setChannels(data)
        setLoading(false)
      })
      .catch(() => {
        setChannels([])
        setLoading(false)
      })
  }, [])

  const formatChannel = (ch: Channel): string => {
    const roleLabel = ch.role === 'PRIMARY' ? 'Primary' :
                      ch.role === 'SECONDARY' ? 'Secondary' : ''
    return `${ch.index}: ${ch.name}${roleLabel ? ` (${roleLabel})` : ''}`
  }

  // Fallback to number input if no channels loaded
  if (!loading && channels.length === 0) {
    if (props.mode === 'single') {
      return (
        <div className="space-y-1">
          <label className="block text-xs text-slate-500 uppercase tracking-wide">{props.label}</label>
          <input
            type="number"
            value={props.value}
            onChange={(e) => props.onChange(Number(e.target.value))}
            min={props.includeDisabled ? -1 : 0}
            max={7}
            className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
          />
          {props.helper && <p className="text-xs text-slate-600">{props.helper}</p>}
        </div>
      )
    } else {
      return (
        <div className="space-y-1">
          <label className="block text-xs text-slate-500 uppercase tracking-wide">{props.label}</label>
          <input
            type="text"
            value={props.value.join(', ')}
            onChange={(e) => {
              const nums = e.target.value.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n))
              props.onChange(nums)
            }}
            placeholder="Enter channel numbers separated by commas"
            className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent"
          />
          {props.helper && <p className="text-xs text-slate-600">{props.helper}</p>}
        </div>
      )
    }
  }

  // Single select mode - dropdown
  if (props.mode === 'single') {
    const { value, onChange, label, helper, includeDisabled } = props
    const enabledChannels = channels.filter(ch => ch.enabled)

    return (
      <div className="space-y-1">
        <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
        <select
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 focus:outline-none focus:border-accent"
        >
          {includeDisabled && (
            <option value={-1}>Disabled</option>
          )}
          {enabledChannels.map((ch) => (
            <option key={ch.index} value={ch.index}>
              {formatChannel(ch)}
            </option>
          ))}
        </select>
        {helper && <p className="text-xs text-slate-600">{helper}</p>}
      </div>
    )
  }

  // Multi select mode - checkboxes
  const { value, onChange, label, helper } = props
  const enabledChannels = channels.filter(ch => ch.enabled)

  const toggleChannel = (index: number) => {
    if (value.includes(index)) {
      onChange(value.filter(v => v !== index))
    } else {
      onChange([...value, index].sort((a, b) => a - b))
    }
  }

  return (
    <div className="space-y-1">
      <label className="block text-xs text-slate-500 uppercase tracking-wide">{label}</label>
      <div className="border border-[#1e2a3a] p-2 space-y-1">
        {enabledChannels.map((ch) => (
          <label
            key={ch.index}
            onClick={() => toggleChannel(ch.index)}
            className="flex items-center gap-2 p-2 rounded hover:bg-[#0a0e17] cursor-pointer"
          >
            <div className={`w-4 h-4 rounded border flex items-center justify-center ${
              value.includes(ch.index) ? 'bg-accent border-accent' : 'border-slate-600'
            }`}>
              {value.includes(ch.index) && <Check size={12} className="text-white" />}
            </div>
            <span className="text-sm text-slate-200">{formatChannel(ch)}</span>
          </label>
        ))}
        {enabledChannels.length === 0 && (
          <div className="text-sm text-slate-500 p-2">No channels available</div>
        )}
      </div>
      {helper && <p className="text-xs text-slate-600">{helper}</p>}
    </div>
  )
}
