# M4 Wiki / entity navigation

This node adds read-only, revision-backed navigation; entity summaries are never source evidence. No new database, dependency or persisted graph projection is required. Migration `0009_m4_entity_lookup` adds a leading `evidence_ref.chunk_id` index for the new exact membership lookup; the existing revision-leading unique index cannot cover that filter directly. Apply this additive migration explicitly; readiness requires its schema head.

## API

- `GET /api/v1/wiki/pages/{page_id}/entities` returns `{page_id, content_sha256, binding_current, evidence_current, entities, truncated}`. Manual pages have no automatic binding and an empty entity list. A generated/reviewed page must match its current generation binding hash and all retained evidence must pass the existing current-revision/file verification. Changed pages or stale sources return empty entities and explicit false validity flags. Each entity is `{entity_id, name, entity_type, evidence_ids}`; the ID is SHA-256 of the exact UTF-8 name, and evidence IDs are retained citations belonging to this page. At most 100 entities; indicate truncation.
- `GET /api/v1/graph/entity-pages?name=...` accepts an exact name (1–512 characters, valid Unicode, no control characters) and returns `{entity_id, name, pages, truncated}`. Pages are `{page_id, title, vault_path, content_sha256, evidence_ids}`. Select at most 50 candidate pages plus one overflow sentinel; indicate candidate truncation, including when some candidates fail freshness validation. Every returned page is rechecked through generation_detail and the Core mapping, so replaced proposals and externally edited pages cannot masquerade as current links. Missing Core entities return an empty page list. No URL or path supplied by a browser is opened by the server.

## Core adapter contract

`LightRAGRuntime.entities_for_evidence(evidence: Sequence[Evidence]) -> dict` owns all upstream access on the initialization loop. At most 24 evidence items. Read exact `text_chunks` by chunk ID and require matching full_doc_id (revision UUID), plus content starting with the retained quote. Read `full_entities` by those document IDs for candidate names, cap names at 500 with a truncation flag, then intersect `entity_chunks.chunk_ids` (complete membership, not truncated graph source_id) with the verified evidence chunks. `chunk_entity_relation_graph.get_nodes_batch` supplies name/type metadata only. At most 100 output entities; citations unique and deterministic. Missing/malformed records are excluded; storage exceptions propagate as unavailable. Do not return upstream descriptions, paths, raw objects or query-model output.

`LightRAGRuntime.entity_chunk_ids(name: str) -> tuple[str, ...]` validates a name and reads graph presence + complete entity_chunks membership, capped at 10,000 IDs (fail closed above limit). This is a bounded candidate lookup, not authority: the application service proves the reverse link again with entities_for_evidence.

## Repository / service

`EntityMappingRepository(database).candidate_page_ids(chunk_ids, limit=50)` returns `{page_ids: tuple[UUID, ...], truncated: bool}` using exact PageEvidence → EvidenceRef chunk joins. For reviewed pages select PageGenerationBinding.generation_page_id; without binding use a page's own GeneratedPage only. Never associate a reviewed target with its old superseded manifest. Distinct IDs, deterministic ordering, bounded result. Existing tables are reused with the additive chunk lookup index.

`EntityMappingService(generation, repository, lightrag, wiki)` implements page_entities(page_id) and entity_pages(name). Reuse generation_detail for authoritative page hash/current evidence checks and restore_evidence for immutable snapshots. Handle a page without a manifest as manual (page exists, no binding). Recheck the page hash after the Core read before returning; omit a candidate that changed during assembly. No file writes or model calls. Invalid page IDs yield 404; unsafe/incomplete scans or Core/storage failures yield safe 503. The router holds ApplicationRuntime._runtime_lock so Vault replacement cannot race navigation, with a 60-second operation timeout. Reverse results propagate both candidate-page and Core-entity truncation. Titles and paths retain existing Wiki metadata rules; UI display shortens long labels without modifying metadata or files.

## Ownership / acceptance

- Luna Core: lightrag_runtime.py graph methods and focused tests only.
- Luna mapping: entity_mapping_repository.py, entity_mapping_service.py, entity_mapping_api.py and focused tests only.
- Sol: Runtime/router wiring, Wiki entity UI, frontend guards, documentation and actual Web/Core/PostgreSQL acceptance.

Verify exact revision/chunk membership, same-name unrelated chunks, removed/missing entities, external Wiki edits, superseded proposal bindings and stale evidence. UI entity → current Wiki → evidence should work against the real acceptance Vault, with the reviewed file hash unchanged. Full graph visualization, M5/M6 and third-party providers remain separate scope.
