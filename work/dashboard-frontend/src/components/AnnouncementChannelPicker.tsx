// Checkbox channel picker for custom announcements. Polls both radios for
// their CURRENT channels and lets the owner check any number of them,
// mixing Meshtastic and MeshCore freely. This is deliberately NOT the
// single-transport ChannelPicker.tsx (that one is Meshtastic-only and
// stores plain channel indices) -- announcement channels are a mixed-
// transport list of {transport, channel, name}.
import { useState, useEffect, useCallback } from 'react'
import { Check, AlertTriangle } from 'lucide-react'
import {
  fetchMeshtasticChannels, getMeshcoreChannelsDetail,
  type MeshtasticChannel, type MeshcoreChannelsDetail, type AnnouncementChannelRef,
} from '@/lib/api'

interface Props {
  value: AnnouncementChannelRef[]
  onChange: (value: AnnouncementChannelRef[]) => void
}

export default function AnnouncementChannelPicker({ value, onChange }: Props) {
  const [mtChannels, setMtChannels] = useState<MeshtasticChannel[] | null>(null)
  const [mcDetail, setMcDetail] = useState<MeshcoreChannelsDetail | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    setLoading(true)
    Promise.allSettled([fetchMeshtasticChannels(), getMeshcoreChannelsDetail()]).then(
      ([mt, mc]) => {
        setMtChannels(mt.status === 'fulfilled' ? mt.value : [])
        setMcDetail(mc.status === 'fulfilled' ? mc.value : { active: false, channels: [] })
        setLoading(false)
      }
    )
  }, [])

  useEffect(() => { load() }, [load])

  const isSelected = (transport: 'meshtastic' | 'meshcore', channel: number | string) =>
    value.some((v) => v.transport === transport && v.channel === channel)

  const toggle = (transport: 'meshtastic' | 'meshcore', channel: number | string, name: string) => {
    if (isSelected(transport, channel)) {
      onChange(value.filter((v) => !(v.transport === transport && v.channel === channel)))
    } else {
      onChange([...value, { transport, channel, name }])
    }
  }

  if (loading) {
    return <div className="text-sm text-slate-500">Polling radios for current channels...</div>
  }

  // Meshtastic: only channels the radio currently reports as enabled are
  // offerable. Empty list (radio unreachable or nothing enabled) still
  // needs to surface any channel this announcement already had selected,
  // by its stored name, so an existing announcement stays editable.
  const mtLive = (mtChannels ?? []).filter((c) => c.enabled)
  const mtStaleSelections = value.filter(
    (v) => v.transport === 'meshtastic' && !mtLive.some((c) => c.index === v.channel)
  )

  const mcActive = mcDetail?.active ?? false
  const mcLive = mcDetail?.channels ?? []
  const mcStaleSelections = value.filter(
    (v) => v.transport === 'meshcore' && !mcLive.some((c) => c.name === v.channel)
  )

  return (
    <div className="space-y-4">
      {/* Meshtastic */}
      <div className="space-y-1">
        <label className="block text-xs text-slate-500 uppercase tracking-wide">Meshtastic channels</label>
        {mtLive.length === 0 && (
          <div className="flex items-center gap-2 text-xs text-amber-400 mb-1">
            <AlertTriangle size={13} />
            Meshtastic radio unreachable, or it isn&apos;t reporting any enabled channels right now.
          </div>
        )}
        <div className="border border-[#1e2a3a] p-2 space-y-1">
          {mtLive.map((ch) => (
            <label
              key={ch.index}
              onClick={() => toggle('meshtastic', ch.index, ch.name || `Channel ${ch.index}`)}
              className="flex items-center gap-2 p-2 rounded hover:bg-[#0a0e17] cursor-pointer"
            >
              <CheckBox checked={isSelected('meshtastic', ch.index)} />
              <span className="text-sm text-slate-200">
                {ch.index}: {ch.name || `Channel ${ch.index}`}
                {ch.role === 'PRIMARY' && <span className="text-slate-500"> (Primary)</span>}
              </span>
            </label>
          ))}
          {mtStaleSelections.map((v) => (
            <label
              key={`stale-mt-${v.channel}`}
              onClick={() => toggle('meshtastic', v.channel, v.name || `Channel ${v.channel}`)}
              className="flex items-center gap-2 p-2 rounded hover:bg-[#0a0e17] cursor-pointer"
            >
              <CheckBox checked />
              <span className="text-sm text-slate-400">
                {v.name || `Channel ${v.channel}`} <span className="text-slate-600">(not currently reported by the radio)</span>
              </span>
            </label>
          ))}
          {mtLive.length === 0 && mtStaleSelections.length === 0 && (
            <div className="text-sm text-slate-500 p-2">No Meshtastic channels available.</div>
          )}
        </div>
      </div>

      {/* MeshCore */}
      <div className="space-y-1">
        <label className="block text-xs text-slate-500 uppercase tracking-wide">MeshCore channels</label>
        {!mcActive && (
          <div className="flex items-center gap-2 text-xs text-amber-400 mb-1">
            <AlertTriangle size={13} />
            MeshCore companion unreachable right now.
          </div>
        )}
        <div className="border border-[#1e2a3a] p-2 space-y-1">
          {mcLive.map((ch) => (
            <label
              key={ch.name}
              onClick={() => toggle('meshcore', ch.name, ch.name)}
              className="flex items-center gap-2 p-2 rounded hover:bg-[#0a0e17] cursor-pointer"
            >
              <CheckBox checked={isSelected('meshcore', ch.name)} />
              <span className="text-sm text-slate-200">{ch.name}</span>
            </label>
          ))}
          {mcStaleSelections.map((v) => (
            <label
              key={`stale-mc-${v.channel}`}
              onClick={() => toggle('meshcore', v.channel, v.name || String(v.channel))}
              className="flex items-center gap-2 p-2 rounded hover:bg-[#0a0e17] cursor-pointer"
            >
              <CheckBox checked />
              <span className="text-sm text-slate-400">
                {v.name || v.channel} <span className="text-slate-600">(not currently reported by the companion)</span>
              </span>
            </label>
          ))}
          {mcLive.length === 0 && mcStaleSelections.length === 0 && (
            <div className="text-sm text-slate-500 p-2">No MeshCore channels available.</div>
          )}
        </div>
      </div>

      {value.length === 0 && (
        <p className="text-xs text-red-400">Select at least one channel -- an announcement with nowhere to send can&apos;t be saved.</p>
      )}
    </div>
  )
}

function CheckBox({ checked }: { checked: boolean }) {
  return (
    <div className={`w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 ${
      checked ? 'bg-accent border-accent' : 'border-slate-600'
    }`}>
      {checked && <Check size={12} className="text-white" />}
    </div>
  )
}
