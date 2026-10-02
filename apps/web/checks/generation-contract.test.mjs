import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as contract from '../src/generation-contract.ts'

const ids = {
  page: '11111111-1111-4111-8111-111111111111',
  job: '22222222-2222-4222-8222-222222222222',
  evidence: '33333333-3333-4333-8333-333333333333',
  source: '44444444-4444-4444-8444-444444444444',
  revision: '55555555-5555-4555-8555-555555555555',
  target: '66666666-6666-4666-8666-666666666666',
}

const hash = (character) => character.repeat(64)

function evidence(overrides = {}) {
  return {
    evidence_id: ids.evidence,
    source_id: ids.source,
    revision_id: ids.revision,
    filename: 'source.pdf',
    vault_path: 'Sources/source.pdf',
    source_sha256: hash('a'),
    parsed_text_sha256: hash('b'),
    chunk_id: 'chunk-1',
    excerpt: '原文引用',
    excerpt_sha256: hash('c'),
    start: 0,
    end: 4,
    page: 2,
    heading: '背景',
    indexed_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

function detail(overrides = {}) {
  return {
    page_id: ids.page,
    generation_page_id: ids.page,
    job_id: ids.job,
    draft: {
      title: '生成页面',
      sections: [{ heading: '依据', claims: [{ key: 'claim-1', text: '一条有证据的声明。', evidence_ids: [ids.evidence] }] }],
      related_page_ids: [],
    },
    evidence: [evidence()],
    model: { name: 'ollama/local-model', provider: 'ollama' },
    generated_sha256: hash('d'),
    proposal_target_page_id: null,
    proposal_target_sha256: null,
    proposal: null,
    reviewed_at: null,
    reviewed_sha256: null,
    created_at: '2026-10-01T00:00:00Z',
    current_sha256: hash('d'),
    content_modified: false,
    evidence_current: true,
    status: 'draft',
    vault_path: 'Wiki/Drafts/page.md',
    ...overrides,
  }
}

function job(overrides = {}) {
  return {
    job_id: ids.job,
    topic: '生成主题',
    target_page_id: null,
    expected_target_sha256: null,
    output_page_id: ids.page,
    state: 'queued',
    phase: 'queued',
    attempts: 0,
    error: null,
    output_sha256: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: null,
    ...overrides,
  }
}

test('generation guards accept queued jobs and a manifest with matching evidence', () => {
  assert.equal(contract.isGenerationJob(job()), true)
  assert.equal(contract.isGenerationJobList({ jobs: [job()] }), true)
  assert.equal(contract.isGenerationDetail(detail()), true)
  assert.equal(contract.isEvidenceDetail({ ...evidence(), current: false, evidence_path: 'Sources/Evidence/e.md' }), true)
})

test('one backend-bounded failure does not hide the recent job list', () => {
  const failed = job({ state: 'failed', phase: 'failed', error: 'x'.repeat(4_000) })
  assert.equal(contract.isGenerationJobList({ jobs: [failed, job()] }), true)
  assert.equal(contract.isGenerationJob(job({ error: 'x'.repeat(4_001) })), false)
})

test('generation guards reject malformed hashes, unbounded diffs, and dangling claim evidence', () => {
  assert.equal(contract.isGenerationJob(job({ output_sha256: 'not-a-hash' })), false)
  assert.equal(contract.isGenerationDetail(detail({ evidence: [] })), false)
  assert.equal(contract.isGenerationDetail(detail({ current_sha256: 'x' })), false)
  assert.equal(contract.isGenerationDetail(detail({
    proposal_target_page_id: ids.target,
    proposal_target_sha256: hash('e'),
    proposal: {
      target_page_id: ids.target,
      target_title: '目标页面',
      target_sha256: hash('f'),
      expected_target_sha256: hash('e'),
      target_changed: false,
      diff: 'x'.repeat(64 * 1024 + 1),
    },
  })), false)
})

test('proposal detail preserves original and current target hashes with a bounded textual diff', () => {
  const value = detail({
    proposal_target_page_id: ids.target,
    proposal_target_sha256: hash('e'),
    proposal: {
      target_page_id: ids.target,
      target_title: '目标页面',
      target_sha256: hash('e'),
      expected_target_sha256: hash('e'),
      target_changed: false,
      diff: '@@ -1 +1 @@\n-old\n+new',
    },
  })
  assert.equal(contract.isGenerationDetail(value), true)
  assert.equal(contract.isGenerationDetail({ ...value, proposal: { ...value.proposal, target_changed: 'false' } }), false)
})
