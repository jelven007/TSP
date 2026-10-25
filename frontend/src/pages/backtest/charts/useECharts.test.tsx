// @vitest-environment jsdom
import { act, createRef } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EChartsOption } from '@/lib/echarts'
import { useECharts } from './useECharts'

const mocks = vi.hoisted(() => ({
  init: vi.fn(),
  instances: [] as { setOption: ReturnType<typeof vi.fn>; resize: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn> }[],
  observers: [] as { disconnect: ReturnType<typeof vi.fn> }[],
}))
vi.mock('@/lib/echarts', () => ({ init: mocks.init }))
let root: Root
let host: HTMLDivElement
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.instances = []
  mocks.observers = []
  mocks.init.mockReset().mockImplementation(() => {
    const instance = { setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }
    mocks.instances.push(instance)
    return instance
  })
  vi.stubGlobal('ResizeObserver', class {
    disconnect = vi.fn()
    observe() {}
    constructor() { mocks.observers.push(this) }
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

function Chart({ option, run, container }: { option: EChartsOption; run?: string; container?: React.RefObject<HTMLDivElement> }) {
  const ref = useECharts(option, run, container)
  return <div ref={ref} />
}

it('updates for data and task changes without rebuilding the chart on an ordinary rerender', async () => {
  const first: EChartsOption = { series: [{ type: 'line', data: [1, 2] }] }
  const next: EChartsOption = { series: [{ type: 'line', data: [3, 4] }] }
  const render = async (option: EChartsOption, run: string) => {
    await act(async () => root.render(<Chart option={option} run={run} />))
  }
  await render(first, 'run-1')
  const instance = mocks.instances[0]
  await render(first, 'run-1')
  expect(instance.setOption).toHaveBeenCalledTimes(1)
  await render(next, 'run-1')
  expect(instance.setOption).toHaveBeenLastCalledWith(next, { notMerge: true })
  await render(next, 'run-2')
  expect(instance.setOption).toHaveBeenCalledTimes(3)
  expect(mocks.init).toHaveBeenCalledOnce()
})

it('releases the previous container and applies the option to the replacement instance', async () => {
  const firstRef = createRef<HTMLDivElement>()
  const secondRef = createRef<HTMLDivElement>()
  const option: EChartsOption = { series: [{ type: 'line', data: [1] }] }
  await act(async () => root.render(<Chart option={option} container={firstRef} />))
  await act(async () => root.render(<Chart option={option} container={secondRef} />))
  expect(mocks.instances[0].dispose).toHaveBeenCalledOnce()
  expect(mocks.observers[0].disconnect).toHaveBeenCalledOnce()
  expect(mocks.instances[1].setOption).toHaveBeenCalledWith(option, { notMerge: true })
  expect(mocks.init).toHaveBeenLastCalledWith(secondRef.current, undefined, { renderer: 'canvas' })
})
