// @vitest-environment jsdom
import { act, Component, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { ListColumnCustomizer } from './ListColumnCustomizer'

const { fail } = vi.hoisted(() => ({ fail: { value: true } }))
vi.mock('./ListColumnCustomizerInner', () => ({
  ListColumnCustomizerInner: () => {
    if (fail.value) throw new Error('Failed to fetch dynamically imported module')
    return <div role="dialog">正常列表配置</div>
  },
}))
class RouteBoundary extends Component<{ children: ReactNode }, { error: boolean }> {
  state = { error: false }
  static getDerivedStateFromError() { return { error: true } }
  render() { return this.state.error ? <p>Unexpected Application Error!</p> : this.props.children }
}
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
afterEach(() => vi.restoreAllMocks())
it('contains loader/render errors locally and lets the user retry without losing the result page', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const host = document.createElement('div'); document.body.append(host)
  const root = createRoot(host)
  try {
    await act(async () => root.render(<RouteBoundary>
      <p>原策略结果</p>
      <ListColumnCustomizer open columns={[]} groups={[]} onChange={() => {}} onClose={() => {}} />
    </RouteBoundary>))
    expect(host.textContent).toContain('原策略结果')
    expect(host.textContent).not.toContain('Unexpected Application Error')
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('列表配置加载失败')
    fail.value = false
    const retry = [...document.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent === '重试')!
    await act(async () => retry.click())
    expect(host.querySelector('[role="dialog"]')?.textContent).toBe('正常列表配置')
  } finally {
    await act(async () => root.unmount()); host.remove()
  }
})
