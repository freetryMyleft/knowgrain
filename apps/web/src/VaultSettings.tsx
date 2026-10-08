import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { GearSix } from '@phosphor-icons/react/dist/csr/GearSix'
import { X } from '@phosphor-icons/react/dist/csr/X'

type VaultStatus = {
  binding_id: string | null
  root: string
  configured_root: string
  allowed_parent: string
  ready: boolean
  selection_enabled: boolean
  directories: string[]
  detail: string | null
}

type VaultPreview = {
  name: string
  root: string
  exists: boolean
  directories: string[]
  create_directories?: string[]
  selection_allowed: boolean
  binding_id: string | null
  expected_root: string
}

type VaultSettingsProps = {
  onVaultSelected: () => Promise<void>
}

class VaultApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

class VaultProtocolError extends Error {
  constructor(message: string) {
    super(message)
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isVaultStatus(value: unknown): value is VaultStatus {
  return isRecord(value)
    && isNullableString(value.binding_id)
    && typeof value.root === 'string'
    && typeof value.configured_root === 'string'
    && typeof value.allowed_parent === 'string'
    && typeof value.ready === 'boolean'
    && typeof value.selection_enabled === 'boolean'
    && isStringArray(value.directories)
    && isNullableString(value.detail)
}

function isVaultPreview(value: unknown): value is VaultPreview {
  if (!isRecord(value)) return false
  const hasCreateDirectories = Object.prototype.hasOwnProperty.call(value, 'create_directories')
  return typeof value.name === 'string'
    && typeof value.root === 'string'
    && typeof value.exists === 'boolean'
    && isStringArray(value.directories)
    && (!hasCreateDirectories || isStringArray(value.create_directories))
    && typeof value.selection_allowed === 'boolean'
    && isNullableString(value.binding_id)
    && typeof value.expected_root === 'string'
}

function errorText(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value
  if (Array.isArray(value)) {
    const messages = value.map((item) => {
      if (typeof item === 'string') return item
      if (typeof item === 'object' && item !== null && 'msg' in item) {
        return typeof item.msg === 'string' ? item.msg : ''
      }
      return ''
    }).filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  if (typeof value === 'object' && value !== null && 'detail' in value) {
    return errorText(value.detail)
  }
  return '服务暂时无法处理请求，请稍后重试。'
}

async function requestVault(path: string, init?: RequestInit): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(`/api/v1/system/vault${path}`, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new VaultApiError(0, '无法连接本机服务。请确认 Knowgrain 正在运行。')
  }

  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  if (!response.ok) throw new VaultApiError(response.status, errorText(body))
  return body
}

function readableError(error: unknown): string {
  if (error instanceof VaultProtocolError) return error.message
  return errorText(error instanceof VaultApiError ? error.message : error)
}

function directoryRoot(root: string, directory: string): string {
  if (directory.startsWith('/') || /^[A-Za-z]:[\\/]/.test(directory)) return directory
  return `${root.replace(/[\\/]$/, '')}/${directory.replace(/^[\\/]/, '')}`
}

export default function VaultSettings({ onVaultSelected }: VaultSettingsProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [status, setStatus] = useState<VaultStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [folderName, setFolderName] = useState('')
  const [preview, setPreview] = useState<VaultPreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [selecting, setSelecting] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const statusRequestRef = useRef(0)
  const previewRequestRef = useRef(0)
  const sessionEpochRef = useRef(0)
  const dialogOpenRef = useRef(false)
  const selectionInFlightRef = useRef(false)

  const isCurrentSession = (sessionId: number) => dialogOpenRef.current && sessionEpochRef.current === sessionId

  const closeDialog = useCallback(() => {
    if (!dialogOpenRef.current) return
    dialogOpenRef.current = false
    sessionEpochRef.current += 1
    previewRequestRef.current += 1
    setPreview(null)
    setPreviewLoading(false)
    setIsOpen(false)
  }, [])

  const loadStatus = useCallback(async (signal?: AbortSignal, requestedSession = sessionEpochRef.current) => {
    if (!isCurrentSession(requestedSession)) return
    const requestId = ++statusRequestRef.current
    setStatusLoading(true)
    setStatusError(null)
    try {
      const payload = await requestVault('', { signal })
      if (signal?.aborted || requestId !== statusRequestRef.current || !isCurrentSession(requestedSession)) return
      if (!isVaultStatus(payload)) throw new VaultProtocolError('服务返回的 Vault 状态格式无法识别。请重试读取。')
      const result = payload
      setStatus(result)
    } catch (error) {
      if (signal?.aborted || requestId !== statusRequestRef.current || !isCurrentSession(requestedSession)) return
      setStatus(null)
      setStatusError(readableError(error))
    } finally {
      if (!signal?.aborted && requestId === statusRequestRef.current && isCurrentSession(requestedSession)) setStatusLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!isOpen) return
    const controller = new AbortController()
    const sessionId = sessionEpochRef.current
    void loadStatus(controller.signal, sessionId)
    const previousFocus = document.activeElement as HTMLElement | null
    const focusTimer = window.setTimeout(() => {
      if (folderInputRef.current && !folderInputRef.current.disabled) folderInputRef.current.focus()
      else closeButtonRef.current?.focus()
    }, 0)

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeDialog()
        return
      }
      if (event.key !== 'Tab' || !dialogRef.current) return
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.getAttribute('aria-hidden') !== 'true')
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)

    return () => {
      controller.abort()
      window.clearTimeout(focusTimer)
      window.removeEventListener('keydown', onKeyDown)
      window.requestAnimationFrame(() => {
        if (dialogOpenRef.current) return
        if (triggerRef.current) triggerRef.current.focus()
        else previousFocus?.focus()
      })
    }
  }, [isOpen, loadStatus, closeDialog])

  useEffect(() => {
    if (!isOpen || !status || statusLoading) return
    if (status.selection_enabled) folderInputRef.current?.focus()
    else closeButtonRef.current?.focus()
  }, [isOpen, status, statusLoading])

  const openDialog = () => {
    sessionEpochRef.current += 1
    dialogOpenRef.current = true
    previewRequestRef.current += 1
    setActionError(null)
    setActionMessage(null)
    setPreview(null)
    setIsOpen(true)
  }

  const updateFolderName = (value: string) => {
    setFolderName(value)
    setPreview(null)
    setActionError(null)
    setActionMessage(null)
    previewRequestRef.current += 1
    setPreviewLoading(false)
  }

  const previewFolder = async () => {
    const sessionId = sessionEpochRef.current
    const name = folderName.trim()
    if (!isCurrentSession(sessionId) || !name || !status?.selection_enabled || selecting || selectionInFlightRef.current) return
    const requestId = ++previewRequestRef.current
    setPreview(null)
    setPreviewLoading(true)
    setActionError(null)
    setActionMessage(null)
    try {
      const payload = await requestVault('/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      if (!isCurrentSession(sessionId) || requestId !== previewRequestRef.current) return
      if (!isVaultPreview(payload)) throw new VaultProtocolError('服务返回的目录预览格式无法识别。请重新预览。')
      setPreview(payload)
    } catch (error) {
      if (!isCurrentSession(sessionId) || requestId !== previewRequestRef.current) return
      const apiError = error instanceof VaultApiError ? error : null
      setActionError(apiError?.status === 409
        ? `Vault 已有资料或 Wiki 页面，不能切换保存位置。${apiError.message}`
        : readableError(error))
      if (apiError?.status === 409 || apiError?.status === 503) void loadStatus(undefined, sessionId)
    } finally {
      if (isCurrentSession(sessionId) && requestId === previewRequestRef.current) setPreviewLoading(false)
    }
  }

  const selectVault = async () => {
    const sessionId = sessionEpochRef.current
    if (!isCurrentSession(sessionId) || !preview || !preview.selection_allowed || !status?.selection_enabled || selecting || selectionInFlightRef.current) return
    selectionInFlightRef.current = true
    setSelecting(true)
    setActionError(null)
    setActionMessage(null)
    try {
      let payload: unknown
      try {
        payload = await requestVault('/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: preview.name,
            expected_binding_id: preview.binding_id,
            expected_root: preview.expected_root,
          }),
        })
      } catch (error) {
        const apiError = error instanceof VaultApiError ? error : null
        if (isCurrentSession(sessionId)) {
          if (apiError?.status === 409) setPreview(null)
          setActionError(apiError?.status === 409
            ? `Vault 状态已变化，尚未完成选择。请重新读取状态并预览目录。${apiError.message}`
            : apiError?.status === 503
              ? `应用数据库暂不可用，Vault 尚未确认绑定。请重试。${apiError.message}`
              : readableError(error))
          if (apiError?.status === 409 || apiError?.status === 503) void loadStatus(undefined, sessionId)
        } else if (dialogOpenRef.current && (apiError?.status === 409 || apiError?.status === 503)) {
          void loadStatus(undefined, sessionEpochRef.current)
        }
        return
      }

      // A 2xx response may mean the database binding committed, even if its body is malformed.
      // Reconcile application health and source views independently of this dialog session.
      const refreshPromise = Promise.resolve().then(onVaultSelected).catch(() => undefined)
      if (!isVaultStatus(payload)) {
        if (isCurrentSession(sessionId)) {
          setPreview(null)
          setActionError('服务返回的 Vault 状态格式无法识别。请核对当前路径并重新预览。')
          void loadStatus(undefined, sessionId)
        } else if (dialogOpenRef.current) {
          void loadStatus(undefined, sessionEpochRef.current)
        }
        await refreshPromise
        return
      }

      const result = payload
      if (isCurrentSession(sessionId)) {
        setStatus(result)
        setPreview(null)
        if (result.ready) {
          closeDialog()
        } else {
          setActionMessage(result.detail
            ? `Vault 已绑定，但尚未就绪：${result.detail}`
            : 'Vault 已绑定，但服务尚未就绪。请重试读取状态或重连服务。')
        }
      } else if (dialogOpenRef.current) {
        // A newer dialog session should read the committed binding without being closed by this one.
        void loadStatus(undefined, sessionEpochRef.current)
      }
      await refreshPromise
    } catch (error) {
      if (isCurrentSession(sessionId)) setActionError(readableError(error))
    } finally {
      selectionInFlightRef.current = false
      setSelecting(false)
    }
  }

  const previewCanBeSelected = Boolean(preview?.selection_allowed && status?.selection_enabled)

  return <>
    <button ref={triggerRef} className="vault-settings-trigger" type="button" onClick={openDialog} aria-haspopup="dialog" aria-expanded={isOpen}>
      <GearSix aria-hidden="true" size={15} weight="regular" />
      <span>Vault 设置</span>
    </button>

    {isOpen && createPortal(
      <div className="vault-dialog-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDialog() }}>
        <div ref={dialogRef} className="vault-dialog" role="dialog" aria-modal="true" aria-labelledby="vault-dialog-title" aria-describedby="vault-dialog-description">
          <div className="vault-dialog-header">
            <div>
              <span className="vault-dialog-eyebrow">本机存储位置</span>
              <h2 id="vault-dialog-title">Vault 设置</h2>
            </div>
            <button ref={closeButtonRef} className="vault-dialog-close" type="button" onClick={closeDialog} aria-label="关闭 Vault 设置"><X aria-hidden="true" size={16} weight="regular" /></button>
          </div>
          <p id="vault-dialog-description" className="vault-dialog-intro">选择一个资料文件夹。预览只会检查路径，不会创建或更改文件。</p>

          {statusLoading && !status ? <div className="vault-dialog-loading" role="status"><span className="spinner" />正在读取 Vault 状态…</div> : null}
          {statusError && <div className="vault-dialog-error" role="alert">
            <strong>无法读取 Vault 状态</strong><span>{statusError}</span>
            <button type="button" className="text-action" onClick={() => void loadStatus()} disabled={statusLoading}>{statusLoading ? '正在重试…' : '重试读取'}</button>
          </div>}

          {status && <>
            <div className="vault-location-card">
              <span className="vault-location-label">{status.binding_id ? '当前 Vault' : '当前配置路径'}</span>
              <code className="vault-location-path">{status.root}</code>
              <div className="vault-location-foot">
                <span className={`vault-ready-mark ${status.ready ? 'ready' : 'waiting'}`} />
                <span>{status.ready ? '可用' : status.detail || '尚未就绪'}</span>
                {!status.ready && <button className="vault-inline-retry" type="button" onClick={() => void loadStatus()} disabled={statusLoading}>{statusLoading ? '读取中…' : '重试读取'}</button>}
              </div>
            </div>
            <div className="vault-parent-row"><span>允许选择的位置</span><code>{status.allowed_parent}</code></div>
            {status.configured_root !== status.root && <div className="vault-parent-row"><span>服务器初始配置</span><code>{status.configured_root}</code></div>}

            {!status.selection_enabled && <div className="vault-lock-note" role="status">
              <strong>Vault 位置已锁定</strong>
              <span>已有资料或 Wiki 页面时不能切换保存位置。当前路径仍可查看；迁移和恢复需要后续的专用流程。</span>
            </div>}

            <div className="vault-folder-form">
              <label htmlFor="vault-folder-name">文件夹名称</label>
              <p id="vault-folder-hint" className="vault-field-hint">只输入一个文件夹名称；不能输入完整路径。</p>
              <div className="vault-name-row">
                <input
                  ref={folderInputRef}
                  id="vault-folder-name"
                  value={folderName}
                  onChange={(event) => updateFolderName(event.currentTarget.value)}
                  onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void previewFolder() } }}
                  placeholder="例如：KnowledgeVault"
                  maxLength={120}
                  autoComplete="off"
                  disabled={!status.selection_enabled || statusLoading || selecting}
                  aria-describedby="vault-folder-hint"
                />
                <button className="secondary-button" type="button" onClick={() => void previewFolder()} disabled={!folderName.trim() || !status.selection_enabled || statusLoading || previewLoading || selecting}>
                  {previewLoading ? <><span className="spinner" />正在预览…</> : '预览目录'}
                </button>
              </div>
            </div>

            {preview && <section className="vault-preview" aria-labelledby="vault-preview-title" aria-live="polite">
              <div className="vault-preview-heading"><h3 id="vault-preview-title">目录预览</h3><span className={`vault-preview-badge ${preview.exists ? 'existing' : 'new'}`}>{preview.exists ? '已存在' : '将新建'}</span></div>
              <code className="vault-preview-root">{preview.root}</code>
              <p className="vault-preview-caption">Vault 目录</p>
              <ul className="vault-directory-list">
                {preview.directories.map((directory) => {
                  const hasCreateMetadata = Array.isArray(preview.create_directories)
                  const willCreate = preview.create_directories?.includes(directory) ?? false
                  return <li key={directory}>
                    <code>{directoryRoot(preview.root, directory)}</code>
                    <span className={willCreate ? 'directory-new' : 'directory-existing'}>{willCreate ? '将创建' : hasCreateMetadata ? '已存在' : '状态未知'}</span>
                  </li>
                })}
              </ul>
              {!preview.selection_allowed && <p className="vault-preview-locked" role="status">已有资料或 Wiki 页面，不能将 Vault 切换到此位置。</p>}
              <p className="vault-preserve-note">预览不会写入文件；使用 Vault 时只会准备这些目录，不会覆盖现有文件。</p>
            </section>}

            {actionError && <div className="vault-dialog-error compact-error" role="alert"><strong>操作未完成</strong><span>{actionError}</span></div>}
            {actionMessage && <div className="vault-dialog-message" role="status">{actionMessage}</div>}

            <div className="vault-dialog-actions">
              {preview && <button className="primary-button" type="button" onClick={() => void selectVault()} disabled={!previewCanBeSelected || selecting || statusLoading}>
                {selecting ? <><span className="spinner" />正在使用…</> : '使用此 Vault'}
              </button>}
              <button className="vault-cancel-button" type="button" onClick={closeDialog}>{preview ? '关闭' : '完成'}</button>
            </div>
          </>}
        </div>
      </div>,
      document.body,
    )}
  </>
}
