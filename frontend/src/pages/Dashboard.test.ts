import { describe, expect, it } from 'vitest'
import {
  DASHBOARD_REALTIME_FALLBACK_MS,
  dashboardQuoteIsRealtime,
  dashboardRefetchInterval,
} from './Dashboard'

describe('dashboard realtime fallback', () => {
  it('polls latest data while realtime trading is active', () => {
    expect(dashboardRefetchInterval(undefined, {
      quote_status: { running: true, is_trading_hours: true },
    })).toBe(DASHBOARD_REALTIME_FALLBACK_MS)
  })

  it.each([
    ['historical date', '2026-10-19', true, true],
    ['service stopped', undefined, false, true],
    ['market closed', undefined, true, false],
  ])('does not poll for %s', (_label, selectedDate, running, isTrading) => {
    expect(dashboardRefetchInterval(selectedDate, {
      quote_status: { running, is_trading_hours: isTrading },
    })).toBe(false)
  })

  it('shows realtime only for the latest date during trading hours', () => {
    const live = { running: true, is_trading_hours: true }
    expect(dashboardQuoteIsRealtime(undefined, '2026-10-20', live)).toBe(true)
    expect(dashboardQuoteIsRealtime('2026-10-20', '2026-10-20', live)).toBe(true)
    expect(dashboardQuoteIsRealtime('2026-10-19', '2026-10-20', live)).toBe(false)
    expect(dashboardQuoteIsRealtime(undefined, '2026-10-20', {
      running: true,
      is_trading_hours: false,
    })).toBe(false)
  })
})
