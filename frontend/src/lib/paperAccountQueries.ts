import type { QueryClient } from '@tanstack/react-query'
import type { PaperAccountSummary, PaperCompareRow } from './api'
import { QK } from './queryKeys'

/** Cancel stale snapshots before refreshing/removing the affected account. */
export async function paperAccountChanged(client: QueryClient, account: string, action: 'updated' | 'deleted') {
  const isDetail = (key: readonly unknown[]) => key[0] === QK.paperAll[0] && key[2] === account
  const isShared = (key: readonly unknown[]) => key[0] === QK.paperAll[0] && key.length === 2
  await client.cancelQueries({ predicate: query => isDetail(query.queryKey) || isShared(query.queryKey) })
  if (action === 'deleted') {
    client.removeQueries({ predicate: query => isDetail(query.queryKey) })
    client.setQueryData<{ accounts: PaperCompareRow[] }>(QK.paperCompare, previous =>
      previous && { ...previous, accounts: previous.accounts.filter(row => row.account !== account) })
    client.setQueryData<{ accounts: PaperAccountSummary[] }>(QK.paperAccounts, previous =>
      previous && { ...previous, accounts: previous.accounts.filter(row => row.id !== account) })
  }
  await client.invalidateQueries({
    predicate: query => isShared(query.queryKey) || (action === 'updated' && isDetail(query.queryKey)),
  })
  void client.invalidateQueries({ queryKey: QK.alertsAll })
  void client.invalidateQueries({ queryKey: QK.alertsTotal })
}
