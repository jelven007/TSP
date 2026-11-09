/**
 * 股票列表表格骨架（自选/策略页共享）。
 *
 * 职责：表头渲染（读列配置 label/align + 可排序三态指示器）、表体遍历、sticky 表头。
 * 不内置任何业务逻辑：单元格内容（含 symbol 列交互、操作列、ext 列）由调用方通过
 * renderCell / renderExtraCol 注入。这样两个页面的特有交互得以保留，同时表头能力一致。
 */
import { cloneElement, isValidElement, useRef, type ReactElement, type ReactNode } from 'react'
import { useVirtualizer, type VirtualItem } from '@tanstack/react-virtual'
import { cn } from '@/lib/cn'
import type { ColumnConfig } from '@/lib/list-columns'
import { UNSORTABLE_KEYS } from '@/lib/stock-table'
import { VIRTUAL_LIST_THRESHOLD, useParentScroll } from '@/components/virtual-list/useParentScroll'
import type { SortState } from './useTableSort'

export type { SortState }

export interface StockDataTableProps {
  columns: ColumnConfig[]
  rows: any[]
  /** 单元格渲染回调。返回 null 时回退到内置纯数据列渲染。 */
  renderCell: (r: any, col: ColumnConfig) => ReactNode
  /** 行 key（默认取 r.symbol） */
  rowKey?: (r: any) => string | number
  /** 行 className（默认含 hover） */
  rowClassName?: (r: any) => string
  /** 表头是否 sticky（自选页需要，策略页不需要） */
  headerSticky?: boolean
  /** 最小表格宽度，默认按列数计算 */
  minWidth?: number
  /** 显式列宽使虚拟滚动时的布局不受可见行内容影响。 */
  columnWidths?: Readonly<Record<string, number>>
  /** 此列在其配置的最小宽度之外独占剩余宽度。 */
  stretchColumnId?: string
  extraColumnWidth?: number
  /** 与策略每日结果一致的紧凑密度。 */
  compact?: boolean
  /** 排序：外部受控时传入（含当前 sort 与 toggle）；不传则表头不可排序 */
  sort?: SortState | null
  onSortToggle?: (colId: string) => void
  /** 实例级放行: 让 UNSORTABLE_KEYS 中的 builtin 列在本表也可排序 (如自选页分时列) */
  extraSortableKeys?: ReadonlySet<string>
  /** 追加在每行末尾的额外单元格（如自选页的操作列） */
  renderExtraCol?: (r: any) => ReactNode
  /** 追加的表头单元格（对应 renderExtraCol） */
  extraHeader?: ReactNode
  /** 自定义表头单元格内容覆盖（如日k眼睛按钮）。返回 undefined 则用 col.label */
  renderHeaderContent?: (col: ColumnConfig) => ReactNode | undefined
  /** 外层容器 className */
  className?: string
}

function alignThClass(align: ColumnConfig['align'], compact: boolean): string {
  // 表头一律不换行: 窄列(如收起的图表列)中标签/排序箭头折行会把整行表头顶高
  return cn(compact ? 'px-2 py-1.5' : 'px-3 py-2.5', 'font-medium whitespace-nowrap',
    align === 'right' && 'text-right', align === 'center' && 'text-center')
}

export function StockDataTable({
  columns, rows, renderCell,
  rowKey = (r: any) => r.symbol,
  rowClassName = () => 'border-t border-border hover:bg-elevated/50',
  headerSticky = false,
  minWidth,
  columnWidths,
  stretchColumnId,
  extraColumnWidth = 88,
  compact = false,
  sort,
  onSortToggle,
  extraSortableKeys,
  renderExtraCol,
  extraHeader,
  renderHeaderContent,
  className = 'rounded-card border border-border overflow-x-auto',
}: StockDataTableProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const visibleColumns = columns.filter(c => c.visible)
  const width = (id: string) => columnWidths?.[id] ?? 110
  const hasExtraColumn = !!(renderExtraCol || extraHeader)
  const computedMinWidth = columnWidths
    ? Math.max(minWidth ?? 0, visibleColumns.reduce((sum, col) => sum + width(col.id), 0)
      + (hasExtraColumn ? extraColumnWidth : 0))
    : minWidth ?? Math.max(900, visibleColumns.length * 110)
  const virtualized = rows.length > VIRTUAL_LIST_THRESHOLD
  const { getScrollElement, scrollMargin } = useParentScroll(containerRef, virtualized)
  const rowVirtualizer = useVirtualizer({
    count: virtualized ? rows.length : 0,
    getScrollElement,
    estimateSize: () => compact ? 32 : 56,
    getItemKey: index => rowKey(rows[index]),
    overscan: 10,
    scrollMargin,
  })
  const virtualRows = virtualized ? rowVirtualizer.getVirtualItems() : []
  const totalSize = virtualized ? rowVirtualizer.getTotalSize() : 0
  const firstVirtualRow = virtualRows[0]
  const lastVirtualRow = virtualRows[virtualRows.length - 1]
  const topPadding = firstVirtualRow ? firstVirtualRow.start - scrollMargin : 0
  const bottomPadding = lastVirtualRow
    ? totalSize - (lastVirtualRow.end - scrollMargin)
    : totalSize
  const columnCount = visibleColumns.length + (renderExtraCol || extraHeader ? 1 : 0)

  const isColSortable = (col: ColumnConfig): boolean => {
    // 排序能力由调用方是否提供 onSortToggle 决定；sort 是否为 null 只影响当前指示器
    if (!onSortToggle) return false
    if (col.source.type === 'builtin' && UNSORTABLE_KEYS.has(col.source.key) && !extraSortableKeys?.has(col.source.key)) return false
    return true
  }

  const theadClass = headerSticky
    ? cn('sticky top-0 z-10 after:absolute after:inset-x-0 after:bottom-0 after:h-px after:bg-border',
      compact ? 'bg-elevated' : 'bg-surface')
    : 'bg-elevated'

  const prepareCell = (cell: ReactNode, id: string) => {
    if (!isValidElement(cell)) return cell
    const element = cell as ReactElement<{ className?: string; 'data-column'?: string }>
    return cloneElement(element, {
      key: id,
      'data-column': id,
      className: cn(element.props.className, compact && 'px-2 py-1.5 align-middle',
        columnWidths && 'overflow-hidden'),
    })
  }

  const renderRow = (r: any, virtualRow?: VirtualItem) => (
    <tr
      key={rowKey(r)}
      ref={virtualRow ? rowVirtualizer.measureElement : undefined}
      data-index={virtualRow?.index}
      className={cn('transition-colors duration-150 ease-smooth group', compact && 'h-8', rowClassName(r))}
    >
      {visibleColumns.map(col => {
        // renderCell 返回的 <td> 无 key, 这里补上避免 React key 警告
        const cell = renderCell(r, col)
        return prepareCell(cell, col.id)
      })}
      {renderExtraCol && prepareCell(renderExtraCol(r), 'extra')}
    </tr>
  )

  return (
    <div ref={containerRef} className={className}>
      <table className={cn('w-full', compact ? 'text-xs' : 'text-sm', columnWidths && 'table-fixed')}
        style={{ minWidth: computedMinWidth,
          ...(columnWidths && !stretchColumnId ? { width: computedMinWidth } : {}) }}>
        {columnWidths && <colgroup>
          {visibleColumns.map(col => <col key={col.id}
            style={col.id === stretchColumnId ? undefined : { width: width(col.id) }} />)}
          {hasExtraColumn && <col style={{ width: extraColumnWidth }} />}
        </colgroup>}
        <thead className={theadClass}>
          <tr className="text-left text-secondary">
            {visibleColumns.map(col => {
              const sortable = isColSortable(col)
              const isSorted = sort?.key === col.id
              const dir = isSorted ? sort!.dir : null
              const contentOverride = renderHeaderContent?.(col)
              return (
                <th
                  key={col.id}
                  data-column={col.id}
                  className={`${alignThClass(col.align, compact)} ${sortable ? 'cursor-pointer select-none group' : ''}`}
                  onClick={sortable ? () => onSortToggle!(col.id) : undefined}
                >
                  {contentOverride !== undefined ? contentOverride : col.label}
                  {sortable && (
                    <span className="inline-block ml-1 text-[10px] opacity-30 group-hover:opacity-60 transition-opacity">
                      {isSorted ? (dir === 'asc' ? '↑' : '↓') : '↕'}
                    </span>
                  )}
                </th>
              )
            })}
            {extraHeader && (
              <th data-column="extra" className={alignThClass('right', compact)}>{extraHeader}</th>
            )}
          </tr>
        </thead>
        <tbody>
          {virtualized && topPadding > 0 && (
            <tr aria-hidden="true">
              <td colSpan={columnCount} className="p-0 border-0" style={{ height: topPadding }} />
            </tr>
          )}
          {virtualized
            ? virtualRows.map(virtualRow => renderRow(rows[virtualRow.index], virtualRow))
            : rows.map((r: any) => renderRow(r))}
          {virtualized && bottomPadding > 0 && (
            <tr aria-hidden="true">
              <td colSpan={columnCount} className="p-0 border-0" style={{ height: bottomPadding }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
