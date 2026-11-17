import { lazy, Suspense, useEffect, useState } from 'react'
import { useDialogTask, useDialogState } from '@/lib/aiReportStore'

const AiAnalysisDialog = lazy(() => import('./AiAnalysisDialog').then(module => ({ default: module.AiAnalysisDialog })))

/**
 * AI 分析对话框宿主 —— 单点挂载在 Layout。
 *
 * 从 store 读取当前对话框状态(任务 + 最小化),把 AiAnalysisDialog 作为纯视图渲染。
 * 一次挂载,全局生效:任意页面发起的分析都会显示在这个对话框里。
 */
export function AiAnalysisHost() {
  const { task, mode } = useDialogTask()
  const { minimized } = useDialogState()
  const [opened, setOpened] = useState(false)
  useEffect(() => { if (task && !minimized) setOpened(true) }, [task, minimized])
  // Once opened, retain the view instance so minimizing keeps its input state.
  if (!opened && (!task || minimized)) return null
  return (
    <Suspense fallback={null}>
      <AiAnalysisDialog task={task} mode={mode} minimized={minimized} />
    </Suspense>
  )
}
