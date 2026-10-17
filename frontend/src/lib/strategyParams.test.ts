import { describe, expect, it } from 'vitest'
import type { StrategyParamDef } from './api'
import { applyParamValue, fixedParamIds, groupParams, visibleParams } from './strategyParams'

export const conditionalParams: StrategyParamDef[] = [
  {
    id: 'mode',
    label: '模式',
    type: 'select',
    default: 'simple',
    options: ['simple', 'advanced'],
    group: '模式',
    option_applies: { advanced: { enabled: true, window: 12, missing: 99, mode: 'simple' } },
  },
  { id: 'enabled', label: '趋势过滤', type: 'bool', default: false, group: '趋势' },
  {
    id: 'window',
    label: '窗口',
    type: 'int',
    default: 5,
    min: 1,
    max: 20,
    step: 1,
    group: '趋势',
    visible_if: { all: [{ param: 'mode', in: ['advanced'] }, { param: 'enabled', in: [true] }] },
  },
  { id: 'threshold', label: '阈值', type: 'float', default: 0.1, min: 0, max: 1, step: 0.1 },
]

describe('strategy parameter metadata', () => {
  it('supports old flat metadata and stable adjacent groups', () => {
    expect(visibleParams(conditionalParams, {}).map(param => param.id))
      .toEqual(['mode', 'enabled', 'threshold'])
    expect(groupParams(conditionalParams).map(group => [group.name, group.items.map(param => param.id)]))
      .toEqual([['模式', ['mode']], ['趋势', ['enabled', 'window']], [null, ['threshold']]])
  })

  it('evaluates recursive conditions with defaults, bool strings, null, and explicit false', () => {
    const param: StrategyParamDef = {
      ...conditionalParams[2],
      visible_if: {
        any: [
          { all: [{ param: 'mode', in: ['advanced'] }, { param: 'enabled', in: ['true'] }] },
          { param: 'threshold', in: [0, null] },
        ],
      },
    }
    const defs = [...conditionalParams.slice(0, 2), param, conditionalParams[3]]
    expect(visibleParams(defs, { mode: 'advanced', enabled: 'True' })).toContain(param)
    expect(visibleParams(defs, { mode: 'advanced', enabled: false })).not.toContain(param)
    expect(visibleParams(defs, { threshold: 0 })).toContain(param)
    expect(visibleParams(defs, { threshold: null })).toContain(param)
    expect(visibleParams([{ ...param, visible_if: { all: [] } }], {})).toHaveLength(1)
    expect(visibleParams([{ ...param, visible_if: { any: [] } }], {})).toHaveLength(0)
  })

  it('applies known preset values once, keeps chosen selector and hidden values', () => {
    const original = { mode: 'simple', enabled: false, window: 8, threshold: 0.4 }
    const next = applyParamValue(conditionalParams, original, 'mode', 'advanced')
    expect(next).toEqual({ mode: 'advanced', enabled: true, window: 12, threshold: 0.4 })
    expect(original.window).toBe(8)
    expect(applyParamValue(conditionalParams, next, 'mode', 'simple').window).toBe(12)
    expect(visibleParams(conditionalParams, next)).toHaveLength(4)
  })

  it('keeps visibility controllers and presets fixed during a scan', () => {
    expect([...fixedParamIds(conditionalParams)].sort()).toEqual(['enabled', 'mode'])
  })

  it('isolates malformed optional metadata without crashing the form', () => {
    const malformed = {
      ...conditionalParams[2],
      visible_if: { all: 'bad' },
    } as unknown as StrategyParamDef
    expect(visibleParams([malformed], {})).toEqual([])
    expect(applyParamValue(conditionalParams, {}, 'unknown', 10)).toEqual({})
  })
})
