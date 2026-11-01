/** 仅迁移浏览器保存的旧标识, 旧策略不再注册。 */
export function migrateStoredStrategyValue(value: unknown): unknown {
  if (typeof value === 'string') return value.includes('s022') ? null : value.replaceAll('s021', 's02')
  if (Array.isArray(value)) return value.map(migrateStoredStrategyValue).filter(item => item !== null)
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    if (record.id === 's022' || record.strategy_id === 's022' ||
      (record.strategy_info as Record<string, unknown> | undefined)?.id === 's022') return null
    return Object.fromEntries(Object.entries(record).filter(([key]) => key !== 's022')
      .map(([key, item]) => [key.replaceAll('s021', 's02'), migrateStoredStrategyValue(item)]))
  }
  return value
}
