import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as contract from '../src/wiki-contract.ts'

function summary(overrides = {}) {
  return {
    page_id: 'page-1',
    vault_path: 'Wiki/Drafts/page-1.md',
    title: 'Page One',
    status: 'draft',
    content_sha256: 'a'.repeat(64),
    updated_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

function detail(overrides = {}) {
  return { ...summary(), markdown: '---\nkg_id: page-1\n---\n# Page One\n', links: [], ...overrides }
}

test('runtime guards reject malformed 2xx payloads and validate conflict current pages', () => {
  assert.equal(contract.isPageSummary(summary()), true)
  assert.equal(contract.isPageSummary(summary({ status: 'archived' })), false)
  assert.equal(contract.isPageDetail(detail()), true)
  assert.equal(contract.isPageDetailFor(detail(), 'page-1'), true)
  assert.equal(contract.isPageDetailFor(detail(), 'page-other'), false)
  assert.equal(contract.isPageDetail(detail({ links: [null] })), false)
  assert.equal(contract.isPageListResponse({ pages: [summary()], issues: [{ vault_path: 'x.md', code: 'bad', detail: 'bad yaml' }] }), true)
  assert.equal(contract.isPageListResponse({ pages: [summary()], issues: [null] }), false)
  assert.equal(contract.isPageListResponse({ pages: [summary(), summary()], issues: [] }), false)
  assert.equal(contract.isBacklinkResponse({ pages: [{ ...summary(), target: 'page-1', anchor: null, line: 2, embed: false }] }), true)
  assert.equal(contract.isBacklinkResponse({ pages: [{ ...summary(), target: 'page-1', anchor: null, line: 0, embed: false }] }), false)
  assert.equal(contract.isConflictDetail({ code: 'hash_changed', message: 'changed', current: detail(), diff: '@@ diff' }), true)
  assert.equal(contract.isConflictDetail({ code: 'hash_changed', message: 'changed', current: { ...detail(), links: [null] }, diff: '@@ diff' }), false)
  assert.equal(contract.isConflictDetail({ code: 'hash_changed', message: 'changed', current: null, diff: 'x'.repeat(64 * 1024 + 1) }), false)
})

test('Wiki token preview matches escaping, comments, and fenced code boundaries', () => {
  const markdown = [
    '<!--',
    '```',
    '[[hidden]]',
    '-->',
    '\\`literal `[[visible]]',
    '\\\\[[even-slashes]]',
    '```',
    '[[inside-fence]]',
    '```',
    '[[outside]]',
  ].join('\n')
  const links = [
    { target: 'visible', anchor: null, label: null, embed: false, line: 5, to_page_id: 'target-visible' },
    { target: 'even-slashes', anchor: null, label: null, embed: false, line: 6, to_page_id: 'target-even' },
    { target: 'outside', anchor: null, label: null, embed: false, line: 10, to_page_id: 'target-outside' },
  ]
  const result = contract.renderWikilinks(markdown, links)
  assert.equal(result.targets.size, 3)
  assert.deepEqual([...result.targets.values()].map((link) => link?.target), ['visible', 'even-slashes', 'outside'])
  assert.match(result.markdown, /\[\[hidden\]\]/)
  assert.match(result.markdown, /\[\[inside-fence\]\]/)
  assert.equal((result.markdown.match(/knowgrain-wiki:\/\//g) || []).length, 3)
})

test('a longer backtick line with info text does not close a fenced code block', () => {
  const markdown = '```\n````python\n[[Target]]\n```\n[[Outside]]'
  const links = [{ target: 'Outside', anchor: null, label: null, embed: false, line: 5, to_page_id: 'target-outside' }]
  const result = contract.renderWikilinks(markdown, links)

  assert.equal(result.targets.size, 1)
  assert.equal([...result.targets.values()][0]?.target, 'Outside')
  assert.match(result.markdown, /\[\[Target\]\]/)
  assert.equal((result.markdown.match(/knowgrain-wiki:\/\//g) || []).length, 1)
})

test('frontmatter accepts YAML end markers and keeps preview line positions', () => {
  const closedWithDashes = '---\r\nkg_id: x\r\n---\r\n# Body\r\n'
  const closedWithDots = '---\r\nkg_id: x\r\n...\r\n# Body\r\n'
  assert.match(contract.stripFrontmatter(closedWithDashes), /^\n\n\n# Body/)
  assert.match(contract.stripFrontmatter(closedWithDots), /^\n\n\n# Body/)
  assert.equal(contract.stripFrontmatter('---\nnot closed\n# Text'), '---\nnot closed\n# Text')
  assert.equal(contract.isEscapedAt('x\\`', 2), true)
  assert.equal(contract.isEscapedAt('x\\\\`', 3), false)
})

test('read lifecycle rejects old A reads after A-B-A and skips reads during same-page saves', () => {
  const lifecycle = new contract.WikiReadLifecycle()
  lifecycle.beginSelection()
  const firstA = lifecycle.beginRead()
  assert.ok(firstA)
  lifecycle.beginSelection()
  lifecycle.beginSelection()
  const secondA = lifecycle.beginRead()
  assert.ok(secondA)
  assert.equal(lifecycle.isCurrent(firstA), false)
  assert.equal(lifecycle.isCurrent(secondA), true)

  const savingGeneration = lifecycle.beginSave()
  assert.equal(savingGeneration, lifecycle.currentGeneration)
  assert.equal(lifecycle.isCurrent(secondA), false)
  assert.equal(lifecycle.beginRead(), null)
  lifecycle.endSave()
  const afterSave = lifecycle.beginRead()
  assert.ok(afterSave)
  assert.equal(lifecycle.isCurrent(afterSave), true)
  assert.equal(contract.shouldSyncSavedDraft('submitted', 'submitted'), true)
  assert.equal(contract.shouldSyncSavedDraft('newer local text', 'submitted'), false)
})
