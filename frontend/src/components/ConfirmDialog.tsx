import { useId, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { Modal } from './Modal'

export function ConfirmDialog({ title, children, onClose, onConfirm, confirmText = '确认', danger = false, pending = false }: {
  title: string
  children: ReactNode
  onClose: () => void
  onConfirm: () => void
  confirmText?: string
  danger?: boolean
  pending?: boolean
}) {
  const titleId = useId()
  return (
    <Modal onClose={onClose} labelledBy={titleId} size="sm" closeDisabled={pending}>
      <div className="modal-header flex items-center justify-between gap-3">
        <h3 id={titleId}>{title}</h3>
        <button onClick={onClose} disabled={pending} aria-label={`关闭${title}`} className="modal-close">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="modal-body text-xs leading-relaxed text-secondary">{children}</div>
      <div className="modal-footer flex justify-end gap-2">
        <button onClick={onClose} disabled={pending} className="h-8 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated disabled:opacity-50">取消</button>
        <button onClick={onConfirm} disabled={pending} className={`h-8 rounded-btn px-3 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 ${danger ? 'bg-danger' : 'bg-accent'}`}>
          {pending ? '处理中…' : confirmText}
        </button>
      </div>
    </Modal>
  )
}
