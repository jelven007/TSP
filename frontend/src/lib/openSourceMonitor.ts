import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { accountScope, accountStorage } from '@/lib/accountSession'
import { isOpenSourceRelease, OPEN_SOURCE_REPO, type OpenSourceRelease } from '@/lib/api/openSourceRelease'
import { QK } from '@/lib/queryKeys'

const CACHE_KEY = 'open_source_release_monitor'
const CHECK_INTERVAL = 60 * 60 * 1000

interface MonitorSnapshot {
  schema: 1
  repository: typeof OPEN_SOURCE_REPO
  seen: OpenSourceRelease | null
  latest: OpenSourceRelease | null
  checkedAt: number
}

function readSnapshot(): MonitorSnapshot | undefined {
  try {
    const raw = accountStorage.getItem(CACHE_KEY)
    if (!raw) return undefined
    const value = JSON.parse(raw)
    if (value.schema !== 1 || value.repository !== OPEN_SOURCE_REPO
      || !(value.seen === null || isOpenSourceRelease(value.seen))
      || !(value.latest === null || isOpenSourceRelease(value.latest))
      || typeof value.checkedAt !== 'number' || !Number.isFinite(value.checkedAt) || value.checkedAt <= 0) {
      return undefined
    }
    return value
  } catch {
    return undefined
  }
}

function saveSnapshot(snapshot: MonitorSnapshot): void {
  try {
    accountStorage.setItem(CACHE_KEY, JSON.stringify(snapshot))
  } catch { /* 存储不可用时，仍保留本次会话的监控和已读状态。 */ }
}

function hasNewRelease(snapshot: MonitorSnapshot | undefined): boolean {
  if (!snapshot?.latest) return false
  if (!snapshot.seen) return true
  return snapshot.latest.id !== snapshot.seen.id
    && Date.parse(snapshot.latest.publishedAt) >= Date.parse(snapshot.seen.publishedAt)
}

/** 独立监控开源 Release；不读取本地版本，不使用应用更新检查缓存。 */
export function useOpenSourceMonitor({ enabled = true, poll = false } = {}) {
  const qc = useQueryClient()
  const scope = accountScope()
  const queryKey = QK.openSourceRelease(scope)
  const query = useQuery({
    queryKey,
    queryFn: async () => {
      const latest = await api.openSourceRelease()
      if (scope !== accountScope()) throw new DOMException('账户已切换', 'AbortError')
      // 读取请求完成时的已读基准，避免并发检查覆盖用户的“标记已读”。
      const previous = qc.getQueryData<MonitorSnapshot>(queryKey)
      const snapshot: MonitorSnapshot = {
        schema: 1,
        repository: OPEN_SOURCE_REPO,
        seen: previous ? previous.seen : latest,
        latest,
        checkedAt: Date.now(),
      }
      saveSnapshot(snapshot)
      return snapshot
    },
    enabled,
    initialData: readSnapshot,
    initialDataUpdatedAt: () => readSnapshot()?.checkedAt,
    staleTime: CHECK_INTERVAL,
    gcTime: Infinity,
    retry: false,
    refetchOnMount: poll ? 'always' : false,
    refetchInterval: poll && enabled ? CHECK_INTERVAL : false,
  })

  const markRead = () => {
    const current = qc.getQueryData<MonitorSnapshot>(queryKey)
    if (!current?.latest) return
    const next = { ...current, seen: current.latest }
    saveSnapshot(next)
    qc.setQueryData(queryKey, next)
  }

  return {
    latest: query.data?.latest,
    checkedAt: query.data?.checkedAt,
    hasNewRelease: enabled && hasNewRelease(query.data),
    isChecking: query.isFetching,
    error: query.error,
    check: () => { if (enabled) void query.refetch() },
    markRead,
  }
}
