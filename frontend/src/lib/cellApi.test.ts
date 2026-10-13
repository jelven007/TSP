// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { safeCellOrigin, workspaceClient, type Workspace } from './cellApi'
import { request } from './api/client'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })
const workspace: Workspace = { id: 'tenant-a', name: 'A', cell_id: 'cell-a', api_url: 'https://cell-a.example.test',
  routing_epoch: 3, policy_version: 1, status: 'active', role: 'member' }

describe('Cell request isolation', () => {
  it('binds every request to the selected origin, tenant and routing epoch', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ strategies: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await workspaceClient(workspace, 'session').strategies()
    expect(fetchMock).toHaveBeenCalledWith('https://cell-a.example.test/api/tenants/tenant-a/strategies',
      expect.objectContaining({ headers: {
        'Content-Type': 'application/json', Authorization: 'Bearer session', 'X-Routing-Epoch': '3',
      } }))
  })
  it('drops late responses after the old workspace is disposed', async () => {
    const lifetime = new AbortController()
    let finish: (value: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const pending = workspaceClient(workspace, 'session', lifetime.signal).watchlist()
    lifetime.abort()
    finish(new Response(JSON.stringify({ symbols: [{ symbol: 'private' }], groups: [] }), { status: 200 }))
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })
  it('handles empty successful mutation responses', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })))
    await expect(request('/empty', { method: 'DELETE' })).resolves.toBeUndefined()
  })
  it('rejects insecure or credential-bearing Cell origins', () => {
    expect(() => safeCellOrigin('http://evil.example')).toThrow()
    expect(() => safeCellOrigin('https://user:pass@example.com')).toThrow()
    expect(safeCellOrigin('http://127.0.0.1:3020')).toBe('http://127.0.0.1:3020')
  })
})
