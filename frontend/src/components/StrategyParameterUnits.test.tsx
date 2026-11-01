// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { ParamField } from './screener/StrategySettingsDialog'
import { StrategyParamInput } from '../pages/backtest/StrategyBacktest'
import type { StrategyParamDef } from '@/lib/api'

it.each(['settings', 'backtest'])('edits displayed percentages and sends fractional values in %s', async layout => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  document.body.append(host)
  const root = createRoot(host)
  const change = vi.fn()
  const def = { id: 'stop_loss_pct', label: '扣费净止损', type: 'float',
    default: .06, display_scale: 100, unit: '%' } as StrategyParamDef
  try {
    await act(async () => root.render(layout === 'settings'
      ? <ParamField def={def} value={.06} onChange={change} />
      : <StrategyParamInput param={def} value={.06} onChange={change} />))
    const input = host.querySelector('input')!
    expect(input.value).toBe('6')
    expect(host.textContent).toContain('%')
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '5.5')
      input.dispatchEvent(new Event('input', { bubbles: true }))
      input.dispatchEvent(new Event('focusout', { bubbles: true }))
    })
    expect(change).toHaveBeenCalledWith(.055)
  } finally {
    await act(async () => root.unmount())
    host.remove()
  }
})
