import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { OvernightExecutionSummary } from './OvernightExecutionSummary'

describe('overnight allocation explanation', () => {
  it('shows equal selected allocation and the two-stock example', () => {
    const html = renderToStaticMarkup(<OvernightExecutionSummary
      params={{ initial_cash: 1000000, position_limit: 4, entry_nav_fraction: .25 }}
      entryAllocation="equal_selected_v1" optionalTriggers />)
    expect(html).toContain('按本批入选数量等分可用现金')
    expect(html).toContain('1只100%、2只各50%、3只各约33.33%、4只各25%')
    expect(html).toContain('最多4只')
    expect(html).not.toContain('单票含费≤净值')
  })

  it('keeps a legacy strategy fixed fraction', () => {
    const html = renderToStaticMarkup(<OvernightExecutionSummary
      params={{ position_limit: 4, entry_nav_fraction: .25 }} />)
    expect(html).toContain('单票含费≤净值25%')
    expect(html).not.toContain('按本批入选数量等分')
  })
})
