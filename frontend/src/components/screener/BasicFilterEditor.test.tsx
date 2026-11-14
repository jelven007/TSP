// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { BasicFilterEditor } from './BasicFilterEditor'
import { numericBasicFilter, validateBasicFilter } from '@/lib/strategyBasicFilter'

it('shows eight previous-day fields in order and constrains signed percentage points', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  const root = createRoot(host)
  await act(async () => root.render(<BasicFilterEditor value={{}} onChange={() => {}} />))
  expect([...host.querySelectorAll('input[type="number"]')].map(input => input.getAttribute('aria-label'))).toEqual([
    '总市值最小值', '总市值最大值', '流通市值最小值', '流通市值最大值',
    '成交额最小值', '成交额最大值', '换手率最小值', '换手率最大值',
    '涨跌幅最小值', '涨跌幅最大值', '上下振幅最小值', '上下振幅最大值',
    '价格范围最小值', '价格范围最大值', '上市天数',
  ])
  const input = host.querySelector<HTMLInputElement>('input[aria-label="涨跌幅最小值"]')!
  expect([input.min, input.max, input.step]).toEqual(['-50', '50', '0.1'])
  const amplitude = host.querySelector<HTMLInputElement>('input[aria-label="上下振幅最小值"]')!
  expect([amplitude.min, amplitude.max, amplitude.step]).toEqual(['0', '', '0.1'])
  expect(host.textContent).toContain('上一个交易日')
  await act(async () => root.unmount())
})

it('preserves amplitude percentage points and explicitly clears bounds', () => {
  const saved = numericBasicFilter({ amplitude_min: 0, amplitude_max: 12 })
  expect(saved).toMatchObject({ data_reference: 'previous_trade_day', amplitude_min: 0, amplitude_max: 12 })
  expect(numericBasicFilter({ ...saved, amplitude_max: null }).amplitude_max).toBeNull()
  expect(validateBasicFilter({ amplitude_min: 0, amplitude_max: 12.25 })).toBeNull()
  for (const value of [-0.1, NaN, Infinity]) expect(validateBasicFilter({ amplitude_max: value })).not.toBeNull()
  expect(validateBasicFilter({ amplitude_min: 13, amplitude_max: 12 })).not.toBeNull()
})

it('edits always-enabled filters without a switch and still disables fields during saving', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  const root = createRoot(host)
  const onChange = vi.fn()
  const value = { enabled: false, price_min: 5, amount_max: 2e8 }
  await act(async () => root.render(<BasicFilterEditor value={value} onChange={onChange} alwaysEnabled />))
  expect(host.querySelector('input[type="checkbox"]')).toBeNull()
  expect(host.textContent).not.toContain('启用数值过滤')
  expect(host.textContent).not.toContain('关闭数值过滤')
  expect([...host.querySelectorAll<HTMLInputElement>('input[type="number"]')].every(input => !input.disabled)).toBe(true)
  const price = host.querySelector<HTMLInputElement>('input[aria-label="价格范围最小值"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(price, '3')
    price.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(onChange).toHaveBeenLastCalledWith({ ...value, enabled: true, price_min: 3 })
  await act(async () => root.render(<BasicFilterEditor value={value} onChange={onChange} alwaysEnabled disabled />))
  expect([...host.querySelectorAll<HTMLInputElement>('input[type="number"]')].every(input => input.disabled)).toBe(true)
  await act(async () => root.unmount())
})

it('validates signed return range and one decimal without changing other units', () => {
  expect(validateBasicFilter({ change_pct_min: -50, change_pct_max: 50 })).toBeNull()
  expect(validateBasicFilter({ change_pct_min: -1.2, change_pct_max: 2.3 })).toBeNull()
  for (const value of [-50.1, 50.1, 0.11, NaN]) expect(validateBasicFilter({ change_pct_min: value })).not.toBeNull()
  expect(validateBasicFilter({ change_pct_min: 2, change_pct_max: -2 })).not.toBeNull()
  expect(validateBasicFilter({ amount_min: -1 })).not.toBeNull()
})
