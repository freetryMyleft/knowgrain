import { useEffect, useRef, useState } from 'react'
import EvidencePanel from './EvidencePanel'
import { isPageDetailFor } from './wiki-contract'
import type { PageDetail } from './wiki-contract'
import {
  generationErrorMessage,
  isGenerationDetail,
  isGenerationJob,
  isGenerationJobList,
} from './generation-contract'
import type { DraftClaim, EvidenceReference, GenerationDetail, GenerationJob } from './generation-contract'
import './generation.css'

const API_ROOT = '/api/v1'
const JOB_POLL_MS = 5_000

class GenerationApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
  }
}

type ResponseGuard<T> = (value: unknown) => value is T
type ReviewAction = {
  kind: 'review' | 'apply'
  pageId: string
  targetId: string
  body: { expected_sha256: string } | {
    expected_proposal_sha256: string
    expected_target_sha256: string
  }
}

async function requestJson<T>(path: string, guard: ResponseGuard<T>, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_ROOT}${path}`, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new GenerationApiError(0, '无法连接本机服务。请确认 Knowgrain 正在运行。')
  }

  let body: unknown = null
  try { body = await response.json() } catch { body = null }
  if (!response.ok) throw new GenerationApiError(response.status, generationErrorMessage(body))
  if (!guard(body)) throw new GenerationApiError(response.status, '本机服务返回的生成数据格式无法识别，请刷新后重试。')
  return body
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function shortHash(value: string): string {
  return `${value.slice(0, 12)}…`
}

function jobState(job: GenerationJob): string {
  if (job.state === 'queued') return '等待处理'
  if (job.state === 'running') return job.phase === 'projecting' ? '正在写入生成结果' : '正在检索资料并生成'
  if (job.state === 'succeeded') return '已生成'
  return '生成失败'
}

function evidenceLabel(item: EvidenceReference): string {
  const parts = [item.vault_path]
  if (item.page !== null) parts.push(`第 ${item.page} 页`)
  if (item.heading) parts.push(item.heading)
  return parts.join(' · ')
}

function ClaimEvidence({ claim, evidence, current }: { claim: DraftClaim; evidence: EvidenceReference[]; current: boolean }) {
  const [selectedEvidence, setSelectedEvidence] = useState<string | null>(null)
  return <article className="generation-claim-row">
    <div className="generation-claim-copy">
      <span className="generation-claim-key mono">{claim.key}</span>
      <p>{claim.text}</p>
    </div>
    <div className="generation-evidence-stack">
      {evidence.length === 0 ? <p className="generation-muted">找不到这条声明对应的保留引文。</p> : evidence.map((item) => <article className="generation-evidence-card" key={item.evidence_id}>
        <div className="generation-evidence-title">
          <strong>{item.filename}</strong>
          <span className={`generation-validity ${current ? 'current' : 'stale'}`}>{current ? '当前有效' : '来源已变化'}</span>
        </div>
        <p className="generation-evidence-location">{evidenceLabel(item)}</p>
        <blockquote>{item.excerpt}</blockquote>
        <div className="generation-evidence-meta">
          <span>证据 ID <code>{item.evidence_id}</code></span>
          <span>来源 ID <code>{item.source_id}</code></span>
          <span>修订 ID <code>{item.revision_id}</code></span>
          <span>文本位置 {item.start}–{item.end}</span>
          <span>原件 SHA-256 <code title={item.source_sha256}>{shortHash(item.source_sha256)}</code></span>
          <span>解析文本 SHA-256 <code title={item.parsed_text_sha256}>{shortHash(item.parsed_text_sha256)}</code></span>
          <span>引文 SHA-256 <code title={item.excerpt_sha256}>{shortHash(item.excerpt_sha256)}</code></span>
          <span>索引于 {formatDate(item.indexed_at)}</span>
        </div>
        <button type="button" className="quiet-button" onClick={() => setSelectedEvidence(item.evidence_id)}>打开原文证据 ↗</button>
      </article>)}
    </div>
    {selectedEvidence && <EvidencePanel evidenceId={selectedEvidence} onClose={() => setSelectedEvidence(null)} />}
  </article>
}

export type WikiGenerationPanelProps = {
  page: PageDetail | null
  dirty: boolean
  onOpenPage: (id: string) => void
  onChanged: () => void
}

export default function WikiGenerationPanel({ page, dirty, onOpenPage, onChanged }: WikiGenerationPanelProps) {
  const [topic, setTopic] = useState('')
  const [jobs, setJobs] = useState<GenerationJob[]>([])
  const [jobsError, setJobsError] = useState<string | null>(null)
  const [detail, setDetail] = useState<GenerationDetail | null>(null)
  const [detailState, setDetailState] = useState<'loading' | 'ready' | 'missing' | 'error'>('loading')
  const [detailError, setDetailError] = useState<string | null>(null)
  const [mutation, setMutation] = useState<'enqueue' | 'retry' | 'review' | 'apply' | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [retryAction, setRetryAction] = useState<ReviewAction | null>(null)
  const pageId = page?.page_id ?? null
  const pageIdRef = useRef(pageId)
  const dirtyRef = useRef(dirty)
  const onOpenPageRef = useRef(onOpenPage)
  const onChangedRef = useRef(onChanged)
  const initiatedJobsRef = useRef(new Map<string, { pageId: string | null; selectionToken: number }>())
  const detailRequestRef = useRef(0)
  const mountedRef = useRef(true)
  const mutationRef = useRef(false)

  pageIdRef.current = pageId
  dirtyRef.current = dirty
  onOpenPageRef.current = onOpenPage
  onChangedRef.current = onChanged

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    const requestId = ++detailRequestRef.current
    setDetail(null)
    setDetailError(null)
    setDetailState(pageId ? 'loading' : 'missing')
    if (!pageId) return () => controller.abort()

    void requestJson<GenerationDetail>(
      `/wiki/pages/${encodeURIComponent(pageId)}/generation`,
      isGenerationDetail,
      { signal: controller.signal },
    ).then((result) => {
      if (controller.signal.aborted || requestId !== detailRequestRef.current) return
      setDetail(result)
      setDetailState('ready')
    }).catch((error: unknown) => {
      if (controller.signal.aborted || requestId !== detailRequestRef.current) return
      if (error instanceof GenerationApiError && error.status === 404) {
        setDetail(null)
        setDetailState('missing')
      } else {
        setDetailError(generationErrorMessage(error instanceof Error ? error.message : error))
        setDetailState('error')
      }
    })

    return () => controller.abort()
  }, [pageId, page?.content_sha256])

  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined

    const pollJobs = async () => {
      try {
        const result = await requestJson(`/wiki/generation-jobs?limit=20&offset=0`, isGenerationJobList, { signal: controller.signal })
        if (controller.signal.aborted) return
        const nextJobs = result.jobs.slice(0, 12)
        setJobs(nextJobs)
        setJobsError(null)

        for (const job of nextJobs) {
          const origin = initiatedJobsRef.current.get(job.job_id)
          if (job.state !== 'succeeded' || !initiatedJobsRef.current.has(job.job_id)) continue
          initiatedJobsRef.current.delete(job.job_id)
          onChangedRef.current()
          if (!dirtyRef.current && origin && pageIdRef.current === origin.pageId
            && detailRequestRef.current === origin.selectionToken) onOpenPageRef.current(job.output_page_id)
        }
      } catch (error) {
        if (!controller.signal.aborted) setJobsError(generationErrorMessage(error instanceof Error ? error.message : error))
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(() => void pollJobs(), JOB_POLL_MS)
      }
    }

    void pollJobs()
    return () => {
      controller.abort()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [])

  const withMutation = async (kind: NonNullable<typeof mutation>, action: () => Promise<void>) => {
    if (dirty || mutationRef.current) return
    const selectionToken = detailRequestRef.current
    mutationRef.current = true
    setMutation(kind)
    setMutationError(null)
    setNotice(null)
    try { await action() }
    catch (error) {
      const selectionBound = kind === 'review' || kind === 'apply'
      if (mountedRef.current && (!selectionBound || selectionToken === detailRequestRef.current)) {
        setMutationError(generationErrorMessage(error instanceof Error ? error.message : error))
      }
    } finally {
      mutationRef.current = false
      if (mountedRef.current) setMutation(null)
    }
  }

  const enqueue = async (proposal: boolean) => {
    const cleanTopic = topic.trim()
    if (!cleanTopic || dirty || mutation) return
    const requestPageId = pageId
    const selectionToken = detailRequestRef.current
    const expectedTargetHash = proposal && page ? page.content_sha256 : null
    await withMutation('enqueue', async () => {
      const job = await requestJson('/wiki/drafts', isGenerationJob, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          topic: cleanTopic,
          ...(proposal && requestPageId && expectedTargetHash
            ? { target_page_id: requestPageId, expected_target_sha256: expectedTargetHash }
            : {}),
        }),
      })
      initiatedJobsRef.current.set(job.job_id, { pageId: requestPageId, selectionToken })
      setTopic('')
      setNotice(proposal ? '提案任务已排队。目标页面原文不会被自动替换。' : '生成任务已排队；完成后会打开新草稿。')
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)].slice(0, 12))
    })
  }

  const retry = async (jobId: string) => withMutation('retry', async () => {
    const currentJob = jobs.find((job) => job.job_id === jobId)
    if (!currentJob) return
    const result = await requestJson(`/wiki/generation-jobs/${encodeURIComponent(jobId)}/retry`, isGenerationJob, { method: 'POST' })
    initiatedJobsRef.current.set(jobId, { pageId, selectionToken: detailRequestRef.current })
    setJobs((current) => current.map((job) => job.job_id === jobId ? result : job))
    setNotice('任务已重新排队。之前保留的有效结果会按服务端记录继续处理。')
  })

  const refreshDetail = async () => {
    if (!pageId) return
    const requestPageId = pageId
    const requestId = ++detailRequestRef.current
    setDetailState('loading')
    setDetailError(null)
    try {
      const result = await requestJson<GenerationDetail>(
        `/wiki/pages/${encodeURIComponent(requestPageId)}/generation`, isGenerationDetail,
      )
      if (pageIdRef.current !== requestPageId || detailRequestRef.current !== requestId) return
      setDetail(result)
      setDetailState('ready')
    } catch (error) {
      if (pageIdRef.current !== requestPageId || detailRequestRef.current !== requestId) return
      if (error instanceof GenerationApiError && error.status === 404) {
        setDetail(null)
        setDetailState('missing')
      } else {
        setDetailError(generationErrorMessage(error instanceof Error ? error.message : error))
        setDetailState('error')
      }
    }
  }

  const runReviewAction = async (action: ReviewAction) => {
    if (pageIdRef.current !== action.pageId || dirtyRef.current) return
    const requestToken = detailRequestRef.current
    await withMutation(action.kind, async () => {
      let updated: PageDetail
      try {
        updated = await requestJson(
          `/wiki/pages/${encodeURIComponent(action.pageId)}/${action.kind}`,
          (value): value is PageDetail => isPageDetailFor(value, action.targetId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(action.body),
          },
        )
      } catch (error) {
        if (mountedRef.current && pageIdRef.current === action.pageId) {
          setRetryAction(error instanceof GenerationApiError
            && (error.status === 0 || error.status === 503) ? action : null)
        }
        throw error
      }
      if (mountedRef.current) setRetryAction(null)
      onChangedRef.current()
      if (mountedRef.current && !dirtyRef.current && pageIdRef.current === action.pageId
        && detailRequestRef.current === requestToken) {
        setNotice(action.kind === 'review' ? '页面已明确标记为已审阅并保存到 Vault。'
          : '提案已应用到目标页面。原提案仍保留，可继续查看证据。')
        onOpenPageRef.current(updated.page_id)
      }
    })
  }

  const review = async () => {
    if (!page || !detail) return
    await runReviewAction({ kind: 'review', pageId: page.page_id, targetId: page.page_id,
      body: { expected_sha256: detail.current_sha256 } })
  }

  const applyProposal = async () => {
    if (!page || !detail) return
    const proposal = detail.proposal
    if (!proposal) return
    await runReviewAction({ kind: 'apply', pageId: page.page_id,
      targetId: proposal.target_page_id, body: {
        expected_proposal_sha256: detail.current_sha256,
        expected_target_sha256: proposal.expected_target_sha256,
      } })
  }

  const openGenerated = (job: GenerationJob) => onOpenPageRef.current(job.output_page_id)
  const proposal = detail?.proposal ?? null
  const detailHasProposal = Boolean(proposal || detail?.proposal_target_page_id)
  const targetHasDrifted = Boolean(proposal && (proposal.target_changed || proposal.target_sha256 !== proposal.expected_target_sha256))
  const pageMatchesDetail = Boolean(page && detail && page.content_sha256 === detail.current_sha256)
  const currentDocument = Boolean(detail && pageMatchesDetail && !detail.content_modified)
  const currentEvidence = detail?.evidence_current === true
  const canReview = Boolean(page && detail && page.status === 'draft' && detail.status === 'draft'
    && currentDocument && currentEvidence && !detailHasProposal && !detail.reviewed_at && !dirty && !mutation)
  const canApply = Boolean(page && detail && proposal && currentDocument && currentEvidence
    && !targetHasDrifted && !dirty && !mutation)

  const evidenceById = new Map((detail?.evidence ?? []).map((item) => [item.evidence_id, item]))

  return <section className="wiki-generation-panel" aria-labelledby="wiki-generation-title">
    <div className="generation-panel-heading">
      <div><div className="column-kicker">依据原始资料生成</div><h3 id="wiki-generation-title">生成与审阅</h3></div>
      <span className="generation-local-note">仅使用当前有效且已索引的资料</span>
    </div>

    <form className="generation-compose" onSubmit={(event) => { event.preventDefault(); void enqueue(false) }}>
      <label htmlFor="generation-topic">主题</label>
      <div className="generation-compose-row">
        <input id="generation-topic" value={topic} onChange={(event) => setTopic(event.target.value)} maxLength={600} placeholder="例如：总结这个项目的核心决策与依据" disabled={Boolean(mutation)} />
        <button className="primary-button" type="submit" disabled={!topic.trim() || dirty || Boolean(mutation)}>{mutation === 'enqueue' ? '正在排队…' : '生成新草稿'}</button>
      </div>
      {page && <div className="generation-proposal-row">
        <span>提案目标版本 <code title={page.content_sha256}>{shortHash(page.content_sha256)}</code></span>
        <button className="secondary-button" type="button" disabled={!topic.trim() || dirty || Boolean(mutation)} onClick={() => void enqueue(true)}>
          {mutation === 'enqueue' ? '正在排队…' : '针对当前页面生成提案'}
        </button>
      </div>}
      {dirty && <p className="generation-inline-warning" role="status">当前编辑尚未保存。保存或解决冲突后，才能创建任务、审阅或应用提案。</p>}
      {mutationError && <p className="generation-error" role="alert">{mutationError}</p>}
      {retryAction && retryAction.pageId === pageId && <div className="generation-inline-warning" role="status">
        <p>上次操作可能已写入文件但尚未完成登记。请保留页面原文，使用原请求版本继续；服务端会再次核对来源与文件。</p>
        <button type="button" className="secondary-button" disabled={dirty || Boolean(mutation)} onClick={() => void runReviewAction(retryAction)}>继续上次操作</button>
      </div>}
      {notice && <p className="generation-notice" role="status">{notice}</p>}
    </form>

    <section className="generation-jobs" aria-labelledby="generation-jobs-title" aria-live="polite">
      <div className="generation-section-heading"><h4 id="generation-jobs-title">最近任务</h4><span>{jobs.length}</span></div>
      {jobsError && <p className="generation-inline-warning" role="status">任务状态暂时无法刷新：{jobsError}</p>}
      {jobs.length === 0 ? <p className="generation-muted">排队后的任务和失败重试会显示在这里。</p> : jobs.map((job) => <article className="generation-job" key={job.job_id}>
        <div className="generation-job-main">
          <strong>{job.topic}</strong>
          <span className={`generation-job-state ${job.state}`}>{jobState(job)}</span>
          {job.state === 'failed' && job.error && <p className="generation-job-error">{job.error}</p>}
          <small>任务 {job.job_id} · 尝试 {job.attempts} 次 · {formatDate(job.updated_at ?? job.created_at)}</small>
        </div>
        <div className="generation-job-actions">
          {job.state === 'failed' && <button type="button" className="secondary-button" disabled={dirty || Boolean(mutation)} onClick={() => void retry(job.job_id)}>{mutation === 'retry' ? '正在重试…' : '重试'}</button>}
          {job.state === 'succeeded' && <button type="button" className="secondary-button" onClick={() => openGenerated(job)}>打开草稿</button>}
        </div>
      </article>)}
    </section>

    {page && <section className="generation-review" aria-labelledby="generation-review-title" aria-busy={detailState === 'loading'}>
      <div className="generation-section-heading"><h4 id="generation-review-title">当前页面的生成记录</h4>{detailState === 'ready' && <button type="button" className="generation-text-button" onClick={() => void refreshDetail()}>刷新</button>}</div>
      {detailState === 'loading' && <p className="generation-muted"><span className="spinner" />正在读取生成记录…</p>}
      {detailState === 'error' && <div className="generation-inline-error" role="status"><span>生成记录暂时无法读取：{detailError}</span><button type="button" className="generation-text-button" onClick={() => void refreshDetail()}>重试读取</button></div>}
      {detailState === 'missing' && <p className="generation-muted">这是一篇普通 Wiki 页面，尚无生成记录。你可以用上方主题创建草稿，或针对这篇页面生成提案。</p>}
      {detail && <>
        <div className="generation-integrity-row">
          <span className={`generation-integrity ${currentDocument ? 'current' : 'stale'}`}>{currentDocument ? '文件与生成版本一致' : '文件已修改或页面版本已更新'}</span>
          <span className={`generation-integrity ${currentEvidence ? 'current' : 'stale'}`}>{currentEvidence ? '引用来源仍然有效' : '至少一个引用来源已变化'}</span>
        </div>
        <div className="generation-hashes">
          <span>生成版本 <code title={detail.generated_sha256}>{detail.generated_sha256}</code></span>
          <span>当前文件 <code title={detail.current_sha256}>{detail.current_sha256}</code></span>
          {detail.reviewed_sha256 && <span>已审阅版本 <code title={detail.reviewed_sha256}>{detail.reviewed_sha256}</code></span>}
        </div>
        {detail.content_modified && <p className="generation-inline-warning" role="status">Vault 文件内容与生成清单不匹配。需要先检查文件；审阅和提案应用已锁定。</p>}
        {!currentEvidence && <p className="generation-inline-warning" role="status">证据已过时或不可核实。保留的原始引文仍可查看，但不能审阅或应用。</p>}
        {detail.reviewed_at && <p className="generation-reviewed-note">已审阅于 {formatDate(detail.reviewed_at)}</p>}

        {proposal && <section className="generation-proposal-detail" aria-label="提案目标差异">
          <div className="generation-proposal-title"><div><strong>目标页面：{proposal.target_title}</strong><span className="mono">{proposal.target_page_id}</span></div><span className={`generation-integrity ${targetHasDrifted ? 'stale' : 'current'}`}>{targetHasDrifted ? '目标已变化' : '目标版本未变'}</span></div>
          <div className="generation-target-hashes">
            <span>提案依据的目标版本 <code title={proposal.expected_target_sha256}>{proposal.expected_target_sha256}</code></span>
            <span>目标当前版本 <code title={proposal.target_sha256}>{proposal.target_sha256}</code></span>
          </div>
          {targetHasDrifted && <p className="generation-inline-warning" role="status">提案创建后目标页面已变化。请刷新页面并重新生成提案，避免覆盖其他编辑。</p>}
          <div className="generation-diff"><strong>目标差异</strong><pre>{proposal.diff || '服务端没有提供差异内容。'}</pre></div>
        </section>}

        <div className="generation-claims-heading"><strong>声明与原始依据</strong><span>引用按声明逐条展示，正文不会被浏览器解释为 HTML。</span></div>
        {detail.draft.sections.map((section, sectionIndex) => <section className="generation-claim-section" key={`${section.heading}-${sectionIndex}`}>
          <h5>{section.heading}</h5>
          <div className="generation-claim-table">
            {section.claims.map((claim) => <ClaimEvidence key={claim.key} claim={claim} evidence={claim.evidence_ids.map((id) => evidenceById.get(id)).filter((item): item is EvidenceReference => Boolean(item))} current={currentEvidence} />)}
          </div>
        </section>)}

        <div className="generation-review-actions">
          {!detailHasProposal && <button type="button" className="primary-button" disabled={!canReview} onClick={() => void review()}>{mutation === 'review' ? '正在审阅…' : detail.reviewed_at ? '已审阅' : '明确标记为已审阅'}</button>}
          {proposal && <button type="button" className="primary-button" disabled={!canApply} onClick={() => void applyProposal()}>{mutation === 'apply' ? '正在应用…' : '应用提案到目标页面'}</button>}
          {!canReview && !canApply && !detail.reviewed_at && !dirty && currentDocument && currentEvidence && !targetHasDrifted && <span>页面状态或生成清单不满足审阅条件。</span>}
        </div>
      </>}
    </section>}
  </section>
}
