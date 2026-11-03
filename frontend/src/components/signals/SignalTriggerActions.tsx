import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ListChecks, Plus, Settings2, Zap } from 'lucide-react'
import type { CustomSignal } from '@/lib/api'
import { CustomSignalDialog } from './CustomSignalDialog'
import { AddFactorSignalDialog } from '@/pages/backtest/AddFactorSignalDialog'
import { customSignalColumn } from '@/lib/customSignals'
import { ExistingSignalDialog } from './ExistingSignalDialog'

interface Props {
  kind: 'entry' | 'exit'
  signals: string[]
  onChange: (next: string[]) => void
  buttonClassName?: string
  iconClassName?: string
  allowIntraday?: boolean
  selectExisting?: boolean
}

export function SignalTriggerActions({ kind, signals, onChange, buttonClassName, iconClassName, allowIntraday = false, selectExisting = false }: Props) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [factorOpen, setFactorOpen] = useState(false)
  const [existingOpen, setExistingOpen] = useState(false)

  const accent = kind === 'entry' ? 'hover:text-accent hover:border-accent/40' : 'hover:text-warning hover:border-warning/40'
  const btnCls = buttonClassName ?? 'rounded-btn border border-border bg-base p-1 text-muted transition-colors cursor-pointer'
  const iconCls = iconClassName ?? 'h-3.5 w-3.5'

  const enableSignal = (signalId: string, signalKind: 'entry' | 'exit' | 'both') => {
    if (signalKind !== kind && signalKind !== 'both') return
    onChange(signals.includes(signalId) ? signals : [...signals, signalId])
  }

  const handleSaved = (signal: CustomSignal) => enableSignal(customSignalColumn(signal), signal.kind)
  const handleFactorCreated = (signal: { id: string; kind: 'entry' | 'exit' | 'both' }) =>
    enableSignal(`csg_${signal.id}`, signal.kind)

  return (
    <>
      {selectExisting && <button type="button" onClick={() => setExistingOpen(true)}
        aria-label={`选择已有${kind === 'entry' ? '入场' : '出场'}信号`} title="从信号页选择已有自定义信号"
        className={`${btnCls} inline-flex items-center gap-1.5 whitespace-nowrap ${accent}`}>
        <ListChecks className={iconCls} /><span className="text-[10px]">选择已有信号</span>
      </button>}
      <button
        type="button"
        onClick={() => setFactorOpen(true)}
        title="从因子快速创建条件（阈值给建议值）"
        className={`${btnCls} hover:border-amber-400/40 hover:text-amber-400`}
      >
        <Zap className={iconCls} />
      </button>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="新增自定义信号"
        className={`${btnCls} ${accent}`}
      >
        <Plus className={iconCls} />
      </button>
      <button
        type="button"
        onClick={() => navigate('/signals?highlight=signals')}
        title="去信号页"
        className={`${btnCls} hover:border-amber-400/40 hover:text-amber-400`}
      >
        <Settings2 className={iconCls} />
      </button>

      <CustomSignalDialog
        open={open}
        defaultKind={kind}
        allowLateSession={allowIntraday && kind === 'entry'}
        allowMorningLiquidation={allowIntraday && kind === 'exit'}
        onClose={() => setOpen(false)}
        onSaved={handleSaved}
      />
      {existingOpen && <ExistingSignalDialog kind={kind} signals={signals} allowIntraday={allowIntraday}
        onChange={onChange} onClose={() => setExistingOpen(false)} />}
      {factorOpen && (
        <AddFactorSignalDialog
          defaultKind={kind}
          onCreated={handleFactorCreated}
          onClose={() => setFactorOpen(false)}
        />
      )}
    </>
  )
}
