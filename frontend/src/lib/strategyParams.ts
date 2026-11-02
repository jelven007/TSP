import type { ParamVisibleIf, StrategyParamDef } from './api'

type Values = Record<string, any>

const S02_PARAM_LAYOUT = [
  ['尾盘选股', [
    'prior_return_min', 'prior_return_max', 'current_return_min', 'current_return_max',
    'float_cap_min', 'float_cap_max', 'amount20_min', 'market_positive_min',
  ]],
  ['买入与仓位', [
    'initial_cash', 'entry_nav_fraction', 'entry_budget', 'position_limit',
  ]],
  ['次日卖出', [
    'stop_loss_pct', 'take_profit_pct', 'guard_time', 'sell_deadline',
  ]],
  ['费用与容量', [
    'participation', 'slippage', 'commission', 'minimum_commission', 'transfer_fee', 'stamp_tax',
  ]],
] as const

/** UI-only ordering for builtin strategies; execution metadata stays unchanged. */
export function layoutStrategyParams(strategyId: string, defs: StrategyParamDef[]): StrategyParamDef[] {
  if (strategyId !== 's02') return defs
  const byId = new Map(defs.map(param => [param.id, param]))
  const known = new Set<string>(S02_PARAM_LAYOUT.flatMap(([, ids]) => ids))
  return [
    ...S02_PARAM_LAYOUT.flatMap(([group, ids]) =>
      ids.flatMap(id => byId.has(id) ? [{ ...byId.get(id)!, group }] : [])),
    ...defs.filter(param => !known.has(param.id)),
  ]
}

function conditionMatches(cond: ParamVisibleIf, values: Values, defs: StrategyParamDef[], depth = 0): boolean {
  if (!cond || typeof cond !== 'object' || depth > 32) return false
  if (cond.all !== undefined) {
    return Array.isArray(cond.all) && cond.all.every(item => conditionMatches(item, values, defs, depth + 1))
  }
  if (cond.any !== undefined) {
    return Array.isArray(cond.any) && cond.any.some(item => conditionMatches(item, values, defs, depth + 1))
  }
  if (!cond.param || !Array.isArray(cond.in)) return false

  const definition = defs.find(param => param.id === cond.param)
  const actual = Object.hasOwn(values, cond.param) ? values[cond.param] : definition?.default
  const normalize = (value: unknown) => (
    definition?.type === 'bool'
    && typeof value === 'string'
    && ['true', 'false'].includes(value.toLowerCase())
      ? value.toLowerCase() === 'true'
      : value
  )
  return cond.in.some(expected => normalize(actual) === normalize(expected))
}

/** Visibility changes the editor only; hidden values remain available to strategy execution. */
export function visibleParams(defs: StrategyParamDef[], values: Values): StrategyParamDef[] {
  return defs.filter(param => param.visible_if == null || conditionMatches(param.visible_if, values, defs))
}

export function groupParams(defs: StrategyParamDef[]): { name: string | null; items: StrategyParamDef[] }[] {
  const groups: { name: string | null; items: StrategyParamDef[] }[] = []
  for (const param of defs) {
    const name = typeof param.group === 'string' && param.group.trim() ? param.group : null
    const last = groups[groups.length - 1]
    if (last && last.name === name) last.items.push(param)
    else groups.push({ name, items: [param] })
  }
  return groups
}

/** Apply a select preset once without recursively triggering another preset. */
export function applyParamValue(defs: StrategyParamDef[], values: Values, id: string, value: any): Values {
  const definition = defs.find(param => param.id === id)
  if (!definition) return values

  const next = { ...values }
  const presets = definition.option_applies
  const patch = presets && Object.hasOwn(presets, String(value)) ? presets[String(value)] : undefined
  if (patch && typeof patch === 'object' && !Array.isArray(patch)) {
    for (const target of defs) {
      if (Object.hasOwn(patch, target.id) && !['__proto__', 'constructor', 'prototype'].includes(target.id)) {
        next[target.id] = patch[target.id]
      }
    }
  }
  return { ...next, [id]: value }
}

/** Controllers and preset selectors remain fixed so each scan uses one applicable parameter set. */
export function fixedParamIds(defs: StrategyParamDef[]): Set<string> {
  const fixed = new Set<string>()
  const visit = (cond?: ParamVisibleIf, depth = 0) => {
    if (!cond || typeof cond !== 'object' || depth > 32) return
    if (cond.param) fixed.add(cond.param)
    if (Array.isArray(cond.all)) cond.all.forEach(item => visit(item, depth + 1))
    if (Array.isArray(cond.any)) cond.any.forEach(item => visit(item, depth + 1))
  }
  for (const param of defs) {
    visit(param.visible_if)
    if (param.option_applies && Object.keys(param.option_applies).length) fixed.add(param.id)
  }
  return fixed
}
