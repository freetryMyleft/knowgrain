export type MaintenanceJob = {
  job_id: string
  source_id: string
  revision_id: string
  lifecycle_version: number
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  attempts: number
  error: string | null
  created_at: string
  updated_at: string
  lease_until: string | null
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const STATES = new Set(['queued', 'running', 'succeeded', 'failed', 'cancelled'])

function time(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 64 && Number.isFinite(Date.parse(value))
}

export function isMaintenanceJob(value: unknown, sourceId?: string): value is MaintenanceJob {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const row = value as Record<string, unknown>
  return typeof row.job_id === 'string' && UUID_PATTERN.test(row.job_id)
    && typeof row.source_id === 'string' && UUID_PATTERN.test(row.source_id)
    && (sourceId === undefined || row.source_id === sourceId)
    && typeof row.revision_id === 'string' && UUID_PATTERN.test(row.revision_id)
    && Number.isSafeInteger(row.lifecycle_version) && (row.lifecycle_version as number) >= 0
    && typeof row.state === 'string' && STATES.has(row.state)
    && Number.isSafeInteger(row.attempts) && (row.attempts as number) >= 0
    && (row.error === null || (typeof row.error === 'string' && row.error.length <= 4000))
    && time(row.created_at) && time(row.updated_at)
    && (row.lease_until === null || time(row.lease_until))
}

export function isMaintenanceList(value: unknown, sourceId: string): value is MaintenanceJob[] {
  return Array.isArray(value) && value.length <= 100
    && value.every((row) => isMaintenanceJob(row, sourceId))
    && new Set(value.map((row: MaintenanceJob) => row.job_id)).size === value.length
}

export function canRetryMaintenance(job: MaintenanceJob, state: string, version: number): boolean {
  return state === 'deleted' && job.lifecycle_version === version && job.state === 'failed'
}
