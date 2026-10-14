import { useSearchParams } from 'react-router-dom'
import { SlidersHorizontal, Waypoints } from 'lucide-react'
import { StrategyOptimizer } from './StrategyOptimizer'
import { StrategyWalkForward } from './StrategyWalkForward'

type Mode = 'sensitivity' | 'walkforward'

export function RobustnessValidation() {
  const [searchParams, setSearchParams] = useSearchParams()
  const mode: Mode = searchParams.get('mode') === 'walkforward' ? 'walkforward' : 'sensitivity'
  const setMode = (next: Mode) => {
    const params = new URLSearchParams(searchParams)
    params.set('mode', next)
    setSearchParams(params, { replace: true })
  }
  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex shrink-0 items-center border-b border-border/70 px-1 pb-2">
        <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5">
          {([
            ['sensitivity', '参数优化', SlidersHorizontal],
            ['walkforward', '步进优化', Waypoints],
          ] as const).map(([value, label, Icon]) => (
            <button
              key={value}
              type="button"
              onClick={() => setMode(value)}
              className={`inline-flex items-center gap-1.5 rounded-[5px] px-3 py-1.5 text-xs font-medium transition-colors ${mode === value
                ? 'bg-accent text-white shadow-sm'
                : 'text-secondary hover:bg-elevated hover:text-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        {mode === 'sensitivity' ? <StrategyOptimizer /> : <StrategyWalkForward />}
      </div>
    </div>
  )
}
