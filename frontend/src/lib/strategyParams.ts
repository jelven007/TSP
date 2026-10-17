import type { ParamVisibleIf, StrategyParamDef } from './api'

type Values = Record<string, any>

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
