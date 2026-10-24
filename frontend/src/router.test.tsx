// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Circle } from 'lucide-react'
import type { RouteObject } from 'react-router-dom'
import type { FrontendExtension } from './extensions/types'

// Test the actual route table and extension registry without mounting the app.
vi.mock('./components/Layout', () => ({ Layout: () => null }))
vi.mock('./lib/api', () => ({ api: {} }))

let router: (typeof import('./router'))['router'] | undefined

beforeEach(() => {
  vi.resetModules()
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  router?.dispose()
  router = undefined
  vi.restoreAllMocks()
})

async function loadRouter() {
  router = (await import('./router')).router
  return router
}

function extension(id: string, path: `/${string}`): FrontendExtension {
  return {
    id,
    apiVersion: 1,
    routes: [{ id: `${id}.page`, path, component: () => null }],
  }
}

function corePaths(routes: RouteObject[], parent = ''): string[] {
  return routes.flatMap(route => {
    const path = route.path?.startsWith('/')
      ? route.path
      : `${parent}/${route.path ?? ''}`.replace(/\/+$/, '') || '/'
    return [
      ...(route.path != null || route.index ? [path] : []),
      ...corePaths(route.children ?? [], path === '/' ? '' : path),
    ]
  })
}

describe('frontend extension route protection', () => {
  it.each(['/lots', '/paper', '/signals'] as const)(
    'rejects an extension occupying %s',
    async path => {
      const registry = await import('./extensions/registry')
      await registry.loadFrontendExtensions({
        conflict: async () => ({ default: extension('conflict', path) }),
      })

      await loadRouter()

      expect(registry.getFrontendExtensionRoutes()).toEqual([])
      expect(registry.getFrontendExtensionLoadErrors()).toEqual([
        expect.objectContaining({
          extensionId: 'conflict',
          error: `试图覆盖核心路由 ${path}`,
        }),
      ])
    },
  )

  it.each(['/dev', '/branding', '/settings/keys', '/mining', '/analysis/audit-menu'] as const)(
    'continues protecting hidden, legacy and dynamic core paths: %s',
    async path => {
      const registry = await import('./extensions/registry')
      await registry.loadFrontendExtensions({
        conflict: async () => ({ default: extension('conflict', path) }),
      })

      await loadRouter()

      expect(registry.getFrontendExtensionRoutes()).toEqual([])
      expect(registry.getFrontendExtensionLoadErrors()).toEqual([
        expect.objectContaining({ error: `试图覆盖核心路由 ${path}` }),
      ])
    },
  )

  it('protects every path in the actual core route table', async () => {
    const coreRouter = await loadRouter()
    const paths = [...new Set(corePaths(coreRouter.routes))]
      .filter(path => path !== '/')
      .map(path => path.replace(/:[^/]+/g, 'audit-menu') as `/${string}`)
    coreRouter.dispose()
    router = undefined
    vi.resetModules()
    const registry = await import('./extensions/registry')
    await registry.loadFrontendExtensions(Object.fromEntries(paths.map((path, index) => [
      `conflict-${index}`,
      async () => ({ default: extension(`conflict-${index}`, path) }),
    ])))

    await loadRouter()

    expect(paths.length).toBeGreaterThan(0)
    expect(registry.getFrontendExtensionRoutes()).toEqual([])
    expect(registry.getFrontendExtensionLoadErrors().map(error => error.error).sort())
      .toEqual(paths.map(path => `试图覆盖核心路由 ${path}`).sort())
  })

  it('removes all registrations of the conflicting extension and keeps unrelated extensions', async () => {
    const registry = await import('./extensions/registry')
    const conflict = extension('conflict', '/paper')
    conflict.routes!.push({ id: 'conflict.other', path: '/company/unused', component: () => null })
    conflict.navigation = [{
      id: 'conflict.nav', routeId: 'conflict.other', label: 'Unused', icon: Circle,
    }]
    conflict.slots = [{ name: 'layout.navigation.extra', id: 'conflict.slot', component: () => null }]
    await registry.loadFrontendExtensions({
      conflict: async () => ({ default: conflict }),
      healthy: async () => ({ default: extension('healthy', '/company/risk') }),
    })

    const appRouter = await loadRouter()

    expect(registry.getFrontendExtensionRoutes().map(route => route.path)).toEqual(['/company/risk'])
    expect(registry.getFrontendExtensionNavigation()).toEqual([])
    expect(registry.getFrontendSlotRegistrations('layout.navigation.extra')).toEqual([])
    expect(appRouter.routes.find(route => route.path === '/')?.children?.map(route => route.path))
      .toContain('company/risk')
    expect(registry.getFrontendExtensionLoadErrors()).toHaveLength(1)
  })

  it('allows valid extension routes with and without navigation', async () => {
    const registry = await import('./extensions/registry')
    const healthy = extension('healthy', '/company/risk')
    healthy.navigation = [{
      id: 'healthy.nav', routeId: 'healthy.page', label: 'Risk', icon: Circle,
    }]
    await registry.loadFrontendExtensions({
      healthy: async () => ({ default: healthy }),
      hidden: async () => ({ default: extension('hidden', '/company/internal') }),
    })

    await loadRouter()

    expect(registry.getFrontendExtensionRoutes().map(route => route.path))
      .toEqual(['/company/internal', '/company/risk'])
    expect(registry.getFrontendExtensionNavigation().map(item => item.route.path))
      .toEqual(['/company/risk'])
    expect(registry.getFrontendExtensionLoadErrors()).toEqual([])
  })

  it('keeps the core router available without extensions', async () => {
    const registry = await import('./extensions/registry')
    const appRouter = await loadRouter()

    expect(corePaths(appRouter.routes)).toEqual(expect.arrayContaining(['/lots', '/paper', '/signals']))
    expect(registry.getFrontendExtensionRoutes()).toEqual([])
    expect(registry.getFrontendExtensionLoadErrors()).toEqual([])
  })
})
