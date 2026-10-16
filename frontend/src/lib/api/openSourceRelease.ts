export const OPEN_SOURCE_REPO = 'shy3130/tick-stock-panel'
export const OPEN_SOURCE_REPO_URL = `https://github.com/${OPEN_SOURCE_REPO}`
const API_URL = `https://api.github.com/repos/${OPEN_SOURCE_REPO}`

export interface OpenSourceRelease {
  id: number
  tag: string
  publishedAt: string
}

export function openSourceReleaseUrl(release: OpenSourceRelease): string {
  return `${OPEN_SOURCE_REPO_URL}/releases/tag/${encodeURIComponent(release.tag)}`
}

export function isOpenSourceRelease(value: unknown): value is OpenSourceRelease {
  if (!value || typeof value !== 'object') return false
  const release = value as Partial<OpenSourceRelease>
  return Number.isSafeInteger(release.id) && (release.id ?? 0) > 0
    && typeof release.tag === 'string' && release.tag.trim().length > 0
    && typeof release.publishedAt === 'string' && Number.isFinite(Date.parse(release.publishedAt))
}

async function githubRequest(url: string, signal: AbortSignal): Promise<Response> {
  const response = await globalThis.fetch(url, {
    signal,
    credentials: 'omit',
    cache: 'no-store',
    headers: { Accept: 'application/vnd.github+json' },
  })
  if (response.status === 403 || response.status === 429) {
    throw new Error('GitHub 暂时限制访问，请稍后重试或打开开源仓库查看')
  }
  if (!response.ok && response.status !== 404) {
    throw new Error(`开源仓库检查失败（HTTP ${response.status}），请稍后重试`)
  }
  return response
}

export const openSourceReleaseApi = {
  openSourceRelease: async (): Promise<OpenSourceRelease | null> => {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 10_000)
    try {
      const response = await githubRequest(`${API_URL}/releases/latest`, controller.signal)
      if (response.status === 404) {
        // /latest 的 404 也可能表示仓库不可访问，不能误记为“尚无 Release”。
        const repository = await githubRequest(API_URL, controller.signal)
        if (!repository.ok) throw new Error('开源仓库暂时不可访问，请稍后重试')
        return null
      }
      const raw = await response.json()
      const release = {
        id: raw?.id,
        tag: raw?.tag_name,
        publishedAt: raw?.published_at,
      }
      if (!isOpenSourceRelease(release) || raw.draft !== false || raw.prerelease !== false) {
        throw new Error('GitHub 发布信息不完整，请稍后重试')
      }
      return release
    } catch (error) {
      if (controller.signal.aborted) throw new Error('开源仓库检查超时，请稍后重试')
      if (error instanceof TypeError) throw new Error('无法连接 GitHub，请检查网络后重试')
      throw error
    } finally {
      clearTimeout(timeout)
    }
  },
}
