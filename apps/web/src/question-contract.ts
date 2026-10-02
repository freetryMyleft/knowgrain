import { isEvidenceReference } from './generation-contract.ts'
import type { DraftClaim, EvidenceReference } from './generation-contract'

export type QueryResult = {
  status: 'answered' | 'insufficient'
  message: string
  claims: DraftClaim[]
  evidence: (EvidenceReference & { current: boolean })[]
  model: { name: string; provider: string; generated_at: string }
  evidence_current: boolean
}
export type QueryJob = {
  job_id: string; question: string
  state: 'queued' | 'running' | 'succeeded' | 'failed'
  attempts: number; error: string | null
  created_at: string; updated_at: string | null; lease_until: string | null
  result?: QueryResult | null
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
function nullableText(value: unknown): boolean { return value === null || typeof value === 'string' }
function currentEvidence(value: unknown): value is EvidenceReference & { current: boolean } {
  return record(value) && typeof value.current === 'boolean' && isEvidenceReference(value)
}
function claim(value: unknown): value is DraftClaim {
  return record(value) && typeof value.key === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value.key)
    && typeof value.text === 'string' && value.text.length > 0 && value.text.length <= 1000
    && Array.isArray(value.evidence_ids) && value.evidence_ids.length > 0 && value.evidence_ids.length <= 6
    && value.evidence_ids.every(id => typeof id === 'string' && UUID.test(id))
    && new Set(value.evidence_ids).size === value.evidence_ids.length
}
export function isQueryResult(value: unknown): value is QueryResult {
  if (!record(value) || !['answered', 'insufficient'].includes(String(value.status))
    || typeof value.message !== 'string' || value.message.length > 1000
    || typeof value.evidence_current !== 'boolean'
    || !record(value.model) || typeof value.model.name !== 'string' || value.model.name.length > 256
    || typeof value.model.provider !== 'string' || value.model.provider.length > 80
    || typeof value.model.generated_at !== 'string'
    || !Array.isArray(value.claims) || value.claims.length > 12 || !value.claims.every(claim)
    || !Array.isArray(value.evidence) || value.evidence.length > 24
    || !value.evidence.every(currentEvidence)) return false
  const ids = new Set(value.evidence.map(item => item.evidence_id))
  const claims = value.claims as DraftClaim[]
  if (ids.size !== value.evidence.length || new Set(claims.map(item => item.key)).size !== claims.length) return false
  if (value.status === 'insufficient') return claims.length === 0 && ids.size === 0
    && value.message === '无法核实：当前资料不足以支持该问题。' && value.evidence_current === true
  return value.message === '' && claims.length > 0 && claims.every(item => item.evidence_ids.every(id => ids.has(id)))
    && value.evidence.every(item => claims.some(c => c.evidence_ids.includes(item.evidence_id)))
    && value.evidence_current === value.evidence.every(item => item.current)
}
export function isQueryJob(value: unknown, detail = false): value is QueryJob {
  return record(value) && typeof value.job_id === 'string' && UUID.test(value.job_id)
    && typeof value.question === 'string' && value.question.length > 0 && value.question.length <= 1000
    && ['queued', 'running', 'succeeded', 'failed'].includes(String(value.state))
    && Number.isInteger(value.attempts) && (value.attempts as number) >= 0
    && (value.error === null || (typeof value.error === 'string' && value.error.length <= 4000))
    && typeof value.created_at === 'string' && nullableText(value.updated_at) && nullableText(value.lease_until)
    && (value.result === undefined ? !detail : value.result === null ? value.state !== 'succeeded' : value.state === 'succeeded' && isQueryResult(value.result))
}
export function isQueryList(value: unknown): value is { jobs: QueryJob[] } {
  return record(value) && Array.isArray(value.jobs) && value.jobs.length <= 100 && value.jobs.every(item => isQueryJob(item))
}
export function evidenceIdFromWikiTarget(target: string): string | null {
  const match = target.match(/^Sources\/Evidence\/([0-9a-f-]{36})(?:\.md)?$/i)
  return match && UUID.test(match[1]) ? match[1] : null
}
export function queryStateLabel(state: QueryJob['state']): string {
  return { queued: '等待检索', running: '正在核对资料', succeeded: '回答已保存', failed: '任务失败' }[state]
}
