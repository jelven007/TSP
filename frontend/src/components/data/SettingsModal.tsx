import { useId } from 'react'
import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import { Modal } from '@/components/Modal'

export function SettingsModal({ title, onClose, children, width = 'max-w-md', closeDisabled = false }: {
  title: string
  onClose: () => void
  children: React.ReactNode
  /** 弹窗最大宽度 (Tailwind max-w-* 类), 默认 max-w-md */
  width?: string
  closeDisabled?: boolean
}) {
  const titleId = useId()
  return (
    <Modal
      onClose={onClose}
      closeDisabled={closeDisabled}
      labelledBy={titleId}
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      panelClassName={`relative rounded-card border border-border bg-surface shadow-2xl mx-4 w-full ${width} max-h-[88vh] flex flex-col overflow-hidden`}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 8 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="flex flex-col min-h-0"
      >
        <div className="flex items-center justify-between px-5 py-3 border-b border-border shrink-0">
          <h3 id={titleId} className="text-sm font-medium text-foreground">{title}</h3>
          <button onClick={onClose} disabled={closeDisabled} aria-label={`关闭${title}`} className="p-0.5 rounded hover:bg-elevated text-secondary disabled:opacity-50">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-5 flex-1 overflow-y-auto">
          {children}
        </div>
      </motion.div>
    </Modal>
  )
}
