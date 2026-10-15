import { useQuery } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { api, type CustomSignalCondition } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { DaysInput, FieldPicker, RightValueInput } from '@/components/signals/CustomSignalDialog'

export function OverlayFilterEditor({ value, onChange, numericOnly = false }: {
  value: CustomSignalCondition[]
  onChange: (value: CustomSignalCondition[]) => void
  numericOnly?: boolean
}) {
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions })
  const stringFields = options.data?.stringFields ?? []
  const fields = (options.data?.fields ?? []).filter(f => !numericOnly || !stringFields.includes(f.key))
  const groups = options.data?.groups?.map(g => ({
    ...g, fields: g.fields.filter(f => !numericOnly || !stringFields.includes(f.key)),
  })).filter(g => g.fields.length > 0)
  const numericGroups = groups?.map(g => ({
    ...g, fields: g.fields.filter(f => !stringFields.includes(f.key)),
  })).filter(g => g.fields.length > 0)
  const maxDays = options.data?.maxDays ?? 60
  const update = (index: number, patch: Partial<CustomSignalCondition>) =>
    onChange(value.map((c, i) => i === index ? { ...c, ...patch } : c))

  return (
    <div className="space-y-2">
      <p className="text-[10px] leading-4 text-muted">
        所有条件同时满足才允许入场，对选股、监控和回测生效。退出不受影响；偏移按证券交易日计算，缺值不放行。
      </p>
      {options.isError && <p role="alert" className="text-xs text-danger">条件字段加载失败，请稍后重试。</p>}
      {value.map((c, i) => {
        const isString = stringFields.includes(c.left)
        const operators = isString ? ['contains', '==', '!='] : ['>', '>=', '<', '<=', '==', '!=']
        return (
          <div key={i} className="space-y-1.5 rounded-lg border border-border/30 p-2">
            <div className="flex items-center gap-1">
              <DaysInput value={c.leftDays ?? 0} max={maxDays} onChange={v => update(i, { leftDays: v })} />
              <FieldPicker value={c.left} fields={fields} groups={groups} onChange={left => {
                const nextString = stringFields.includes(left)
                update(i, { left, op: nextString ? 'contains' : '>', right: nextString ? '' : '0', rightDays: 0 })
              }} />
              <button type="button" aria-label={`删除条件 ${i + 1}`} onClick={() => onChange(value.filter((_, j) => i !== j))}
                className="p-1 text-muted hover:text-danger"><Trash2 className="h-3.5 w-3.5" /></button>
            </div>
            <div className="flex items-center gap-1">
              <select aria-label={`条件 ${i + 1} 运算符`} value={c.op} onChange={e => update(i, { op: e.target.value })}
                className="h-7 rounded border border-border bg-base px-1 text-xs">
                {operators.map(op => <option key={op} value={op}>{op === 'contains' ? '包含' : op}</option>)}
              </select>
              {isString ? (
                <input aria-label={`条件 ${i + 1} 右值`} value={c.right} maxLength={64}
                  onChange={e => update(i, { right: e.target.value })}
                  className="h-7 min-w-0 flex-1 rounded border border-border bg-base px-1.5 text-xs" />
              ) : (
                <RightValueInput cond={c} fields={fields.filter(f => !stringFields.includes(f.key))} groups={numericGroups}
                  maxDays={maxDays} onChangeRight={right => update(i, { right })}
                  onChangeDays={rightDays => update(i, { rightDays })} />
              )}
            </div>
          </div>
        )
      })}
      <button type="button" disabled={value.length >= 8 || !options.data} onClick={() => onChange([
        ...value, { left: 'close', op: '>', right: 'field:ma20', leftDays: 0, rightDays: 0 },
      ])} className="flex items-center gap-1 text-[11px] text-accent disabled:opacity-40">
        <Plus className="h-3 w-3" />添加条件（{value.length}/8）
      </button>
    </div>
  )
}
