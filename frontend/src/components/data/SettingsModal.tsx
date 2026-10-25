import { useId } from 'react'
import { X } from 'lucide-react'
import { Modal } from '@/components/Modal'

export function SettingsModal({ title, onClose, children, size = 'sm', closeDisabled = false }: {
  title: string
  onClose: () => void
  children: React.ReactNode
  size?: 'sm' | 'md' | 'lg' | 'xl'
  closeDisabled?: boolean
}) {
  const titleId = useId()
  return (
    <Modal
      onClose={onClose}
      closeDisabled={closeDisabled}
      labelledBy={titleId}
      size={size}
    >
      <div className="modal-header flex items-center justify-between">
        <h3 id={titleId} className="text-sm font-medium text-foreground">{title}</h3>
        <button onClick={onClose} disabled={closeDisabled} aria-label={`关闭${title}`} className="modal-close">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="modal-body">
        {children}
      </div>
    </Modal>
  )
}
