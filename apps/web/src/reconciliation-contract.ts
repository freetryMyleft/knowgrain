export type ReconciliationReport = {
  state: 'pending' | 'complete' | 'unavailable'
  checked: number
  healthy: number
  repair_queued: number
  skipped: number
  finished_at: string | null
  detail: string | null
}

export function isReconciliationReport(value: unknown): value is ReconciliationReport {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const row = value as Record<string, unknown>
  const counters = ['checked', 'healthy', 'repair_queued', 'skipped'] as const
  if (!counters.every((key) => Number.isSafeInteger(row[key]) && (row[key] as number) >= 0)) return false
  return typeof row.state === 'string' && ['pending', 'complete', 'unavailable'].includes(row.state)
    && (row.healthy as number) + (row.repair_queued as number) + (row.skipped as number) <= (row.checked as number)
    && (row.finished_at === null || (typeof row.finished_at === 'string'
      && row.finished_at.length <= 64 && Number.isFinite(Date.parse(row.finished_at))))
    && (row.state !== 'complete' || row.finished_at !== null)
    && (row.detail === null || (typeof row.detail === 'string' && row.detail.length <= 2000))
}
