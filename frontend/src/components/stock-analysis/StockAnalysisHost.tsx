import { lazy, Suspense, useEffect, useState } from 'react'
import { useDialogTask, useDialogState } from '@/lib/stockAnalysisStore'

const StockAnalysisDialog = lazy(() => import('./StockAnalysisDialog').then(module => ({ default: module.StockAnalysisDialog })))

/**
 * AI 个股分析对话框宿主 —— 单点挂载在 Layout。
 * 与财务分析的 AiAnalysisHost 并列,独立 store,蓝色主题。
 */
export function StockAnalysisHost() {
  const { task, mode } = useDialogTask()
  const { minimized } = useDialogState()
  const [opened, setOpened] = useState(false)
  useEffect(() => { if (task && !minimized) setOpened(true) }, [task, minimized])
  if (!opened && (!task || minimized)) return null
  return (
    <Suspense fallback={null}>
      <StockAnalysisDialog task={task} mode={mode} minimized={minimized} />
    </Suspense>
  )
}
