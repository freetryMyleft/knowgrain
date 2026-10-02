export type WikiEntity = { entity_id: string; name: string; entity_type: string; evidence_ids: string[] }
export type PageEntities = {
  page_id: string; content_sha256: string; binding_current: boolean; evidence_current: boolean
  entities: WikiEntity[]; truncated: boolean
}
export type EntityPage = {
  page_id: string; title: string; vault_path: string; content_sha256: string; evidence_ids: string[]
}
export type EntityPages = { entity_id: string; name: string; pages: EntityPage[]; truncated: boolean }

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const HASH = /^[0-9a-f]{64}$/
function record(v: unknown): v is Record<string, unknown> { return typeof v === 'object' && v !== null && !Array.isArray(v) }
function text(v: unknown, maximum: number): v is string {
  return typeof v === 'string' && v.length > 0 && v.length <= maximum && !/[\u0000-\u001f\u007f]/.test(v)
}
function citations(v: unknown): v is string[] {
  return Array.isArray(v) && v.length > 0 && v.length <= 24
    && v.every(id => typeof id === 'string' && UUID.test(id)) && new Set(v).size === v.length
}
function entity(v: unknown): v is WikiEntity {
  return record(v) && typeof v.entity_id === 'string' && HASH.test(v.entity_id)
    && text(v.name, 1024) && text(v.entity_type, 1024) && citations(v.evidence_ids)
}
export function isPageEntities(v: unknown): v is PageEntities {
  return record(v) && typeof v.page_id === 'string' && UUID.test(v.page_id)
    && typeof v.content_sha256 === 'string' && HASH.test(v.content_sha256)
    && typeof v.binding_current === 'boolean' && typeof v.evidence_current === 'boolean'
    && typeof v.truncated === 'boolean' && Array.isArray(v.entities) && v.entities.length <= 100
    && v.entities.every(entity) && new Set(v.entities.map(e => e.entity_id)).size === v.entities.length
    && ((v.binding_current && v.evidence_current) || v.entities.length === 0)
}
function page(v: unknown): v is EntityPage {
  return record(v) && typeof v.page_id === 'string' && UUID.test(v.page_id)
    && typeof v.title === 'string' && typeof v.vault_path === 'string' && v.vault_path.length > 0
    && typeof v.content_sha256 === 'string' && HASH.test(v.content_sha256) && citations(v.evidence_ids)
}
export function isEntityPages(v: unknown): v is EntityPages {
  return record(v) && typeof v.entity_id === 'string' && HASH.test(v.entity_id) && text(v.name, 1024)
    && typeof v.truncated === 'boolean' && Array.isArray(v.pages) && v.pages.length <= 50 && v.pages.every(page)
    && new Set(v.pages.map(p => p.page_id)).size === v.pages.length
}
