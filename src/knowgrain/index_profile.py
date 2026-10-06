"""Server-only, read-only seals for the supported raw-text indexing contract.

This module never constructs a Core or calls its initialization/model/storage APIs.
The canonical document contains resolved prompt text: expose only public_summary().
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from pathlib import Path
from types import CodeType
from typing import Any, Literal

from knowgrain.providers import ProviderCallbacks, build_provider_callbacks
from knowgrain.tokenizer_cache import MAX_RESOURCE_BYTES, TOKENIZER_SHA256, cache_path

_ROLES = ("extract", "keyword", "query", "vlm")
_PACKAGES = ("lightrag-hku", "tiktoken", "pypdf", "python-docx", "lxml")
_MODULES = (
    "knowgrain.parsers",
    "knowgrain.providers.callbacks",
    "knowgrain.providers.config",
    "knowgrain.tokenizer_cache",
    "lightrag.lightrag",
    "lightrag.pipeline",
    "lightrag.operate",
    "lightrag.utils",
    "lightrag.utils_pipeline",
    "lightrag.chunker.token_size",
    "lightrag.parser.routing",
    "lightrag.constants",
    "lightrag.prompt",
    "lightrag.addon_params",
    "lightrag.llm_roles",
)
_CONTENT_KNOBS = (
    "chunk_token_size",
    "chunk_overlap_token_size",
    "embedding_token_limit",
    "embedding_chunk_overlap_token_size",
)
_GRAPH_KNOBS = (
    "entity_extract_max_gleaning",
    "entity_extract_max_records",
    "entity_extract_max_entities",
    "force_llm_summary_on_merge",
    "summary_max_tokens",
    "summary_context_size",
    "summary_length_recommended",
    "max_source_ids_per_entity",
    "max_source_ids_per_relation",
    "max_file_paths",
    "source_ids_limit_method",
    "file_path_more_placeholder",
    "entity_extraction_use_json",
    "enable_content_headings",
)
_OPERATIONS = (
    "embedding_batch_num",
    "embedding_func_max_async",
    "default_embedding_timeout",
    "llm_model_max_async",
    "default_llm_timeout",
    "max_parallel_insert",
    "enable_llm_cache",
    "enable_llm_cache_for_entity_extract",
)


class ProfileValidationError(ValueError):
    """A safe diagnostic containing schema paths, never rejected values."""


class ProfileMismatchError(ValueError):
    def __init__(self, comparison: ProfileComparison):
        self.comparison = comparison
        super().__init__("Index profile mismatch: " + ", ".join(comparison.paths))


@dataclass(frozen=True)
class ModelRevision:
    provider: str
    base_url: str
    model: str
    observed_revision: str | None = None
    provenance: Literal["unavailable", "ollama-tag", "operator"] = "unavailable"


@dataclass(frozen=True)
class ProfileComparison:
    paths: tuple[str, ...]
    categories: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.paths


def _fail(path: str) -> None:
    raise ProfileValidationError("Unsupported or invalid index profile field: " + path)


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


# Closed schema. A tuple is a union; '?' keys are explicitly optional.
_SIZE = {"?chunk_token_size": int, "?chunk_overlap_token_size": int}
_CHUNK = {
    "chunk_token_size": int,
    "?chunk_overlap_token_size": int,
    "fixed_token": {
        **_SIZE,
        "split_by_character": (str, type(None)),
        "split_by_character_only": bool,
    },
    "recursive_character": {**_SIZE, "separators": [str]},
    "semantic_vector": {
        **_SIZE,
        "breakpoint_threshold_type": str,
        "breakpoint_threshold_amount": (float, int, type(None)),
        "buffer_size": int,
        "sentence_split_regex": str,
    },
    "paragraph_semantic": _SIZE,
}
_REVISION = {
    "provider": str,
    "base_url": str,
    "model": str,
    "observed_revision": (str, type(None)),
    "provenance": str,
}
_ROLE = {
    "provider": str,
    "base_url": str,
    "model": str,
    "timeout_seconds": (float, int),
    "context_size": int,
}
_EMBED = {
    "provider": str,
    "base_url": str,
    "model": str,
    "timeout_seconds": (float, int),
    "dimension": int,
    "max_token_size": int,
    "document_prefix": str,
    "query_prefix": str,
    "send_dimensions": bool,
}
_SAMPLING = {
    "?temperature": (int, float),
    "?top_p": (int, float),
    "?seed": int,
    "?stop": (str, list),
    "?max_tokens": int,
}
_SCHEMA = {
    "schema_version": int,
    "implementation": {
        "packages": {key: str for key in _PACKAGES},
        "sources": {key: str for key in _MODULES},
        "prompts_sha256": str,
        "graph_prompts_sha256": str,
    },
    "content": {
        "parser_version": str,
        "segment_contract": str,
        "ingestion_contract": str,
        "chunk_id_contract": str,
        "tokenizer": {
            "class": str,
            "model": str,
            "encoding": str,
            "resource_sha256": str,
            "encoding_sha256": str,
        },
        "chunker": _CHUNK,
        "legacy_options": {
            "chunk_token_size": int,
            "chunk_overlap_token_size": int,
            "split_by_character": (str, type(None)),
            "split_by_character_only": bool,
        },
        "limits": {key: int for key in _CONTENT_KNOBS},
    },
    "embedding": {
        "config": _EMBED,
        "revision": _REVISION,
        "supports_asymmetric": bool,
        "core_send_dimensions": bool,
        "server_truncate": bool,
    },
    "graph": {
        "knobs": {
            key: (
                bool
                if key in {"entity_extraction_use_json", "enable_content_headings"}
                else str
                if key in {"source_ids_limit_method", "file_path_more_placeholder"}
                else int
            )
            for key in _GRAPH_KNOBS
        },
        "live_max_extract_input_tokens": int,
        "language": str,
        "prompt": {
            "entity_types_guidance": str,
            "entity_extraction_examples": [str],
            "entity_extraction_json_examples": [str],
        },
        "constants": {
            "entity_name_max_length": int,
            "entity_name_max_bytes": int,
            "max_section_context_tokens": int,
        },
    },
    "llm": {
        "config": _ROLE,
        "revision": _REVISION,
        "roles": {
            role: {"sampling": _SAMPLING, "max_async": int, "timeout": int} for role in _ROLES
        },
    },
    "operations": {key: bool if key.startswith("enable_") else int for key in _OPERATIONS},
}


def _validate(value: Any, schema: Any, path: str = "profile") -> None:
    if isinstance(schema, dict):
        if type(value) is not dict:
            _fail(path)
        allowed = {key.lstrip("?") for key in schema}
        if set(value) - allowed:
            _fail(path + ".unknown")
        for key, child in schema.items():
            name = key.lstrip("?")
            if name not in value:
                if key.startswith("?"):
                    continue
                _fail(path + "." + name)
            _validate(value[name], child, path + "." + name)
    elif isinstance(schema, list):
        if type(value) is not list:
            _fail(path)
        for child in value:
            _validate(child, schema[0], path)
    else:
        types = schema if isinstance(schema, tuple) else (schema,)
        if type(value) not in types or (type(value) is float and not math.isfinite(value)):
            _fail(path)
        if type(value) is list and any(type(item) is not str for item in value):
            _fail(path)


def _revision(config: Any, revision: ModelRevision | None, path: str) -> dict[str, Any]:
    if revision is None:
        revision = ModelRevision(config.provider, config.base_url, config.model)
    result = {key: getattr(revision, key) for key in _REVISION}
    _validate(result, _REVISION, path)
    if any(result[key] != getattr(config, key) for key in ("provider", "base_url", "model")):
        _fail(path + ".binding")
    if revision.provenance not in {"unavailable", "ollama-tag", "operator"}:
        _fail(path + ".provenance")
    if (revision.provenance == "unavailable") != (revision.observed_revision is None):
        _fail(path + ".observed_revision")
    if revision.observed_revision is not None and (
        not revision.observed_revision.strip()
        or len(revision.observed_revision) > 512
        or any(ord(c) < 32 for c in revision.observed_revision)
    ):
        _fail(path + ".observed_revision")
    if revision.provenance == "ollama-tag" and config.provider != "ollama":
        _fail(path + ".provenance")
    return result


def _sampling(value: Any, path: str) -> dict[str, Any]:
    _validate(value, _SAMPLING, path)
    for key in ("temperature", "top_p"):
        if key in value and (value[key] < 0 or (key == "top_p" and value[key] > 1)):
            _fail(path + "." + key)
    if "max_tokens" in value and value["max_tokens"] <= 0:
        _fail(path + ".max_tokens")
    return json.loads(_json(value))


def _priority_target(
    wrapper: Any,
    max_async: int,
    timeout: int,
    queue_name: str,
    concurrency_group: str,
    path: str,
) -> Any:
    """Validate the pinned queue's real closure, not its advertised __wrapped__."""
    from lightrag.utils import priority_limit_async_func_call

    def nested_codes(code: CodeType) -> set[CodeType]:
        children = {child for child in code.co_consts if isinstance(child, CodeType)}
        return children | {descendant for child in children for descendant in nested_codes(child)}

    codes = nested_codes(priority_limit_async_func_call.__code__)

    def closure(function: Any, name: str) -> dict[str, Any]:
        if (
            not inspect.isfunction(function)
            or function.__code__ not in codes
            or function.__code__.co_name != name
        ):
            _fail(path)
        return inspect.getclosurevars(function).nonlocals

    waiting = closure(wrapper, "wait_func")
    ensuring = closure(waiting["ensure_workers"], "ensure_workers")
    creating = closure(ensuring["_create_worker_task"], "_create_worker_task")
    worker = closure(creating["worker"], "worker")
    limited = closure(creating["limited_worker"], "limited_worker")
    target = worker["func"]
    if limited["func"] is not target or getattr(wrapper, "__wrapped__", None) is not target:
        _fail(path)
    expected = {
        "max_size": max_async,
        "llm_timeout": timeout,
        "max_execution_timeout": timeout * 2,
        "max_task_duration": timeout * 2 + 15,
        "max_queue_size": 1000,
        "queue_name": queue_name,
    }
    if any(ensuring.get(key) != value for key, value in expected.items()):
        _fail(path)
    if (
        waiting.get("cleanup_timeout") != 2.0
        or closure(ensuring["_resolve_mode"], "_resolve_mode").get("concurrency_group")
        != concurrency_group
    ):
        _fail(path)

    # All reachable queue functions must be the fixed implementation. The provider
    # is the only permitted leaf outside that tree and is checked by the caller.
    pending = [wrapper, wrapper.shutdown, wrapper.get_queue_stats]
    seen: set[int] = set()
    while pending:
        function = pending.pop()
        if id(function) in seen:
            continue
        seen.add(id(function))
        if not inspect.isfunction(function) or function.__code__ not in codes:
            _fail(path)
        for name, value in inspect.getclosurevars(function).nonlocals.items():
            if name == "func":
                if value is not target:
                    _fail(path)
            elif inspect.isfunction(value):
                pending.append(value)
    return target


def _validate_payload(payload: dict[str, Any]) -> None:
    _validate(payload, _SCHEMA)
    if payload["schema_version"] != 1:
        _fail("schema_version")
    from knowgrain.providers import EmbeddingRoleConfig, LLMRoleConfig

    for name, cls in (("embedding", EmbeddingRoleConfig), ("llm", LLMRoleConfig)):
        try:
            config = cls(**payload[name]["config"])
        except ValueError:
            _fail(name + ".config")
        _revision(config, ModelRevision(**payload[name]["revision"]), name + ".revision")
    for role, data in payload["llm"]["roles"].items():
        _sampling(data["sampling"], "llm.roles." + role + ".sampling")
    content = payload["content"]
    literals = {
        "parser_version": "1",
        "segment_contract": "knowgrain-parsed-segment-v1:ordered-text/page/heading",
        "ingestion_contract": "parse_document->ainsert(rawtext);process_options='';legacy6arg",
        "chunk_id_contract": "doc_id-chunk-order-index;utils_pipeline.build_chunks_dict-v1",
    }
    for key, expected in literals.items():
        if content[key] != expected:
            _fail("content." + key)
    tokenizer = content["tokenizer"]
    if (
        tokenizer["class"] != "lightrag.utils.TiktokenTokenizer"
        or tokenizer["model"] != "gpt-4o"
        or tokenizer["encoding"] != "o200k_base"
        or tokenizer["resource_sha256"] != TOKENIZER_SHA256
    ):
        _fail("content.tokenizer")
    embedding = payload["embedding"]
    if (
        embedding["supports_asymmetric"] is not True
        or embedding["server_truncate"] is not False
        or embedding["core_send_dimensions"] != embedding["config"]["send_dimensions"]
        or content["limits"]["embedding_token_limit"] != embedding["config"]["max_token_size"]
    ):
        _fail("embedding.contract")
    if payload["graph"]["knobs"]["source_ids_limit_method"] not in {"FIFO", "KEEP"}:
        _fail("graph.knobs.source_ids_limit_method")

    def bounds(value: Any, path: str) -> None:
        if type(value) is dict:
            for key, child in value.items():
                bounds(child, path + "." + key)
        elif type(value) is int and value < 0:
            _fail(path)

    for key in ("content", "graph", "operations"):
        bounds(payload[key], key)
    for key, value in payload["operations"].items():
        if type(value) is int and value <= 0:
            _fail("operations." + key)
    for role, data in payload["llm"]["roles"].items():
        if data["max_async"] <= 0 or data["timeout"] <= 0:
            _fail("llm.roles." + role)
    for key, value in content["limits"].items():
        if "overlap" not in key and value <= 0:
            _fail("content.limits." + key)
    chunker = content["chunker"]
    for strategy in ("fixed_token", "recursive_character", "paragraph_semantic", "semantic_vector"):
        options = chunker[strategy]
        size = options.get("chunk_token_size", chunker["chunk_token_size"])
        overlap = options.get(
            "chunk_overlap_token_size", content["limits"]["chunk_overlap_token_size"]
        )
        if size <= 0 or overlap < 0 or overlap >= size:
            _fail("content.chunker." + strategy)
    if chunker["semantic_vector"]["breakpoint_threshold_type"] not in {
        "percentile",
        "standard_deviation",
        "interquartile",
        "gradient",
    }:
        _fail("content.chunker.semantic_vector.breakpoint_threshold_type")
    if chunker["semantic_vector"]["buffer_size"] < 0:
        _fail("content.chunker.semantic_vector.buffer_size")
    legacy = content["legacy_options"]
    fixed = chunker["fixed_token"]
    expected_legacy = {
        "chunk_token_size": fixed.get("chunk_token_size", chunker["chunk_token_size"]),
        "chunk_overlap_token_size": fixed.get(
            "chunk_overlap_token_size", content["limits"]["chunk_overlap_token_size"]
        ),
        "split_by_character": fixed["split_by_character"],
        "split_by_character_only": fixed["split_by_character_only"],
    }
    if (
        legacy != expected_legacy
        or chunker["chunk_token_size"] != content["limits"]["chunk_token_size"]
        or legacy["chunk_token_size"] <= 0
        or legacy["chunk_overlap_token_size"] >= legacy["chunk_token_size"]
    ):
        _fail("content.legacy_options")
    for digest in (
        *payload["implementation"]["sources"].values(),
        payload["implementation"]["prompts_sha256"],
        payload["implementation"]["graph_prompts_sha256"],
        payload["content"]["tokenizer"]["encoding_sha256"],
        payload["content"]["tokenizer"]["resource_sha256"],
    ):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            _fail("sha256")


@dataclass(frozen=True, slots=True)
class IndexProfile:
    _canonical_json: str = field(repr=False)
    _bindings: tuple[Any, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            payload = json.loads(self._canonical_json, object_pairs_hook=_closed_pairs)
            _validate_payload(payload)
            canonical = _json(payload)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ProfileValidationError):
                raise
            raise ProfileValidationError("Invalid index profile JSON") from None
        object.__setattr__(self, "_canonical_json", canonical)

    @classmethod
    def from_canonical_json(cls, value: str) -> IndexProfile:
        return cls(value)

    def to_canonical_json(self) -> str:
        return self._canonical_json

    @classmethod
    def capture(
        cls,
        rag: Any,
        callbacks: ProviderCallbacks,
        tokenizer_cache_dir: Path,
        model_revisions: dict[str, ModelRevision | None] | None = None,
    ) -> IndexProfile:
        try:
            return cls._capture(rag, callbacks, tokenizer_cache_dir, model_revisions)
        except ProfileValidationError:
            raise
        except (OSError, AttributeError, TypeError, KeyError, ValueError):
            raise ProfileValidationError(
                "Unsupported or unavailable index profile inputs"
            ) from None

    @classmethod
    def _capture(
        cls,
        rag: Any,
        callbacks: ProviderCallbacks,
        tokenizer_cache_dir: Path,
        model_revisions: dict[str, ModelRevision | None] | None = None,
    ) -> IndexProfile:
        from lightrag import constants
        from lightrag.chunker import chunking_by_token_size
        from lightrag.prompt import PROMPTS
        from lightrag.utils import EmbeddingFunc, TiktokenTokenizer, get_env_value

        from knowgrain.parsers import ParsedDocument

        if type(callbacks) is not ProviderCallbacks:
            _fail("callbacks.factory_binding")
        factory_codes = {
            code.co_name: code
            for code in build_provider_callbacks.__code__.co_consts
            if isinstance(code, CodeType) and code.co_name in {"llm", "embed"}
        }
        for role, function, config_name in (
            ("llm", callbacks.llm, "llm_config"),
            ("embed", callbacks.embed, "embedding_config"),
        ):
            if (
                not inspect.isfunction(function)
                or function.__code__ is not factory_codes.get(role)
                or inspect.getclosurevars(function).nonlocals.get(config_name)
                is not getattr(callbacks, config_name)
            ):
                _fail("callbacks." + role + ".factory_binding")
        revisions = model_revisions or {}
        if set(revisions) - {"llm", "embedding"}:
            _fail("model_revisions.unknown")
        if (
            rag.llm_model_func is not callbacks.llm
            or rag.role_llm_configs
            or rag.llm_model_name != callbacks.llm_config.model
        ):
            _fail("llm.binding")
        if rag.chunking_func is not chunking_by_token_size:
            _fail("content.chunking_func")
        tokenizer = rag.tokenizer
        if type(tokenizer) is not TiktokenTokenizer or tokenizer.model_name != "gpt-4o":
            _fail("content.tokenizer")
        if tokenizer.tokenizer.name != "o200k_base":
            _fail("content.tokenizer.encoding")
        embedding = rag.embedding_func
        if (
            type(embedding) is not EmbeddingFunc
            or _priority_target(
                embedding.func,
                rag.embedding_func_max_async,
                rag.default_embedding_timeout,
                "Embedding func",
                "embedding",
                "embedding.queue_binding",
            )
            is not callbacks.embed
            or embedding.embedding_dim != callbacks.embedding_config.dimension
            or embedding.max_token_size != callbacks.embedding_config.max_token_size
            or embedding.send_dimensions != callbacks.embedding_config.send_dimensions
            or embedding.supports_asymmetric is not True
            or rag.embedding_token_limit != embedding.max_token_size
        ):
            _fail("embedding.binding")
        vector_bindings = []
        for name in ("chunks_vdb", "entities_vdb", "relationships_vdb"):
            storage = getattr(rag, name)
            if storage.embedding_func is not embedding:
                _fail("embedding." + name + ".binding")
            vector_bindings.extend((storage, storage.embedding_func))
        if rag._addon_params_dirty or (
            rag._cached_entity_extraction_use_json != rag.entity_extraction_use_json
        ):
            _fail("graph.prompt_cache")
        addon = rag.addon_params
        if set(addon) - {"language", "entity_type_prompt_file", "entity_types_guidance", "chunker"}:
            _fail("addon.unknown")
        language = addon.get("language")
        if language != rag._resolved_summary_language:
            _fail("graph.language_cache")
        prompt = rag._entity_extraction_prompt_profile
        if (
            "entity_types_guidance" in addon
            and addon["entity_types_guidance"] != prompt["entity_types_guidance"]
        ):
            _fail("graph.prompt_cache")
        chunker = json.loads(_json(addon["chunker"]))
        _validate(chunker, _CHUNK, "content.chunker")
        fixed = chunker["fixed_token"]
        if rag.vlm_process_enable:
            _fail("vlm_process_enable")
        if rag.embedding_cache_config != {
            "enabled": False,
            "similarity_threshold": 0.95,
            "use_llm_check": False,
        }:
            _fail("embedding_cache_config")
        resource = cache_path(tokenizer_cache_dir)
        with resource.open("rb") as stream:
            content = stream.read(MAX_RESOURCE_BYTES + 1)
        resource_sha = hashlib.sha256(content).hexdigest()
        if len(content) > MAX_RESOURCE_BYTES or resource_sha != TOKENIZER_SHA256:
            _fail("content.tokenizer.resource_sha256")
        roles = {}
        role_bindings = []
        if set(rag._role_llm_states) != set(_ROLES):
            _fail("llm.roles")
        for role in _ROLES:
            state = rag._role_llm_states[role]
            if state.raw_func is not callbacks.llm or state.metadata.get("is_cross_provider"):
                _fail("llm.roles." + role + ".binding")
            # The supported factory path uses the default Core cache namespace.
            # Read its actual inputs without calling the mutating global-config helper.
            metadata = state.metadata
            if (
                type(metadata) is not dict
                or set(metadata) - {"binding", "model", "host", "is_cross_provider"}
                or metadata.get("binding") is not None
                or metadata.get("host") is not None
                or metadata.get("model") not in (None, "", callbacks.llm_config.model)
                or type(metadata.get("is_cross_provider", False)) is not bool
            ):
                _fail("llm.roles." + role + ".cache_identity")
            kwargs = state.kwargs if state.kwargs is not None else rag.llm_model_kwargs
            sampling = _sampling(kwargs, "llm.roles." + role + ".sampling")
            max_async = state.max_async if state.max_async is not None else rag.llm_model_max_async
            timeout = state.timeout if state.timeout is not None else rag.default_llm_timeout
            from lightrag.llm_roles import ROLES_BY_NAME

            actual = _priority_target(
                state.wrapped,
                max_async,
                timeout,
                ROLES_BY_NAME[role].queue_name,
                "llm:" + role,
                "llm.roles." + role + ".queue_binding",
            )
            if (
                not isinstance(actual, partial)
                or actual.func is not callbacks.llm
                or actual.args
                or actual.keywords.get("hashing_kv") is not rag.llm_response_cache
            ):
                _fail("llm.roles." + role + ".wrapped_binding")
            actual_sampling = {
                key: value for key, value in actual.keywords.items() if key != "hashing_kv"
            }
            _sampling(actual_sampling, "llm.roles." + role + ".wrapped_sampling")
            if actual_sampling != sampling:
                _fail("llm.roles." + role + ".wrapped_sampling")
            role_bindings.extend((state.wrapped, actual))
            roles[role] = {
                "sampling": sampling,
                "max_async": max_async,
                "timeout": timeout,
            }
        sources = {}
        for name in _MODULES:
            module = importlib.import_module(name)
            sources[name] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        payload = {
            "schema_version": 1,
            "implementation": {
                "packages": {name: version(name) for name in _PACKAGES},
                "sources": sources,
                "prompts_sha256": _sha(PROMPTS),
                "graph_prompts_sha256": _sha(
                    {
                        key: value
                        for key, value in PROMPTS.items()
                        if key.startswith(("entity_", "DEFAULT_"))
                        or key == "summarize_entity_descriptions"
                    }
                ),
            },
            "content": {
                "parser_version": ParsedDocument.__dataclass_fields__["parser_version"].default,
                "segment_contract": "knowgrain-parsed-segment-v1:ordered-text/page/heading",
                "ingestion_contract": "parse_document->ainsert(rawtext);process_options='';legacy6arg",
                "chunk_id_contract": "doc_id-chunk-order-index;utils_pipeline.build_chunks_dict-v1",
                "tokenizer": {
                    "class": "lightrag.utils.TiktokenTokenizer",
                    "model": "gpt-4o",
                    "encoding": tokenizer.tokenizer.name,
                    "resource_sha256": resource_sha,
                    "encoding_sha256": _sha(
                        {
                            "ranks": sorted(
                                (key.hex(), value)
                                for key, value in tokenizer.tokenizer._mergeable_ranks.items()
                            ),
                            "special": tokenizer.tokenizer._special_tokens,
                            "pattern": tokenizer.tokenizer._pat_str,
                        }
                    ),
                },
                "chunker": chunker,
                "legacy_options": {
                    "chunk_token_size": fixed.get("chunk_token_size", chunker["chunk_token_size"]),
                    "chunk_overlap_token_size": fixed.get(
                        "chunk_overlap_token_size", rag.chunk_overlap_token_size
                    ),
                    "split_by_character": fixed.get("split_by_character"),
                    "split_by_character_only": fixed.get("split_by_character_only", False),
                },
                "limits": {key: getattr(rag, key) for key in _CONTENT_KNOBS},
            },
            "embedding": {
                "config": callbacks.embedding_metadata,
                "revision": _revision(
                    callbacks.embedding_config, revisions.get("embedding"), "embedding.revision"
                ),
                "supports_asymmetric": embedding.supports_asymmetric,
                "core_send_dimensions": embedding.send_dimensions,
                "server_truncate": False,
            },
            "graph": {
                "knobs": {key: getattr(rag, key) for key in _GRAPH_KNOBS},
                "live_max_extract_input_tokens": get_env_value(
                    "MAX_EXTRACT_INPUT_TOKENS", constants.DEFAULT_MAX_EXTRACT_INPUT_TOKENS, int
                ),
                "language": language,
                "prompt": prompt,
                "constants": {
                    "entity_name_max_length": constants.DEFAULT_ENTITY_NAME_MAX_LENGTH,
                    "entity_name_max_bytes": constants.DEFAULT_ENTITY_NAME_MAX_BYTES,
                    "max_section_context_tokens": constants.DEFAULT_MAX_SECTION_CONTEXT_TOKENS,
                },
            },
            "llm": {
                "config": callbacks.llm_metadata,
                "revision": _revision(callbacks.llm_config, revisions.get("llm"), "llm.revision"),
                "roles": roles,
            },
            "operations": {key: getattr(rag, key) for key in _OPERATIONS},
        }
        return cls(
            _json(payload),
            (
                rag,
                tokenizer,
                tokenizer.tokenizer,
                rag.chunking_func,
                embedding.func,
                callbacks.llm,
                callbacks.embed,
                *role_bindings,
                *vector_bindings,
            ),
        )

    @property
    def content_embedding_fingerprint(self) -> str:
        data = json.loads(self._canonical_json)
        embedding = data["embedding"]
        embedding["config"].pop("timeout_seconds")
        implementation = data["implementation"]
        implementation.pop("prompts_sha256")
        implementation.pop("graph_prompts_sha256")
        return _sha(
            {"implementation": implementation, "content": data["content"], "embedding": embedding}
        )

    @property
    def graph_write_fingerprint(self) -> str:
        data = json.loads(self._canonical_json)
        llm = data["llm"]
        llm["config"].pop("timeout_seconds")
        implementation = data["implementation"]
        implementation.pop("prompts_sha256")
        return _sha(
            {
                "implementation": implementation,
                "graph": data["graph"],
                "llm_config": llm["config"],
                "llm_revision": llm["revision"],
                "extract_sampling": llm["roles"]["extract"]["sampling"],
            }
        )

    @property
    def llm_fingerprint(self) -> str:
        return _sha(json.loads(self._canonical_json)["llm"])

    @property
    def snapshot_fingerprint(self) -> str:
        return hashlib.sha256(self._canonical_json.encode()).hexdigest()

    def public_summary(self) -> dict[str, Any]:
        data = json.loads(self._canonical_json)
        return {
            "content_embedding_fingerprint": self.content_embedding_fingerprint,
            "graph_write_fingerprint": self.graph_write_fingerprint,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "models": {
                name: {key: data[name]["config"][key] for key in ("provider", "model")}
                for name in ("llm", "embedding")
            },
        }

    def compare(
        self,
        current: IndexProfile | Any,
        callbacks: ProviderCallbacks | None = None,
        tokenizer_cache_dir: Path | None = None,
        model_revisions: dict[str, ModelRevision | None] | None = None,
    ) -> ProfileComparison:
        if not isinstance(current, IndexProfile):
            if callbacks is None or tokenizer_cache_dir is None:
                _fail("comparison.inputs")
            current = self.capture(current, callbacks, tokenizer_cache_dir, model_revisions)
        paths: list[str] = []
        _diff(json.loads(self._canonical_json), json.loads(current._canonical_json), "", paths)
        if self._bindings and current._bindings:
            names = (
                "core",
                "tokenizer",
                "encoding",
                "chunking_func",
                "embedding_wrapper",
                "llm_callback",
                "embedding_callback",
                *(name for role in _ROLES for name in (role + "_wrapper", role + "_partial")),
            )
            names += tuple(
                name
                for storage in ("chunks_vdb", "entities_vdb", "relationships_vdb")
                for name in (storage, storage + "_embedding")
            )
            for name, previous, present in zip(
                names, self._bindings, current._bindings, strict=True
            ):
                if previous is not present:
                    paths.append("runtime_identity." + name)
        categories = []
        for name in ("content_embedding", "graph_write", "llm", "snapshot"):
            if getattr(self, name + "_fingerprint") != getattr(current, name + "_fingerprint"):
                categories.append(name)
        return ProfileComparison(tuple(paths), tuple(categories))

    def assert_matches(
        self,
        current: IndexProfile | Any,
        callbacks: ProviderCallbacks | None = None,
        tokenizer_cache_dir: Path | None = None,
        model_revisions: dict[str, ModelRevision | None] | None = None,
    ) -> None:
        comparison = self.compare(current, callbacks, tokenizer_cache_dir, model_revisions)
        if not comparison.matches:
            raise ProfileMismatchError(comparison)


def _closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_key")
        result[key] = value
    return result


def _diff(left: Any, right: Any, path: str, result: list[str]) -> None:
    if type(left) is dict and type(right) is dict:
        for key in sorted(set(left) | set(right)):
            child = path + "." + key if path else key
            if key not in left or key not in right:
                result.append(child)
            else:
                _diff(left[key], right[key], child, result)
    elif left != right:
        result.append(path)
