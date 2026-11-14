import { useState, useCallback, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { storage } from '@/lib/storage'
import { migrateStoredStrategyValue } from './strategyIdMigration'
import { accountScope } from './accountSession'
import { api } from './api'
import { QK } from './queryKeys'

// 旧版按周期隔离的双池 ('strategy-pool' 日线 + 'strategy-pool-1m' 分钟) 已合并为
// 统一池 ('strategy-pool'): 策略自带周期声明, 执行时按各自 timeframes 路由,
// 池不再按周期隔离。此处做一次性迁移: 日线在前、分钟在后、按 ID 去重,
// 服务端接受旧选择后才清除旧分钟 key；读取失败保留迁移证据。
function loadUnifiedPool(): string[] {
  const minute = storage.strategyPoolMinute.get([])
  const daily = storage.strategyPool.get([])
  // 旧标识只用于已保存偏好的单次迁移, 不再注册或运行旧策略。
  const merged = [...new Set(migrateStoredStrategyValue([...daily, ...minute]) as string[])]
  return merged
}

/**
 * 策略池 — 同账号/租户的服务器名单，浏览器偏好仅作初次迁移及读取失败的展示。
 * 卡片列表可按周期筛选显示, 但池本身只有一份;
 * addToPool 不再区分周期 (构建器/叠加策略创建的日线策略与分钟策略同池)。
 */
export function useStrategyPool() {
  const [legacy] = useState<string[]>(loadUnifiedPool)
  const [saving, setSaving] = useState(false)
  const scope = accountScope()
  const qc = useQueryClient()
  const imported = useRef(false)
  const savingRef = useRef(false)
  const queryKey = QK.strategyPool(scope)
  const query = useQuery({
    queryKey,
    queryFn: async () => {
      let saved = await api.strategyPool()
      if (!imported.current && legacy.length) {
        saved = await api.strategyPoolSave({ ids: legacy, revision: saved.revision, migrate: true })
      }
      imported.current = true
      return saved
    },
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always',
    // Also covers standalone mode and temporarily disconnected event streams.
    refetchInterval: 30_000,
    retry: false,
  })
  const pool = query.data?.ids ?? legacy
  useEffect(() => {
    if (!query.data || accountScope() !== scope) return
    storage.strategyPool.set(query.data.ids)
    storage.strategyPoolMinute.remove()
  }, [query.data, scope])

  const reorderPool = useCallback(async (ids: string[], revision = query.data?.revision) => {
    if (revision === undefined || query.isError) throw new Error('策略池尚未同步，请重试后再保存')
    if (savingRef.current) throw new Error('策略池正在保存，请稍后重试')
    savingRef.current = true
    setSaving(true)
    try {
      await qc.cancelQueries({ queryKey: QK.strategyPool(scope) })
      const saved = await api.strategyPoolSave({ ids, revision })
      if (accountScope() !== scope) return
      qc.setQueryData(QK.strategyPool(scope), saved)
      storage.strategyPool.set(saved.ids)
      storage.strategyPoolMinute.remove()
    } finally {
      savingRef.current = false
      if (accountScope() === scope) setSaving(false)
    }
  }, [qc, query.data?.revision, query.isError, scope])

  const addToPool = useCallback((id: string) =>
    reorderPool(pool.includes(id) ? pool : [...pool, id]), [pool, reorderPool])
  const removeFromPool = useCallback((id: string) =>
    reorderPool(pool.filter(item => item !== id)), [pool, reorderPool])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return { pool, addToPool, removeFromPool, reorderPool, isInPool,
    revision: query.data?.revision, loading: query.isPending, saving,
    error: query.error, retry: query.refetch }
}
