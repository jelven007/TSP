/** Shared with backend/app/services/task_state.py; pending is a legacy alias. */
export const TASK_STATUSES = [
  'queued', 'running', 'cancelling', 'succeeded', 'succeeded_with_budget_exhausted',
  'failed', 'cancelled', 'interrupted', 'skipped_prerequisite',
] as const

export type TaskStatus = typeof TASK_STATUSES[number]

export function parseTaskStatus(value: unknown): TaskStatus {
  if (value === 'pending') return 'queued'
  if (typeof value === 'string' && TASK_STATUSES.some(status => status === value)) {
    return value as TaskStatus
  }
  throw new Error('任务状态无法识别，请刷新后重试')
}
