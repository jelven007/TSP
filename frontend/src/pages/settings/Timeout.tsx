/**
 * 超时设置面板 — 数据任务超时配置。
 */
import { JobTimeoutCard } from './JobTimeoutCard'

export function SettingsTimeoutPanel() {
  return (
    <div className="w-full max-w-5xl space-y-5">
      <JobTimeoutCard />
    </div>
  )
}
