export type GenerationJobState = 'queued' | 'running' | 'succeeded' | 'failed'

export type GenerationJob = {
  job_id: string
  topic: string
  target_page_id: string | null
  expected_target_sha256: string | null
  output_page_id: string
  state: GenerationJobState
  phase: string
  attempts: number
  error: string | null
  output_sha256: string | null
  created_at: string
  updated_at: string | null
}

export type EvidenceReference = {
  evidence_id: string
  source_id: string
  revision_id: string
  filename: string
  vault_path: string
  source_sha256: string
  parsed_text_sha256: string
  chunk_id: string
  excerpt: string
  excerpt_sha256: string
  start: number
  end: number
  page: number | null
  heading: string | null
  indexed_at: string
}

export type DraftClaim = { key: string; text: string; evidence_ids: string[] }
export type DraftSection = { heading: string; claims: DraftClaim[] }
export type GeneratedDraft = { title: string; sections: DraftSection[]; related_page_ids: string[] }

export type GenerationProposal = {
  target_page_id: string
  target_title: string
  target_sha256: string
  expected_target_sha256: string
  target_changed: boolean
  diff: string
}

export type GenerationDetail = {
  page_id: string
  generation_page_id: string
  job_id: string
  draft: GeneratedDraft
  evidence: EvidenceReference[]
  model: Record<string, unknown>
  generated_sha256: string
  proposal_target_page_id: string | null
  proposal_target_sha256: string | null
  proposal: GenerationProposal | null
  reviewed_at: string | null
  reviewed_sha256: string | null
  created_at: string
  current_sha256: string
  content_modified: boolean
  evidence_current: boolean
  status: 'draft' | 'reviewed'
  vault_path: string
}

export type EvidenceDetail = EvidenceReference & {
  current: boolean
  evidence_path: string
}

export type GenerationJobList = { jobs: GenerationJob[] }

const HASH = /^[a-f0-9]{64}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const MAX_DIFF_BYTES = 64 * 1024
const MAX_EXCERPT_CHARS = 6_000

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isNullableHash(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && HASH.test(value))
}

function isNullableUuid(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && UUID.test(value))
}

export function isGenerationJob(value: unknown): value is GenerationJob {
  if (!isRecord(value)) return false
  return typeof value.job_id === 'string' && UUID.test(value.job_id)
    && typeof value.topic === 'string' && value.topic.length <= 600
    && isNullableUuid(value.target_page_id)
    && isNullableHash(value.expected_target_sha256)
    && typeof value.output_page_id === 'string' && UUID.test(value.output_page_id)
    && (value.state === 'queued' || value.state === 'running' || value.state === 'succeeded' || value.state === 'failed')
    && typeof value.phase === 'string' && value.phase.length <= 40
    && Number.isInteger(value.attempts) && (value.attempts as number) >= 0
    && isNullableString(value.error) && (value.error === null || value.error.length <= 4_000)
    && isNullableHash(value.output_sha256)
    && typeof value.created_at === 'string'
    && isNullableString(value.updated_at)
}

export function isGenerationJobList(value: unknown): value is GenerationJobList {
  return isRecord(value) && Array.isArray(value.jobs)
    && value.jobs.length <= 500 && value.jobs.every(isGenerationJob)
}

export function isEvidenceReference(value: unknown): value is EvidenceReference {
  if (!isRecord(value)) return false
  return typeof value.evidence_id === 'string' && UUID.test(value.evidence_id)
    && typeof value.source_id === 'string' && UUID.test(value.source_id)
    && typeof value.revision_id === 'string' && UUID.test(value.revision_id)
    && typeof value.filename === 'string' && value.filename.length <= 512
    && typeof value.vault_path === 'string' && value.vault_path.length <= 2048
    && typeof value.source_sha256 === 'string' && HASH.test(value.source_sha256)
    && typeof value.parsed_text_sha256 === 'string' && HASH.test(value.parsed_text_sha256)
    && typeof value.chunk_id === 'string' && value.chunk_id.length <= 512
    && typeof value.excerpt === 'string' && value.excerpt.length <= MAX_EXCERPT_CHARS
    && typeof value.excerpt_sha256 === 'string' && HASH.test(value.excerpt_sha256)
    && Number.isInteger(value.start) && (value.start as number) >= 0
    && Number.isInteger(value.end) && (value.end as number) >= (value.start as number)
    && (value.page === null || (Number.isInteger(value.page) && (value.page as number) >= 1))
    && (value.heading === null || (typeof value.heading === 'string' && value.heading.length <= 512))
    && typeof value.indexed_at === 'string'
}

export function isEvidenceDetail(value: unknown): value is EvidenceDetail {
  if (!isEvidenceReference(value) || !isRecord(value)) return false
  const record: Record<string, unknown> = value
  return typeof record.current === 'boolean'
    && typeof record.evidence_path === 'string' && record.evidence_path.length <= 2048
}

function isDraftClaim(value: unknown): value is DraftClaim {
  return isRecord(value) && typeof value.key === 'string' && value.key.length > 0 && value.key.length <= 64
    && typeof value.text === 'string' && value.text.length > 0 && value.text.length <= 1_000
    && Array.isArray(value.evidence_ids) && value.evidence_ids.length >= 1 && value.evidence_ids.length <= 6
    && value.evidence_ids.every((id) => typeof id === 'string' && UUID.test(id))
    && new Set(value.evidence_ids).size === value.evidence_ids.length
}

function isDraftSection(value: unknown): value is DraftSection {
  return isRecord(value) && typeof value.heading === 'string' && value.heading.length > 0 && value.heading.length <= 160
    && Array.isArray(value.claims) && value.claims.length >= 1 && value.claims.length <= 12
    && value.claims.every(isDraftClaim)
}

function isGeneratedDraft(value: unknown): value is GeneratedDraft {
  if (!isRecord(value) || typeof value.title !== 'string' || value.title.length < 1 || value.title.length > 200
    || !Array.isArray(value.sections) || value.sections.length < 1 || value.sections.length > 8
    || !value.sections.every(isDraftSection)
    || !Array.isArray(value.related_page_ids) || value.related_page_ids.length > 8
    || !value.related_page_ids.every((id) => typeof id === 'string' && UUID.test(id))) return false
  const claims = value.sections.flatMap((section) => (section as DraftSection).claims)
  return claims.length <= 48 && new Set(claims.map((claim) => claim.key)).size === claims.length
}

function isProposal(value: unknown): value is GenerationProposal {
  return isRecord(value)
    && typeof value.target_page_id === 'string' && UUID.test(value.target_page_id)
    && typeof value.target_title === 'string' && value.target_title.length <= 200
    && typeof value.target_sha256 === 'string' && HASH.test(value.target_sha256)
    && typeof value.expected_target_sha256 === 'string' && HASH.test(value.expected_target_sha256)
    && typeof value.target_changed === 'boolean'
    && typeof value.diff === 'string' && new TextEncoder().encode(value.diff).length <= MAX_DIFF_BYTES
}

export function isGenerationDetail(value: unknown): value is GenerationDetail {
  if (!isRecord(value)) return false
  if (typeof value.page_id !== 'string' || !UUID.test(value.page_id)
    || typeof value.generation_page_id !== 'string' || !UUID.test(value.generation_page_id)
    || typeof value.job_id !== 'string' || !UUID.test(value.job_id)
    || !isGeneratedDraft(value.draft)
    || !Array.isArray(value.evidence) || value.evidence.length > 24 || !value.evidence.every(isEvidenceReference)
    || new Set(value.evidence.map((item) => (item as EvidenceReference).evidence_id)).size !== value.evidence.length
    || !isRecord(value.model)
    || typeof value.generated_sha256 !== 'string' || !HASH.test(value.generated_sha256)
    || !isNullableUuid(value.proposal_target_page_id)
    || !isNullableHash(value.proposal_target_sha256)
    || !('proposal' in value) || !(value.proposal === null || isProposal(value.proposal))
    || !isNullableString(value.reviewed_at)
    || !isNullableHash(value.reviewed_sha256)
    || typeof value.created_at !== 'string'
    || typeof value.current_sha256 !== 'string' || !HASH.test(value.current_sha256)
    || typeof value.content_modified !== 'boolean'
    || typeof value.evidence_current !== 'boolean'
    || (value.status !== 'draft' && value.status !== 'reviewed')
    || typeof value.vault_path !== 'string' || value.vault_path.length > 2048) return false

  const proposal = value.proposal as GenerationProposal | null
  if (proposal && (value.proposal_target_page_id !== proposal.target_page_id
    || value.proposal_target_sha256 !== proposal.expected_target_sha256)) return false
  if (!proposal && (value.proposal_target_page_id !== null || value.proposal_target_sha256 !== null)) return false

  const evidenceIds = new Set(value.evidence.map((item) => (item as EvidenceReference).evidence_id))
  return (value.draft as GeneratedDraft).sections.every((section) => section.claims.every((claim) =>
    claim.evidence_ids.every((id) => evidenceIds.has(id))))
}

export function generationErrorMessage(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value.slice(0, 600)
  if (isRecord(value)) {
    const detail = value.detail
    if (typeof detail === 'string') return detail.slice(0, 600)
    if (isRecord(detail) && typeof detail.message === 'string') return detail.message.slice(0, 600)
  }
  return '本机服务暂时无法处理生成请求，请稍后重试。'
}
