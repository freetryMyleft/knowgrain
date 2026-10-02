import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isQueryJob, isQueryResult, evidenceIdFromWikiTarget } from '../src/question-contract.ts'

const id = '11111111-1111-4111-8111-111111111111'
const quote = {
  evidence_id: id, source_id: id, revision_id: id, filename: 'sample.txt',
  vault_path: 'Sources/Files/sample.txt', source_sha256: 'a'.repeat(64),
  parsed_text_sha256: 'b'.repeat(64), excerpt_sha256: 'c'.repeat(64),
  chunk_id: 'chunk-one', excerpt: '原文', start: 0, end: 2, page: null,
  heading: null, indexed_at: '2026-10-02T00:00:00Z', current: true,
}
const result = {
  status: 'answered', message: '', claims: [{ key: 'claim-1', text: '原文中的事实。', evidence_ids: [id] }],
  evidence: [quote], model: { name: 'qwen3.6:35b', provider: 'ollama', generated_at: '2026-10-02T00:00:00Z' }, evidence_current: true,
}
test('answers require complete known citations and consistent freshness', () => {
  assert.equal(isQueryResult(result), true)
  assert.equal(isQueryResult({ ...result, evidence: [] }), false)
  assert.equal(isQueryResult({ ...result, evidence: [{ ...quote, current: false }] }), false)
  assert.equal(isQueryResult({ ...result, evidence: [{ ...quote, current: false }], evidence_current: false }), true)
  assert.equal(isQueryResult({ ...result, claims: [result.claims[0], result.claims[0]] }), false)
})
test('insufficient answers cannot carry model claims or citations', () => {
  const empty = { ...result, status: 'insufficient', claims: [], evidence: [], message: '无法核实：当前资料不足以支持该问题。' }
  assert.equal(isQueryResult(empty), true)
  assert.equal(isQueryResult({ ...empty, claims: result.claims }), false)
  assert.equal(isQueryResult({ ...empty, evidence: [quote] }), false)
  assert.equal(isQueryResult({ ...empty, message: '模型自由发挥。' }), false)
  assert.equal(isQueryResult({ ...result, message: '未引用的补充事实。' }), false)
})
test('completed job details must contain a valid retained answer, lists may omit it', () => {
  const job = { job_id: id, question: '问题', state: 'succeeded', attempts: 1, error: null, created_at: '', updated_at: '', lease_until: null }
  assert.equal(isQueryJob(job), true)
  assert.equal(isQueryJob(job, true), false)
  assert.equal(isQueryJob({ ...job, result }, true), true)
  assert.equal(isQueryJob({ ...job, result: null }, true), false)
})
test('Wiki evidence navigation accepts only the canonical relative UUID target', () => {
  assert.equal(evidenceIdFromWikiTarget(`Sources/Evidence/${id}`), id)
  assert.equal(evidenceIdFromWikiTarget(`Sources/Evidence/${id}.md`), id)
  for (const path of [`../Sources/Evidence/${id}`, `/Sources/Evidence/${id}`, `https://site/Sources/Evidence/${id}`, 'Sources/Evidence/not-an-id']) assert.equal(evidenceIdFromWikiTarget(path), null)
})
