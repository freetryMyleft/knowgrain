import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as contract from '../src/source-lifecycle-contract.ts'

const source = (overrides = {}) => ({
  source_id: '11111111-1111-4111-8111-111111111111',
  state: 'active',
  lifecycle_version: 4,
  latest_revision_id: '22222222-2222-4222-8222-222222222222',
  ...overrides,
})

test('lifecycle write body contains only the two optimistic concurrency fields', () => {
  const request = contract.sourceLifecycleRequest(source())
  assert.deepEqual(request, {
    expected_lifecycle_version: 4,
    expected_latest_revision_id: '22222222-2222-4222-8222-222222222222',
  })
  assert.deepEqual(Object.keys(request), ['expected_lifecycle_version', 'expected_latest_revision_id'])
  assert.deepEqual(contract.sourceLifecycleRequest(source({ latest_revision_id: null })), {
    expected_lifecycle_version: 4,
    expected_latest_revision_id: null,
  })
})

test('transition response must match id, desired state, next version, and revision', () => {
  const selected = source()
  const response = { ...selected, state: 'deleted', lifecycle_version: 5 }
  assert.equal(contract.isSourceLifecycleResponse(response, selected, 'deleted'), true)
  assert.equal(contract.isSourceLifecycleResponse({ ...response, source_id: 'other' }, selected, 'deleted'), false)
  assert.equal(contract.isSourceLifecycleResponse({ ...response, lifecycle_version: 6 }, selected, 'deleted'), false)
  assert.equal(contract.isSourceLifecycleResponse({ ...response, latest_revision_id: null }, selected, 'deleted'), false)
  assert.equal(contract.isSourceLifecycleResponse({ ...response, lifecycle_version: true }, selected, 'deleted'), false)
})

test('a delayed delete response from before restore cannot overwrite the newer active selection', () => {
  const originalRequest = source()
  const restoredSelection = source({ state: 'active', lifecycle_version: 6 })
  const delayedDelete = { ...originalRequest, state: 'deleted', lifecycle_version: 5 }
  assert.equal(contract.isSourceLifecycleResponse(delayedDelete, originalRequest, 'deleted', restoredSelection), false)
})
