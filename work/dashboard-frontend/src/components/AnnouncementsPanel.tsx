// Custom scheduled announcements (owner-authored free-text broadcasts on a
// clock-slot schedule). CRUD over /api/announcements -- see
// work/meshai/dashboard/api/announcement_routes.py for the backend
// contract this follows exactly (new rows always start disabled; PUT is
// the only place `enabled` can change; preview never sends).
import { useState, useEffect, useCallback } from 'react'
import { Plus, Trash2, Check, X, Loader2, Eye, Megaphone } from 'lucide-react'
import {
  fetchAnnouncements, createAnnouncement, updateAnnouncement, deleteAnnouncement,
  previewAnnouncement,
  type Announcement, type AnnouncementDraft, type AnnouncementChannelRef, type AnnouncementPreview,
} from '@/lib/api'
import { TextInput, NumberInput, TimeInput } from '@/pages/Notifications'
import { SelectInput } from '@/pages/Config'
import AnnouncementChannelPicker from '@/components/AnnouncementChannelPicker'

const DOW_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const MESSAGE_BUDGET_HINT = 140 // typical mesh packet budget; Preview after saving confirms the exact figure

function ordinal(n: number): string {
  const rem100 = n % 100
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`
  switch (n % 10) {
    case 1: return `${n}st`
    case 2: return `${n}nd`
    case 3: return `${n}rd`
    default: return `${n}th`
  }
}

function describeSchedule(a: {
  schedule_kind: string
  time_of_day: string
  interval_days: number | null
  dow_mask: boolean[] | null
  day_of_month: number | null
}): string {
  const t = a.time_of_day
  switch (a.schedule_kind) {
    case 'daily':
      return `Every day at ${t}`
    case 'interval_days': {
      const n = a.interval_days ?? 1
      return `Every ${n} day${n === 1 ? '' : 's'} at ${t}`
    }
    case 'weekly': {
      const days = (a.dow_mask ?? []).map((on, i) => (on ? DOW_LABELS[i] : null)).filter(Boolean)
      return days.length ? `${days.join('/')} at ${t}` : `No days selected, at ${t}`
    }
    case 'monthly':
      return `The ${ordinal(a.day_of_month ?? 1)} of each month at ${t}`
    default:
      return t
  }
}

function channelSummary(channels: AnnouncementChannelRef[]): string {
  if (channels.length === 0) return 'No channels'
  const names = channels.map((c) => c.name || String(c.channel))
  return `${channels.length} channel${channels.length === 1 ? '' : 's'}: ${names.join(', ')}`
}

function formatLastSent(v: number | null): string {
  if (!v) return 'Never'
  return new Date(v * 1000).toLocaleString()
}

const EMPTY_DRAFT: AnnouncementDraft = {
  name: '',
  message: '',
  schedule_kind: 'daily',
  time_of_day: '08:00',
  interval_days: 1,
  dow_mask: [false, false, false, false, false, false, false],
  day_of_month: 1,
  timezone: 'America/Boise',
  channels: [],
}

export default function AnnouncementsPanel() {
  const [rows, setRows] = useState<Announcement[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [formOpen, setFormOpen] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [draft, setDraft] = useState<AnnouncementDraft>(EMPTY_DRAFT)
  const [savedMessage, setSavedMessage] = useState<string>('') // the last-persisted message, for the preview staleness note
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [preview, setPreview] = useState<AnnouncementPreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)

  const [busyRowId, setBusyRowId] = useState<number | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      setRows(await fetchAnnouncements())
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const openNew = () => {
    setEditingId(null)
    setDraft({ ...EMPTY_DRAFT, dow_mask: [...EMPTY_DRAFT.dow_mask!] })
    setSavedMessage('')
    setPreview(null)
    setPreviewError(null)
    setSaveError(null)
    setFormOpen(true)
  }

  const openEdit = (a: Announcement) => {
    setEditingId(a.announcement_id)
    setDraft({
      name: a.name,
      message: a.message,
      schedule_kind: a.schedule_kind,
      time_of_day: a.time_of_day,
      interval_days: a.interval_days ?? 1,
      dow_mask: a.dow_mask ?? [false, false, false, false, false, false, false],
      day_of_month: a.day_of_month ?? 1,
      timezone: a.timezone,
      channels: a.channels,
    })
    setSavedMessage(a.message)
    setPreview(null)
    setPreviewError(null)
    setSaveError(null)
    setFormOpen(true)
  }

  const closeForm = () => {
    setFormOpen(false)
    setEditingId(null)
    setPreview(null)
    setPreviewError(null)
    setSaveError(null)
  }

  const save = async () => {
    setSaveError(null)
    if (!draft.name.trim()) { setSaveError('Name is required.'); return }
    if (!draft.message.trim()) { setSaveError('Message is required.'); return }
    if (draft.channels.length === 0) { setSaveError('Select at least one channel.'); return }
    if (draft.schedule_kind === 'interval_days' && (!draft.interval_days || draft.interval_days < 1)) {
      setSaveError('Interval must be at least 1 day.'); return
    }
    if (draft.schedule_kind === 'weekly' && !(draft.dow_mask ?? []).some(Boolean)) {
      setSaveError('Select at least one day of the week.'); return
    }
    if (draft.schedule_kind === 'monthly' && (!draft.day_of_month || draft.day_of_month < 1 || draft.day_of_month > 31)) {
      setSaveError('Day of month must be between 1 and 31.'); return
    }

    setSaving(true)
    try {
      if (editingId === null) {
        await createAnnouncement(draft)
        setNotice('Saved. Enable it when you’re ready — new announcements always start disabled.')
      } else {
        // Never send `enabled` from this form -- the list view's toggle is
        // the only thing allowed to arm/disarm an announcement.
        await updateAnnouncement(editingId, draft)
        setNotice('Saved.')
      }
      setFormOpen(false)
      setEditingId(null)
      await refresh()
      setTimeout(() => setNotice(null), 5000)
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const runPreview = async () => {
    if (editingId === null) return
    setPreviewLoading(true)
    setPreviewError(null)
    try {
      setPreview(await previewAnnouncement(editingId))
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : String(e))
    } finally {
      setPreviewLoading(false)
    }
  }

  const toggleEnabled = async (a: Announcement) => {
    setBusyRowId(a.announcement_id)
    try {
      await updateAnnouncement(a.announcement_id, { enabled: !a.enabled })
      await refresh()
    } catch (e) {
      alert(`Could not change enabled state: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusyRowId(null)
    }
  }

  const remove = async (a: Announcement) => {
    if (!confirm(`Delete announcement "${a.name}"? This cannot be undone.`)) return
    setBusyRowId(a.announcement_id)
    try {
      await deleteAnnouncement(a.announcement_id)
      await refresh()
    } catch (e) {
      alert(`Delete failed: ${e instanceof Error ? e.message : String(e)}`)
      setBusyRowId(null)
    }
  }

  const messageLen = draft.message.length
  const overBudget = messageLen > MESSAGE_BUDGET_HINT
  const messageDirty = editingId !== null && draft.message !== savedMessage

  return (
    <div className="bg-bg-card border border-border p-6 space-y-4">
      <div className="flex items-center gap-2">
        <Megaphone size={16} className="text-accent" />
        <label className="text-xs text-slate-500 uppercase tracking-wide">Announcements</label>
        <span className="text-xs text-slate-600">{rows.length} configured</span>
        {!formOpen && (
          <button
            onClick={openNew}
            className="ml-auto flex items-center gap-1 px-3 py-1.5 bg-accent hover:bg-accent/80 text-white text-sm transition-colors"
          >
            <Plus size={14} /> New announcement
          </button>
        )}
      </div>
      <p className="text-xs text-slate-600">
        Free-text broadcasts you write and schedule yourself, separate from the automatic hazard/weather
        notifications above. New announcements always save disabled &mdash; nothing transmits until you
        switch one on below.
      </p>

      {notice && (
        <div className="p-3 text-sm bg-green-500/10 text-green-400 border border-green-500/20">
          <Check size={14} className="inline mr-2" />{notice}
        </div>
      )}

      {/* ---- Create / edit form ---- */}
      {formOpen && (
        <div className="border border-[#2a3a4a] p-4 space-y-4 bg-[#0d1420]">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-slate-200">{editingId === null ? 'New announcement' : 'Edit announcement'}</h3>
            <button onClick={closeForm} className="text-slate-500 hover:text-slate-300"><X size={16} /></button>
          </div>

          {saveError && (
            <div className="p-2 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{saveError}</div>
          )}

          <TextInput
            label="Name"
            value={draft.name}
            onChange={(v) => setDraft({ ...draft, name: v })}
            placeholder="Short label, e.g. Range closure notice"
            helper="For your own reference in the list below -- not sent on the air."
          />

          <div className="space-y-1">
            <label className="block text-xs text-slate-500 uppercase tracking-wide">Message</label>
            <textarea
              value={draft.message}
              onChange={(e) => setDraft({ ...draft, message: e.target.value })}
              rows={4}
              placeholder="The exact text to broadcast"
              className="w-full px-3 py-2 bg-[#0a0e17] border border-[#1e2a3a] rounded text-sm text-slate-200 font-mono focus:outline-none focus:border-accent placeholder-slate-600"
            />
            <p className={`text-xs ${overBudget ? 'text-amber-400' : 'text-slate-600'}`}>
              {messageLen} / {MESSAGE_BUDGET_HINT} characters
              {overBudget && ' — this will be truncated to fit the packet budget. Save, then use Preview below to see exactly what gets sent.'}
            </p>
          </div>

          <SelectInput
            label="Schedule"
            value={draft.schedule_kind}
            onChange={(v) => setDraft({ ...draft, schedule_kind: v as AnnouncementDraft['schedule_kind'] })}
            options={[
              { value: 'daily', label: 'Every day' },
              { value: 'interval_days', label: 'Every N days' },
              { value: 'weekly', label: 'Specific days of the week' },
              { value: 'monthly', label: 'Specific day of the month' },
            ]}
          />

          {draft.schedule_kind === 'interval_days' && (
            <NumberInput
              label="Interval (days)"
              value={draft.interval_days ?? 1}
              onChange={(v) => setDraft({ ...draft, interval_days: v })}
              min={1}
              helper="Sends every N days, counted from the first day it fires."
            />
          )}

          {draft.schedule_kind === 'weekly' && (
            <div className="space-y-1">
              <label className="block text-xs text-slate-500 uppercase tracking-wide">Days of the week</label>
              <div className="flex flex-wrap gap-2">
                {DOW_LABELS.map((label, i) => {
                  const checked = (draft.dow_mask ?? [])[i] ?? false
                  return (
                    <label
                      key={label}
                      onClick={() => {
                        const mask = [...(draft.dow_mask ?? [false, false, false, false, false, false, false])]
                        mask[i] = !mask[i]
                        setDraft({ ...draft, dow_mask: mask })
                      }}
                      className={`px-3 py-1.5 text-sm cursor-pointer border ${
                        checked ? 'bg-accent border-accent text-white' : 'border-[#1e2a3a] text-slate-300 hover:bg-[#0a0e17]'
                      }`}
                    >
                      {label}
                    </label>
                  )
                })}
              </div>
            </div>
          )}

          {draft.schedule_kind === 'monthly' && (
            <NumberInput
              label="Day of month"
              value={draft.day_of_month ?? 1}
              onChange={(v) => setDraft({ ...draft, day_of_month: v })}
              min={1}
              max={31}
              helper="If the month is shorter than this day (e.g. day 31 in April), it fires on that month's last day instead."
            />
          )}

          <div className="grid grid-cols-2 gap-3">
            <TimeInput
              label="Time of day"
              value={draft.time_of_day}
              onChange={(v) => setDraft({ ...draft, time_of_day: v })}
            />
            <TextInput
              label="Timezone"
              value={draft.timezone}
              onChange={(v) => setDraft({ ...draft, timezone: v })}
              helper="IANA timezone name, e.g. America/Boise."
            />
          </div>

          <AnnouncementChannelPicker
            value={draft.channels}
            onChange={(v) => setDraft({ ...draft, channels: v })}
          />

          {/* Preview -- requires a saved row, since the backend reads the
              stored message by id. Never sends. */}
          <div className="space-y-2 pt-2 border-t border-[#1e2a3a]">
            <div className="flex items-center gap-2">
              <button
                onClick={runPreview}
                disabled={editingId === null || previewLoading}
                className="flex items-center gap-1 px-3 py-1.5 border border-[#1e2a3a] text-slate-300 hover:bg-[#0a0e17] disabled:opacity-40 disabled:cursor-not-allowed text-sm transition-colors"
              >
                <Eye size={14} /> {previewLoading ? 'Loading preview...' : 'Preview'}
              </button>
              <span className="text-xs text-slate-600">
                {editingId === null
                  ? 'Save the announcement first, then Preview shows the exact wire text. This never sends anything.'
                  : 'Shows the exact wire text and size. This never sends anything.'}
              </span>
            </div>
            {messageDirty && editingId !== null && (
              <p className="text-xs text-amber-400">You&apos;ve edited the message since it was last saved -- Preview still shows the saved version until you save again.</p>
            )}
            {previewError && <div className="p-2 text-sm bg-red-500/10 text-red-400 border border-red-500/20">{previewError}</div>}
            {preview && (
              <div className="p-3 bg-[#0a0e17] border border-[#1e2a3a] space-y-1">
                <p className="text-sm text-slate-200 font-mono whitespace-pre-wrap break-words">{preview.wire_text}</p>
                <p className={`text-xs ${preview.truncated ? 'text-amber-400' : 'text-slate-500'}`}>
                  {preview.char_count} characters / {preview.byte_count} bytes (budget {preview.budget})
                  {preview.truncated ? ' — truncated to fit the budget' : ' — fits, not truncated'}
                </p>
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 pt-2">
            <button onClick={closeForm} className="px-3 py-1.5 text-slate-400 hover:text-slate-200 text-sm">Cancel</button>
            <button
              onClick={save}
              disabled={saving}
              className="flex items-center gap-1 px-4 py-1.5 bg-accent hover:bg-accent/80 disabled:bg-slate-700 disabled:cursor-not-allowed text-white text-sm transition-colors"
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
      )}

      {/* ---- List ---- */}
      {loading ? (
        <div className="text-sm text-slate-500 flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> Loading announcements...</div>
      ) : loadError ? (
        <div className="text-sm text-red-400">Failed to load announcements: {loadError}</div>
      ) : rows.length === 0 ? (
        <div className="text-sm text-slate-500">No announcements yet.</div>
      ) : (
        <div className="border border-border divide-y divide-border">
          {rows.map((a) => (
            <div key={a.announcement_id} className="p-3 flex items-start gap-3">
              <button
                onClick={() => toggleEnabled(a)}
                disabled={busyRowId === a.announcement_id}
                title={a.enabled ? 'Enabled -- click to disable' : 'Disabled -- click to enable'}
                className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 mt-0.5 disabled:opacity-50 ${
                  a.enabled ? 'bg-accent' : 'bg-[#1e2a3a]'
                }`}
              >
                <span className={`absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform ${a.enabled ? 'translate-x-5' : ''}`} />
              </button>
              <div className="flex-1 min-w-0 space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-slate-100 font-medium">{a.name}</span>
                  <span className={`text-xs ${a.enabled ? 'text-green-400' : 'text-slate-500'}`}>
                    {a.enabled ? 'Enabled' : 'Disabled'}
                  </span>
                </div>
                <p className="text-xs text-slate-400">{describeSchedule(a)}</p>
                <p className="text-xs text-slate-500 truncate">{channelSummary(a.channels)}</p>
                <p className="text-xs text-slate-600">Last sent: {formatLastSent(a.last_sent_at)}</p>
              </div>
              <div className="flex items-center gap-3 flex-shrink-0">
                <button onClick={() => openEdit(a)} className="text-accent hover:text-accent text-xs">Edit</button>
                <button
                  onClick={() => remove(a)}
                  disabled={busyRowId === a.announcement_id}
                  className="text-red-400 hover:text-red-300 disabled:opacity-50"
                  title="Delete"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
