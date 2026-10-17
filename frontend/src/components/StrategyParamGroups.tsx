import type { ReactNode } from 'react'
import type { StrategyParamDef } from '@/lib/api'
import { groupParams, visibleParams } from '@/lib/strategyParams'

/** Shared grouping and visibility for strategy settings, backtests, and parameter scans. */
export function StrategyParamGroups({ params, values, children, className = 'space-y-1.5' }: {
  params: StrategyParamDef[]
  values: Record<string, any>
  children: (param: StrategyParamDef) => ReactNode
  className?: string
}) {
  const groups = groupParams(visibleParams(params, values))
  if (!groups.length) return <div className="text-xs text-muted">当前条件下无可调参数。</div>

  return (
    <div className="space-y-3">
      {groups.map((group, index) => (
        <div key={`${group.name ?? ''}-${index}`}>
          {group.name && (
            <div className="mb-2 border-b border-border/40 pb-1 text-xs font-medium text-secondary">
              {group.name}
            </div>
          )}
          <div className={className}>{group.items.map(children)}</div>
        </div>
      ))}
    </div>
  )
}
