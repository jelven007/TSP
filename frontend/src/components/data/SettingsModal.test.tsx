// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { SettingsModal } from './SettingsModal'

vi.mock('framer-motion', async () => {
  const { createElement } = await import('react')
  return { motion: { div: ({ children, className }: any) => createElement('div', { className }, children) } }
})

let cleanup: (() => Promise<void>) | undefined
afterEach(async () => { await cleanup?.(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('traps focus, preserves editing focus during rerender and restores the trigger', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockReturnValue(document.body)
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    queueMicrotask(() => callback(0)); return 1
  })
  vi.stubGlobal('cancelAnimationFrame', () => {})
  const trigger = document.createElement('button')
  const host = document.createElement('div')
  document.body.append(trigger, host)
  trigger.focus()
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()); trigger.remove(); host.remove() }
  const onClose = vi.fn()
  const render = async (disabled: boolean) => {
    await act(async () => root.render(
      <SettingsModal title="设置" onClose={onClose} closeDisabled={disabled}>
        <input aria-label="编辑值" /><button>保存</button>
      </SettingsModal>,
    ))
  }
  await render(false)
  const dialog = host.querySelector<HTMLElement>('[role="dialog"]')!
  expect(dialog.getAttribute('aria-modal')).toBe('true')
  expect(document.getElementById(dialog.getAttribute('aria-labelledby')!)?.textContent).toBe('设置')
  const buttons = dialog.querySelectorAll('button')
  expect(document.activeElement).toBe(buttons[0])
  buttons[1].focus()
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }))
  expect(document.activeElement).toBe(buttons[0])
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }))
  expect(document.activeElement).toBe(buttons[1])
  // Browser/native traversal may escape without a document keydown event.
  trigger.focus()
  expect(document.activeElement).toBe(buttons[0])
  const input = dialog.querySelector('input')!
  input.focus()
  await render(true)
  expect(document.activeElement).toBe(input)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  expect(onClose).not.toHaveBeenCalled()
  expect(buttons[0].disabled).toBe(true)
  await render(false)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  expect(onClose).toHaveBeenCalledOnce()
  await act(async () => root.unmount())
  expect(document.activeElement).toBe(trigger)
  cleanup = async () => { trigger.remove(); host.remove() }
})
