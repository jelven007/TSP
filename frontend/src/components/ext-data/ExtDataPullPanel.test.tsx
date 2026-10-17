// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ExtDataConfig } from '@/lib/api'
import { ExtDataPullPanel } from './ExtDataPullPanel'

const mocks = vi.hoisted(() => ({
  save: vi.fn(),
  run: vi.fn(),
  key: vi.fn(),
  test: vi.fn(),
}))

vi.mock('@/lib/api', () => ({
  api: {
    extDataPullConfig: mocks.save,
    extDataPullRun: mocks.run,
    extDataApiKey: mocks.key,
    extDataPullTest: mocks.test,
  },
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

let root: Root
let node: HTMLDivElement
const onSaved = vi.fn()
const config: ExtDataConfig = {
  id: 'demo',
  label: '演示',
  mode: 'snapshot',
  fields: [],
  created_at: '2026-10-15T01:00:00Z',
  updated_at: '2026-10-17T01:00:00Z',
  pull: {
    url: 'https://source.test/catalog',
    method: 'GET',
    response_path: '',
    schedule_minutes: 1,
    enabled: false,
    page_start: 0,
    fanout: {
      member_url: 'https://source.test/member',
      timeout_seconds: 600,
    },
    last_status: 'error',
    last_run: '2026-10-17T01:00:00Z',
    last_attempt: {
      total: 2,
      completed: 1,
      coverage: 0.5,
      failed: [{ id: 'chip', reason: '上游 HTTP 503' }],
    },
    last_complete: {
      received_at: '2026-10-16T01:00:00Z',
      source_date: null,
      rows: 100,
      source: 'https://source.test/catalog',
      sha256: 'a'.repeat(64),
      replaced: true,
    },
    previous_complete: {
      received_at: '2026-10-15T01:00:00Z',
      rows: 90,
      source: 'https://source.test/catalog',
      sha256: 'b'.repeat(64),
      replaced: true,
    },
  },
}

beforeEach(async () => {
  vi.clearAllMocks()
  mocks.key.mockResolvedValue({ key_set: false, masked_key: '' })
  mocks.save.mockResolvedValue({ status: 'ok' })
  mocks.run.mockResolvedValue({ rows: 12, date: '2026-10-17' })
  mocks.test.mockResolvedValue({ total_rows: 12, preview: [], has_symbol: true })
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  node = document.createElement('div')
  document.body.append(node)
  root = createRoot(node)
  await act(async () => root.render(
    <ExtDataPullPanel config={config} onSaved={onSaved} />,
  ))
})

afterEach(() => {
  act(() => root.unmount())
  node.remove()
})

async function click(text: string) {
  const button = [...node.querySelectorAll('button')]
    .find(item => item.textContent?.trim() === text)!
  await act(async () => button.click())
}

it('shows failed coverage and retained data without inventing a source date', () => {
  expect(node.textContent).toContain('请求完成 1 / 2 · 50%')
  expect(node.textContent).toContain('100 行')
  expect(node.textContent).toContain('未知（接口未提供完整日期）')
  expect(node.textContent).toContain('90 行，已保留')
  expect(node.textContent).toContain('chip · 上游 HTTP 503')
})

it('saves edited recipe before run and waits for the whole recipe budget', async () => {
  await click('立即执行')
  expect(mocks.save).toHaveBeenCalledWith('demo', expect.objectContaining({
    page_start: 0,
    fanout: {
      member_url: 'https://source.test/member',
      timeout_seconds: 600,
    },
    snapshot_policy: 'auto',
  }))
  expect(mocks.save.mock.invocationCallOrder[0])
    .toBeLessThan(mocks.run.mock.invocationCallOrder[0])
  expect(mocks.run).toHaveBeenCalledWith('demo', 600)
  expect(node.textContent).toContain('12 行')
})

it('rejects malformed recipe locally without changing the saved source', async () => {
  const field = node.querySelector('textarea[aria-label="两级配方"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      'value',
    )!.set!.call(field, '[]')
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click('保存配置')
  expect(mocks.save).not.toHaveBeenCalled()
  expect(node.textContent).toContain('两级配方需为包含 member_url 的 JSON 对象')
})

it('refreshes persisted failure state while keeping the last valid snapshot visible', async () => {
  mocks.run.mockRejectedValueOnce(new Error('两级拉取不完整'))
  await click('立即执行')
  expect(onSaved).toHaveBeenCalled()
  expect(node.textContent).toContain('两级拉取不完整')
  expect(node.textContent).toContain('100 行')
})
