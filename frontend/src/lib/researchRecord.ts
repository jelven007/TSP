export interface ResearchRecord {
  family_id?: string
  config?: Record<string, unknown>
  status?: string
  n_attempted?: number
  n_succeeded?: number
  n_failed?: number
  data_snapshot?: { id: string; file_count: number }
  provenance?: {
    job_id: string
    input_snapshot: string
    public_release_id?: string | null
  }
  selection_correction?: {
    status: string
    probability: number | null
    n_trials: number
    n_obs?: number
    scope: string
  }
}
