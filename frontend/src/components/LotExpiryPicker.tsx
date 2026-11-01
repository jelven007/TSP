import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { cnDateFromUtc } from '@/lib/format'
import { DatePicker } from './DatePicker'

interface Props {
  value: string
  onChange: (value: string) => void
  buyDate?: string | null
  ariaLabel: string
}

/** 批次与单股共用到期日期; 快捷日由服务端交易日历计算。 */
export function LotExpiryPicker({ value, onChange, buyDate, ariaLabel }: Props) {
  const base = buyDate || cnDateFromUtc(new Date().toISOString())
  const dates = useQuery({
    queryKey: QK.lotExpiryOptions(base),
    queryFn: () => api.lotExpiryOptions(base),
    staleTime: 300000,
    retry: false,
  })
  const options = [
    { label: '下个交易日', date: dates.data?.next_trading_day },
    { label: '5个交易日', date: dates.data?.five_trading_days },
  ]
  const unavailable = dates.isSuccess && options.some(option => !option.date)

  return <div className="min-w-0 space-y-1.5">
    <div className="flex flex-wrap items-center gap-1">
      {options.map(option => {
        const active = !!option.date && value === option.date
        return <button key={option.label} type="button"
          aria-pressed={active} title={option.date || '请使用选择日期'}
          disabled={dates.isFetching || dates.isError || !option.date}
          onClick={() => { if (option.date) onChange(active ? '' : option.date) }}
          className={cn(
            'rounded-md border px-2 py-1 text-[10px] leading-none transition-colors disabled:opacity-40 disabled:cursor-not-allowed',
            active ? 'bg-accent/15 text-accent border-accent/30'
              : 'bg-elevated text-muted border-transparent hover:text-foreground',
          )}>
          {option.label}
        </button>
      })}
    </div>
    <DatePicker aria-label={ariaLabel} value={value} onChange={onChange}
      placeholder="选择日期" clearable className="w-full" buttonClassName="w-full justify-start" />
    <p className="text-[10px] text-muted">从{buyDate ? '买入日期' : '今天'}起算，不含当天。</p>
    {dates.isPending && <p role="status" className="text-[10px] text-muted">交易日历加载中…</p>}
    {dates.isError && <p role="status" className="text-[10px] text-warning">交易日历加载失败，请选择日期。</p>}
    {unavailable && <p role="status" className="text-[10px] text-warning">部分交易日历未覆盖，请选择日期。</p>}
  </div>
}
