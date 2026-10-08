import { useEffect, useState } from 'react'
import { ArrowSquareOut } from '@phosphor-icons/react/dist/csr/ArrowSquareOut'
import EvidencePanel from './EvidencePanel'
import { generationErrorMessage } from './generation-contract'
import { isPageEntities, isEntityPages } from './entity-contract'
import type { PageEntities, EntityPages, WikiEntity } from './entity-contract'
import './entity.css'

async function read<T>(url: string, guard: (v: unknown) => v is T, signal: AbortSignal): Promise<T> {
  let response: Response
  try { response = await fetch(`/api/v1${url}`, { signal }) }
  catch (error) {
    if (signal.aborted) throw error
    throw new Error('无法连接本机服务，请确认 Knowgrain 正在运行。')
  }
  let value: unknown
  try { value = await response.json() }
  catch { throw new Error('本机服务返回了无法识别的实体数据，请刷新后重试。') }
  if (!response.ok) throw new Error(generationErrorMessage(value))
  if (!guard(value)) throw new Error('实体关联数据无法识别，请刷新后重试。')
  return value
}
function message(error: unknown): string { return error instanceof Error ? error.message : '实体关联暂不可用。' }

function EntityWikiLinks({ entity, onOpenPage, onEvidence }: {
  entity: WikiEntity; onOpenPage: (id: string) => void; onEvidence: (id: string) => void
}) {
  const [data, setData] = useState<EntityPages | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setData(null); setError(null)
    void read(`/graph/entity-pages?name=${encodeURIComponent(entity.name)}`, isEntityPages, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return
        if (result.entity_id !== entity.entity_id || result.name !== entity.name) throw new Error('实体关联与当前选择不一致。')
        setData(result)
      }).catch(error => { if (!controller.signal.aborted) setError(message(error)) })
    return () => controller.abort()
  }, [entity.entity_id, entity.name, version])
  return <div className="entity-links" aria-live="polite" aria-busy={!data && !error}>
    <div className="entity-links-head"><strong>{entity.name} · 关联 Wiki</strong><button type="button" className="text-action" onClick={() => setVersion(v => v + 1)}>重新核对</button></div>
    {error ? <p className="wiki-inline-error">{error}</p> : !data ? <p className="wiki-muted-line">正在核对页面与来源…</p> : <>
      {data.pages.length === 0 && <p className="wiki-muted-line">没有通过当前版本校验的 Wiki 页面。</p>}
      {data.pages.map(page => <article className="entity-linked-page" key={page.page_id}>
        <button type="button" className="entity-page-open" onClick={() => onOpenPage(page.page_id)}><strong>{page.title.length > 200 ? `${page.title.slice(0, 200)}…` : page.title} <ArrowSquareOut aria-hidden="true" size={14} weight="regular" /></strong><small>{page.vault_path.length > 512 ? `${page.vault_path.slice(0, 512)}…` : page.vault_path}</small></button>
        <div className="entity-evidence-actions">{page.evidence_ids.map((id, index) => <button type="button" className="text-action" key={id} onClick={() => onEvidence(id)}>原文证据 {index + 1}</button>)}</div>
      </article>)}
      {data.truncated && <p className="wiki-muted-line">已达到候选页面上限；此列表不是完整关联集合。</p>}
    </>}
  </div>
}

export default function WikiEntityPanel({ pageId, contentHash, dirty, onOpenPage }: {
  pageId: string; contentHash: string; dirty: boolean; onOpenPage: (id: string) => void
}) {
  const [data, setData] = useState<PageEntities | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [evidenceId, setEvidenceId] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setData(null); setError(null); setSelected(null); setEvidenceId(null)
    void read(`/wiki/pages/${encodeURIComponent(pageId)}/entities`, isPageEntities, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return
        if (result.page_id !== pageId || result.content_sha256 !== contentHash) throw new Error('页面版本已变化，等待 Wiki 同步后重试。')
        setData(result)
      }).catch(error => { if (!controller.signal.aborted) setError(message(error)) })
    return () => controller.abort()
  }, [pageId, contentHash, version])
  const current = data?.page_id === pageId && data.content_sha256 === contentHash ? data : null
  const entity = current?.entities.find(item => item.entity_id === selected)
  return <section className="wiki-entity-section" aria-label="LightRAG 实体关联">
    <div className="wiki-entity-head"><div><div className="column-kicker">知识索引</div><h2>LightRAG 实体</h2></div><button type="button" className="secondary-button compact" onClick={() => setVersion(v => v + 1)}>刷新关联</button></div>
    <p className="wiki-muted-line">关联来自已保存页面的引用文本块。实体名称用于导航，事实以原文证据为准。</p>
    {dirty ? <p className="wiki-muted-line">页面有未保存更改；保存后再查看对应关联。</p> : error ? <p className="wiki-inline-error" role="status">{error}</p> : !current ? <p className="wiki-muted-line">正在读取实体关联…</p> : !current.binding_current ? <p className="wiki-muted-line">当前页面没有有效生成绑定，或正文已被编辑。重新生成并审阅后可建立关联。</p> : !current.evidence_current ? <p className="wiki-muted-line">引用来源已变化，暂不显示当前实体关联。</p> : <>
      {current.entities.length === 0 && <p className="wiki-muted-line">当前引用文本块尚无可核验的实体。</p>}
      <div className="entity-pills">{current.entities.map(item => <button type="button" key={item.entity_id} aria-pressed={item.entity_id === selected} className={`entity-pill ${item.entity_id === selected ? 'selected' : ''}`} onClick={() => setSelected(item.entity_id === selected ? null : item.entity_id)}><strong>{item.name}</strong><small>{item.entity_type}</small></button>)}</div>
      {current.truncated && <p className="wiki-muted-line">实体数量达到显示上限；部分关联未展示。</p>}
      {entity && <EntityWikiLinks key={entity.entity_id} entity={entity} onOpenPage={onOpenPage} onEvidence={setEvidenceId} />}
    </>}
    {evidenceId && <EvidencePanel evidenceId={evidenceId} onClose={() => setEvidenceId(null)} />}
  </section>
}
