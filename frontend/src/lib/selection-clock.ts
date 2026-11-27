export function selectionWindow(strategyId?: string | null) {
  return strategyId === 'custom_mvxxcs03' || strategyId === 'custom_mvxxcs05' ? '22:00～23:00' : '21:00～22:00'
}
