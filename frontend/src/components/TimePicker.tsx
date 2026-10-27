import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { Clock3 } from 'lucide-react'
import { PICKER_BUTTON_CLASS, PICKER_POPOVER_CLASS, usePickerPopover, type PickerButtonProps } from './usePickerPopover'

interface TimePickerProps extends PickerButtonProps {
  value: string // HH:mm，24 小时制
  onChange: (value: string) => void
  placeholder?: string
  clearable?: boolean
  className?: string
  buttonClassName?: string
  align?: 'left' | 'right'
}

const pad = (value: number) => String(value).padStart(2, '0')

export function TimePicker({
  value, onChange, placeholder = '选择时间', clearable = false,
  className = '', buttonClassName = '', align = 'right', disabled = false, ...buttonProps
}: TimePickerProps) {
  const { btnRef, popRef, pos, open, toggle, close, portalContainer } = usePickerPopover({ align, disabled })
  const [draft, setDraft] = useState((value || '00:00').split(':'))
  const hourRef = useRef<HTMLButtonElement>(null)
  const minuteRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (open) {
      hourRef.current?.scrollIntoView?.({ block: 'nearest' })
      minuteRef.current?.scrollIntoView?.({ block: 'nearest' })
    }
  }, [open])
  const select = (next: string) => { onChange(next); close(); btnRef.current?.focus() }
  return (
    <div className={`relative inline-flex ${className}`}>
      <button {...buttonProps} ref={btnRef} type="button" disabled={disabled} aria-expanded={open}
        onClick={() => { if (!open) setDraft((value || '00:00').split(':')); toggle() }}
        className={`${PICKER_BUTTON_CLASS} ${buttonClassName}`}>
        <Clock3 aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className={value ? undefined : 'text-muted'}>{value || placeholder}</span>
      </button>
      {createPortal(
        <AnimatePresence>
          {open && pos && (
            <motion.div ref={popRef} role="group" aria-label={`${buttonProps['aria-label'] || '时间'}选择`}
              initial={{ opacity: 0, y: -4, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -4, scale: 0.97 }}
              transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
              style={{ position: 'fixed', top: pos.top, left: pos.left }}
              className={`w-[260px] ${PICKER_POPOVER_CLASS}`}>
              <div className="grid grid-cols-2 gap-2">
                {(['时', '分'] as const).map((label, index) => (
                  <div key={label}>
                    <div className="mb-1 text-center text-xs text-muted">{label}</div>
                    <div className="max-h-48 overflow-y-auto space-y-0.5">
                      {Array.from({ length: index === 0 ? 24 : 60 }, (_, number) => {
                        const option = pad(number)
                        const selected = draft[index] === option
                        return <button key={option} type="button" aria-label={`${option} ${label}`} aria-pressed={selected}
                          ref={selected ? index === 0 ? hourRef : minuteRef : undefined}
                          onClick={() => setDraft(current => current.map((part, i) => i === index ? option : part))}
                          className={`h-7 w-full rounded-btn text-xs num transition-colors ${selected ? 'bg-accent text-white font-bold' : 'text-foreground hover:bg-elevated'}`}>
                          {option}
                        </button>
                      })}
                    </div>
                  </div>
                ))}
              </div>
              <div className="mt-2 flex items-center justify-between border-t border-border pt-2">
                {clearable ? <button type="button" onClick={() => select('')} className="text-xs text-muted hover:text-accent">清空时间</button>
                  : <span className="text-xs text-secondary num">{draft.join(':')}</span>}
                <button type="button" onClick={() => select(draft.join(':'))} className="rounded-btn bg-accent px-3 py-1 text-xs text-white">确定</button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>, portalContainer,
      )}
    </div>
  )
}
