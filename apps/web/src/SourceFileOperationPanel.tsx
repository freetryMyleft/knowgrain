import { useCallback, useEffect, useRef, useState } from 'react'
import { canRetryFileOperation, isFileOperation, isFileOperationList, type SourceFileOperation } from './file-operation-contract'
import './source-lifecycle.css'

type Props = {
  sourceId: string
  state: 'active' | 'deleted'
  lifecycleVersion: number
  disabled?: boolean
  onSourceChanged: () => void
  onBusyChange: (busy: boolean) => void
}

const LABELS = { queued: '等待处理', running: '正在处理', succeeded: '已完成', failed: '失败', cancelled: '已取消' }
class FileOperationClientError extends Error {}

export default function SourceFileOperationPanel({ sourceId, state, lifecycleVersion, disabled = false, onSourceChanged, onBusyChange }: Props) {
  const [jobs, setJobs] = useState<SourceFileOperation[]>([])
  const [error, setError] = useState<string | null>(null)
  const [operationError, setOperationError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [retrying, setRetrying] = useState<string | null>(null)
  const scope = `${sourceId}:${lifecycleVersion}`
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const callbackRef = useRef(onSourceChanged)
  callbackRef.current = onSourceChanged
  const busyCallbackRef = useRef(onBusyChange)
  busyCallbackRef.current = onBusyChange
  const readerRef = useRef<AbortController | null>(null)
  const writerRef = useRef<AbortController | null>(null)
  const fingerprintRef = useRef('')
  const sequenceRef = useRef(0)

  const refresh = useCallback(async () => {
    if (readerRef.current || writerRef.current) return
    const controller = new AbortController()
    readerRef.current = controller
    const sequence = ++sequenceRef.current
    const current = () => !controller.signal.aborted && scopeRef.current === scope && sequenceRef.current === sequence
    setLoading(true)
    try {
      const response = await fetch(`/api/v1/sources/${encodeURIComponent(sourceId)}/file-operations?limit=100`, { signal: controller.signal })
      if (!response.ok) throw new FileOperationClientError('文件操作状态暂不可读取，请检查本机服务后重试。')
      const value: unknown = await response.json()
      if (!isFileOperationList(value, sourceId)) throw new FileOperationClientError('文件操作状态格式无法识别，请刷新后重试。')
      if (!current()) return
      setJobs(value)
      setError(null)
      const fingerprint = JSON.stringify(value.map((job) => [job.operation_id, job.state, job.attempts]))
      if (fingerprint !== fingerprintRef.current) {
        fingerprintRef.current = fingerprint
        callbackRef.current()
      }
    } catch (failure) {
      if (current()) setError(failure instanceof FileOperationClientError ? failure.message : '文件操作状态读取失败，请检查本机服务后重试。')
    } finally {
      if (readerRef.current === controller) readerRef.current = null
      if (current()) setLoading(false)
    }
  }, [scope, sourceId])

  useEffect(() => {
    sequenceRef.current += 1
    fingerprintRef.current = ''
    setJobs([])
    setError(null)
    setOperationError(null)
    setRetrying(null)
    void refresh()
    return () => {
      sequenceRef.current += 1
      readerRef.current?.abort()
      if (writerRef.current) {
        writerRef.current.abort()
        busyCallbackRef.current(false)
      }
      readerRef.current = null
      writerRef.current = null
    }
  }, [refresh])

  const pending = jobs.some((job) => job.state === 'queued' || job.state === 'running')
  useEffect(() => {
    if (!pending && state !== 'deleted') return
    const timer = window.setInterval(() => void refresh(), 2200)
    return () => window.clearInterval(timer)
  }, [pending, refresh, state])

  const retry = async (job: SourceFileOperation) => {
    if (disabled || writerRef.current || !canRetryFileOperation(job, state, lifecycleVersion)) return
    readerRef.current?.abort()
    readerRef.current = null
    sequenceRef.current += 1
    const controller = new AbortController()
    writerRef.current = controller
    busyCallbackRef.current(true)
    setRetrying(job.operation_id)
    setError(null)
    setOperationError(null)
    const current = () => !controller.signal.aborted && scopeRef.current === scope && writerRef.current === controller
    try {
      const response = await fetch(`/api/v1/file-operations/${encodeURIComponent(job.operation_id)}/retry`, { method: 'POST', signal: controller.signal })
      if (!response.ok) throw new FileOperationClientError(response.status === 409 ? '文件任务状态已变化，请刷新后重试。' : '文件操作重试暂不可用，请检查本机服务。')
      const value: unknown = await response.json()
      if (!isFileOperation(value, sourceId) || value.operation_id !== job.operation_id || value.lifecycle_version !== lifecycleVersion || value.kind !== job.kind || value.state !== 'queued') {
        throw new FileOperationClientError('文件操作重试返回了不一致的状态，请刷新后重试。')
      }
      if (!current()) return
      setJobs((rows) => rows.map((row) => row.operation_id === job.operation_id ? value : row))
      callbackRef.current()
    } catch (failure) {
      if (current()) setOperationError(failure instanceof FileOperationClientError ? failure.message : '文件操作重试失败，请刷新状态后重试。')
    } finally {
      if (current()) {
        writerRef.current = null
        busyCallbackRef.current(false)
        setRetrying(null)
        void refresh()
      }
    }
  }

  return <section className="source-lifecycle-panel core-maintenance-panel" aria-label="原件文件任务">
    <h3>原件归档与恢复</h3>
    <p className="source-lifecycle-description">索引清理完成后，全部修订原件移入 Vault 的 Trash/Files。恢复时先核验并搬回原件，再重建索引；历史证据可继续访问，Wiki 保留。</p>
    {loading && jobs.length === 0 && <p role="status">正在读取文件任务…</p>}
    {!loading && jobs.length === 0 && !error && <p>此来源暂无文件任务。</p>}
    {jobs.length > 0 && <ul className="core-maintenance-jobs">{jobs.map((job) => <li key={job.operation_id}>
      <div><strong>{job.kind === 'archive' ? '归档' : '恢复'} · {LABELS[job.state]}</strong><small>全部原件修订 · 删除周期 {job.lifecycle_version} · 已尝试 {job.attempts} 次</small></div>
      {job.error && <p className="source-lifecycle-error">{job.error}</p>}
      {canRetryFileOperation(job, state, lifecycleVersion) && <button className="source-lifecycle-secondary" type="button" disabled={disabled || Boolean(retrying)} onClick={() => void retry(job)}>{retrying === job.operation_id ? '正在排队…' : '重试文件操作'}</button>}
    </li>)}</ul>}
    {jobs.length === 100 && <p>显示最近 100 项任务。</p>}
    {error && <p className="source-lifecycle-error" role="alert">{error}</p>}
    {operationError && <p className="source-lifecycle-error" role="alert">{operationError}</p>}
    <button className="source-lifecycle-secondary" type="button" disabled={disabled || loading || Boolean(retrying)} onClick={() => void refresh()}>刷新文件操作状态</button>
  </section>
}
