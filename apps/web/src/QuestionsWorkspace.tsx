import { useEffect, useRef, useState } from 'react'
import { ArrowSquareOut } from '@phosphor-icons/react/dist/csr/ArrowSquareOut'
import type { FormEvent } from 'react'
import EvidencePanel, { evidenceDate } from './EvidencePanel'
import { isQueryJob, isQueryList, queryStateLabel } from './question-contract'
import type { QueryJob } from './question-contract'
import './question.css'

async function api(path: string, signal?: AbortSignal, body?: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(`/api/v1${path}`, {
    signal, method: body ? 'POST' : 'GET',
    ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  })
  if (!response.ok) throw new Error(response.status === 503 ? '问答服务暂不可用，请检查本机服务后重试。' : response.status === 409 ? '任务状态已变化，请刷新后重试。' : response.status === 422 ? '问题格式不符合要求，请输入 1-1000 个字符。' : '请求失败，请重试。')
  return response.json() as Promise<unknown>
}

export default function QuestionsWorkspace({ onReturnToSources }: { onReturnToSources: () => void }) {
  const [question, setQuestion] = useState('')
  const [jobs, setJobs] = useState<QueryJob[]>([])
  const [page, setPage] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [storedDetail, setDetail] = useState<QueryJob | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [evidenceId, setEvidenceId] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)
  const mounted = useRef(false)
  const posting = useRef(false)
  const selection = useRef<string | null>(null)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const detail = storedDetail?.job_id === selectedId ? storedDetail : null
  const select = (id: string) => { selection.current = id; setSelectedId(id); setDetail(null); setEvidenceId(null) }

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const read = async () => {
      try {
        const value = await api(`/queries?limit=100&offset=${page * 100}`, controller.signal)
        if (!isQueryList(value)) throw new Error('任务列表响应格式不正确。')
        if (controller.signal.aborted) return
        setJobs(value.jobs); setListError(null)
        if (!selection.current && !posting.current && value.jobs[0]) select(value.jobs[0].job_id)
      } catch (cause) {
        if (!controller.signal.aborted) setListError(cause instanceof Error ? cause.message : '任务列表读取失败。')
      } finally { if (!controller.signal.aborted) timer = setTimeout(() => void read(), 5000) }
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [page, refresh])

  useEffect(() => {
    setDetail(null); setDetailError(null)
    if (!selectedId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const read = async () => {
      let interval = 3000
      try {
        const value = await api(`/queries/${encodeURIComponent(selectedId)}`, controller.signal)
        if (!isQueryJob(value, true) || value.job_id !== selectedId) throw new Error('回答响应格式不正确。')
        if (controller.signal.aborted || selection.current !== selectedId) return
        setDetail(value); setDetailError(null)
        interval = value.state === 'succeeded' || value.state === 'failed' ? 15000 : 2000
      } catch (cause) {
        if (!controller.signal.aborted && selection.current === selectedId) setDetailError(cause instanceof Error ? cause.message : '回答读取失败。')
      } finally { if (!controller.signal.aborted) timer = setTimeout(() => void read(), interval) }
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [selectedId, refresh])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (posting.current || !question.trim() || question.trim().length > 1000) return
    posting.current = true; setBusy(true); setError(null)
    const submitted = question.trim()
    const startingSelection = selection.current
    try {
      const value = await api('/queries', undefined, { question: submitted })
      if (!isQueryJob(value)) throw new Error('任务响应格式不正确，请在记录中查看是否已创建。')
      if (!mounted.current) return
      if (selection.current === startingSelection) select(value.job_id)
      setRefresh(value => value + 1)
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : '提问失败。')
    } finally { posting.current = false; if (mounted.current) setBusy(false) }
  }
  const retry = async () => {
    if (!detail || posting.current) return
    const id = detail.job_id
    posting.current = true; setBusy(true); setError(null)
    try {
      const value = await api(`/queries/${encodeURIComponent(id)}/retry`, undefined, {})
      if (!isQueryJob(value) || value.job_id !== id) throw new Error('任务响应格式不正确。')
      if (mounted.current) setRefresh(value => value + 1)
    } catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : '重试失败。') }
    finally { posting.current = false; if (mounted.current) setBusy(false) }
  }
  const result = detail?.result
  const evidence = new Map(result?.evidence.map(item => [item.evidence_id, item]))

  return <main className="desktop-shell">
    <div className="app-window question-window">
      <header className="titlebar"><div className="brand-mark" aria-hidden="true">◇</div><span className="brand-name">Knowgrain</span><span className="titlebar-separator">/</span><span className="titlebar-page">问答</span><span className="titlebar-spacer" /><button className="quiet-button" type="button" onClick={onReturnToSources}>返回资料</button></header>
      <div className="question-layout">
        <aside className="question-history" aria-label="问答记录">
          <div className="question-history-heading"><h2>问答记录</h2><span>保存在本机</span></div>
          {listError && <p className="question-error" role="alert">{listError}</p>}
          {!jobs.length && <p className="question-muted">提问后的任务会显示在这里。刷新页面后仍可查看。</p>}
          <div className="question-job-list">{jobs.map(job => <button key={job.job_id} className={selectedId === job.job_id ? 'selected' : ''} aria-current={selectedId === job.job_id ? 'true' : undefined} onClick={() => select(job.job_id)} type="button"><strong>{job.question}</strong><span>{queryStateLabel(job.state)}</span><small>{evidenceDate(job.created_at)}</small></button>)}</div>
          <div className="question-pagination"><button className="quiet-button" type="button" disabled={page === 0} onClick={() => setPage(page => page - 1)}>上一页</button><span>{page + 1}</span><button className="quiet-button" type="button" disabled={jobs.length < 100} onClick={() => setPage(page => page + 1)}>下一页</button></div>
        </aside>
        <section className="question-main" aria-label="有依据的问答">
          <div className="question-intro"><small>在自己的资料中查证</small><h1>每个回答，都能回到原文。</h1><p>输入问题。系统从当前已完成索引的资料中检索，并逐条列出支持回答的引文。</p></div>
          <form className="question-form" onSubmit={event => void submit(event)}>
            <label htmlFor="question-input">你的问题</label><textarea id="question-input" value={question} onChange={event => setQuestion(event.target.value)} maxLength={1000} rows={3} placeholder="例如：验收资料中的原件保存在什么位置？" />
            <div><span>{question.length} / 1000</span><button type="submit" className="primary-button" disabled={busy || !question.trim()}>{busy ? '正在提交…' : '查阅资料'}</button></div>
          </form>
          {error && <p className="question-error" role="alert">{error}</p>}
          {detailError && <p className="question-error" role="alert">{detailError}</p>}
          {selectedId && !detail && !detailError && <p className="question-muted" role="status">正在读取回答…</p>}
          {detail && <section className="question-answer" aria-label="当前回答">
            <header><h2>{detail.question}</h2><span className={`question-job-state ${detail.state}`} role="status">{queryStateLabel(detail.state)}</span></header>
            {(detail.state === 'queued' || detail.state === 'running') && <p className="question-muted">可以离开此页，稍后从问答记录查看结果。</p>}
            {detail.state === 'failed' && <div className="question-error"><p>{detail.error || '任务未能完成。原始资料仍然保留，可重试。'}</p><button className="quiet-button" type="button" disabled={busy} onClick={() => void retry()}>重试这个问题</button></div>}
            {result && <>
              <div className="question-model">{result.model.name} · {result.model.provider} · {evidenceDate(result.model.generated_at)}</div>
              {result.status === 'insufficient' ? <p className="question-insufficient" role="status">{result.message}</p> : <>
                {!result.evidence_current && <p className="question-error" role="status">这份回答包含历史证据，部分来源已变化或暂时无法核实。请重新提问获取当前答案。</p>}
                <ol className="question-claims">{result.claims.map(item => <li key={item.key}><p>{item.text}</p><div>{item.evidence_ids.map(id => { const source = evidence.get(id); return source ? <button className="question-citation" type="button" key={id} onClick={() => setEvidenceId(id)}>{source.filename}<span>{source.current ? '查看原文' : '历史引文'} <ArrowSquareOut aria-hidden="true" size={13} weight="regular" /></span></button> : null })}</div></li>)}</ol>
                <div className="question-source-ledger"><h3>引用的修订</h3>{result.evidence.map(item => <div key={item.evidence_id}><button type="button" onClick={() => setEvidenceId(item.evidence_id)}>{item.filename}</button><code>{item.revision_id}</code><span>索引于 {evidenceDate(item.indexed_at)}</span></div>)}</div>
              </>}
            </>}
          </section>}
        </section>
      </div>
    </div>
    {evidenceId && <EvidencePanel evidenceId={evidenceId} onClose={() => setEvidenceId(null)} />}
  </main>
}
