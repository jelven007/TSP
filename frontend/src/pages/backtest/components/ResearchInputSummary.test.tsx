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
})
