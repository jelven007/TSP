import { createContext, useContext, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'

/**
 * 共享模态对话框原语 — 统一处理可访问性:
 * - role="dialog" + aria-modal + aria-labelledby / aria-label
 * - ESC 关闭
 * - 打开时把焦点移入对话框 (initialFocusRef 或首个可聚焦元素)
 * - Tab / Shift+Tab 焦点陷阱 (焦点不会跑出对话框)
 * - 关闭时把焦点还给打开前的元素
 * - 点击遮罩关闭 (可用 closeOnBackdrop 关闭)
 *
 * 视觉与定位由 index.css 的 modal-* 契约统一，调用方只指定内容布局及尺寸档位。
 */
export interface ModalProps {
  onClose: () => void
  children: ReactNode
  /** 对话框标题元素 id (用于 aria-labelledby) */
  labelledBy?: string
  /** 无可见标题时的无障碍名称 */
  ariaLabel?: string
  /** 标准宽度: 480 / 720 / 960 / 1200px，窄屏自动收缩 */
  size?: 'sm' | 'md' | 'lg' | 'xl'
  /** 图表、表格等工作区占满可用高度 */
  fillHeight?: boolean
  /** 仅用于用户主动最大化的图表 */
  maximized?: boolean
  /** 面板内容布局，不用于覆盖尺寸、背景或定位 */
  panelClassName?: string
  /** 打开时聚焦的元素; 不传则聚焦面板内首个可聚焦元素 */
  initialFocusRef?: React.RefObject<HTMLElement>
  /** 点击遮罩是否关闭 (默认 true) */
  closeOnBackdrop?: boolean
  /** 提交期间同时禁止 ESC 和遮罩关闭 */
  closeDisabled?: boolean
}

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

let nextOrder = 0
const openPanels = new Map<number, HTMLDivElement>()
let previousBodyOverflow = ''
const topPanel = () => openPanels.get(Math.max(...openPanels.keys()))
const ModalPortalContext = createContext<RefObject<HTMLDivElement> | null>(null)

/** 弹窗里的日历、菜单保持在当前面板内，便于焦点陷阱识别；页面上仍挂到 body。 */
export function useModalPortalContainer() {
  return useContext(ModalPortalContext)?.current ?? document.body
}

export function Modal({
  onClose,
  children,
  labelledBy,
  ariaLabel,
  size = 'sm',
  fillHeight = false,
  maximized = false,
  panelClassName = '',
  initialFocusRef,
  closeOnBackdrop = true,
  closeDisabled = false,
}: ModalProps) {
  const [order] = useState(() => ++nextOrder)
  const panelRef = useRef<HTMLDivElement>(null)
  // 记录鼠标按下时是否落在遮罩(而非面板)上。
  // 仅当 mousedown 和 mouseup 都在遮罩时才视为"点击遮罩关闭",
  // 避免在面板内拖选文本时鼠标移出面板边缘导致误关 (拖拽穿透)。
  const mouseDownOnBackdrop = useRef(false)
  // onClose 存 ref: 焦点陷阱/ESC effect 只在挂载时装一次。否则父级每次重渲染 (或未 memo 的
  // onClose) 都让 effect 重跑, requestAnimationFrame(focusFirst) 会在每次输入后把焦点抢回
  // 面板首个元素, 导致对话框内文本框无法输入。
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const closeDisabledRef = useRef(closeDisabled)
  closeDisabledRef.current = closeDisabled

  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    if (openPanels.size === 0) {
      previousBodyOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
    openPanels.set(order, panel)
    // 记住打开前的焦点, 关闭时还原
    const prevActive = document.activeElement as HTMLElement | null

    // 初始聚焦
    const focusFirst = () => {
      if (topPanel() !== panel) return
      if (initialFocusRef?.current) {
        initialFocusRef.current.focus()
        return
      }
      const first = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
        .find(el => el.offsetParent !== null)
      ;(first ?? panel).focus()
    }
    // 等一帧确保内容已挂载
    const raf = requestAnimationFrame(focusFirst)

    // Native focus traversal and external controls can move focus without our
    // Tab handler. Keep the topmost modal contained on those paths as well.
    const onFocusIn = (e: FocusEvent) => {
      const panel = panelRef.current
      if (!panel || panel.contains(e.target as Node)) return
      if (topPanel() !== panel) return
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter(el => el.offsetParent !== null)
      const first = nodes[0]
      const last = nodes[nodes.length - 1]
      ;(e.relatedTarget === first ? last : first ?? panel)?.focus()
    }

    const onKeyDown = (e: KeyboardEvent) => {
      if (topPanel() !== panel) return
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopImmediatePropagation()
        if (!closeDisabledRef.current) onCloseRef.current()
        return
      }
      if (e.key !== 'Tab') return
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter(el => el.offsetParent !== null || el === document.activeElement)
      if (nodes.length === 0) {
        e.preventDefault()
        panel.focus()
        return
      }
      const first = nodes[0]
      const last = nodes[nodes.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (e.shiftKey) {
        if (active === first || !panel.contains(active)) {
          e.preventDefault()
          last.focus()
        }
      } else {
        if (active === last || !panel.contains(active)) {
          e.preventDefault()
          first.focus()
        }
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    document.addEventListener('focusin', onFocusIn, true)
    return () => {
      cancelAnimationFrame(raf)
      document.removeEventListener('keydown', onKeyDown, true)
      document.removeEventListener('focusin', onFocusIn, true)
      openPanels.delete(order)
      if (openPanels.size === 0) document.body.style.overflow = previousBodyOverflow
      // 子弹窗关闭后恢复父弹窗的编辑位置；父弹窗卸载时不抢走另一弹窗的焦点。
      if (prevActive?.isConnected && (!topPanel() || topPanel()?.contains(prevActive))) prevActive.focus()
    }
    // 只在挂载时装一次: onClose 走 ref, initialFocusRef 为稳定 ref 对象, 无需进依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return createPortal(
    <div
      className="modal-overlay"
      data-maximized={maximized || undefined}
      style={{ zIndex: 1000 + order }}
      onMouseDown={(e) => {
        // 仅记录"按下时确实在遮罩上"; 在面板内按下时记 false。
        mouseDownOnBackdrop.current = e.target === e.currentTarget
      }}
      onClick={closeOnBackdrop ? (e) => {
        // 只有按下和松开都在遮罩上才关闭, 避免拖选文本误关。
        if (topPanel() === panelRef.current && !closeDisabled && mouseDownOnBackdrop.current && e.target === e.currentTarget) onClose()
      } : undefined}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-label={labelledBy ? undefined : ariaLabel}
        tabIndex={-1}
        className={`modal-panel ${panelClassName}`}
        data-size={size}
        data-fill-height={fillHeight || undefined}
        data-maximized={maximized || undefined}
        onClick={(e) => e.stopPropagation()}
      >
        <ModalPortalContext.Provider value={panelRef}>{children}</ModalPortalContext.Provider>
      </div>
    </div>,
    document.body,
  )
}
