import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isFileOperation, isFileOperationList, canRetryFileOperation } from '../src/file-operation-contract.ts'

const sourceId = '11111111-1111-4111-8111-111111111111'
const job = {
  operation_id: '22222222-2222-4222-8222-222222222222', source_id: sourceId,
  kind: 'archive', lifecycle_version: 3,
  state: 'failed', attempts: 1, error: '清理失败',
  created_at: '2026-10-02T00:00:00Z', updated_at: '2026-10-02T00:00:00Z', lease_until: null,
}

test('file operation snapshots reject foreign sources, duplicate jobs and invalid epochs', () => {
  assert.equal(isFileOperationList([job], sourceId), true)
  assert.equal(isFileOperationList([job, job], sourceId), false)
  assert.equal(isFileOperationList([{ ...job, source_id: '33333333-3333-4333-8333-333333333333' }], sourceId), false)
  for (const version of [true, -1, '3', Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(isFileOperation({ ...job, lifecycle_version: version }), false)
  }
  assert.equal(isFileOperation({ ...job, state: 'unknown' }), false)
  for (const kind of [true, null, 'delete', 'Archive']) {
    assert.equal(isFileOperation({ ...job, kind }), false)
  }
  assert.equal(isFileOperation({ ...job, kind: 'restore' }), true)
  assert.equal(isFileOperation({ ...job, updated_at: 'broken' }), false)
})

test('only a failed file operation in the current deleted epoch can be retried', () => {
  assert.equal(canRetryFileOperation(job, 'deleted', 3), true)
  assert.equal(canRetryFileOperation(job, 'active', 3), false)
  assert.equal(canRetryFileOperation(job, 'deleted', 5), false)
  for (const state of ['queued', 'running', 'succeeded', 'cancelled']) {
    assert.equal(canRetryFileOperation({ ...job, state }, 'deleted', 3), false)
  }
})
