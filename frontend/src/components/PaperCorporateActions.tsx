import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function PaperCorporateActions({ account, receivables = 0 }: { account: string; receivables?: number }) {
  const client = useQueryClient()
  const [message, setMessage] = useState('')
  const query = useQuery({
    queryKey: QK.paperCorporateActions(account),
    queryFn: () => api.paperCorporateActions(account),
    refetchInterval: 30_000,
  })
  const mutation = useMutation({
    mutationFn: ({ id, qty }: { id: string; qty: number }) => api.paperSubscribeRights(account, id, qty),
    onSuccess: () => {
      setMessage('配股缴款已入账，上市前计入应收资产。')
      void client.invalidateQueries({ queryKey: QK.paperAll })
    },
    onError: (error: Error) => setMessage(error.message),
  })
  return (
    <section className="mt-3 rounded-card border border-border bg-surface p-3 text-xs">
      <div className="font-medium">公司行为与费用口径</div>
      <p className="mt-1 text-muted">印花税采用账户固定税率；回测可按历史税率计算。现金红利按税前金额记录，个人红利税未模拟。</p>
      {query.isLoading && <p className="mt-2 text-muted">正在读取权益记录…</p>}
      {query.isError && <p role="alert" className="mt-2 text-warning">权益记录加载失败：{query.error.message}</p>}
      {query.data?.mode === 'legacy_factor' && <p className="mt-2 text-warning">此存量账户沿用价格因子估算股数，不代表实际分红及配股账务。</p>}
      {query.data?.mode === 'events' && (
        <>
          <p className="mt-2">应收资产 ¥{receivables.toFixed(2)}（已计入总资产，不能用于买入）</p>
          {query.data.events.length === 0 && <p className="mt-1 text-muted">暂无与此账户交易范围相关的公司行为。</p>}
          {query.data.events.slice(-20).reverse().map(event => (
            <div key={event.id} className="mt-2 break-words border-t border-border pt-2">
              <div>{event.symbol} · 除权日 {event.ex_date} · 登记日 {event.record_date ?? '未知'}</div>
              <div className="mt-1 text-muted">
                每股红利 {event.cash_per_share} 元 · 送转 {event.bonus_per_share} 股 · 配股 {event.rights_per_share} 股
              </div>
              <div className="mt-1 text-muted">
                红利到账 {event.cash_pay_date ?? '未知'} · 送转上市 {event.bonus_listing_date ?? '未知'} · 配股上市 {event.rights_listing_date ?? '未知'}
              </div>
              {event.reason && <p className="mt-1 text-warning">日期证据不足或账务待核对；暂停相关事件入账及撮合。</p>}
              {event.subscribed_qty > 0 && <p className="mt-1">已认购 {event.subscribed_qty} 股</p>}
              {event.can_subscribe && (
                <button
                  className="mt-2 rounded-btn border border-border px-3 py-1.5 disabled:opacity-50"
                  disabled={mutation.isPending}
                  onClick={() => {
                    setMessage('')
                    mutation.mutate({ id: event.id, qty: Math.round(event.base_qty * event.rights_per_share) })
                  }}
                >
                  认购全部 {Math.round(event.base_qty * event.rights_per_share)} 股
                  （缴款 ¥{(Math.round(event.base_qty * event.rights_per_share) * (event.rights_price ?? 0)).toFixed(2)}）
                </button>
              )}
            </div>
          ))}
        </>
      )}
      {message && <p role="status" className="mt-2">{message}</p>}
    </section>
  )
}
