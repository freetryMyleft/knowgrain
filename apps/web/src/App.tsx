import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import VaultSettings from './VaultSettings'
const WikiWorkspace = lazy(() => import('./WikiWorkspace'))
const QuestionsWorkspace = lazy(() => import('./QuestionsWorkspace'))

const API_ROOT = '/api/v1'
const PAGE_SIZE = 100
const ACCEPTED_FILES = '.md,.markdown,.txt,.pdf,.docx'

type RevisionSnapshot = {
  revision_id?: string
  id?: string
  source_id?: string
  filename: string
  sha256: string | null
  vault_path: string | null
  media_type: string | null
  state?: string | null
  index_state?: string | null
  parsed_text_sha256?: string | null
  error: string | null
  created_at: string | null
  indexed_at: string | null
}

type SourceSnapshot = {
  id?: string
  source_id: string
  filename: string
  state: string
  latest_revision_id: string | null
  current_revision_id: string | null
  revision_status: string | null
  sha256: string | null
  vault_path: string | null
  error: string | null
  latest_revision: RevisionSnapshot | null
  current_revision: RevisionSnapshot | null
  created_at: string
}

type JobSnapshot = {
  job_id: string
  revision_id: string
  kind: string
  state: string
  attempts: number
  error: string | null
  created_at: string
  updated_at: string
  lease_until: string | null
}

type ReadySnapshot = {
  status: string
  lightrag: string
  postgres: string
  ollama: string
  app_database: string
  vault: string
  detail: string | null
}

type ImportResponse = {
  source_id: string
  revision_id: string
  job_id: string
  duplicate: boolean
  vault_path: string
}

class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function readableError(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value
  if (Array.isArray(value)) {
    const messages = value
      .map((item) => {
        if (typeof item === 'string') return item
        if (typeof item === 'object' && item !== null && 'msg' in item) {
          const message = (item as { msg?: unknown }).msg
          return typeof message === 'string' ? message : ''
        }
        return ''
      })
      .filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  if (typeof value === 'object' && value !== null && 'detail' in value) {
    return readableError((value as { detail?: unknown }).detail)
  }
  return '服务暂时无法处理请求，请稍后重试。'
}

async function requestJson<T>(path: string, init: RequestInit = {}, allowedStatuses: number[] = []): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_ROOT}${path}`, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, '无法连接本机服务。请确认 Knowgrain 正在运行。')
  }

  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  if (!response.ok && !allowedStatuses.includes(response.status)) {
    throw new ApiError(response.status, readableError(body))
  }
  return body as T
}

function Icon({ name, size = 18 }: { name: 'grain' | 'file' | 'refresh' | 'upload' | 'plus' | 'clock' | 'check' | 'alert' | 'retry' | 'database' | 'server' | 'chevron' | 'search'; size?: number }) {
  const paths: Record<typeof name, React.ReactNode> = {
    grain: <><path d="M12 21V9" /><path d="M12 16c-4.1 0-6.8-2.4-6.8-6 4.1 0 6.8 2.4 6.8 6Z" /><path d="M12 12c4 0 6.8-2.4 6.8-6-4 0-6.8 2.4-6.8 6Z" /><path d="M12 7c-2.5 0-4-1.6-4-4 2.5 0 4 1.6 4 4Z" /><path d="M12 7c2.5 0 4-1.6 4-4-2.5 0-4 1.6-4 4Z" /></>,
    file: <><path d="M6 3.5h7l5 5V20a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 5 20V5a1.5 1.5 0 0 1 1-1.5Z" /><path d="M13 4v5h5M8 13h8M8 16.5h8" /></>,
    refresh: <><path d="M20 7v5h-5" /><path d="M4.9 9a7.5 7.5 0 0 1 12.5-2L20 12M4 17v-5h5" /><path d="M19.1 15a7.5 7.5 0 0 1-12.5 2L4 12" /></>,
    upload: <><path d="M12 16V4" /><path d="m7 9 5-5 5 5" /><path d="M4 16.5v3A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.5-1.5v-3" /></>,
    plus: <><path d="M12 5v14M5 12h14" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    check: <><path d="m5 12.5 4.2 4.2L19.5 6.5" /></>,
    alert: <><path d="M12 3 2.8 19a1.3 1.3 0 0 0 1.1 2h16.2a1.3 1.3 0 0 0 1.1-2L12 3Z" /><path d="M12 9v4.5M12 17h.01" /></>,
    retry: <><path d="M20 11a8 8 0 0 0-14-5L4 8" /><path d="M4 4v4h4" /><path d="M4 13a8 8 0 0 0 14 5l2-2" /><path d="M20 20v-4h-4" /></>,
    database: <><ellipse cx="12" cy="5" rx="8.5" ry="3" /><path d="M3.5 5v7c0 1.7 3.8 3 8.5 3 .7 0 1.4 0 2-.1M20.5 5v5" /><path d="M3.5 12v7c0 1.7 3.8 3 8.5 3 .7 0 1.4 0 2-.1M20.5 13v7M16 13h7M19.5 9.5v7" /></>,
    server: <><rect x="3" y="4" width="18" height="7" rx="2" /><rect x="3" y="13" width="18" height="7" rx="2" /><path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6" /></>,
    chevron: <><path d="m9 5 7 7-7 7" /></>,
    search: <><circle cx="10.8" cy="10.8" r="6.7" /><path d="m16 16 4.4 4.4" /></>,
  }
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>
}

function statusLabel(value: string | null | undefined): { label: string; tone: string } {
  switch (value) {
    case 'ready':
    case 'succeeded':
      return { label: value === 'ready' ? '已索引' : '已完成', tone: 'positive' }
    case 'queued':
      return { label: '等待处理', tone: 'waiting' }
    case 'indexing':
    case 'running':
      return { label: '正在索引', tone: 'working' }
    case 'failed':
      return { label: '处理失败', tone: 'negative' }
    case 'unavailable':
      return { label: '不可用', tone: 'negative' }
    case 'restart_required':
      return { label: '需重启', tone: 'waiting' }
    case 'not_ready':
      return { label: '未就绪', tone: 'waiting' }
    default:
      return { label: '尚无状态', tone: 'neutral' }
  }
}

function isPendingIndexState(value: string | null | undefined): boolean {
  return value === 'queued' || value === 'indexing' || value === 'running'
}

function isActiveJobState(value: string | null | undefined): boolean {
  return value === 'queued' || value === 'running'
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(date)
}

function shortId(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)} · ${value.slice(-4)}` : '尚未创建'
}

function fileType(filename: string): string {
  const extension = filename.split('.').pop()?.toLowerCase()
  if (extension === 'pdf') return 'PDF'
  if (extension === 'docx') return 'DOCX'
  if (extension === 'txt') return 'TXT'
  return 'MD'
}

function Badge({ value }: { value: string | null | undefined }) {
  const status = statusLabel(value)
  return <span className={`status-badge ${status.tone}`}><span className="badge-dot" />{status.label}</span>
}

function RevisionCard({ title, revision, missingText, current }: { title: string; revision: RevisionSnapshot | null; missingText: string; current?: boolean }) {
  if (!revision) {
    return <section className="revision-card revision-empty" aria-label={title}>
      <div className="revision-card-head"><span>{title}</span><span className="revision-tag">未建立</span></div>
      <p>{missingText}</p>
    </section>
  }
  const revisionId = revision.revision_id ?? revision.id
  const revisionState = revision.index_state ?? revision.state
  return <section className={`revision-card ${current ? 'is-current' : ''}`} aria-label={title}>
    <div className="revision-card-head"><span>{title}</span><Badge value={revisionState} /></div>
    <dl className="revision-facts">
      <div><dt>修订 ID</dt><dd className="mono">{shortId(revisionId)}</dd></div>
      <div><dt>上传时间</dt><dd>{formatDate(revision.created_at)}</dd></div>
      {revision.indexed_at && <div><dt>索引完成</dt><dd>{formatDate(revision.indexed_at)}</dd></div>}
      <div><dt>SHA-256</dt><dd className="mono hash-value">{revision.sha256 || '暂不可用'}</dd></div>
      <div><dt>Vault 路径</dt><dd className="mono path-value">{revision.vault_path || '暂不可用'}</dd></div>
    </dl>
    {revision.error && <p className="inline-error"><Icon name="alert" size={15} />{revision.error}</p>}
  </section>
}

function DependencyRow({ label, value }: { label: string; value: string }) {
  const state = statusLabel(value)
  return <div className="dependency-row">
    <span className={`dependency-mark ${state.tone}`}><span /></span>
    <span className="dependency-name">{label}</span>
    <span className={`dependency-state ${state.tone}`}>{value === 'ready' ? '就绪' : state.label}</span>
  </div>
}

export default function App() {
  const [workspace, setWorkspace] = useState<'sources' | 'wiki' | 'questions'>('sources')
  const [sources, setSources] = useState<SourceSnapshot[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selected, setSelected] = useState<SourceSnapshot | null>(null)
  const [health, setHealth] = useState<ReadySnapshot | null>(null)
  const [listLoading, setListLoading] = useState(true)
  const [listError, setListError] = useState<string | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const [filter, setFilter] = useState('')
  const [uploading, setUploading] = useState(false)
  const [retrying, setRetrying] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [activeJob, setActiveJob] = useState<JobSnapshot | null>(null)
  const [notice, setNotice] = useState<{ tone: 'positive' | 'negative'; text: string } | null>(null)
  const [lastUpdated, setLastUpdated] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const listRequestRef = useRef(0)
  const detailRequestRef = useRef(0)
  const mountedRef = useRef(true)
  const selectedIdRef = useRef<string | null>(null)
  const uploadTargetRef = useRef<string | null>(null)
  const healthRequestRef = useRef(0)
  const noticeTimerRef = useRef<number | undefined>(undefined)
  const activeJobRef = useRef<JobSnapshot | null>(null)
  const detailLifecycleControllerRef = useRef<AbortController | null>(null)
  const previousPollingStateRef = useRef<{ pending: boolean; selectedId: string | null }>({ pending: false, selectedId: null })

  const refreshList = useCallback(async (signal?: AbortSignal, showLoading = true) => {
    const requestId = ++listRequestRef.current
    if (showLoading) setListLoading(true)
    try {
      const result = await requestJson<SourceSnapshot[]>(`/sources?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`, { signal })
      if (!mountedRef.current || signal?.aborted || requestId !== listRequestRef.current) return
      setSources(Array.isArray(result) ? result : [])
      setListError(null)
      setLastUpdated(new Date().toISOString())
    } catch (error) {
      if (!mountedRef.current || signal?.aborted || requestId !== listRequestRef.current) return
      setListError(readableError(error instanceof ApiError ? error.message : error))
    } finally {
      if (mountedRef.current && !signal?.aborted && requestId === listRequestRef.current) setListLoading(false)
    }
  }, [page])

  const refreshHealth = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++healthRequestRef.current
    try {
      const result = await requestJson<ReadySnapshot>('/health/ready', { signal }, [503])
      if (!mountedRef.current || signal?.aborted || requestId !== healthRequestRef.current) return
      setHealth(result)
      setHealthError(null)
    } catch (error) {
      if (!mountedRef.current || signal?.aborted || requestId !== healthRequestRef.current) return
      setHealth(null)
      setHealthError(readableError(error instanceof ApiError ? error.message : error))
    }
  }, [])

  const refreshDetail = useCallback(async (sourceId: string, signal?: AbortSignal, showLoading = false) => {
    const requestId = ++detailRequestRef.current
    if (showLoading) setDetailLoading(true)
    try {
      const result = await requestJson<SourceSnapshot>(`/sources/${encodeURIComponent(sourceId)}`, { signal })
      if (!mountedRef.current || signal?.aborted || requestId !== detailRequestRef.current || sourceId !== selectedIdRef.current) return
      setSelected(result)
      setDetailError(null)
    } catch (error) {
      if (!mountedRef.current || signal?.aborted || requestId !== detailRequestRef.current || sourceId !== selectedIdRef.current) return
      setDetailError(readableError(error instanceof ApiError ? error.message : error))
    } finally {
      if (mountedRef.current && !signal?.aborted && requestId === detailRequestRef.current && showLoading) setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      listRequestRef.current += 1
      detailRequestRef.current += 1
      healthRequestRef.current += 1
      window.clearTimeout(noticeTimerRef.current)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void refreshList(controller.signal)
    void refreshHealth(controller.signal)
    return () => controller.abort()
  }, [refreshHealth, refreshList])

  useEffect(() => {
    if (!selectedId) {
      detailLifecycleControllerRef.current = null
      setSelected(null)
      setDetailError(null)
      return
    }
    const controller = new AbortController()
    detailLifecycleControllerRef.current = controller
    void refreshDetail(selectedId, controller.signal, true)
    return () => {
      controller.abort()
      if (detailLifecycleControllerRef.current === controller) detailLifecycleControllerRef.current = null
      detailRequestRef.current += 1
    }
  }, [selectedId, refreshDetail])

  const hasPendingSourceRevision = sources.some((source) => isPendingIndexState(
    source.latest_revision?.index_state ?? source.latest_revision?.state ?? source.revision_status,
  ))
  const selectedRevisionState = selected?.latest_revision?.index_state ?? selected?.latest_revision?.state ?? selected?.revision_status
  const selectedRevisionIsPending = isPendingIndexState(selectedRevisionState)
  const activeJobIsPending = isActiveJobState(activeJob?.state)
  const pollingNeeded = hasPendingSourceRevision || selectedRevisionIsPending || activeJobIsPending

  useEffect(() => {
    if (!pollingNeeded) return
    const controller = new AbortController()
    const timer = window.setInterval(() => {
      void refreshList(controller.signal, false)
      const detailController = detailLifecycleControllerRef.current
      if (selectedId && detailController && !detailController.signal.aborted) {
        void refreshDetail(selectedId, detailController.signal)
      }
      const job = activeJobRef.current
      if (job && isActiveJobState(job.state)) {
        void requestJson<JobSnapshot>(`/jobs/${encodeURIComponent(job.job_id)}`, { signal: controller.signal })
          .then((updatedJob) => {
            if (mountedRef.current && !controller.signal.aborted && activeJobRef.current?.job_id === job.job_id) {
              activeJobRef.current = updatedJob
              setActiveJob(updatedJob)
            }
          })
          .catch(() => { /* Source snapshots remain the recovery path after a reload. */ })
      }
    }, 2200)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [pollingNeeded, refreshDetail, refreshList, selectedId])

  useEffect(() => {
    const previous = previousPollingStateRef.current
    previousPollingStateRef.current = { pending: pollingNeeded, selectedId }
    if (!previous.pending || pollingNeeded || !selectedId || previous.selectedId !== selectedId) return

    const detailController = detailLifecycleControllerRef.current
    if (detailController && !detailController.signal.aborted) {
      void refreshDetail(selectedId, detailController.signal)
    }
  }, [pollingNeeded, refreshDetail, selectedId])

  useEffect(() => {
    const controller = new AbortController()
    const timer = window.setInterval(() => void refreshHealth(controller.signal), 15000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [refreshHealth])

  useEffect(() => {
    return () => window.clearTimeout(noticeTimerRef.current)
  }, [])

  const visibleSources = useMemo(() => {
    const normalized = filter.trim().toLocaleLowerCase()
    if (!normalized) return sources
    return sources.filter((source) => `${source.filename} ${source.vault_path ?? ''}`.toLocaleLowerCase().includes(normalized))
  }, [filter, sources])

  const canUpload = health?.app_database === 'ready' && health.vault === 'ready'
  const latestStatus = selected?.latest_revision?.index_state ?? selected?.latest_revision?.state ?? selected?.revision_status
  const latestRevisionId = selected?.latest_revision?.revision_id ?? selected?.latest_revision?.id ?? selected?.latest_revision_id
  const canRetry = Boolean(selected && latestStatus === 'failed' && latestRevisionId)
  const activeDependencies = health ? [health.app_database, health.vault].every((state) => state === 'ready') : false

  const selectSource = (sourceId: string) => {
    selectedIdRef.current = sourceId
    setSelectedId(sourceId)
    setSelected(null)
    setDetailError(null)
    activeJobRef.current = null
    setActiveJob(null)
  }

  const showNotice = (tone: 'positive' | 'negative', text: string) => {
    setNotice({ tone, text })
    window.clearTimeout(noticeTimerRef.current)
    noticeTimerRef.current = window.setTimeout(() => setNotice(null), 5200)
  }

  const chooseFile = (sourceId: string | null = null) => {
    uploadTargetRef.current = sourceId
    fileInputRef.current?.click()
  }

  const uploadFile = async (file: File) => {
    if (!canUpload) {
      showNotice('negative', '应用数据库或 Vault 尚未就绪，暂时无法导入。')
      return
    }
    setUploading(true)
    setNotice(null)
    const form = new FormData()
    form.append('file', file)
    const sourceId = uploadTargetRef.current
    try {
      const response = await requestJson<ImportResponse>(
        sourceId ? `/sources/${encodeURIComponent(sourceId)}/revisions` : '/sources',
        { method: 'POST', body: form },
      )
      if (!mountedRef.current) return
      selectedIdRef.current = response.source_id
      setSelectedId(response.source_id)
      activeJobRef.current = null
      setActiveJob(null)
      await refreshList(undefined, false)
      await refreshDetail(response.source_id, undefined, true)
      const job = await requestJson<JobSnapshot>(`/jobs/${encodeURIComponent(response.job_id)}`)
      if (!mountedRef.current) return
      activeJobRef.current = job
      setActiveJob(job)
      const duplicateText = response.duplicate ? '内容相同，已复用现有修订。' : '原件已保存，索引任务已排队。'
      showNotice('positive', `${duplicateText} 当前状态以修订记录为准。`)
    } catch (error) {
      if (!mountedRef.current) return
      showNotice('negative', readableError(error instanceof ApiError ? error.message : error))
    } finally {
      if (mountedRef.current) setUploading(false)
    }
  }

  const onFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0]
    event.currentTarget.value = ''
    if (file) void uploadFile(file)
  }

  const retryIndexing = async () => {
    if (!selected || !canRetry) return
    setRetrying(true)
    try {
      const result = await requestJson<{ job_id: string }>(`/sources/${encodeURIComponent(selected.source_id)}/reindex`, { method: 'POST' })
      if (!mountedRef.current) return
      const job = await requestJson<JobSnapshot>(`/jobs/${encodeURIComponent(result.job_id)}`)
      if (!mountedRef.current) return
      activeJobRef.current = job
      setActiveJob(job)
      await Promise.all([refreshDetail(selected.source_id, undefined, false), refreshList(undefined, false)])
      showNotice('positive', '已重新排入索引队列；完成前仍会显示旧的当前修订。')
    } catch (error) {
      showNotice('negative', readableError(error instanceof ApiError ? error.message : error))
    } finally {
      if (mountedRef.current) setRetrying(false)
    }
  }

  const reconnect = async () => {
    setReconnecting(true)
    try {
      const result = await requestJson<ReadySnapshot>('/system/retry-initialize', { method: 'POST' }, [503])
      if (!mountedRef.current) return
      setHealth(result)
      setHealthError(null)
      const needsRestart = result.lightrag === 'restart_required'
      showNotice(result.status === 'ready' ? 'positive' : 'negative', needsRestart
        ? '服务需要重启 Knowgrain 进程后才能重新初始化。'
        : result.status === 'ready' ? '本机服务已重新连接。' : result.detail || '部分服务仍未就绪，请检查下方状态。')
    } catch (error) {
      if (!mountedRef.current) return
      setHealthError(readableError(error instanceof ApiError ? error.message : error))
      showNotice('negative', readableError(error instanceof ApiError ? error.message : error))
    } finally {
      if (mountedRef.current) setReconnecting(false)
    }
  }

  const refreshAll = async () => {
    await Promise.all([refreshList(undefined, false), refreshHealth()])
    if (selectedId) await refreshDetail(selectedId)
  }

  const nextPage = () => {
    if (sources.length === PAGE_SIZE) setPage((value) => value + 1)
  }

  const previousPage = () => setPage((value) => Math.max(0, value - 1))

  const serviceStatus = healthError ? 'unavailable' : health?.status || 'unavailable'
  const latest = selected?.latest_revision ?? null
  const current = selected?.current_revision ?? null

  if (workspace === 'wiki') {
    return <Suspense fallback={<main className="desktop-shell"><section className="app-window"><p role="status">正在打开 Wiki…</p></section></main>}>
      <WikiWorkspace onReturnToSources={() => setWorkspace('sources')} />
    </Suspense>
  }
  if (workspace === 'questions') {
    return <Suspense fallback={<main className="desktop-shell"><section className="app-window"><p role="status">正在打开问答…</p></section></main>}>
      <QuestionsWorkspace onReturnToSources={() => setWorkspace('sources')} />
    </Suspense>
  }

  return <main className="desktop-shell">
    <div className="app-window">
      <header className="titlebar">
        <div className="brand-mark"><Icon name="grain" size={18} /></div>
        <span className="brand-name">Knowgrain</span>
        <span className="titlebar-separator" aria-hidden="true">/</span>
        <span className="titlebar-page">资料</span>
        <span className="titlebar-spacer" />
        <span className="local-indicator"><span />本机 Vault</span>
        <VaultSettings onVaultSelected={refreshAll} />
      </header>

      <div className="workspace-grid">
        <aside className="sidebar" aria-label="工作区">
          <div className="side-label">工作区</div>
          <div className="side-current" aria-current="page">
            <Icon name="file" size={17} /><span>资料</span><span className="side-count">{sources.length}</span>
          </div>
          <button className="side-nav-link" type="button" onClick={() => setWorkspace('wiki')}>
            <Icon name="file" size={17} /><span>Wiki</span>
          </button>
          <button className="side-nav-link" type="button" onClick={() => setWorkspace('questions')}>
            <span aria-hidden="true">◇</span><span>问答</span>
          </button>
          <div className="sidebar-rule" />

          <section className="service-card" aria-labelledby="service-heading">
            <div className="service-card-title">
              <div className="service-icon"><Icon name="server" size={16} /></div>
              <div><h2 id="service-heading">服务状态</h2><span>本机连接</span></div>
              <span className={`overall-state ${statusLabel(serviceStatus).tone}`} title={statusLabel(serviceStatus).label} />
            </div>
            <div className="dependency-list">
              {health ? <>
                <DependencyRow label="应用数据库" value={health.app_database} />
                <DependencyRow label="Vault 文件夹" value={health.vault} />
                <DependencyRow label="PostgreSQL" value={health.postgres} />
                <DependencyRow label="Ollama 模型" value={health.ollama} />
                <DependencyRow label="LightRAG" value={health.lightrag} />
              </> : healthError ? <p className="service-error">{healthError}</p> : <div className="skeleton-stack" aria-label="正在检查服务状态"><i /><i /><i /></div>}
            </div>
            {health?.detail && <p className="service-detail">{health.detail}</p>}
            <button className="reconnect-button" type="button" onClick={() => void reconnect()} disabled={reconnecting}>
              <Icon name="refresh" size={14} />{reconnecting ? '正在重连…' : '重连服务'}
            </button>
            <p className="service-hint">索引模型未就绪时，资料仍可保存；索引任务会等待模型恢复。</p>
          </section>

          <div className="sidebar-bottom">
            <div className={`vault-health ${activeDependencies ? 'is-ready' : health ? 'is-waiting' : 'is-unknown'}`}>
              <span className="vault-health-dot" />
              <span>{activeDependencies ? '资料可导入' : health ? '导入暂不可用' : '正在检查连接'}</span>
            </div>
            <span className="sidebar-caption">资料原件保存在所配置的 Vault 中</span>
          </div>
        </aside>

        <section className="source-column" aria-label="资料列表">
          <div className="column-toolbar">
            <div className="column-heading">
              <div className="column-kicker">来源库</div>
              <h1>资料</h1>
            </div>
            <button className="icon-button" type="button" onClick={() => void refreshAll()} aria-label="刷新资料和服务状态" title="刷新">
              <Icon name="refresh" size={16} />
            </button>
            <button className="primary-button compact" type="button" onClick={() => chooseFile()} disabled={!canUpload || uploading}>
              <Icon name="upload" size={15} />{uploading ? '正在导入…' : '导入资料'}
            </button>
          </div>
          <div className="list-tools">
            <label className="search-box">
              <Icon name="search" size={15} />
              <span className="visually-hidden">筛选已加载资料</span>
              <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="筛选已加载资料" />
              {filter && <button type="button" className="clear-search" onClick={() => setFilter('')} aria-label="清除筛选">×</button>}
            </label>
            <span className="result-count">{visibleSources.length}{filter ? ' 条匹配' : ' 条资料'}</span>
          </div>
          <div className="source-list" aria-live="polite" aria-busy={listLoading}>
            {listLoading && sources.length === 0 ? <div className="list-loading"><span className="spinner" />正在读取资料…</div> : null}
            {!listLoading && listError && sources.length === 0 ? <div className="list-state error-state">
              <div className="state-icon"><Icon name="alert" size={19} /></div>
              <strong>资料暂时无法载入</strong><p>{listError}</p>
              <button type="button" className="text-action" onClick={() => void refreshList()}>重新载入</button>
            </div> : null}
            {!listLoading && !listError && sources.length === 0 ? <div className="list-state empty-state">
              <div className="empty-illustration"><div><Icon name="file" size={26} /></div><span /><i /></div>
              <strong>Vault 里还没有资料</strong>
              <p>导入 Markdown、TXT、PDF 或 DOCX 文件，原件会保存到 Vault，并排入索引。</p>
              <button type="button" className="primary-button compact" onClick={() => chooseFile()} disabled={!canUpload || uploading}>
                <Icon name="upload" size={15} />导入第一份资料
              </button>
            </div> : null}
            {sources.length > 0 && visibleSources.length === 0 ? <div className="list-state filter-empty"><strong>没有匹配的资料</strong><p>试试其他文件名。</p></div> : null}
            {visibleSources.map((source) => {
              const sourceId = source.source_id || source.id || ''
              const latest = source.latest_revision
              const state = latest?.index_state ?? latest?.state ?? source.revision_status
              const stateInfo = statusLabel(state)
              const selectedRow = selectedId === sourceId
              return <button key={sourceId} type="button" className={`source-row ${selectedRow ? 'selected' : ''}`} onClick={() => selectSource(sourceId)} aria-pressed={selectedRow}>
                <span className={`file-emblem ${fileType(source.filename).toLowerCase()}`}><Icon name="file" size={18} /></span>
                <span className="source-row-copy">
                  <strong title={source.filename}>{source.filename}</strong>
                  <span className="source-row-meta">{formatDate(source.created_at)}</span>
                  <span className={`row-status ${stateInfo.tone}`}><span />{stateInfo.label}</span>
                </span>
                {source.current_revision_id && source.latest_revision_id && source.current_revision_id !== source.latest_revision_id && <span className="outdated-mark" title="当前索引对应较早修订">旧索引</span>}
                <span className="row-chevron"><Icon name="chevron" size={14} /></span>
              </button>
            })}
          </div>
          {sources.length > 0 && <div className="list-pagination">
            <span>第 {page + 1} 页 · 每页最多 {PAGE_SIZE} 条</span>
            <div><button type="button" onClick={previousPage} disabled={page === 0}>上一页</button><button type="button" onClick={nextPage} disabled={sources.length < PAGE_SIZE}>下一页</button></div>
          </div>}
          {listError && sources.length > 0 && <div className="inline-list-error" role="status">刷新失败：{listError} <button type="button" onClick={() => void refreshList(undefined, false)}>重试</button></div>}
        </section>

        <section className="inspector" aria-label="资料详情" aria-live="polite" aria-busy={detailLoading}>
          {!selectedId ? <div className="inspector-empty">
            <div className="empty-inspector-mark"><Icon name="file" size={22} /></div>
            <strong>查看资料修订</strong>
            <p>选择一份资料，核对最近上传的修订与当前索引所用版本。</p>
          </div> : detailLoading && !selected ? <div className="inspector-loading"><span className="spinner" /><span>正在读取资料记录…</span></div> : detailError && !selected ? <div className="inspector-empty error-state">
            <div className="state-icon"><Icon name="alert" size={19} /></div><strong>详情暂时无法载入</strong><p>{detailError}</p>
            <button type="button" className="text-action" onClick={() => selectedId && void refreshDetail(selectedId, undefined, true)}>重新载入</button>
          </div> : selected ? <>
            <div className="inspector-topline">
              <span>资料检查器</span>
              <span className="record-state"><span />{selected.state === 'active' ? '有效资料' : '已删除'}</span>
            </div>
            <div className="inspector-title">
              <div className={`large-file-emblem ${fileType(selected.filename).toLowerCase()}`}><Icon name="file" size={21} /></div>
              <div className="inspector-name-wrap"><div className="file-type-label">{fileType(selected.filename)} · 来源资料</div><h2 title={selected.filename}>{selected.filename}</h2></div>
            </div>
            <div className="inspector-actions">
              <button className="primary-button" type="button" onClick={() => chooseFile(selected.source_id)} disabled={!canUpload || uploading || selected.state !== 'active'}>
                <Icon name={uploading ? 'clock' : 'plus'} size={15} />{uploading ? '正在导入…' : '上传新修订'}
              </button>
              {canRetry && <button className="secondary-button" type="button" onClick={() => void retryIndexing()} disabled={retrying}>
                <Icon name="retry" size={15} />{retrying ? '正在排队…' : '重试索引'}
              </button>}
            </div>
            {detailError && <p className="detail-refresh-error" role="status">详情刷新失败：{detailError}</p>}
            <div className="inspector-section-heading"><span>修订状态</span><span className="revision-counter">{selected.latest_revision_id ? '最近上传与当前索引' : '等待导入'}</span></div>
            <div className="revision-stack">
              <RevisionCard title="最近上传" revision={latest} missingText="尚未导入修订。选择文件以创建第一份版本。" />
              <div className="revision-connector" aria-hidden="true"><span /></div>
              <RevisionCard title="当前索引" revision={current} current missingText="尚无已完成索引。最近上传的修订会在处理成功后成为当前版本。" />
            </div>
            {activeJob && activeJob.revision_id === latestRevisionId && <div className="job-note" role="status">
              <span className={`job-note-icon ${statusLabel(activeJob.state).tone}`}><Icon name={activeJob.state === 'succeeded' ? 'check' : activeJob.state === 'failed' ? 'alert' : 'clock'} size={15} /></span>
              <span><strong>{activeJob.state === 'queued' ? '索引任务已排队' : activeJob.state === 'running' ? '正在处理最近修订' : activeJob.state === 'succeeded' ? '索引任务已完成' : '索引任务失败'}</strong><small>{activeJob.state === 'queued' ? '等待模型和索引服务可用。' : activeJob.error || `已尝试 ${activeJob.attempts} 次`}</small></span>
            </div>}
            <div className="inspector-section-heading detail-heading"><span>来源记录</span><span className="record-id">{shortId(selected.source_id)}</span></div>
            <dl className="source-facts">
              <div><dt>创建时间</dt><dd>{formatDate(selected.created_at)}</dd></div>
              <div><dt>最近修订</dt><dd className="mono">{shortId(selected.latest_revision_id)}</dd></div>
              <div><dt>当前修订</dt><dd className="mono">{shortId(selected.current_revision_id)}</dd></div>
              <div><dt>创建于 Vault</dt><dd className="mono path-value">{selected.vault_path || '路径待生成'}</dd></div>
            </dl>
          </> : null}
        </section>
      </div>

      <footer className="statusbar">
        <span className={`statusbar-dot ${activeDependencies ? 'ready' : 'waiting'}`} />
        <span>{activeDependencies ? '本机存储已连接' : '等待本机存储就绪'}</span>
        <span className="statusbar-divider">·</span>
        <span>{lastUpdated ? `最近同步 ${formatDate(lastUpdated)}` : '等待首次同步'}</span>
        <span className="statusbar-spacer" />
        <span className="statusbar-note">原始资料与修订保存在 Vault</span>
      </footer>
      <input ref={fileInputRef} className="visually-hidden" type="file" accept={ACCEPTED_FILES} onChange={onFileChange} aria-label="选择要导入的资料文件" />
    </div>
    {notice && <div className={`toast ${notice.tone}`} role={notice.tone === 'negative' ? 'alert' : 'status'}>{notice.tone === 'negative' ? <Icon name="alert" size={16} /> : <Icon name="check" size={16} />}{notice.text}</div>}
  </main>
}
