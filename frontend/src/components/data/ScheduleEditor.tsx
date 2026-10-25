import { useState, useEffect } from 'react'

export function ScheduleEditor({ value, onSave, loading, hint }: {
  value: { hour: number; minute: number }
  onSave: (hour: number, minute: number) => void
  loading: boolean
  hint?: string
}) {
  const [h, setH] = useState(value.hour)
  const [m, setM] = useState(value.minute)

  useEffect(() => { setH(value.hour); setM(value.minute) }, [value.hour, value.minute])
  const invalid = !Number.isInteger(h) || !Number.isInteger(m)
    || h < 0 || h > 23 || m < 0 || m > 59 || h * 60 + m < 15 * 60 + 35

  const handleSave = () => {
    if (loading || invalid || (h === value.hour && m === value.minute)) return
    onSave(h, m)
  }

  return (
    <div className="flex flex-wrap items-center gap-2 pt-1.5">
      <span className="text-[10px] text-muted">交易日</span>
      <input
        type="number" min={15} max={23} value={h} aria-label="管道同步小时"
        onChange={e => setH(Math.max(0, Math.min(23, Number(e.target.value))))}
        className="w-12 px-1.5 py-1 rounded-btn bg-base border border-border text-xs font-mono text-foreground text-center"
      />
      <span className="text-xs text-muted">:</span>
      <input
        type="number" min={h === 15 ? 35 : 0} max={59} value={m} aria-label="管道同步分钟"
        onChange={e => setM(Math.max(0, Math.min(59, Number(e.target.value))))}
        className="w-12 px-1.5 py-1 rounded-btn bg-base border border-border text-xs font-mono text-foreground text-center"
      />
      <button
        onClick={handleSave}
        disabled={loading || invalid || (h === value.hour && m === value.minute)}
        className="px-2.5 py-1 rounded-btn bg-accent/15 text-accent text-[11px] font-medium hover:bg-accent/25 disabled:opacity-40 transition-colors"
      >
        {loading ? '保存中…' : '保存'}
      </button>
      <span className="text-[10px] text-muted">北京时间 · 休市日跳过 · {hint ?? '不早于 15:35'}</span>
    </div>
  )
}
