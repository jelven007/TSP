// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'
import { QuoteConfigCard } from './QuoteConfigCard'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let root: Root | null = null

afterEach(async () => {
  if (root) await act(async () => root?.unmount())
  root = null
  document.body.innerHTML = ''
})

describe('QuoteConfigCard', () => {
  it('keeps the non-trading-day switch enabled and shows the waiting state', async () => {
    const container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)

    await act(async () => {
      root?.render(<QuoteConfigCard
        enabled
        running
        isTrading={false}
        lastFetchMs={null}
        intervalS={1}
        intervalMin={1}
        intervalMax={60}
        loading={false}
        onToggle={() => undefined}
        toggling={false}
        showIntervalEdit={false}
        onShowIntervalEdit={() => undefined}
        onIntervalChange={() => undefined}
      />)
    })

    const toggle = container.querySelector<HTMLButtonElement>('button')
    expect(toggle?.disabled).toBe(false)
    expect(container.textContent).toContain('已开启 · 等待交易日')
    expect(container.textContent).toContain('休市')
  })
})
