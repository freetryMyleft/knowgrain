import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isMaintenanceJob, isMaintenanceList, canRetryMaintenance } from '../src/maintenance-contract.ts'

const sourceId = '11111111-1111-4111-8111-111111111111'
const job = {
  job_id: '22222222-2222-4222-8222-222222222222', source_id: sourceId,
  revision_id: '33333333-3333-4333-8333-333333333333', lifecycle_version: 3,
  state: 'failed', attempts: 1, error: '清理失败',
  created_at: '2026-10-02T00:00:00Z', updated_at: '2026-10-02T00:00:00Z', lease_until: null,
}

test('cleanup snapshots reject foreign sources, duplicate jobs and invalid epochs', () => {
  assert.equal(isMaintenanceList([job], sourceId), true)
  assert.equal(isMaintenanceList([job, job], sourceId), false)
  assert.equal(isMaintenanceList([{ ...job, source_id: job.revision_id }], sourceId), false)
  for (const version of [true, -1, '3', Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(isMaintenanceJob({ ...job, lifecycle_version: version }), false)
  }
  assert.equal(isMaintenanceJob({ ...job, state: 'unknown' }), false)
  assert.equal(isMaintenanceJob({ ...job, updated_at: 'broken' }), false)
})

test('only a failed cleanup in the current deleted epoch can be retried', () => {
  assert.equal(canRetryMaintenance(job, 'deleted', 3), true)
  assert.equal(canRetryMaintenance(job, 'active', 3), false)
  assert.equal(canRetryMaintenance(job, 'deleted', 5), false)
  for (const state of ['queued', 'running', 'succeeded', 'cancelled']) {
    assert.equal(canRetryMaintenance({ ...job, state }, 'deleted', 3), false)
  }
})
