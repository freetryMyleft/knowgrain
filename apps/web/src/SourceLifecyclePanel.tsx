import { useEffect, useRef, useState } from 'react'
import {
  isSourceLifecycleResponse,
  sourceLifecycleRequest,
  type SourceLifecycleSnapshot,
  type SourceLifecycleState,
} from './source-lifecycle-contract'
import './source-lifecycle.css'

export type SourceLifecyclePanelSource = SourceLifecycleSnapshot & { filename: string }

type SourceLifecyclePanelProps = {
  source: SourceLifecyclePanelSource
  disabled?: boolean
  onChanged: (source_id: string, state: SourceLifecycleState) => void
  onConflict: () => void
  onBusyChange?: (busy: boolean) => void
}

type ActiveRequest = {
  sequence: number
  selectionKey: string
  controller: AbortController
}

function selectionKey(source: SourceLifecycleSnapshot, disabled: boolean): string {
  return JSON.stringify([
    source.source_id,
    source.state,
    source.lifecycle_version,
    source.latest_revision_id,
    disabled,
  ])
}

function apiErrorMessage(status: number): string {
  if (status === 0) return '无法连接本机服务，请确认 Knowgrain 正在运行。'
  if (status === 404) return '来源已不存在，正在重新读取来源列表。'
  if (status === 409) return '来源或原件状态已变化，请刷新后重试。'
  if (status === 422) return '来源状态请求无法识别，请刷新后重试。'
  if (status === 503) return '本机服务暂时无法核对 Vault 原件，请稍后重试。'
  return '本机服务暂时无法处理此操作，请稍后重试。'
}

class SourceLifecycleApiError extends Error {
  status: number

  constructor(status: number) {
    super(apiErrorMessage(status))
    this.status = status
  }
}

class SourceLifecycleProtocolError extends Error {
  constructor() {
    super('本机服务返回了无法识别的来源状态，请刷新后重试。')
  }
}

async function changeSourceLifecycle(
  source: SourceLifecycleSnapshot,
  targetState: SourceLifecycleState,
  signal: AbortSignal,
): Promise<unknown> {
  const sourcePath = `/api/v1/sources/${encodeURIComponent(source.source_id)}`
  const restoring = targetState === 'active'
  let response: Response
  try {
    response = await fetch(restoring ? `${sourcePath}/restore` : sourcePath, {
      method: restoring ? 'POST' : 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sourceLifecycleRequest(source)),
      signal,
    })
  } catch (error) {
    if (signal.aborted) throw error
    throw new SourceLifecycleApiError(0)
  }

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    if (!response.ok) throw new SourceLifecycleApiError(response.status)
    throw new SourceLifecycleProtocolError()
  }
  if (!response.ok) throw new SourceLifecycleApiError(response.status)
  return payload
}

function readableError(error: unknown): string {
  if (error instanceof SourceLifecycleApiError || error instanceof SourceLifecycleProtocolError) return error.message
  return '来源状态暂时无法更新，请稍后重试。'
}

export default function SourceLifecyclePanel({
  source,
  disabled = false,
  onChanged,
  onConflict,
  onBusyChange,
}: SourceLifecyclePanelProps) {
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const sequenceRef = useRef(0)
  const activeRequestRef = useRef<ActiveRequest | null>(null)
  const selectionRef = useRef<SourceLifecyclePanelSource>(source)
  const selectionKeyRef = useRef('')
  const currentSelectionKey = selectionKey(source, disabled)
  selectionRef.current = source
  selectionKeyRef.current = currentSelectionKey

  useEffect(() => {
    setConfirmingDelete(false)
    setError(null)
    setNotice(null)
    if (disabled) {
      setBusy(false)
    }
    return () => {
      sequenceRef.current += 1
      activeRequestRef.current?.controller.abort()
      activeRequestRef.current = null
      setBusy(false)
      onBusyChange?.(false)
    }
  }, [currentSelectionKey, disabled, onBusyChange])

  const changeState = async (targetState: SourceLifecycleState) => {
    if (disabled || activeRequestRef.current) return
    const requestedSource: SourceLifecycleSnapshot = {
      source_id: source.source_id,
      state: source.state,
      lifecycle_version: source.lifecycle_version,
      latest_revision_id: source.latest_revision_id,
    }
    if ((targetState === 'deleted' && source.state !== 'active')
      || (targetState === 'active' && source.state !== 'deleted')) return

    const sequence = ++sequenceRef.current
    const requestKey = currentSelectionKey
    const controller = new AbortController()
    activeRequestRef.current = { sequence, selectionKey: requestKey, controller }
    setBusy(true)
    onBusyChange?.(true)
    setError(null)
    setNotice(null)

    const isCurrent = () => sequenceRef.current === sequence
      && activeRequestRef.current?.sequence === sequence
      && activeRequestRef.current.selectionKey === requestKey
      && selectionKeyRef.current === requestKey
      && !controller.signal.aborted

    try {
      const payload = await changeSourceLifecycle(requestedSource, targetState, controller.signal)
      if (!isCurrent()) return
      if (!isSourceLifecycleResponse(payload, requestedSource, targetState, selectionRef.current)) {
        onConflict()
        throw new SourceLifecycleProtocolError()
      }
      setConfirmingDelete(false)
      setNotice(targetState === 'deleted' ? '来源已标记为已删除。' : '来源已恢复。')
      onChanged(source.source_id, targetState)
    } catch (requestError) {
      if (!isCurrent()) return
      if (requestError instanceof SourceLifecycleApiError && (requestError.status === 404 || requestError.status === 409)) {
        onConflict()
      }
      setError(readableError(requestError))
    } finally {
      if (isCurrent()) {
        activeRequestRef.current = null
        setBusy(false)
        onBusyChange?.(false)
      }
    }
  }

  const unavailable = disabled || busy
  const deleted = source.state === 'deleted'

  return <section className="source-lifecycle-panel" aria-label="来源生命周期" aria-busy={busy}>
    <div className="source-lifecycle-heading">
      <div>
        <div className="source-lifecycle-kicker">来源管理</div>
        <h3>{deleted ? '已删除来源' : '来源状态'}</h3>
      </div>
      <span className={`source-lifecycle-state ${deleted ? 'deleted' : 'active'}`}>
        {deleted ? '已删除' : '参与新问答'}
      </span>
    </div>

    <p className="source-lifecycle-filename" title={source.filename}>{source.filename}</p>
    {deleted
      ? <p className="source-lifecycle-description">此来源已停止参与新问答。Vault 原件和 Wiki 页面仍保留，可核验原件后恢复。</p>
      : <p className="source-lifecycle-description">删除会停止此来源参与新问答，并保留原件和 Wiki 页面供恢复。</p>}

    {confirmingDelete && !deleted && <div className="source-lifecycle-confirm" role="group" aria-label="确认删除来源">
      <p>删除后将立即停止参与新问答；原件和 Wiki 保留，可恢复</p>
      <div className="source-lifecycle-actions">
        <button type="button" className="source-lifecycle-danger" disabled={unavailable} onClick={() => void changeState('deleted')}>
          {busy ? '正在删除…' : '确认删除来源'}
        </button>
        <button type="button" className="source-lifecycle-secondary" disabled={unavailable} onClick={() => setConfirmingDelete(false)}>
          取消
        </button>
      </div>
    </div>}

    {!confirmingDelete && <div className="source-lifecycle-actions">
      {deleted
        ? <button type="button" className="source-lifecycle-restore" disabled={unavailable} onClick={() => void changeState('active')}>
          {busy ? '正在核对并恢复…' : '核对原件并恢复来源'}
        </button>
        : <button type="button" className="source-lifecycle-delete" disabled={unavailable} onClick={() => { setError(null); setNotice(null); setConfirmingDelete(true) }}>
          删除来源
        </button>}
    </div>}

    {busy && <p className="source-lifecycle-progress" role="status">正在与本机服务核对来源版本…</p>}
    {error && <p className="source-lifecycle-error" role="alert">{error}</p>}
    {notice && <p className="source-lifecycle-notice" role="status">{notice}</p>}
  </section>
}
