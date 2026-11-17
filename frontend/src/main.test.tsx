// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'

const calls = vi.hoisted(() => ({
  auth: vi.fn(), settings: vi.fn(), extensions: vi.fn(), router: vi.fn(), render: vi.fn(),
}))
vi.mock('./lib/api', () => ({ api: { authStatus: calls.auth, settings: calls.settings } }))
vi.mock('./extensions/bootstrap', () => ({ initializeFrontendExtensions: calls.extensions }))
vi.mock('./router', () => { calls.router(); return { router: {} } })
vi.mock('react-dom/client', () => ({ default: { createRoot: () => ({ render: calls.render }) } }))

afterEach(() => {
  vi.resetModules()
  vi.clearAllMocks()
})

it('starts settings after authentication, overlapping extension setup without delaying render on settings', async () => {
  let authenticate!: (value: object) => void
  let register!: () => void
  calls.auth.mockImplementation(() => new Promise(resolve => { authenticate = resolve }))
  calls.settings.mockImplementation(() => new Promise(() => {}))
  calls.extensions.mockImplementation(() => new Promise<void>(resolve => { register = resolve }))
  await import('./main')
  expect(calls.settings).not.toHaveBeenCalled()
  expect(calls.extensions).not.toHaveBeenCalled()
  authenticate({ auth_required: true, authenticated: true })
  await vi.waitFor(() => expect(calls.extensions).toHaveBeenCalledOnce())
  expect(calls.settings).toHaveBeenCalledOnce()
  expect(calls.router).not.toHaveBeenCalled()
  register()
  await vi.waitFor(() => expect(calls.render).toHaveBeenCalledOnce())
})

it('does not prefetch private settings for an unauthenticated visitor', async () => {
  calls.auth.mockResolvedValue({ auth_required: true, authenticated: false })
  calls.extensions.mockResolvedValue(undefined)
  await import('./main')
  await vi.waitFor(() => expect(calls.render).toHaveBeenCalledOnce())
  expect(calls.settings).not.toHaveBeenCalled()
})
