import type { EntryGroups } from '@/lib/api'
import { defaultEntryGroups, ENTRY_GROUP_FIELDS } from '@/lib/entryGroups'

export function EntryGroupsEditor({ value, onChange }: {
  value?: EntryGroups | null
  onChange: (value: EntryGroups | null) => void
}) {
  return <section className="space-y-3 border-t border-border pt-4" aria-label="A/B入场筛选">
    <label className="flex items-center gap-2 text-xs font-medium text-foreground">
      <input type="checkbox" checked={!!value} onChange={e =>
        onChange(e.target.checked ? defaultEntryGroups() : null)} />
      启用A/B入场筛选
    </label>
    <p className="text-[11px] leading-5 text-muted">
      股票须满足A组全部条件，或B组全部条件。两组为“或”关系；同时满足按A组优先处理。
      启用后先通过上方共用条件，再筛选前日五维前30名单。
    </p>
    {value && <>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {(['a', 'b'] as const).map(name => {
          const group = value[name]
          const title = `${name.toUpperCase()}组`
          return <fieldset key={name} className="rounded-card border border-border p-3 space-y-3">
            <legend className="px-1 text-xs font-medium text-foreground">{title}</legend>
            <label className="flex items-center gap-2 text-xs text-foreground">
              <input type="checkbox" aria-label={`启用${title}`} checked={group.enabled}
                onChange={e => onChange({ ...value, [name]: { ...group, enabled: e.target.checked } })} />
              启用{title}
            </label>
            <p className="text-[10px] text-muted">{name === 'a'
              ? '优先于B组；组内按五维排名升序。'
              : '组内按情绪分降序，再按五维排名、代码升序。'}</p>
            {ENTRY_GROUP_FIELDS.map(([key, label, min, max, step]) =>
              <label key={key} className="block space-y-1">
                <span className="text-[11px] text-muted">{label}</span>
                <input type="number" aria-label={`${title} ${label}`} min={min} max={max} step={step}
                  disabled={!group.enabled} value={group[key] ?? ''} placeholder="为空不生效"
                  onChange={e => onChange({ ...value, [name]: { ...group,
                    [key]: e.target.value === '' ? null : Number(e.target.value) } })}
                  className="h-8 w-full rounded-btn border border-border bg-base px-2 text-xs text-foreground disabled:opacity-40" />
              </label>)}
          </fieldset>
        })}
      </div>
      <p className="text-[10px] leading-5 text-muted">
        分数、动量、乖离、均线变化及广度来自精确前交易日归档。
        开盘缺口相对原始昨收；相对开盘涨幅及日内价格位置使用启动截止前已完成分钟。
        位置＝（现价−当日最低）÷（当日最高−当日最低）；高低价相同或数据缺失时不能通过该条件。
        留空停用单项条件，启用组至少保留一项。
      </p>
    </>}
  </section>
}
