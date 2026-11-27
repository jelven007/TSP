import { expect, it } from 'vitest'
import { selectionWindow } from './selection-clock'

it('shows the S05 and S03 late windows while retaining the ordinary S02 window', () => {
  expect(selectionWindow('custom_mvxxcs05')).toBe('22:00～23:00')
  expect(selectionWindow('custom_mvxxcs03')).toBe('22:00～23:00')
  expect(selectionWindow('custom_mvxxcp88')).toBe('21:00～22:00')
  expect(selectionWindow()).toBe('21:00～22:00')
})
