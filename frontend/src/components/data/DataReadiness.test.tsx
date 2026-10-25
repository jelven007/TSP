import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { DataQuality } from '@/lib/api/dataQuality'
import { DataReadiness } from './DataReadiness'

describe('data quality evidence', () => {
  it('renders unknown values and limitations without inventing zeroes', () => {
    const quality = {
      latest_daily: { partition: null, coverage_ratio: null, invalid_price_rows: null, invalid_volume_rows: null },
      historical_inputs: {
        universe: { missing_delisted_ratio: null },
        financial_publication: { unknown_ratio: 1 },
      },
    } as DataQuality
    const html = renderToStaticMarkup(<DataReadiness quality={quality} />)
    expect(html).toContain('相对当前目录覆盖：未知')
    expect(html).toContain('异常价格行：未知')
    expect(html).toContain('财务原公告时间未知比例：100.0%')
    expect(html).toContain('历史退市池缺失比例：未知')
    expect(html).toContain('历史PIT尚未验证')
  })
})
