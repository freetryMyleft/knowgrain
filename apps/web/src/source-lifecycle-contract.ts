export type SourceLifecycleState = 'active' | 'deleted'

export type SourceLifecycleSnapshot = {
  source_id: string
  state: SourceLifecycleState
  lifecycle_version: number
  latest_revision_id: string | null
}

export type SourceLifecycleRequest = {
  expected_lifecycle_version: number
  expected_latest_revision_id: string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isSourceLifecycleSnapshot(value: unknown): value is SourceLifecycleSnapshot {
  return isRecord(value)
    && typeof value.source_id === 'string'
    && (value.state === 'active' || value.state === 'deleted')
    && Number.isSafeInteger(value.lifecycle_version)
    && (value.lifecycle_version as number) >= 0
    && isNullableString(value.latest_revision_id)
}

export function sourceLifecycleRequest(source: SourceLifecycleSnapshot): SourceLifecycleRequest {
  return {
    expected_lifecycle_version: source.lifecycle_version,
    expected_latest_revision_id: source.latest_revision_id,
  }
}

function sameSelection(left: SourceLifecycleSnapshot, right: SourceLifecycleSnapshot): boolean {
  return left.source_id === right.source_id
    && left.state === right.state
    && left.lifecycle_version === right.lifecycle_version
    && left.latest_revision_id === right.latest_revision_id
}

/**
 * Accept only the one-step transition for the exact source selection that
 * initiated the request. The current selection check prevents a delayed
 * response from an earlier delete/restore cycle from winning an ABA race.
 */
export function isSourceLifecycleResponse(
  value: unknown,
  requestedSource: SourceLifecycleSnapshot,
  targetState: SourceLifecycleState,
  currentSource: SourceLifecycleSnapshot = requestedSource,
): value is SourceLifecycleSnapshot {
  if (!sameSelection(requestedSource, currentSource) || !isSourceLifecycleSnapshot(value)) return false
  const nextVersion = requestedSource.lifecycle_version + 1
  return Number.isSafeInteger(nextVersion)
    && value.source_id === requestedSource.source_id
    && value.state === targetState
    && value.lifecycle_version === nextVersion
    && value.latest_revision_id === requestedSource.latest_revision_id
}
