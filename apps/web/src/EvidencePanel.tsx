import { useEffect, useRef, useState } from 'react'
import { isEvidenceDetail } from './generation-contract'
import type { EvidenceDetail } from './generation-contract'
import './question.css'

export function evidenceDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN')
}

export default function EvidencePanel({ evidenceId, onClose }: { evidenceId: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [detail, setDetail] = useState<EvidenceDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const download = useRef<AbortController | null>(null)
  const load = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    load.current = controller
    const host = dialog.current
    host?.showModal()
    setDetail(null); setError(null); setBusy(null)
    void (async () => {
      try {
        const response = await fetch(`/api/v1/evidence/${encodeURIComponent(evidenceId)}`, { signal: controller.signal })
        if (!response.ok) throw new Error(response.status === 404 ? '这条证据不存在。' : '证据暂时无法读取，请关闭后重试。')
        const value: unknown = await response.json()
        if (!isEvidenceDetail(value) || value.evidence_id.toLowerCase() !== evidenceId.toLowerCase()) throw new Error('证据响应格式不正确。')
        if (!controller.signal.aborted) setDetail(value)
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '证据读取失败。')
      }
    })()
    return () => { controller.abort(); download.current?.abort(); host?.close() }
  }, [evidenceId])

  const downloadFile = async (kind: 'original' | 'markdown') => {
    if (!detail || download.current && !download.current.signal.aborted && busy) return
    const controller = new AbortController()
    const selection = load.current
    download.current = controller; setBusy(kind); setError(null)
    try {
      const response = await fetch(`/api/v1/evidence/${encodeURIComponent(evidenceId)}/${kind}`, { signal: controller.signal })
      if (!response.ok) throw new Error(response.status === 409 ? '文件已变化，无法提供与证据匹配的版本。' : response.status === 404 ? '对应文件不存在。' : '文件暂时无法下载，请重试。')
      const bytes = await response.blob()
      if (controller.signal.aborted || selection?.signal.aborted) return
      const url = URL.createObjectURL(bytes)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = kind === 'original' ? detail.filename.replace(/[/\\\x00-\x1f]/g, '_') : `${evidenceId}.md`
      anchor.hidden = true
      document.body.append(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause) {
      if (!controller.signal.aborted && !selection?.signal.aborted) setError(cause instanceof Error ? cause.message : '下载失败。')
    } finally { if (!controller.signal.aborted && !selection?.signal.aborted) setBusy(null) }
  }

  return <dialog ref={dialog} className="evidence-dialog" aria-labelledby="evidence-title" onCancel={onClose}>
    <header><div><small>原文证据</small><h2 id="evidence-title">{detail?.filename || '正在读取证据…'}</h2></div><button type="button" className="quiet-button" onClick={onClose} aria-label="关闭原文证据">×</button></header>
    {error && <p className="question-error" role="alert">{error}</p>}
    {detail && <>
      <p className={`evidence-freshness ${detail.current ? '' : 'stale'}`} role="status">{detail.current ? '与当前有效修订一致' : '历史证据：已过期或原件暂时无法核实，不可用于新答案'}</p>
      <blockquote className="evidence-quote">{detail.excerpt}</blockquote>
      <dl className="evidence-metadata">
        <div><dt>修订</dt><dd>{detail.revision_id}</dd></div>
        <div><dt>原件 SHA-256</dt><dd>{detail.source_sha256}</dd></div>
        <div><dt>摘录 SHA-256</dt><dd>{detail.excerpt_sha256}</dd></div>
        <div><dt>索引时间</dt><dd>{evidenceDate(detail.indexed_at)}</dd></div>
        <div><dt>原文位置</dt><dd>字符 {detail.start}–{detail.end}{detail.page ? ` · 第 ${detail.page} 页` : ''}{detail.heading ? ` · ${detail.heading}` : ''}</dd></div>
        <div><dt>Vault 原件</dt><dd>{detail.vault_path}</dd></div>
      </dl>
      <div className="evidence-downloads"><button type="button" className="primary-button" disabled={busy !== null} onClick={() => void downloadFile('original')}>{busy === 'original' ? '正在核对原件…' : '下载准确修订原件'}</button><button type="button" className="quiet-button" disabled={busy !== null} onClick={() => void downloadFile('markdown')}>{busy === 'markdown' ? '正在核对摘录…' : '下载证据 Markdown'}</button></div>
    </>}
  </dialog>
}
