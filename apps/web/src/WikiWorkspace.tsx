import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { EditorState } from '@codemirror/state'
import { EditorView, highlightActiveLine, lineNumbers, keymap } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import { markdown } from '@codemirror/lang-markdown'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  isBacklinkResponse,
  isConflictDetail,
  isPageDetail,
  isPageDetailFor,
  isPageListResponse,
  renderWikilinks,
  shouldSyncSavedDraft,
  WikiReadLifecycle,
} from './wiki-contract'
import type { Backlink, BacklinkResponse, ConflictDetail, PageDetail, PageListResponse, PageSummary, WikiIssue, WikiLink } from './wiki-contract'
import './wiki.css'

const API_ROOT = '/api/v1'
const PAGE_SIZE = 100
const POLL_INTERVAL_MS = 5000

class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, message: string, body: unknown) {
    super(message)
    this.status = status
    this.body = body
  }
}

function errorMessage(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value
  if (Array.isArray(value)) {
    const messages = value.map((item) => {
      if (typeof item === 'string') return item
      if (typeof item === 'object' && item !== null && 'msg' in item) {
        const message = (item as { msg?: unknown }).msg
        return typeof message === 'string' ? message : ''
      }
      return ''
    }).filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  if (typeof value === 'object' && value !== null) {
    if ('detail' in value) return errorMessage((value as { detail?: unknown }).detail)
    if ('message' in value && typeof (value as { message?: unknown }).message === 'string') {
      const record = value as { message: string; issues?: unknown }
      const issues = Array.isArray(record.issues) ? record.issues.map((issue) => {
        if (typeof issue === 'string') return issue
        if (typeof issue !== 'object' || issue === null) return ''
        const detail = (issue as { detail?: unknown }).detail
        const path = (issue as { vault_path?: unknown }).vault_path
        const issueText = typeof detail === 'string' ? detail : ''
        return [typeof path === 'string' ? path : '', issueText].filter(Boolean).join('：')
      }).filter(Boolean) : []
      const visibleIssues = issues.slice(0, 3)
      const more = issues.length > visibleIssues.length ? `（另有 ${issues.length - visibleIssues.length} 项）` : ''
      return visibleIssues.length ? `${record.message}：${visibleIssues.join('；')}${more}` : record.message
    }
  }
  return '服务暂时无法处理请求，请稍后重试。'
}

type ResponseGuard<T> = (value: unknown) => value is T

async function requestJson<T>(path: string, guard: ResponseGuard<T>, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_ROOT}${path}`, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, '无法连接本机服务。请确认 Knowgrain 正在运行。', null)
  }
  let body: unknown = null
  try { body = await response.json() } catch { body = null }
  if (!response.ok) throw new ApiError(response.status, errorMessage(body), body)
  if (!guard(body)) throw new ApiError(response.status, '本机服务返回了无效 Wiki 数据，请刷新重试；若持续发生，请检查服务版本。', body)
  return body
}

function getConflict(error: ApiError, expectedPageId: string): ConflictDetail | null {
  if (error.status !== 409 || typeof error.body !== 'object' || error.body === null || !('detail' in error.body)) return null
  const detail = (error.body as { detail?: unknown }).detail
  if (typeof detail !== 'object' || detail === null) return null
  if (!isConflictDetail(detail)) return null
  return detail.current === null || detail.current.page_id === expectedPageId ? detail : null
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date)
}

function basename(value: string): string {
  return value.split('/').filter(Boolean).pop() || value
}

function safePreviewUrl(url: string): string {
  if (/^knowgrain-wiki:\/\/\d+$/.test(url) || /^knowgrain-anchor:\/\/[\p{L}\p{N}_.%~-]+$/u.test(url)) return url
  const protocol = url.match(/^([a-z][a-z\d+.-]*:)/i)?.[1]?.toLocaleLowerCase()
  if (protocol && !['http:', 'https:', 'irc:', 'ircs:', 'mailto:', 'xmpp:'].includes(protocol)) return ''
  return url
}

function headingText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(headingText).join('')
  if (typeof node === 'object' && node !== null && 'props' in node) {
    return headingText((node as { props?: { children?: ReactNode } }).props?.children)
  }
  return ''
}

function headingSlug(value: string): string {
  return value.toLocaleLowerCase().trim().replace(/[^\p{L}\p{N}_\-\s]/gu, '').replace(/\s+/g, '-') || 'section'
}

function anchorSlug(value: string | null | undefined): string {
  const anchor = (value ?? '').replace(/^#/, '').replace(/^\^/, '')
  try { return headingSlug(decodeURIComponent(anchor)) } catch { return headingSlug(anchor) }
}

function MarkdownEditor({ value, syncVersion, onChange }: { value: string; syncVersion: number; onChange: (value: string) => void }) {
  const hostRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    if (!hostRef.current) return
    const state = EditorState.create({
      doc: value,
      extensions: [
        lineNumbers(),
        highlightActiveLine(),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
        markdown(),
        EditorView.lineWrapping,
        EditorView.updateListener.of((update) => {
          if (update.docChanged) onChangeRef.current(update.state.doc.toString())
        }),
        EditorView.theme({
          '&': { height: '100%', fontSize: '13px' },
          '.cm-scroller': { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', overflow: 'auto' },
          '.cm-content': { padding: '16px 18px', minHeight: '100%' },
          '.cm-gutters': { backgroundColor: '#f8f9fb', border: 'none', color: '#a3adbb' },
          '&.cm-focused': { outline: 'none' },
        }),
      ],
    })
    const view = new EditorView({ state, parent: hostRef.current })
    viewRef.current = view
    return () => { view.destroy(); viewRef.current = null }
  }, [])

  useEffect(() => {
    const view = viewRef.current
    if (!view || view.state.doc.toString() === value) return
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } })
  }, [syncVersion, value])

  return <div className="wiki-editor-host" ref={hostRef} aria-label="Markdown 编辑器" />
}

type PreviewProps = {
  markdownText: string
  links: WikiLink[]
  onWikiNavigate: (link: WikiLink | null) => void
  onExternalNavigate: (href: string) => void
  onAnchor: (anchor: string) => void
}

function MarkdownPreview({ markdownText, links, onWikiNavigate, onExternalNavigate, onAnchor }: PreviewProps) {
  const transformed = renderWikilinks(markdownText, links)
  const ids = transformed.targets
  const slugs = new Map<string, number>()

  const makeHeading = (tag: 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6') => {
    const Component = ({ children }: { children?: ReactNode }) => {
      const base = headingSlug(headingText(children))
      const count = slugs.get(base) ?? 0
      slugs.set(base, count + 1)
      const id = count === 0 ? base : `${base}-${count}`
      const Element = tag
      return <Element id={id}>{children}</Element>
    }
    Component.displayName = `Preview${tag}`
    return Component
  }

  const components: Components = {
    h1: makeHeading('h1'), h2: makeHeading('h2'), h3: makeHeading('h3'),
    h4: makeHeading('h4'), h5: makeHeading('h5'), h6: makeHeading('h6'),
    img: ({ alt }) => <span className="wiki-image-blocked">图片未加载：{alt || '外部图片'}</span>,
    a: ({ href, children, title }) => {
      if (href?.startsWith('knowgrain-wiki://')) {
        const rawId = Number(href.slice('knowgrain-wiki://'.length))
        const link = Number.isInteger(rawId) ? ids.get(rawId) ?? null : null
        if (!link?.to_page_id) return <span className="wiki-link-unresolved" title={link ? `目标不可用：${link.target || '本页锚点'}` : '此 Wiki 链接格式暂不支持'}>{children}<small>未链接</small></span>
        return <a href="#" title={title} className="wiki-link-resolved" onClick={(event) => { event.preventDefault(); onWikiNavigate(link) }}>{children}</a>
      }
      if (href?.startsWith('knowgrain-anchor://')) {
        let anchor = href.slice('knowgrain-anchor://'.length)
        try { anchor = decodeURIComponent(anchor) } catch { /* The URL was already restricted by safePreviewUrl. */ }
        return <span id={anchorSlug(anchor)} className="wiki-anchor-point" aria-hidden="true" />
      }
      if (!href) return <span>{children}</span>
      if (href.startsWith('#')) return <a href={href} onClick={(event) => { event.preventDefault(); onAnchor(href.slice(1)) }}>{children}</a>
      return <a href={href} title={title} onClick={(event) => { event.preventDefault(); onExternalNavigate(href) }}>{children}</a>
    },
  }

  return <div className="wiki-preview-scroll">
    <div className="wiki-markdown-preview">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={safePreviewUrl}>{transformed.markdown}</ReactMarkdown>
    </div>
  </div>
}

function targetElement(preview: HTMLElement | null, anchor: string): HTMLElement | null {
  if (!preview) return null
  const normalized = anchorSlug(anchor)
  return preview.querySelector<HTMLElement>(`#${CSS.escape(normalized)}`)
}

export default function WikiWorkspace({ onReturnToSources }: { onReturnToSources: () => void }) {
  const [pages, setPages] = useState<PageSummary[]>([])
  const [issues, setIssues] = useState<WikiIssue[]>([])
  const [pageIndex, setPageIndex] = useState(0)
  const [listLoading, setListLoading] = useState(true)
  const [listError, setListError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [baseDetail, setBaseDetail] = useState<PageDetail | null>(null)
  const [observedDetail, setObservedDetail] = useState<PageDetail | null>(null)
  const [draft, setDraft] = useState('')
  const [backlinks, setBacklinks] = useState<Backlink[]>([])
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [backlinkError, setBacklinkError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newBody, setNewBody] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)
  const [editorSyncVersion, setEditorSyncVersion] = useState(0)
  const [conflict, setConflict] = useState<ConflictDetail | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pendingAnchor, setPendingAnchor] = useState<string | null>(null)
  const dirtyRef = useRef(false)
  const draftRef = useRef('')
  const baseRef = useRef<PageDetail | null>(null)
  const selectedIdRef = useRef<string | null>(null)
  const listRequestRef = useRef(0)
  const readLifecycleRef = useRef(new WikiReadLifecycle())
  const previewRef = useRef<HTMLDivElement>(null)
  const listScrollRef = useRef<HTMLDivElement>(null)
  const baseHashAtObservedRef = useRef<string | null>(null)
  const dirty = Boolean(baseDetail && draft !== baseDetail.markdown)

  const hasUnsavedChanges = useCallback(() => dirtyRef.current, [])

  const syncBase = useCallback((detail: PageDetail, syncDraft: boolean) => {
    baseRef.current = detail
    setBaseDetail(detail)
    baseHashAtObservedRef.current = detail.content_sha256
    setObservedDetail(detail)
    if (syncDraft) {
      draftRef.current = detail.markdown
      dirtyRef.current = false
      setDraft(detail.markdown)
      setEditorSyncVersion((version) => version + 1)
    } else dirtyRef.current = draftRef.current !== detail.markdown
  }, [])

  const acceptRemoteDetail = useCallback((detail: PageDetail) => {
    if (selectedIdRef.current !== detail.page_id) return
    setObservedDetail(detail)
    const currentBase = baseRef.current
    const isDirty = currentBase ? draftRef.current !== currentBase.markdown : false
    if (!currentBase || !isDirty) syncBase(detail, true)
  }, [syncBase])

  const refreshSelected = useCallback(async (id: string, signal?: AbortSignal, showLoading = false) => {
    if (selectedIdRef.current !== id || signal?.aborted) return
    const lifecycle = readLifecycleRef.current
    const token = lifecycle.beginRead()
    if (!token) return
    if (showLoading) setDetailLoading(true)
    const isCurrent = () => !signal?.aborted
      && selectedIdRef.current === id
      && lifecycle.isCurrent(token)
    try {
      const [detail, linked] = await Promise.all([
        requestJson<PageDetail>(`/wiki/pages/${encodeURIComponent(id)}`, (value): value is PageDetail => isPageDetailFor(value, id), { signal }),
        requestJson<BacklinkResponse>(`/wiki/pages/${encodeURIComponent(id)}/backlinks`, isBacklinkResponse, { signal }),
      ])
      if (!isCurrent()) return
      acceptRemoteDetail(detail)
      setBacklinks(linked.pages)
      setDetailError(null)
      setBacklinkError(null)
      setDetailLoading(false)
    } catch (error) {
      if (!isCurrent()) return
      const message = errorMessage(error instanceof ApiError ? error.message : error)
      setDetailError(message)
      setBacklinkError(message)
      setDetailLoading(false)
    } finally {
      lifecycle.endRead(token)
    }
  }, [acceptRemoteDetail])

  const loadList = useCallback(async (offset: number, signal?: AbortSignal, quiet = false) => {
    const requestId = ++listRequestRef.current
    if (!quiet && pages.length === 0) setListLoading(true)
    try {
      const result = await requestJson<PageListResponse>(`/wiki/pages?limit=${PAGE_SIZE}&offset=${offset}`, isPageListResponse, { signal })
      if (signal?.aborted || requestId !== listRequestRef.current) return
      setPages(result.pages)
      setIssues(result.issues)
      setListError(null)
      setListLoading(false)
    } catch (error) {
      if (signal?.aborted || requestId !== listRequestRef.current) return
      setListError(errorMessage(error instanceof ApiError ? error.message : error))
      setListLoading(false)
    }
  }, [pages.length])

  useEffect(() => {
    const controller = new AbortController()
    void loadList(pageIndex * PAGE_SIZE, controller.signal)
    const timer = window.setInterval(() => void loadList(pageIndex * PAGE_SIZE, controller.signal, true), POLL_INTERVAL_MS)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [loadList, pageIndex])

  useEffect(() => {
    if (!selectedId) {
      setDetailLoading(false)
      return
    }
    const controller = new AbortController()
    void refreshSelected(selectedId, controller.signal, true)
    const timer = window.setInterval(() => void refreshSelected(selectedId, controller.signal), POLL_INTERVAL_MS)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [refreshSelected, selectedId])

  const resetSelection = useCallback((id: string | null) => {
    readLifecycleRef.current.beginSelection()
    selectedIdRef.current = id
    setSelectedId(id)
    baseRef.current = null
    baseHashAtObservedRef.current = null
    dirtyRef.current = false
    draftRef.current = ''
    setBaseDetail(null)
    setObservedDetail(null)
    setDraft('')
    setBacklinks([])
    setDetailError(null)
    setBacklinkError(null)
    setSaveError(null)
    setConflict(null)
    setNotice(null)
    setPendingAnchor(null)
    setEditorSyncVersion((version) => version + 1)
  }, [])

  const navigateToPage = useCallback((id: string, anchor: string | null = null, confirmed = false) => {
    if (id === selectedIdRef.current) {
      if (anchor) {
        const element = targetElement(previewRef.current, anchor)
        if (element) element.scrollIntoView({ behavior: 'smooth', block: 'start' })
        else setNotice(`页面中没有找到锚点“${anchor}”。`)
      }
      return true
    }
    if (!confirmed && hasUnsavedChanges() && !window.confirm('当前页面有未保存的编辑。离开后这些编辑会丢失，仍要继续吗？')) return false
    resetSelection(id)
    if (anchor) setPendingAnchor(anchor)
    return true
  }, [hasUnsavedChanges, resetSelection])

  const handleDraftChange = (value: string) => {
    draftRef.current = value
    dirtyRef.current = Boolean(baseRef.current && value !== baseRef.current.markdown)
    setDraft(value)
    setSaveError(null)
  }

  const handleBack = () => {
    if (hasUnsavedChanges() && !window.confirm('当前页面有未保存的编辑。返回资料页会丢失这些编辑，仍要继续吗？')) return
    onReturnToSources()
  }

  const save = async () => {
    const base = baseRef.current
    const id = selectedIdRef.current
    if (!base || !id || !dirtyRef.current || conflict || saving) return
    const submittedMarkdown = draftRef.current
    const saveGeneration = readLifecycleRef.current.beginSave()
    setSaving(true)
    setSaveError(null)
    try {
      const saved = await requestJson<PageDetail>(`/wiki/pages/${encodeURIComponent(id)}`, (value): value is PageDetail => isPageDetailFor(value, id), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ markdown: submittedMarkdown, expected_sha256: base.content_sha256 }),
      })
      const sameSelection = selectedIdRef.current === id && readLifecycleRef.current.currentGeneration === saveGeneration
      if (sameSelection) {
        syncBase(saved, shouldSyncSavedDraft(draftRef.current, submittedMarkdown))
        setConflict(null)
        setNotice('Wiki 页面已保存到 Vault。')
      }
      void loadList(pageIndex * PAGE_SIZE, undefined, true)
    } catch (error) {
      if (selectedIdRef.current !== id || readLifecycleRef.current.currentGeneration !== saveGeneration) return
      if (error instanceof ApiError) {
        const serverConflict = getConflict(error, id)
        if (serverConflict) {
          setConflict(serverConflict)
          if (serverConflict.current) setObservedDetail(serverConflict.current)
          setSaveError(serverConflict.message)
        } else {
          setSaveError(error.message)
        }
      } else setSaveError(errorMessage(error))
    } finally {
      readLifecycleRef.current.endSave()
      setSaving(false)
      if (selectedIdRef.current === id && readLifecycleRef.current.currentGeneration !== saveGeneration) {
        readLifecycleRef.current.invalidateReads()
        void refreshSelected(id)
      }
    }
  }

  const reloadConflictVersion = () => {
    if (!conflict?.current) return
    if (hasUnsavedChanges() && !window.confirm('用服务器当前版本替换编辑器中的未保存内容吗？此操作不能撤销。')) return
    readLifecycleRef.current.invalidateReads()
    syncBase(conflict.current, true)
    setConflict(null)
    setSaveError(null)
    setNotice('已载入服务器当前版本。')
  }

  const createPage = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (hasUnsavedChanges() && !window.confirm('创建成功后会打开新页面，当前未保存编辑将丢失。仍要创建吗？')) return
    setCreating(true)
    setCreateError(null)
    try {
      const detail = await requestJson<PageDetail>('/wiki/pages', isPageDetail, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle.trim(), body: newBody }),
      })
      setCreateOpen(false)
      setNewTitle('')
      setNewBody('')
      setPageIndex(0)
      resetSelection(detail.page_id)
      syncBase(detail, true)
      void loadList(0, undefined, true)
    } catch (error) {
      setCreateError(errorMessage(error instanceof ApiError ? error.message : error))
    } finally { setCreating(false) }
  }

  const handleWikiNavigate = (link: WikiLink | null) => {
    if (!link?.to_page_id) {
      setNotice('这个 Wiki 链接尚未解析到有效页面。')
      return
    }
    navigateToPage(link.to_page_id, link.anchor)
  }

  const handleExternalNavigate = (href: string) => {
    if (hasUnsavedChanges() && !window.confirm('当前页面有未保存的编辑。继续打开链接吗？')) return
    try {
      const url = new URL(href, window.location.href)
      if (url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:') {
        window.open(url.href, '_blank', 'noopener,noreferrer')
      } else setNotice('此链接协议不受支持。')
    } catch { setNotice('此链接地址无效。') }
  }

  const scrollAnchor = (anchor: string) => {
    const element = targetElement(previewRef.current, anchor)
    if (element) element.scrollIntoView({ behavior: 'smooth', block: 'start' })
    else setNotice(`页面中没有找到锚点“${anchor}”。`)
  }

  useEffect(() => {
    const confirmCloseWithDraft = (event: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', confirmCloseWithDraft)
    return () => window.removeEventListener('beforeunload', confirmCloseWithDraft)
  }, [])

  useEffect(() => {
    if (!baseDetail || !pendingAnchor) return
    const anchor = pendingAnchor
    const frame = window.requestAnimationFrame(() => {
      const element = targetElement(previewRef.current, anchor)
      if (element) element.scrollIntoView({ behavior: 'smooth', block: 'start' })
      else setNotice(`页面已打开，但没有找到锚点“${anchor}”。`)
      setPendingAnchor(null)
    })
    return () => window.cancelAnimationFrame(frame)
  }, [baseDetail, pendingAnchor])

  const current = observedDetail ?? baseDetail
  const externalChanged = Boolean(baseDetail && observedDetail && observedDetail.content_sha256 !== baseHashAtObservedRef.current)

  return <main className="desktop-shell wiki-shell">
    <div className="app-window wiki-window">
      <header className="titlebar">
        <div className="brand-mark" aria-hidden="true">✳</div>
        <span className="brand-name">Knowgrain</span><span className="titlebar-separator" aria-hidden="true">/</span>
        <span className="titlebar-page">Wiki</span><span className="titlebar-spacer" />
        <span className="local-indicator"><span />本机 Vault</span>
        <button type="button" className="secondary-button wiki-return" onClick={handleBack}>返回资料</button>
      </header>

      <div className="wiki-grid">
        <aside className="wiki-sidebar" aria-label="Wiki 工作区">
          <div className="side-label">工作区</div>
          <div className="side-current" aria-current="page"><span className="wiki-nav-mark">W</span><span>Wiki 页面</span><span className="side-count">{pages.length}</span></div>
          <div className="sidebar-rule" />
          <section className="wiki-sidebar-note">
            <span className="wiki-note-icon">⌘</span>
            <strong>文件就是内容</strong>
            <p>Markdown 页面保存在 Vault 中，可与 Obsidian 一起使用。</p>
          </section>
          <div className="sidebar-bottom"><div className="vault-health is-ready"><span className="vault-health-dot" /><span>Wiki 与 Vault 同步</span></div><span className="sidebar-caption">每几秒检查一次外部文件更新</span></div>
        </aside>

        <section className="wiki-list-column" aria-label="Wiki 页面列表">
          <div className="wiki-list-toolbar">
            <div className="column-heading"><div className="column-kicker">知识库</div><h1>Wiki</h1></div>
            <button type="button" className="icon-button" aria-label="刷新 Wiki" title="刷新" onClick={() => void loadList(pageIndex * PAGE_SIZE, undefined, true)}>↻</button>
            <button type="button" className="primary-button compact" onClick={() => { setCreateError(null); setCreateOpen(true) }}>＋ 新建</button>
          </div>
          <div className="wiki-list-summary">第 {pageIndex + 1} 页 · {pages.length} 条页面</div>
          <div className="wiki-page-list" ref={listScrollRef} aria-live="polite" aria-busy={listLoading}>
            {listLoading && pages.length === 0 && <div className="list-loading"><span className="spinner" />正在读取 Wiki…</div>}
            {!listLoading && listError && pages.length === 0 && <div className="list-state error-state"><div className="state-icon">!</div><strong>Wiki 暂时无法载入</strong><p>{listError}</p><button type="button" className="text-action" onClick={() => void loadList(pageIndex * PAGE_SIZE)}>重新载入</button></div>}
            {!listLoading && !listError && pages.length === 0 && <div className="list-state empty-state"><div className="wiki-empty-icon">W</div><strong>还没有 Wiki 页面</strong><p>创建一篇 Markdown 页面，文件会保存在 Wiki/Drafts 中。</p><button type="button" className="primary-button compact" onClick={() => setCreateOpen(true)}>＋ 新建页面</button></div>}
            {pages.map((page) => <button key={page.page_id} type="button" className={`wiki-page-row ${selectedId === page.page_id ? 'selected' : ''}`} onClick={() => navigateToPage(page.page_id)} aria-pressed={selectedId === page.page_id}>
              <span className="wiki-file-icon">M</span><span className="wiki-row-copy"><strong title={page.title}>{page.title}</strong><span>{basename(page.vault_path)}</span><small>{formatDate(page.updated_at)}</small></span>
              <span className={`wiki-status-pill ${page.status}`}>{page.status === 'reviewed' ? '已审阅' : '草稿'}</span>
            </button>)}
          </div>
          {issues.length > 0 && <details className="wiki-issues"><summary><span className="wiki-issue-dot" />扫描问题 <b>{issues.length}</b></summary><div className="wiki-issue-list">{issues.map((issue, index) => <article key={`${issue.vault_path}-${issue.code}-${index}`}><strong>{issue.code}</strong><span className="mono">{issue.vault_path}</span><p>{issue.detail}</p></article>)}</div></details>}
          {pages.length > 0 && <div className="list-pagination"><span>每页最多 {PAGE_SIZE} 条</span><div><button type="button" onClick={() => { setPageIndex((value) => Math.max(0, value - 1)); listScrollRef.current?.scrollTo(0, 0) }} disabled={pageIndex === 0}>上一页</button><button type="button" onClick={() => { setPageIndex((value) => value + 1); listScrollRef.current?.scrollTo(0, 0) }} disabled={pages.length < PAGE_SIZE}>下一页</button></div></div>}
          {listError && pages.length > 0 && <div className="wiki-inline-error" role="status">列表刷新失败：{listError}</div>}
        </section>

        <section className="wiki-detail" aria-label="Wiki 页面详情" aria-busy={detailLoading}>
          {!selectedId ? <div className="wiki-detail-empty"><div className="wiki-empty-icon">W</div><strong>选择一篇 Wiki 页面</strong><p>查看 Markdown、编辑正文并浏览反向链接。</p></div> : detailLoading && !baseDetail ? <div className="inspector-loading"><span className="spinner" />正在读取页面…</div> : detailError && !baseDetail ? <div className="wiki-detail-empty error-state"><div className="state-icon">!</div><strong>页面暂时无法载入</strong><p>{detailError}</p><button type="button" className="text-action" onClick={() => void loadList(pageIndex * PAGE_SIZE)}>重新载入列表</button></div> : current ? <>
        <div className="wiki-detail-head">
              <div className="wiki-page-heading"><div className="column-kicker">{current.status === 'reviewed' ? '已审阅页面' : '草稿页面'}</div><h2 title={current.title}>{current.title}</h2><div className="wiki-page-path mono">{current.vault_path}</div></div>
              <div className="wiki-detail-actions"><button type="button" className="secondary-button" onClick={() => void refreshSelected(current.page_id, undefined, true)}>刷新</button><button type="button" className="primary-button" onClick={() => void save()} disabled={!dirty || saving || Boolean(conflict)}>{saving ? '正在保存…' : '保存'}</button></div>
            </div>
            <div className="wiki-meta-line"><span className={`wiki-status-pill ${current.status}`}>{current.status === 'reviewed' ? '已审阅' : '草稿'}</span><span>更新于 {formatDate(current.updated_at)}</span><span className="wiki-meta-hash mono" title={current.content_sha256}>SHA-256 {current.content_sha256.slice(0, 12)}…</span>{dirty && <span className="wiki-unsaved-label">有未保存更改</span>}</div>
            {detailError && <div className="wiki-callout error" role="status">详情刷新失败：{detailError}</div>}
            {externalChanged && dirty && <div className="wiki-callout warning" role="status">Vault 中的文件已有更新。编辑器保留了本地内容；保存时会检查版本，发生冲突后可比较并显式载入服务器版本。</div>}
            {saveError && !conflict && <div className="wiki-callout error" role="alert">保存失败：{saveError}</div>}
            {conflict && <section className="wiki-conflict" aria-label="保存冲突">
              <div className="wiki-conflict-heading"><span className="wiki-conflict-mark">!</span><div><strong>服务器版本已变化</strong><p>{conflict.message}</p></div></div>
              {conflict.current ? <div className="wiki-conflict-current"><div className="wiki-conflict-subhead"><strong>当前服务器版本 · {conflict.current.title}</strong><span className="mono">{conflict.current.content_sha256.slice(0, 12)}…</span></div><pre>{conflict.current.markdown}</pre></div> : <p className="wiki-conflict-removed">服务器当前没有这篇页面，可能已被移除。未保存编辑仍保留在编辑器中。</p>}
              <div className="wiki-conflict-diff"><strong>版本差异</strong><pre>{conflict.diff || '服务端未提供差异内容。'}</pre></div>
              <div className="wiki-conflict-actions"><span>保存冲突不会覆盖 Vault 文件。</span><button type="button" className="secondary-button" onClick={reloadConflictVersion} disabled={!conflict.current}>载入服务器当前版本</button></div>
            </section>}
            {notice && <div className="wiki-callout notice" role="status">{notice}<button type="button" aria-label="关闭提示" onClick={() => setNotice(null)}>×</button></div>}

            <div className="wiki-editor-preview-labels"><span>Markdown 编辑器</span><span>预览</span></div>
            <div className="wiki-editor-preview">
              <MarkdownEditor key={selectedId} value={draft} syncVersion={editorSyncVersion} onChange={handleDraftChange} />
              <div ref={previewRef} className="wiki-preview-wrap"><MarkdownPreview markdownText={draft} links={baseDetail?.links ?? []} onWikiNavigate={handleWikiNavigate} onExternalNavigate={handleExternalNavigate} onAnchor={scrollAnchor} /></div>
            </div>

            <section className="wiki-backlinks" aria-labelledby="wiki-backlinks-title">
              <div className="wiki-section-title"><h3 id="wiki-backlinks-title">反向链接</h3><span>{backlinks.length}</span></div>
              {backlinkError ? <p className="wiki-muted-line">反向链接暂时无法载入：{backlinkError}</p> : backlinks.length === 0 ? <p className="wiki-muted-line">还没有其他页面链接到这里。</p> : <div className="wiki-backlink-list">{backlinks.map((link, index) => <button key={`${link.page_id}-${link.line}-${index}`} type="button" onClick={() => navigateToPage(link.page_id)}><span className="wiki-backlink-arrow">↗</span><span><strong>{link.title}</strong><small>{link.vault_path} · 第 {link.line} 行{link.anchor ? ` · #${link.anchor}` : ''}</small></span></button>)}</div>}
            </section>
          </> : null}
        </section>
      </div>

      <footer className="statusbar"><span className="statusbar-dot ready" /><span>Markdown 保存在 Vault</span><span className="statusbar-divider">·</span><span>{selectedId ? (dirty ? '有未保存更改' : '页面已同步') : '等待选择 Wiki 页面'}</span><span className="statusbar-spacer" /><span className="statusbar-note">文件内容是 Wiki 的权威版本</span></footer>
    </div>

    {createOpen && <div className="wiki-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !creating) setCreateOpen(false) }}><section className="wiki-create-dialog" role="dialog" aria-modal="true" aria-labelledby="wiki-create-title"><div className="wiki-create-head"><div><div className="column-kicker">新建 Markdown 文件</div><h2 id="wiki-create-title">创建 Wiki 页面</h2></div><button type="button" className="icon-button" onClick={() => setCreateOpen(false)} disabled={creating} aria-label="关闭">×</button></div><form onSubmit={(event) => void createPage(event)}><label>页面标题<input autoFocus required maxLength={200} value={newTitle} onChange={(event) => setNewTitle(event.target.value)} placeholder="例如：知识库导览" /></label><label>正文<textarea value={newBody} onChange={(event) => setNewBody(event.target.value)} rows={12} placeholder="写下页面正文。页面会以草稿状态创建。" /></label>{createError && <p className="wiki-form-error" role="alert">{createError}</p>}<div className="wiki-create-foot"><span>新页面会保存到 Wiki/Drafts</span><button type="button" className="secondary-button" onClick={() => setCreateOpen(false)} disabled={creating}>取消</button><button type="submit" className="primary-button" disabled={creating || !newTitle.trim()}>{creating ? '正在创建…' : '创建页面'}</button></div></form></section></div>}
  </main>
}
