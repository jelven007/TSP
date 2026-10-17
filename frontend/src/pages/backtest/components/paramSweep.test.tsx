// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import type { StrategyDetail } from '@/lib/api'
import { SweepParamList, useParamSweep } from './paramSweep'

const strategy = {
  id: 'conditional', name: '测试', params_defaults: { mode: 'on', period: 4, text: '保留' },
  params: [
    { id: 'mode', label: '模式', type: 'select', default: 'on', options: ['on', 'off'], group: '模式' },
    { id: 'period', label: '周期', type: 'int', default: 4, min: 2, max: 6, step: 2,
      group: '趋势', visible_if: { param: 'mode', in: ['on'] } },
    { id: 'text', label: '备注', type: 'string', default: '' },
  ],
} as unknown as StrategyDetail
let root: Root
let node: HTMLDivElement
let sweep: ReturnType<typeof useParamSweep>
function Harness() {
  sweep = useParamSweep([strategy, { ...strategy, id: 'other', params_defaults: { period: 2 } }])
  return <SweepParamList params={sweep.params} sweeps={sweep.sweeps} updateSweep={sweep.updateSweep}
    values={sweep.values} updateValue={sweep.updateValue} fixedIds={sweep.fixedIds} />
}

beforeEach(() => {
  ;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true
  node = document.createElement('div')
  document.body.append(node)
  root = createRoot(node)
  act(() => root.render(<Harness />))
  act(() => sweep.selectStrategy('conditional'))
})
afterEach(() => { act(() => root.unmount()); node.remove() })

it('excludes hidden and controller parameters from count and payload while preserving values', () => {
  act(() => { sweep.updateSweep('period', { enabled: true }); sweep.updateSweep('mode', { enabled: true }) })
  expect(sweep.combos).toBe(3)
  expect(sweep.buildGrid()).toEqual({ period: { min: 2, max: 6, step: 2 } })
  act(() => sweep.updateValue('mode', 'off'))
  expect(node.textContent).not.toContain('周期')
  expect(sweep.combos).toBe(0)
  expect(sweep.buildGrid()).toEqual({})
  expect(sweep.values.period).toBe(4)

  act(() => sweep.updateValue('mode', 'on'))
  expect(sweep.combos).toBe(3)
  expect(sweep.values.text).toBe('保留')
})

it('renders groups and edits the fixed controller through the actual form', () => {
  const select = node.querySelector('select')!
  act(() => { select.value = 'off'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(sweep.values.mode).toBe('off')
  expect(node.textContent).not.toContain('周期')
  act(() => sweep.selectStrategy('other'))
  expect(sweep.values.mode).toBe('on')
  expect(sweep.values.period).toBe(2)
  expect(sweep.combos).toBe(0)
  expect(node.textContent).toContain('趋势')
})
