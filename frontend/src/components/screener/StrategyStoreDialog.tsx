import { Modal } from '@/components/Modal'
import { AnimatePresence } from 'framer-motion'
import { X, Store, Hammer, Download, Zap } from 'lucide-react'

interface Props {
  open: boolean
  onClose: () => void
}

/**
 * 获取策略（占位）
 * 目标：不定时更新更多策略。功能正在建设中，当前仅占位。
 */
export function StrategyStoreDialog({ open, onClose }: Props) {
  return (
    <AnimatePresence>
      {open && (
        <Modal onClose={onClose} ariaLabel="策略商店" size="md">
            {/* 标题 */}
            <div className="modal-header flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-sm font-medium text-foreground">
                <Store className="h-4 w-4 text-accent" />
                获取策略
              </span>
              <button onClick={onClose} className="modal-close" aria-label="关闭弹窗">
                <X className="h-4 w-4 text-muted" />
              </button>
            </div>

            {/* 建设中占位内容 */}
            <div className="flex-1 flex flex-col items-center justify-center px-6 py-14 text-center">
              <div className="relative mb-5">
                <div className="absolute inset-0 blur-2xl bg-amber-400/20 rounded-full" />
                <div className="relative h-16 w-16 rounded-2xl bg-amber-400/10 border border-amber-400/30 flex items-center justify-center">
                  <Hammer className="h-8 w-8 text-amber-400" />
                </div>
              </div>

              <h3 className="text-[16px] leading-6 font-semibold text-foreground mb-1.5">
                不定时更新更多策略
              </h3>
              <p className="text-sm text-muted leading-relaxed max-w-[380px]">
                敬请期待。
              </p>

              <div className="mt-6 flex items-center gap-4 text-[11px] text-muted">
                <span className="flex items-center gap-1">
                  <Download className="h-3.5 w-3.5" />
                  一键下载
                </span>
                <span className="w-px h-3 bg-border" />
                <span className="flex items-center gap-1">
                  <Zap className="h-3.5 w-3.5" />
                  无需配置 即可使用
                </span>
              </div>
            </div>

            {/* 底部关闭 */}
            <div className="modal-footer flex justify-end">
              <button
                onClick={onClose}
                className="inline-flex items-center gap-1.5 h-7 px-4 rounded-btn
                  border border-border bg-surface text-xs font-medium text-secondary
                  hover:text-accent hover:border-accent/50 transition-colors cursor-pointer"
              >
                知道了
              </button>
            </div>
        </Modal>
      )}
    </AnimatePresence>
  )
}
