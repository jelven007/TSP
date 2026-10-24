// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getTheme, setTheme, toggleTheme } from './theme'

const startupScript = readFileSync(`${process.cwd()}/index.html`, 'utf8')
  .match(/<script>([\s\S]*?)<\/script>/)?.[1]

function applyStartupTheme() {
  if (!startupScript) throw new Error('Theme startup script missing')
  runInNewContext(startupScript, { document, localStorage })
}

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})

describe('initial theme', () => {
  it.each([
    [null, 'light'],
    ['light', 'light'],
    ['dark', 'dark'],
    ['invalid', 'light'],
  ] as const)('uses %s preference consistently before and after rendering', (saved, expected) => {
    if (saved !== null) localStorage.setItem('tf-theme', saved)
    applyStartupTheme()
    expect(document.documentElement.classList.contains('dark')).toBe(expected === 'dark')
    expect(getTheme()).toBe(expected)
    expect(localStorage.getItem('tf-theme')).toBe(saved)
  })

  it('defaults to daytime mode when browser storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('Storage denied', 'SecurityError')
    })
    applyStartupTheme()
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(getTheme()).toBe('light')
  })

  it('persists an explicit nighttime choice and allows toggling back', () => {
    expect(toggleTheme()).toBe('dark')
    expect(localStorage.getItem('tf-theme')).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(toggleTheme()).toBe('light')
    expect(localStorage.getItem('tf-theme')).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    setTheme('dark')
    document.documentElement.classList.remove('dark')
    applyStartupTheme()
    expect(getTheme()).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })
})
