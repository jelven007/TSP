import { useEffect, useRef, useState, type ButtonHTMLAttributes } from 'react'
import { useModalPortalContainer } from './Modal'

export type PickerButtonProps = Pick<ButtonHTMLAttributes<HTMLButtonElement>,
  'id' | 'disabled' | 'aria-label' | 'aria-labelledby' | 'aria-describedby' | 'aria-invalid'>

/** 日期与时分共用行情页的触发器和弹层样式。 */
export const PICKER_BUTTON_CLASS = `inline-flex items-center gap-1.5 h-7 max-lg:min-h-8 px-2.5 rounded-btn border border-border
  bg-elevated hover:border-accent/50 text-xs text-foreground num
  focus:outline-none focus:border-accent/60 transition-colors duration-150 cursor-pointer
  disabled:opacity-40 disabled:cursor-not-allowed`
export const PICKER_POPOVER_CLASS = `z-[9999] rounded-card border border-border
  bg-surface shadow-[0_8px_30px_rgba(0,0,0,0.4)] p-3`

export function usePickerPopover({ width = 260, height = 320, align = 'right', disabled = false }: {
  width?: number
  height?: number
  align?: 'left' | 'right'
  disabled?: boolean
} = {}) {
  const portalContainer = useModalPortalContainer()
  const btnRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const close = () => setOpen(false)
  const toggle = () => {
    if (disabled) return
    if (open) { close(); return }
    if (!btnRef.current) return
    const r = btnRef.current.getBoundingClientRect()
    const dropUp = window.innerHeight - r.bottom < height + 6 && r.top > height + 6
    const top = dropUp ? Math.max(8, r.top - height - 6) : r.bottom + 6
    const rawLeft = align === 'left' ? r.left : r.right - width
    const left = Math.max(8, Math.min(rawLeft, window.innerWidth - width - 8))
    setPos({ top, left })
    setOpen(true)
  }

  useEffect(() => { if (disabled) setOpen(false) }, [disabled])
  useEffect(() => {
    if (!open) return
    const outside = (event: MouseEvent) => {
      const target = event.target as Node
      if (!btnRef.current?.contains(target) && !popRef.current?.contains(target)) setOpen(false)
    }
    const scroll = (event: Event) => {
      // 时分列表内部滚动不改变弹层相对触发器的位置。
      if (!(event.target instanceof Node) || !popRef.current?.contains(event.target)) setOpen(false)
    }
    const resize = () => setOpen(false)
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      const activeModal = document.activeElement?.closest('[role="dialog"][aria-modal="true"]')
      if (activeModal && !activeModal.contains(popRef.current)) return
      event.preventDefault()
      event.stopImmediatePropagation()
      setOpen(false)
      btnRef.current?.focus()
    }
    document.addEventListener('mousedown', outside)
    window.addEventListener('scroll', scroll, true)
    window.addEventListener('resize', resize)
    // 先于 Modal 的 document 捕获处理，让 Escape 只关闭当前日期/时分弹层。
    window.addEventListener('keydown', escape, true)
    return () => {
      document.removeEventListener('mousedown', outside)
      window.removeEventListener('scroll', scroll, true)
      window.removeEventListener('resize', resize)
      window.removeEventListener('keydown', escape, true)
    }
  }, [open])

  return { btnRef, popRef, pos, open: open && !disabled, toggle, close, portalContainer }
}
