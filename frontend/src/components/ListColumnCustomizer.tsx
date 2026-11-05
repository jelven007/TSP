import { Component, Suspense, lazy, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { ListColumnCustomizerProps } from './ListColumnCustomizerInner'
import { Modal } from './Modal'

// 懒加载壳: 关闭时不挂载内容 → @dnd-kit 不随列表页首屏加载; 关闭后保留 300ms
// 让内容 AnimatePresence 的退出动画播完再卸载。
const loadCustomizer = () => lazy(() =>
  import('./ListColumnCustomizerInner').then(m => ({ default: m.ListColumnCustomizerInner })),
)

class CustomizerBoundary extends Component<{ children: ReactNode; onRetry: () => void; onClose: () => void; open: boolean }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    if (!this.state.failed) return this.props.children
    return this.props.open ? <Modal onClose={this.props.onClose} ariaLabel="列表配置加载失败">
      <div role="alert" className="p-6 space-y-3">
        <p>列表配置加载失败，请重试。</p>
        <div className="flex gap-3">
          <button type="button" className="text-accent" onClick={this.props.onRetry}>重试</button>
          <button type="button" onClick={this.props.onClose}>关闭</button>
        </div>
      </div>
    </Modal> : null
  }
}

export function ListColumnCustomizer(props: ListColumnCustomizerProps) {
  const [mounted, setMounted] = useState(!!props.open)
  const [attempt, setAttempt] = useState(0)
  const ListColumnCustomizerInner = useMemo(loadCustomizer, [attempt])
  useEffect(() => {
    if (props.open) {
      setMounted(true)
      return
    }
    const t = setTimeout(() => setMounted(false), 300)
    return () => clearTimeout(t)
  }, [props.open])
  if (!mounted) return null
  return (
    <CustomizerBoundary key={attempt} open={props.open} onClose={props.onClose} onRetry={() => setAttempt(value => value + 1)}>
      <Suspense fallback={null}>
        <ListColumnCustomizerInner {...props} />
      </Suspense>
    </CustomizerBoundary>
  )
}
