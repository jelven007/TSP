import { afterEach, describe, expect, it, vi } from 'vitest'
import { OPEN_SOURCE_REPO_URL, openSourceReleaseApi, openSourceReleaseUrl } from './openSourceRelease'

const release = {
  id: 412437984,
  tag_name: 'v0.3.7',
  published_at: '2026-10-14T07:19:36Z',
  draft: false,
  prerelease: false,
  html_url: 'https://example.com/untrusted',
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('open-source GitHub API', () => {
  it('读取指定仓库的正式发布，链接固定在该仓库且不发送登录凭据', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(release)))
    vi.stubGlobal('fetch', fetch)
    const latest = await openSourceReleaseApi.openSourceRelease()
    expect(latest).toEqual({ id: release.id, tag: 'v0.3.7', publishedAt: release.published_at })
    expect(fetch).toHaveBeenCalledWith(
      'https://api.github.com/repos/shy3130/tick-stock-panel/releases/latest',
      expect.objectContaining({ credentials: 'omit', signal: expect.any(AbortSignal), cache: 'no-store' }),
    )
    expect(openSourceReleaseUrl(latest!)).toBe(`${OPEN_SOURCE_REPO_URL}/releases/tag/v0.3.7`)
    expect(openSourceReleaseUrl({ ...latest!, tag: 'release/next' })).toContain('release%2Fnext')
  })

  it('404 时确认仓库可访问后才返回尚无 Release', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response('', { status: 404 }))
      .mockResolvedValueOnce(new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    expect(await openSourceReleaseApi.openSourceRelease()).toBeNull()
    expect(fetch).toHaveBeenLastCalledWith(
      'https://api.github.com/repos/shy3130/tick-stock-panel',
      expect.any(Object),
    )
  })

  it('仓库不可访问时报告错误，不作为空发布建立基准', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 404 })))
    await expect(openSourceReleaseApi.openSourceRelease()).rejects.toThrow('仓库暂时不可访问')
  })

  it.each([
    { ...release, id: undefined },
    { ...release, tag_name: '' },
    { ...release, published_at: 'invalid' },
    { ...release, prerelease: true },
    { ...release, draft: true },
  ])('拒绝不完整或非正式发布 %#', async (value) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(value))))
    await expect(openSourceReleaseApi.openSourceRelease()).rejects.toThrow('发布信息不完整')
  })

  it.each([403, 429])('处理 GitHub 访问限制 %s', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status })))
    await expect(openSourceReleaseApi.openSourceRelease()).rejects.toThrow('GitHub 暂时限制访问')
  })

  it('网络失败给出重试提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(openSourceReleaseApi.openSourceRelease()).rejects.toThrow('无法连接 GitHub')
  })

  it('10 秒后终止请求，避免检查按钮一直等待', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    })))
    const assertion = expect(openSourceReleaseApi.openSourceRelease()).rejects.toThrow('检查超时')
    await vi.advanceTimersByTimeAsync(10_000)
    await assertion
  })
})
