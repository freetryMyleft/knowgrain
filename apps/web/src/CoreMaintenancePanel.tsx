import { useCallback, useEffect, useRef, useState } from 'react'
import { canRetryMaintenance, isMaintenanceJob, isMaintenanceList, type MaintenanceJob } from './maintenance-contract'
import './source-lifecycle.css'

type Props = {
  sourceId: string
  state: 'active' | 'deleted'
  lifecycleVersion: number
  disabled?: boolean
  onSourceChanged: () => void
  onBusyChange: (busy: boolean) => void
}

const LABELS = { queued: '等待清理', running: '正在清理', succeeded: '清理完成', failed: '清理失败', cancelled: '已取消' }
class MaintenanceClientError extends Error {}

export default function CoreMaintenancePanel({ sourceId, state, lifecycleVersion, disabled = false, onSourceChanged, onBusyChange }: Props) {
  const [jobs, setJobs] = useState<MaintenanceJob[]>([])
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
      const response = await fetch(`/api/v1/sources/${encodeURIComponent(sourceId)}/maintenance?limit=100`, { signal: controller.signal })
      if (!response.ok) throw new MaintenanceClientError('清理状态暂不可读取，请检查本机服务后重试。')
      const value: unknown = await response.json()
      if (!isMaintenanceList(value, sourceId)) throw new MaintenanceClientError('清理状态格式无法识别，请刷新后重试。')
      if (!current()) return
      setJobs(value)
      setError(null)
      const fingerprint = JSON.stringify(value.map((job) => [job.job_id, job.state, job.attempts]))
      if (fingerprint !== fingerprintRef.current) {
        fingerprintRef.current = fingerprint
        callbackRef.current()
      }
    } catch (failure) {
      if (current()) setError(failure instanceof MaintenanceClientError ? failure.message : '清理状态读取失败，请检查本机服务后重试。')
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
    if (!pending) return
    const timer = window.setInterval(() => void refresh(), 2200)
    return () => window.clearInterval(timer)
  }, [pending, refresh])

  const retry = async (job: MaintenanceJob) => {
    if (disabled || writerRef.current || !canRetryMaintenance(job, state, lifecycleVersion)) return
    readerRef.current?.abort()
    readerRef.current = null
    sequenceRef.current += 1
    const controller = new AbortController()
    writerRef.current = controller
    busyCallbackRef.current(true)
    setRetrying(job.job_id)
    setError(null)
    setOperationError(null)
    const current = () => !controller.signal.aborted && scopeRef.current === scope && writerRef.current === controller
    try {
      const response = await fetch(`/api/v1/maintenance/${encodeURIComponent(job.job_id)}/retry`, { method: 'POST', signal: controller.signal })
      if (!response.ok) throw new MaintenanceClientError(response.status === 409 ? '清理任务状态已变化，请刷新后重试。' : '清理重试暂不可用，请检查本机服务。')
      const value: unknown = await response.json()
      if (!isMaintenanceJob(value, sourceId) || value.job_id !== job.job_id || value.lifecycle_version !== lifecycleVersion || value.state !== 'queued') {
        throw new MaintenanceClientError('清理重试返回了不一致的状态，请刷新后重试。')
      }
      if (!current()) return
      setJobs((rows) => rows.map((row) => row.job_id === job.job_id ? value : row))
      callbackRef.current()
    } catch (failure) {
      if (current()) setOperationError(failure instanceof MaintenanceClientError ? failure.message : '清理重试失败，请刷新状态后重试。')
    } finally {
      if (current()) {
        writerRef.current = null
        busyCallbackRef.current(false)
        setRetrying(null)
        void refresh()
      }
    }
  }

  return <section className="source-lifecycle-panel core-maintenance-panel" aria-label="LightRAG 清理任务">
    <h3>索引维护</h3>
    <p className="source-lifecycle-description">清理派生知识索引，Vault 原件及 Wiki 保留供恢复。索引已清理时，恢复来源会重新排队建立关系。</p>
    {loading && jobs.length === 0 && <p role="status">正在读取清理任务…</p>}
    {!loading && jobs.length === 0 && !error && <p>此来源暂无清理任务。</p>}
    {jobs.length > 0 && <ul className="core-maintenance-jobs">{jobs.map((job) => <li key={job.job_id}>
      <div><strong>{LABELS[job.state]}</strong><small>修订 {job.revision_id.slice(0, 8)} · 删除周期 {job.lifecycle_version} · 已尝试 {job.attempts} 次</small></div>
      {job.error && <p className="source-lifecycle-error">{job.error}</p>}
      {canRetryMaintenance(job, state, lifecycleVersion) && <button className="source-lifecycle-secondary" type="button" disabled={disabled || Boolean(retrying)} onClick={() => void retry(job)}>{retrying === job.job_id ? '正在排队…' : '重试清理'}</button>}
    </li>)}</ul>}
    {jobs.length === 100 && <p>显示最近 100 项任务。</p>}
    {error && <p className="source-lifecycle-error" role="alert">{error}</p>}
    {operationError && <p className="source-lifecycle-error" role="alert">{operationError}</p>}
    <button className="source-lifecycle-secondary" type="button" disabled={disabled || loading || Boolean(retrying)} onClick={() => void refresh()}>刷新清理状态</button>
  </section>
}
