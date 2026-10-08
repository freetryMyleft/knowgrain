"""Pure expectations for the sealed LightRAG 1.5.7 RAW ingestion path.

No Vault reads, Core construction, parser registry, models, stores or audit receipts.
Custom parsers and structured ingestion are intentionally outside this contract.
The canonical snapshot and document bodies are server-only values.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import inspect
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from knowgrain.config import MAX_UPLOAD_BYTES
from knowgrain.index_profile import IndexProfile
from knowgrain.parsers import parse_document
from knowgrain.tokenizer_cache import MAX_RESOURCE_BYTES, cache_path

if TYPE_CHECKING:
    from lightrag.utils import TiktokenTokenizer


class ContentManifestError(ValueError):
    """Static, safe failure without document text, paths or configuration values."""


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExpectedParsedSegment:
    text: str = field(repr=False)
    page: int | None = None
    heading: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ExpectedChunk:
    id: str
    content: str = field(repr=False)
    tokens: int
    chunk_order_index: int
    full_doc_id: str
    file_path: str = field(repr=False)
    split_type: str | None = None
    split_part: int | None = None
    split_total: int | None = None
    llm_cache_list: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ExpectedContentManifest:
    revision_id: UUID
    source_sha256: str
    profile_snapshot: str = field(repr=False)
    profile_snapshot_fingerprint: str
    content_fingerprint: str
    parsed_text: str = field(repr=False)
    parsed_sha256: str
    segments: tuple[ExpectedParsedSegment, ...] = field(repr=False)
    core_text: str = field(repr=False)
    core_sha256: str
    dedup_md5: str
    raw_format: str
    vault_path: str = field(repr=False)
    canonical_file_path: str = field(repr=False)
    chunk_options: str = field(repr=False)
    chunks: tuple[ExpectedChunk, ...] = field(repr=False)
    digest: str


def _verify_implementation(
    payload: dict[str, Any], tokenizer: TiktokenTokenizer, cache: Path
) -> None:
    from lightrag.prompt import PROMPTS
    from lightrag.utils import TiktokenTokenizer
    from tiktoken import Encoding

    implementation = payload["implementation"]
    if implementation["packages"]["lightrag-hku"] != "1.5.7":
        raise ContentManifestError("Unsupported content implementation")
    for name, sealed in implementation["packages"].items():
        if version(name) != sealed:
            raise ContentManifestError("Content implementation package mismatch")
    for name, sealed in implementation["sources"].items():
        module = importlib.import_module(name)
        if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != sealed:
            raise ContentManifestError("Content implementation source mismatch")
    graph_prompts = {
        key: value
        for key, value in PROMPTS.items()
        if key.startswith(("entity_", "DEFAULT_")) or key == "summarize_entity_descriptions"
    }
    if (
        _sha(_json(PROMPTS)) != implementation["prompts_sha256"]
        or _sha(_json(graph_prompts)) != implementation["graph_prompts_sha256"]
    ):
        raise ContentManifestError("Content implementation prompt mismatch")
    sealed = payload["content"]["tokenizer"]
    if (
        type(tokenizer) is not TiktokenTokenizer
        or tokenizer.model_name != sealed["model"]
        or type(tokenizer.tokenizer) is not Encoding
        or tokenizer.tokenizer.name != sealed["encoding"]
    ):
        raise ContentManifestError("Content tokenizer identity mismatch")
    # Reject instance-level replacements of any implementation method used by
    # encoding and splitting; equal ranks alone cannot seal a forged encoder.
    for instance in (tokenizer, tokenizer.tokenizer):
        for cls in type(instance).__mro__:
            for name, method in vars(cls).items():
                if name.startswith("__") or not inspect.isfunction(method):
                    continue
                bound = getattr(instance, name)
                expected = getattr(type(instance), name)
                if (
                    getattr(bound, "__self__", None) is not instance
                    or getattr(bound, "__func__", None) is not expected
                ):
                    raise ContentManifestError("Content tokenizer method mismatch")
    with cache_path(cache).open("rb") as resource:
        data = resource.read(MAX_RESOURCE_BYTES + 1)
    if (
        len(data) > MAX_RESOURCE_BYTES
        or hashlib.sha256(data).hexdigest() != sealed["resource_sha256"]
    ):
        raise ContentManifestError("Content tokenizer resource mismatch")
    encoding = tokenizer.tokenizer
    actual = {
        "ranks": sorted((key.hex(), value) for key, value in encoding._mergeable_ranks.items()),
        "special": encoding._special_tokens,
        "pattern": encoding._pat_str,
    }
    if _sha(_json(actual)) != sealed["encoding_sha256"]:
        raise ContentManifestError("Content tokenizer encoding mismatch")


def build_expected_content_manifest(
    *,
    profile: IndexProfile,
    revision_id: UUID,
    filename: str,
    vault_path: str,
    original_bytes: bytes,
    expected_source_sha256: str,
    tokenizer: TiktokenTokenizer,
    tokenizer_cache_dir: Path,
) -> ExpectedContentManifest:
    """Reproduce fixed RAW passthrough and final stored chunks from original bytes.

    Every option comes from the seal. The resource and implementation checks are
    independent of live Core state and cannot download a missing tokenizer.
    Failure diagnostics discard third-party exceptions that may carry content.
    """
    try:
        return _build(
            profile,
            revision_id,
            filename,
            vault_path,
            original_bytes,
            expected_source_sha256,
            tokenizer,
            tokenizer_cache_dir,
        )
    except ContentManifestError:
        raise
    except Exception:
        # BaseException (including cancellation/KeyboardInterrupt) propagates.
        raise ContentManifestError("Unable to build expected content manifest") from None


def _build(
    profile, revision_id, filename, vault_path, original_bytes, source_sha, tokenizer, cache
):
    from lightrag.chunker import chunking_by_token_size
    from lightrag.constants import FULL_DOCS_FORMAT_RAW
    from lightrag.parser.routing import resolve_chunk_options
    from lightrag.utils import (
        enforce_chunk_token_limit_before_embedding,
        sanitize_text_for_encoding,
    )
    from lightrag.utils_pipeline import (
        build_chunks_dict_from_chunking_result,
        compute_text_content_hash,
        normalize_document_file_path,
    )

    if (
        type(profile) is not IndexProfile
        or type(revision_id) is not UUID
        or type(filename) is not str
        or type(vault_path) is not str
        or type(original_bytes) is not bytes
        or len(original_bytes) > MAX_UPLOAD_BYTES
        or type(source_sha) is not str
        or len(source_sha) != 64
        or any(c not in "0123456789abcdef" for c in source_sha)
        or hashlib.sha256(original_bytes).hexdigest() != source_sha
    ):
        raise ContentManifestError("Invalid original revision inputs")
    snapshot = profile.to_canonical_json()
    payload = json.loads(snapshot)
    _verify_implementation(payload, tokenizer, cache)
    parsed = parse_document(filename, original_bytes)
    segments = tuple(ExpectedParsedSegment(s.text, s.page, s.heading) for s in parsed.segments)
    core_text = sanitize_text_for_encoding(parsed.text)
    limits = payload["content"]["limits"]
    options = resolve_chunk_options({"chunker": payload["content"]["chunker"]}, process_options="")
    fixed = options["fixed_token"]
    chunking = chunking_by_token_size(
        tokenizer,
        core_text,
        fixed.get("split_by_character"),
        fixed.get("split_by_character_only", False),
        fixed.get("chunk_overlap_token_size", limits["chunk_overlap_token_size"]),
        int(fixed.get("chunk_token_size", options["chunk_token_size"])),
        _emit_source_span=True,
    )
    chunking = enforce_chunk_token_limit_before_embedding(
        chunking,
        tokenizer,
        limits["embedding_token_limit"],
        overlap_tokens=limits["embedding_chunk_overlap_token_size"],
        source_content=core_text,
    )
    canonical_path = normalize_document_file_path(vault_path)
    stored = build_chunks_dict_from_chunking_result(
        chunking,
        doc_id=str(revision_id),
        file_path=canonical_path,
    )
    chunks = tuple(
        ExpectedChunk(
            id=key,
            content=chunk["content"],
            tokens=chunk["tokens"],
            chunk_order_index=chunk["chunk_order_index"],
            full_doc_id=chunk["full_doc_id"],
            file_path=chunk["file_path"],
            split_type=chunk.get("split_type"),
            split_part=chunk.get("split_part"),
            split_total=chunk.get("split_total"),
            llm_cache_list=tuple(chunk["llm_cache_list"]),
        )
        for key, chunk in stored.items()
    )
    parsed_sha = _sha(parsed.text)
    core_sha = _sha(core_text)
    dedup = compute_text_content_hash(core_text)
    digest_data = {
        "contract": "knowgrain-expected-content-manifest-v1",
        "revision_id": str(revision_id),
        "source_sha256": source_sha,
        "profile_snapshot_fingerprint": profile.snapshot_fingerprint,
        "content_fingerprint": profile.content_embedding_fingerprint,
        "parsed_sha256": parsed_sha,
        "segments": [{"text": s.text, "page": s.page, "heading": s.heading} for s in segments],
        "core_sha256": core_sha,
        "dedup_md5": dedup,
        "raw_format": FULL_DOCS_FORMAT_RAW,
        "vault_path": vault_path,
        "canonical_file_path": canonical_path,
        "chunk_options": options,
        "chunks": [{"id": key, **value} for key, value in stored.items()],
    }
    return ExpectedContentManifest(
        revision_id,
        source_sha,
        snapshot,
        profile.snapshot_fingerprint,
        profile.content_embedding_fingerprint,
        parsed.text,
        parsed_sha,
        segments,
        core_text,
        core_sha,
        dedup,
        FULL_DOCS_FORMAT_RAW,
        vault_path,
        canonical_path,
        _json(options),
        chunks,
        _sha(_json(digest_data)),
    )
