// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PageHeader } from './PageHeader'

let root: Root
let host: HTMLDivElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

it('preserves heading, rich subtitle and status without changing action behavior', async () => {
  const click = vi.fn()
  await act(async () => root.render(
    <PageHeader
      title="环境"
      subtitle={<span>市场阶段 · 历史趋势</span>}
      titleExtra={<span role="status">数据准备中</span>}
      right={<button onClick={click}>重算</button>}
    />,
  ))
  expect(host.querySelectorAll('h1')).toHaveLength(1)
  expect(host.querySelector('h1')?.textContent).toBe('环境')
  expect(host.querySelector('[role="status"]')?.textContent).toBe('数据准备中')
  expect(host.querySelector('header')?.textContent).toContain('市场阶段 · 历史趋势')
  await act(async () => host.querySelector('button')!.click())
  expect(click).toHaveBeenCalledOnce()
})

it('renders a title-only empty or error state without an empty toolbar', async () => {
  await act(async () => root.render(<PageHeader title="连板梯队" />))
  expect(host.querySelector('h1')?.textContent).toBe('连板梯队')
  expect(host.querySelector('.page-header-actions')).toBeNull()
})
