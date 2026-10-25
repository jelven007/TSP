import { useEffect, useRef } from 'react'
import * as echarts from '@/lib/echarts'
import type { ECharts, EChartsOption } from '@/lib/echarts'

/**
 * ECharts 实例管理 Hook — 自动初始化/resize/销毁。
 * 返回 ref 绑定到容器 div，和 setOption 方法。
 *
 * 可传入一个外部 ref (containerRef) 让调用方共享同一 DOM 节点,
 * 用于在 setOption 前读取图表状态 (例如保留 dataZoom 缩放窗口)。
 * option 包含数据与主题依赖；updateKey 仅用于新任务复用相同数据时强制更新。
 */
export function useECharts(
  option: EChartsOption | null,
  updateKey?: string | number,
  containerRef?: React.RefObject<HTMLDivElement>,
) {
  const ownRef = useRef<HTMLDivElement>(null)
  const chartRef = containerRef ?? ownRef
  const instanceRef = useRef<ECharts | null>(null)

  // 初始化 / 销毁
  useEffect(() => {
    if (!chartRef.current) return
    const container = chartRef.current
    instanceRef.current = echarts.init(container, undefined, { renderer: 'canvas' })
    const resizeObserver = new ResizeObserver(() => instanceRef.current?.resize())
    resizeObserver.observe(container)

    return () => {
      resizeObserver.disconnect()
      instanceRef.current?.dispose()
      instanceRef.current = null
    }
  }, [chartRef])

  // 更新 option
  useEffect(() => {
    if (!instanceRef.current || !option) return
    instanceRef.current.setOption(option, { notMerge: true })
  }, [chartRef, option, updateKey])

  return chartRef
}
