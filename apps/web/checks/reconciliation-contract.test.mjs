import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isReconciliationReport } from '../src/reconciliation-contract.ts'

const report = {
  state: 'complete', checked: 3, healthy: 1, repair_queued: 1, skipped: 1,
  finished_at: '2026-10-03T00:00:00Z', detail: null,
}

test('startup report distinguishes inspection completion from queued repair counts', () => {
  assert.equal(isReconciliationReport(report), true)
  assert.equal(isReconciliationReport({ ...report, checked: 2 }), false)
  assert.equal(isReconciliationReport({ ...report, finished_at: null }), false)
  assert.equal(isReconciliationReport({ ...report, state: 'unavailable', finished_at: null }), true)
  assert.equal(isReconciliationReport({ ...report, state: 'pending', finished_at: null }), true)
})

test('malformed startup state, timestamps and counters cannot render as successful', () => {
  for (const value of [true, -1, '3', NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(isReconciliationReport({ ...report, checked: value }), false)
  }
  for (const value of [null, ['complete'], 'ready', true]) {
    assert.equal(isReconciliationReport({ ...report, state: value }), false)
  }
  assert.equal(isReconciliationReport({ ...report, finished_at: 'invalid' }), false)
  assert.equal(isReconciliationReport({ ...report, detail: 'a'.repeat(2001) }), false)
})
