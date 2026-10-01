export type PageSummary = {
  page_id: string
  vault_path: string
  title: string
  status: 'draft' | 'reviewed'
  content_sha256: string
  updated_at: string
}

export type WikiLink = {
  target: string
  anchor: string | null
  label: string | null
  embed: boolean
  line: number
  to_page_id: string | null
}

export type PageDetail = PageSummary & { markdown: string; links: WikiLink[] }
export type WikiIssue = { vault_path: string; code: string; detail: string }
export type PageListResponse = { pages: PageSummary[]; issues: WikiIssue[] }
export type Backlink = PageSummary & { target: string; anchor: string | null; line: number; embed: boolean }
export type BacklinkResponse = { pages: Backlink[] }
export type ConflictDetail = { code: string; message: string; current: PageDetail | null; diff: string }
export type WikiReadToken = { generation: number; sequence: number }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

export function isPageSummary(value: unknown): value is PageSummary {
  if (!isRecord(value)) return false
  return typeof value.page_id === 'string' && value.page_id.length > 0
    && typeof value.vault_path === 'string' && value.vault_path.length > 0
    && typeof value.title === 'string'
    && (value.status === 'draft' || value.status === 'reviewed')
    && typeof value.content_sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.content_sha256)
    && typeof value.updated_at === 'string'
}

export function isWikiLink(value: unknown): value is WikiLink {
  if (!isRecord(value)) return false
  return typeof value.target === 'string'
    && isNullableString(value.anchor)
    && isNullableString(value.label)
    && typeof value.embed === 'boolean'
    && Number.isInteger(value.line) && (value.line as number) >= 1
    && isNullableString(value.to_page_id)
}

export function isPageDetail(value: unknown): value is PageDetail {
  if (!isPageSummary(value) || !isRecord(value)) return false
  const detail = value as Record<string, unknown>
  return typeof detail.markdown === 'string'
    && Array.isArray(detail.links)
    && detail.links.every(isWikiLink)
}

export function isPageDetailFor(value: unknown, pageId: string): value is PageDetail {
  return isPageDetail(value) && value.page_id === pageId
}

export function isWikiIssue(value: unknown): value is WikiIssue {
  if (!isRecord(value)) return false
  return typeof value.vault_path === 'string' && typeof value.code === 'string' && typeof value.detail === 'string'
}

export function isPageListResponse(value: unknown): value is PageListResponse {
  if (!isRecord(value) || !Array.isArray(value.pages) || !value.pages.every(isPageSummary)) return false
  if (!Array.isArray(value.issues) || !value.issues.every(isWikiIssue)) return false
  return new Set(value.pages.map((page) => (page as PageSummary).page_id)).size === value.pages.length
}

export function isBacklink(value: unknown): value is Backlink {
  if (!isPageSummary(value) || !isRecord(value)) return false
  const backlink = value as Record<string, unknown>
  return typeof backlink.target === 'string'
    && isNullableString(backlink.anchor)
    && Number.isInteger(backlink.line) && (backlink.line as number) >= 1
    && typeof backlink.embed === 'boolean'
}

export function isBacklinkResponse(value: unknown): value is BacklinkResponse {
  return isRecord(value) && Array.isArray(value.pages) && value.pages.every(isBacklink)
}

export function isConflictDetail(value: unknown): value is ConflictDetail {
  if (!isRecord(value)) return false
  return typeof value.code === 'string'
    && typeof value.message === 'string'
    && (value.current === null || isPageDetail(value.current))
    && typeof value.diff === 'string'
    && value.diff.length <= 64 * 1024
}

/** Keeps line numbers stable while hiding YAML frontmatter from the preview. */
export function stripFrontmatter(markdownText: string): string {
  const match = markdownText.match(/^---[ \t]*\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/)
  if (!match) return markdownText
  const newlineCount = (match[0].match(/\r?\n/g) || []).length
  return '\n'.repeat(newlineCount) + markdownText.slice(match[0].length)
}

/** Backslash escaping follows Markdown's odd/even contiguous-backslash rule. */
export function isEscapedAt(source: string, position: number): boolean {
  let slashes = 0
  for (let cursor = position - 1; cursor >= 0 && source[cursor] === '\\'; cursor -= 1) slashes += 1
  return slashes % 2 === 1
}

function escapeMarkdownLabel(label: string): string {
  return label.replace(/[\\`*_{}\[\]()#+.!|>~-]/g, '\\$&')
}

function headingSlug(value: string): string {
  return value.toLocaleLowerCase().trim().replace(/[^\p{L}\p{N}_\-\s]/gu, '').replace(/\s+/g, '-') || 'section'
}

function sameWikiTarget(link: WikiLink, target: string, anchor: string | null, embed: boolean): boolean {
  return link.target.trim() === target.trim()
    && (link.anchor ?? '') === (anchor ?? '')
    && link.embed === embed
}

function lineWithoutCarriageReturn(line: string): string {
  return line.endsWith('\r') ? line.slice(0, -1) : line
}

function openingFence(line: string): { marker: string; length: number } | null {
  const match = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/)
  if (!match || (match[1][0] === '`' && match[2].includes('`'))) return null
  return { marker: match[1][0], length: match[1].length }
}

function findNextFenceBoundary(source: string, from: number): number {
  let lineStart = source.indexOf('\n', from)
  if (lineStart < 0) return -1
  lineStart += 1
  while (lineStart < source.length) {
    const lineEnd = source.indexOf('\n', lineStart)
    const end = lineEnd < 0 ? source.length : lineEnd
    if (openingFence(lineWithoutCarriageReturn(source.slice(lineStart, end)))) return lineStart
    if (lineEnd < 0) return -1
    lineStart = lineEnd + 1
  }
  return -1
}

export type RenderedWikiLinks = { markdown: string; targets: Map<number, WikiLink | null> }

/** Converts supported Wiki tokens to safe preview links using only server-resolved link metadata. */
export function renderWikilinks(markdownText: string, links: WikiLink[]): RenderedWikiLinks {
  const source = stripFrontmatter(markdownText)
  const output: string[] = []
  const targets = new Map<number, WikiLink | null>()
  const used = new Set<number>()
  let position = 0
  let line = 1
  let fence: { marker: string; length: number } | null = null
  let nextTarget = 0

  while (position < source.length) {
    // Markdown comments take precedence over fences in their contents.
    if (!fence && source.startsWith('<!--', position)) {
      const commentEnd = source.indexOf('-->', position + 4)
      const end = commentEnd < 0 ? source.length : commentEnd + 3
      const comment = source.slice(position, end)
      output.push(comment)
      line += (comment.match(/\n/g) || []).length
      position = end
      continue
    }

    if (position === 0 || source[position - 1] === '\n') {
      const lineEnd = source.indexOf('\n', position)
      const end = lineEnd < 0 ? source.length : lineEnd
      const lineText = source.slice(position, end)
      const lineBody = lineWithoutCarriageReturn(lineText)
      if (fence) {
        const close = lineBody.match(/^ {0,3}(`+|~+)[ \t]*$/)
        if (close && close[1][0] === fence.marker && close[1].length >= fence.length) fence = null
        output.push(lineText)
        if (lineEnd >= 0) output.push('\n')
        position = lineEnd < 0 ? source.length : lineEnd + 1
        line += lineEnd < 0 ? 0 : 1
        continue
      }
      const opening = openingFence(lineBody)
      if (opening) {
        fence = opening
        output.push(lineText)
        if (lineEnd >= 0) output.push('\n')
        position = lineEnd < 0 ? source.length : lineEnd + 1
        line += lineEnd < 0 ? 0 : 1
        continue
      }
    }

    if (fence) {
      const char = source[position]
      output.push(char)
      if (char === '\n') line += 1
      position += 1
      continue
    }

    if (source[position] === '`' && !isEscapedAt(source, position)) {
      const ticks = source.slice(position).match(/^`+/)?.[0] ?? '`'
      const close = source.indexOf(ticks, position + ticks.length)
      const fenceBoundary = findNextFenceBoundary(source, position)
      if (close >= 0 && (fenceBoundary < 0 || close < fenceBoundary)) {
        const code = source.slice(position, close + ticks.length)
        output.push(code)
        line += (code.match(/\n/g) || []).length
        position = close + ticks.length
        continue
      }
    }

    const embed = source.startsWith('![[', position)
    const openerLength = embed ? 3 : 2
    const escaped = isEscapedAt(source, position)
    if (escaped && (embed || source.startsWith('[[', position))) {
      output.push(source.slice(position, position + openerLength))
      position += openerLength
      continue
    }
    if (!escaped && (embed || source.startsWith('[[', position))) {
      const close = source.indexOf(']]', position + openerLength)
      if (close >= 0) {
        const raw = source.slice(position + openerLength, close)
        const divider = raw.indexOf('|')
        const targetPart = (divider < 0 ? raw : raw.slice(0, divider)).trim()
        const label = divider < 0 ? '' : raw.slice(divider + 1).trim()
        const hashIndex = targetPart.indexOf('#')
        const target = hashIndex < 0 ? targetPart : targetPart.slice(0, hashIndex).trim()
        const anchor = hashIndex < 0 ? null : targetPart.slice(hashIndex + 1).trim()
        let apiIndex = links.findIndex((link, index) => !used.has(index) && sameWikiTarget(link, target, anchor, embed) && link.line === line)
        if (apiIndex < 0) apiIndex = links.findIndex((link, index) => !used.has(index) && sameWikiTarget(link, target, anchor, embed))
        if (apiIndex >= 0) used.add(apiIndex)
        const id = nextTarget++
        targets.set(id, apiIndex < 0 ? null : links[apiIndex])
        const display = label || (anchor && !target ? anchor : target.split('/').filter(Boolean).pop() || target) || '本页'
        const text = embed ? `嵌入：${display}` : display
        output.push(`[${escapeMarkdownLabel(text)}](knowgrain-wiki://${id})`)
        position = close + 2
        continue
      }
    }

    if (source[position] === '^' && !isEscapedAt(source, position)) {
      const lineStart = source.lastIndexOf('\n', position - 1) + 1
      const prefix = source.slice(lineStart, position)
      const lineEnd = source.indexOf('\n', position)
      const suffix = source.slice(position, lineEnd < 0 ? source.length : lineEnd)
      const block = suffix.match(/^\^([\p{L}\p{N}_-]+)[ \t]*$/u)
      if (block && prefix.trim() && /[ \t]$/.test(prefix)) {
        output.push(`[​](knowgrain-anchor://${encodeURIComponent(headingSlug(block[1]))})`)
        position += suffix.length
        continue
      }
    }

    const char = source[position]
    output.push(char)
    if (char === '\n') line += 1
    position += 1
  }

  return { markdown: output.join(''), targets }
}

/** Prevent stale reads from crossing selection, save, or reload boundaries. */
export class WikiReadLifecycle {
  private generation = 0
  private sequence = 0
  private pending: WikiReadToken | null = null
  private saveGeneration: number | null = null

  get currentGeneration(): number { return this.generation }

  beginSelection(): void {
    this.generation += 1
    this.invalidateReads()
  }

  beginRead(): WikiReadToken | null {
    if (this.saveGeneration === this.generation || this.pending?.generation === this.generation) return null
    const token = { generation: this.generation, sequence: ++this.sequence }
    this.pending = token
    return token
  }

  isCurrent(token: WikiReadToken): boolean {
    return this.saveGeneration !== token.generation
      && token.generation === this.generation
      && token.sequence === this.sequence
  }

  endRead(token: WikiReadToken): void {
    if (this.pending?.generation === token.generation && this.pending.sequence === token.sequence) this.pending = null
  }

  invalidateReads(): void {
    this.sequence += 1
    this.pending = null
  }

  beginSave(): number {
    this.saveGeneration = this.generation
    this.invalidateReads()
    return this.generation
  }

  endSave(): void { this.saveGeneration = null }
}

export function shouldSyncSavedDraft(currentDraft: string, submittedMarkdown: string): boolean {
  return currentDraft === submittedMarkdown
}
