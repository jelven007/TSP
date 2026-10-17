import type { ParamVisibleIf, StrategyParamDef } from './api'

type Values = Record<string, any>

function conditionMatches(cond: ParamVisibleIf, values: Values, defs: StrategyParamDef[], depth = 0): boolean {
  if (!cond || typeof cond !== 'object' || depth > 32) return false
  if (cond.all !== undefined) return Array.isArray(cond.all) && cond.all.every(c => conditionMatches(c, values, defs, depth + 1))
  if (cond.any !== undefined) return Array.isArray(cond.any) && cond.any.some(c => conditionMatches(c, values, defs, depth + 1))
  if (!cond.param || !Array.isArray(cond.in)) return false
  const definition = defs.find(p => p.id === cond.param)
  const actual = values[cond.param] ?? definition?.default
  const normalize = (v: unknown) => definition?.type === 'bool' && typeof v === 'string'
    && ['true', 'false'].includes(v.toLowerCase()) ? v.toLowerCase() === 'true' : v
  return cond.in.some(expected => normalize(actual) === normalize(expected))
}

/** 显隐只影响编辑/扫描，不删除保存值，也不改变策略执行参数。 */
export function visibleParams(defs: StrategyParamDef[], values: Values): StrategyParamDef[] {
  return defs.filter(p => p.visible_if == null || conditionMatches(p.visible_if, values, defs))
}

export function groupParams(defs: StrategyParamDef[]): { name: string | null; items: StrategyParamDef[] }[] {
  const groups: { name: string | null; items: StrategyParamDef[] }[] = []
  for (const p of defs) {
    const name = typeof p.group === 'string' && p.group.trim() ? p.group : null
    const last = groups[groups.length - 1]
    if (last && last.name === name) last.items.push(p)
    else groups.push({ name, items: [p] })
  }
  return groups
}

/** 选项预设仅在用户选择时应用一次；忽略未知参数，不递归触发预设。 */
export function applyParamValue(defs: StrategyParamDef[], values: Values, id: string, value: any): Values {
  const def = defs.find(p => p.id === id)
  if (!def) return values
  const next = { ...values }
  const presets = def.option_applies
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

/** 条件控制器/预设选择器固定后分别扫描，保证每个网格属于同一适用条件。 */
export function fixedParamIds(defs: StrategyParamDef[]): Set<string> {
  const fixed = new Set<string>()
  const visit = (cond?: ParamVisibleIf, depth = 0) => {
    if (!cond || typeof cond !== 'object' || depth > 32) return
    if (cond.param) fixed.add(cond.param)
    if (Array.isArray(cond.all)) cond.all.forEach(c => visit(c, depth + 1))
    if (Array.isArray(cond.any)) cond.any.forEach(c => visit(c, depth + 1))
  }
  for (const p of defs) {
    visit(p.visible_if)
    if (p.option_applies && Object.keys(p.option_applies).length) fixed.add(p.id)
  }
  return fixed
}
