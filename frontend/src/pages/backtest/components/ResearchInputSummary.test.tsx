import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { BacktestProvenance } from '@/lib/api'
import { ResearchInputSummary } from './ResearchInputSummary'

describe('research input evidence', () => {
  it('keeps an unknown universe separate from a measured unknown ratio', () => {
    const provenance = {
      input_snapshot: { id: 'retained-v1', mode: 'local_snapshot', file_count: 12 },
      historical_inputs: {
        universe: { survivorship_verified: false, missing_delisted_ratio: null },
        financial_publication: { rows: 2, unknown_rows: 2, unknown_ratio: 1 },
      },
      research_governance: { status_quality: { unknown_status_ratio: .5 } },
    } as BacktestProvenance
    const html = renderToStaticMarkup(<ResearchInputSummary provenance={provenance} />)
    expect(html).toContain('历史证券状态未知比例：50.0%')
    expect(html).toContain('历史退市证券缺失比例：未知')
    expect(html).toContain('财务原公告时间未知比例：100.0%')
    expect(html).toContain('固定输入版本：retained-v1 · 12 个文件')
  })

  it('accepts older reports without new evidence fields', () => {
    expect(renderToStaticMarkup(<ResearchInputSummary provenance={undefined} />)).toBe('')
  })

  it('shows distinct replay choices and keeps them disabled while running', () => {
    const provenance = {
      input_snapshot: { id: 'frozen', file_count: 2, runtime: { app_sources_sha256: 'original-build' } },
      config_hash: 'parameters', strategy_hash: 'strategy',
    } as BacktestProvenance
    const html = renderToStaticMarkup(<ResearchInputSummary provenance={provenance}
      onReplay={() => {}} onNewInputs={() => {}} disabled />)
    expect(html).toContain('原输入复测')
    expect(html).toContain('用新数据建立试验')
    expect(html).toContain('original-build')
    expect(html.match(/disabled=""/g)).toHaveLength(2)
  })
})
