import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  clearAccountCache,
  getAccountScopeSnapshot,
  subscribeAccountScopeChanges,
} from '@/lib/accountSession'
import { QK } from '@/lib/queryKeys'
import { Logo } from './Logo'

function subscribe(onStoreChange: () => void): () => void {
  return subscribeAccountScopeChanges(onStoreChange)
}

export function AccountScopeBoundary({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const snapshot = useSyncExternalStore(
    subscribe,
    getAccountScopeSnapshot,
    getAccountScopeSnapshot,
  )
  const [readyRevision, setReadyRevision] = useState(snapshot.revision)

  useEffect(() => {
    if (readyRevision === snapshot.revision) return
    let active = true
    clearAccountCache(snapshot.previousScope)
    void queryClient.cancelQueries().then(() => {
      if (!active) return
      queryClient.clear()
      if (snapshot.status) queryClient.setQueryData(QK.authStatus, snapshot.status)
      setReadyRevision(snapshot.revision)
    })
    return () => {
      active = false
    }
  }, [queryClient, readyRevision, snapshot])

  if (readyRevision !== snapshot.revision) {
    return (
      <div className="grid min-h-screen place-items-center bg-base text-muted">
        <Logo size={28} className="text-foreground" />
      </div>
    )
  }

  return <div key={snapshot.nextScope} className="contents">{children}</div>
}
