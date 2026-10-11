import { COMPUTE_REQUEST_TIMEOUT_MS, request } from './client'

export interface SecondQuoteArchiveStatus {
  enabled: boolean
  running: boolean
  retention_days: number | null
  flush_rounds: number
  queue_capacity: number
  queued_rounds: number
  pending_rounds: number
  queue_peak: number
  accepted_rounds: number
  written_rounds: number
  written_rows: number
  written_files: number
  dropped_rounds: number
  last_flush_ms: number | null
  last_write_ms: number | null
  last_drop_ms: number | null
  last_error: string | null
}

export interface SecondQuoteWindow {
  start: string
  end: string
  symbols?: string[]
  asset_type?: 'stock' | 'etf' | 'index'
  start_ms?: number
  end_ms?: number
  trading_dates?: string[]
  expected_interval_s?: number
  expected_symbols?: number
  min_coverage_ratio?: number
  max_source_age_ms?: number
}

export interface SecondQuoteQuality {
  status: 'missing' | 'ready' | 'warning'
  scope: 'requested_window' | 'observed_samples'
  files: number
  fingerprint: string
  rows: number
  captures: number
  gap_count: number
  max_gap_s: number
  gap_threshold_s: number
  expected_symbols: number | null
  observed_max_symbols: number
  incomplete_captures: number
  source_time_coverage: number | null
  inferred_source_date_rows: number
  stale_source_rows: number
  invalid_rows: number
  source_regressions: number
  cumulative_resets: number
  missing_dates: string[]
  first_collected_at_ms: number | null
  last_collected_at_ms: number | null
  providers: string[]
  provider_versions: string[]
  issues: string[]
  exchange_event_time_verified: false
}

export interface SecondQuoteRow {
  schema_version: number
  trade_date: string
  /** 字符串保留纳秒生成的轮次 ID，禁止转成 JS number。 */
  capture_id: string
  collected_at_ms: number
  provider_ts_ms: number | null
  source_ts_ms: number | null
  source_time: string | null
  source_date_inferred: boolean
  provider: string
  provider_version: string
  asset_type: 'stock' | 'etf' | 'index'
  symbol: string
  last_price: number | null
  prev_close: number | null
  open: number | null
  high: number | null
  low: number | null
  volume: number | null
  amount: number | null
  change_pct: number | null
  change_amount: number | null
  amplitude: number | null
  turnover_rate: number | null
  session: string | null
  bid1: number | null
  ask1: number | null
  bid1_volume: number | null
  ask1_volume: number | null
}

export interface SecondOrder {
  order_id: string
  symbol: string
  signal_ms: number
  side: 'buy' | 'sell'
  shares: number
}

export interface SecondOpeningLot {
  symbol: string
  shares: number
  acquired_date: string
}

export interface SecondExecutionRequest {
  day: string
  start_ms: number
  end_ms: number
  orders: SecondOrder[]
  opening_lots?: SecondOpeningLot[]
  config?: {
    initial_cash?: number
    latency_ms?: number
    expiry_ms?: number
    max_gap_ms?: number
    max_source_age_ms?: number
    participation?: number
    commission_pct?: number
    minimum_commission?: number
    slippage_bps?: number
    exclude_st?: boolean
  }
}

export interface SecondExecutionResult {
  model: string
  cost_model: string
  quality: SecondQuoteQuality
  data_manifest: {
    schema_version: number
    fingerprint: string
    files: { path: string; size: number; sha256: string }[]
  }
  instrument_history_sha256: string
  config_hash: string
  orders: (SecondOrder & { status: 'rejected' | 'filled' | 'unfilled'; reason: string | null })[]
  fills: (SecondOrder & {
    fill_ms: number
    capture_id: string
    price: number
    commission: number
    stamp_tax: number
  })[]
  ending_cash: number
  ending_lots: SecondOpeningLot[]
  exchange_fill_guaranteed: false
}

export const secondQuotesApi = {
  secondQuoteStatus: () => request<{
    archive: SecondQuoteArchiveStatus | null
    available_dates: string[]
    earliest_date: string | null
    latest_date: string | null
  }>('/api/backtest/second-quotes/status'),
  validateSecondQuotes: (body: SecondQuoteWindow) =>
    request<SecondQuoteQuality>('/api/backtest/second-quotes/validate', {
      method: 'POST', body: JSON.stringify(body), timeoutMs: COMPUTE_REQUEST_TIMEOUT_MS,
    }),
  sampleSecondQuotes: (body: { start: string; end: string; symbols: string[]; limit?: number }) =>
    request<{ rows: SecondQuoteRow[]; count: number; truncated: boolean }>('/api/backtest/second-quotes/sample', {
      method: 'POST', body: JSON.stringify(body), timeoutMs: COMPUTE_REQUEST_TIMEOUT_MS,
    }),
  validateSecondExecution: (body: SecondExecutionRequest) =>
    request<SecondExecutionResult>('/api/backtest/second-quotes/execution-validate', {
      method: 'POST', body: JSON.stringify(body), timeoutMs: COMPUTE_REQUEST_TIMEOUT_MS,
    }),
}
