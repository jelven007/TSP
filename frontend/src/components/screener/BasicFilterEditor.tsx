import { BASIC_FILTER_RANGES, type NumericBasicFilter } from '@/lib/strategyBasicFilter'

export const ALL_BOARDS = ['沪主板', '深主板', '创业板', '科创板']

export function RangeField({ label, minVal, maxVal, onMinChange, onMaxChange, unit, step, disabled, min = 0, max }: {
  label: string
  minVal: number | null | undefined
  maxVal: number | null | undefined
  onMinChange: (value: number | null) => void
  onMaxChange: (value: number | null) => void
  unit?: string
  step?: string
  disabled?: boolean
  min?: number
  max?: number
}) {
  const inputClass = 'w-0 min-w-0 flex-1 px-1.5 py-1 rounded bg-base border border-border text-xs font-mono text-foreground text-center focus:outline-none focus:border-accent/50 disabled:opacity-40'
  return <div className="flex items-center gap-1.5">
    <span className="text-xs text-secondary w-16 shrink-0 text-right">{label}</span>
    <input type="number" aria-label={`${label}最小值`} value={minVal ?? ''} min={min} max={max}
      disabled={disabled} step={step} placeholder="不限" className={inputClass}
      onChange={e => onMinChange(e.target.value === '' ? null : Number(e.target.value))} />
    <span className="text-[10px] text-muted">~</span>
    <input type="number" aria-label={`${label}最大值`} value={maxVal ?? ''} min={min} max={max}
      disabled={disabled} step={step} placeholder="不限" className={inputClass}
      onChange={e => onMaxChange(e.target.value === '' ? null : Number(e.target.value))} />
    {unit && <span className="text-[10px] text-muted w-4 shrink-0">{unit}</span>}
  </div>
}

export function BasicFilterEditor({ value, onChange, disabled = false, alwaysEnabled = false }: {
  value: NumericBasicFilter
  onChange: (next: NumericBasicFilter) => void
  disabled?: boolean
  alwaysEnabled?: boolean
}) {
  const enabled = alwaysEnabled || value.enabled !== false
  return <div className="space-y-3">
    {!alwaysEnabled && <label className="flex items-center gap-2 text-xs text-secondary">
      <input type="checkbox" aria-label="启用数值过滤" checked={enabled} disabled={disabled}
        onChange={e => onChange({ ...value, enabled: e.target.checked })} />
      启用数值过滤
    </label>}
    <p className="text-[11px] leading-4 text-muted">以下7项均使用上一个交易日数据，价格范围对应收盘价。留空取消限制。{!alwaysEnabled && '关闭数值过滤仍保留股票范围和 ST 选择。'}</p>
    <div className="space-y-2">
      {BASIC_FILTER_RANGES.map(({ key, label, scale, unit, step }) => {
        const min = value[`${key}_min`], max = value[`${key}_max`]
        return <RangeField key={key} label={label} unit={unit} step={step}
          min={key === 'change_pct' ? -50 : 0} max={key === 'change_pct' ? 50 : undefined}
          minVal={min == null ? null : min / scale} maxVal={max == null ? null : max / scale}
          disabled={disabled || !enabled}
          onMinChange={v => onChange({ ...value, enabled, [`${key}_min`]: v == null ? null : v * scale })}
          onMaxChange={v => onChange({ ...value, enabled, [`${key}_max`]: v == null ? null : v * scale })} />
      })}
      <p className="text-[10px] text-muted">涨跌幅：- 表示跌幅，+ 表示涨幅；范围 -50%～+50%，最多1位小数。</p>
      <label className="flex items-center gap-1.5">
        <span className="text-xs text-secondary w-16 shrink-0 text-right">上市天数</span>
        <input type="number" aria-label="上市天数" value={value.exclude_new_days ?? ''}
          disabled={disabled || !enabled} min={0} step={1} placeholder="不限"
          onChange={e => onChange({ ...value, enabled, exclude_new_days: e.target.value === '' ? null : Number(e.target.value) })}
          className="min-w-0 flex-1 w-0 px-1.5 py-1 rounded bg-base border border-border text-xs font-mono text-foreground text-center disabled:opacity-40" />
        <span className="text-[10px] text-muted shrink-0">天以上</span>
      </label>
    </div>
  </div>
}
