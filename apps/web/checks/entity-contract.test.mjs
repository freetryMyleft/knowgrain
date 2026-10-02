import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isPageEntities, isEntityPages } from '../src/entity-contract.ts'

const id = '11111111-1111-4111-8111-111111111111'
const entity = { entity_id: 'a'.repeat(64), name: 'Knowgrain', entity_type: 'PROJECT', evidence_ids: [id] }
const forward = { page_id: id, content_sha256: 'b'.repeat(64), binding_current: true, evidence_current: true, entities: [entity], truncated: false }
test('current entity mappings require valid unique evidence IDs and page freshness', () => {
  assert.equal(isPageEntities(forward), true)
  assert.equal(isPageEntities({ ...forward, binding_current: false }), false)
  assert.equal(isPageEntities({ ...forward, evidence_current: false }), false)
  assert.equal(isPageEntities({ ...forward, evidence_current: false, entities: [] }), true)
  assert.equal(isPageEntities({ ...forward, entities: [entity, entity] }), false)
  assert.equal(isPageEntities({ ...forward, entities: [{ ...entity, evidence_ids: ['../file'] }] }), false)
})
test('reverse entity mappings require bounded distinct pages and evidence', () => {
  const page = { page_id: id, title: 'Wiki', vault_path: 'Wiki/Pages/page.md', content_sha256: 'b'.repeat(64), evidence_ids: [id] }
  const reverse = { entity_id: entity.entity_id, name: entity.name, pages: [page], truncated: false }
  assert.equal(isEntityPages(reverse), true)
  assert.equal(isEntityPages({ ...reverse, pages: [page, page] }), false)
  assert.equal(isEntityPages({ ...reverse, pages: [{ ...page, evidence_ids: [] }] }), false)
  assert.equal(isEntityPages({ ...reverse, pages: [{ ...page, content_sha256: 'wrong' }] }), false)
  assert.equal(isEntityPages({ ...reverse, pages: [{ ...page, title: `多行\n${'长标题'.repeat(1024)}`, vault_path: 'Wiki/Pages/外部\n文件.md' }] }), true)
})
