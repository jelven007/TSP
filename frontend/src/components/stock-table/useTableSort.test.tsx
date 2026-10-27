// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it } from 'vitest'
import type { ColumnConfig } from '@/lib/list-columns'
import { useTableSort } from './useTableSort'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

it('restores saved order while retaining the supplied filtered rows', async () => {
  const rows = [{ price: 3 }, { price: 1 }, { price: 2 }]
  const cols = [{ id: 'price' }] as ColumnConfig[]
  let hook!: ReturnType<typeof useTableSort<(typeof rows)[number]>>
  function Harness() {
    hook = useTableSort(r => r.price)
    return null
  }
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => root.render(<Harness />))
    await act(async () => hook.toggle('price'))
    expect(hook.sortRows(rows, cols).map(r => r.price)).toEqual([1, 2, 3])
    await act(async () => hook.reset())
    expect(hook.sort).toBeNull()
    expect(hook.sortRows(rows, cols)).toEqual(rows)
    expect(hook.sortRows(rows.filter(r => r.price !== 1), cols).map(r => r.price)).toEqual([3, 2])
  } finally {
    await act(async () => root.unmount())
  }
})
