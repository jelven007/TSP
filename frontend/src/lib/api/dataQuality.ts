export interface DataQuality {
  checked_at: string
  historical_pit_verified: boolean
  datasets: Record<string, {
    state: string
    rows: number | null
    latest_date: string | null
    source: string | null
    source_clock_verified: boolean | null
  }>
  latest_daily: {
    partition: string | null
    rows: number | null
    symbols: number | null
    expected_symbols: number | null
    coverage_ratio: number | null
    invalid_price_rows: number | null
    invalid_volume_rows: number | null
    error: string | null
  }
  historical_inputs: {
    universe: { survivorship_verified: boolean; missing_delisted_ratio: number | null }
    financial_publication: { unknown_ratio: number | null; rows: number | null }
  }
  release: { status: string; release_id: string | null }
}

export interface InitializationStatus {
  auto_enabled: boolean
  status: string
  marker_error: string | null
  completed_at: string | null
  job_id: string | null
  progress: number | null
  stages: Record<string, string>
  last_successful_pipeline_at: string | null
}
