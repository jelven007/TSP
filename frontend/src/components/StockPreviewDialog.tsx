import { Component, Suspense, lazy, useEffect, useState, type ReactNode } from 'react'
import type { Props } from './StockPreviewDialogContent'
import { Modal } from './Modal'

// 懒加载壳: 关闭 (symbol=null) 时不挂载内容 → StockPanel→echarts 整条链不进任何
// 列表页/落地页首屏; 关闭后保留 300ms 让 Modal 退出动画播完再卸载。
// 卸载同时清空弹窗内临时状态 (视图/日期范围), 与「弹窗关闭即重置」规范一致。
const StockPreviewDialogContent = lazy(() =>
  import('./StockPreviewDialogContent').then(m => ({ default: m.StockPreviewDialogContent })),
)

function PendingDetail({ props, failed = false }: { props: Props; failed?: boolean }) {
  if (!props.symbol) return null
  return <Modal onClose={props.onClose} ariaLabel="个股详情" size="lg">
    <div className="modal-header flex items-center justify-between">
      <span>{props.symbol} {props.name}</span>
      <button aria-label="关闭个股详情" onClick={props.onClose} className="modal-close">✕</button>
    </div>
    <div className="p-8 text-sm text-secondary" role={failed ? 'alert' : 'status'}>
      {failed ? '个股详情加载失败，请刷新页面后重试。' : '加载个股详情…'}
      {failed && <button onClick={() => window.location.reload()} className="ml-3 text-accent hover:underline">刷新重试</button>}
    </div>
  </Modal>
}

class DetailBoundary extends Component<{ props: Props; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    return this.state.failed ? <PendingDetail props={this.props.props} failed /> : this.props.children
  }
}

export function StockPreviewDialog(props: Props) {
  const [mounted, setMounted] = useState(!!props.symbol)
  useEffect(() => {
    if (props.symbol) {
      setMounted(true)
      return
    }
    const t = setTimeout(() => setMounted(false), 300)
    return () => clearTimeout(t)
  }, [props.symbol])
  if (!mounted) return null
  return (
    <DetailBoundary props={props}>
      <Suspense fallback={<PendingDetail props={props} />}>
        <StockPreviewDialogContent {...props} />
      </Suspense>
    </DetailBoundary>
  )
}
