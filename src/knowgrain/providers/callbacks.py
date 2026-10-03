"""Thin non-streaming async role adapters with no ambient credential fallback."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Awaitable, Callable

import httpx
import numpy as np

from .config import EmbeddingRoleConfig, LLMRoleConfig


class ProviderError(RuntimeError):
    """Safe role/provider/status/type diagnostic; never contains response content."""


_INTERNAL = {
    "hashing_kv", "_priority", "_timeout", "_queue_timeout", "token_tracker",
    "enable_llm_cache", "enable_llm_cache_for_entity_extract", "cache_type",
}
_SAMPLING = {"temperature", "top_p", "seed", "stop", "max_tokens"}


def _fail(role: str, provider: str, kind: str, status: int | None = None) -> ProviderError:
    suffix = f" status={status}" if status is not None else ""
    return ProviderError(f"Provider failure role={role} provider={provider} type={kind}{suffix}")


@dataclass(frozen=True, repr=False)
class ProviderCallbacks:
    llm: Callable[..., Awaitable[str]]
    embed: Callable[..., Awaitable[np.ndarray]]
    llm_config: LLMRoleConfig
    embedding_config: EmbeddingRoleConfig

    @property
    def llm_metadata(self) -> dict[str, object]:
        return self.llm_config.canonical_config()

    @property
    def embedding_metadata(self) -> dict[str, object]:
        return self.embedding_config.canonical_config()

    @property
    def embedding_role_fingerprint(self) -> str:
        """Embedding role only: parser/chunker identity is deliberately not included."""
        return self.embedding_config.fingerprint()


def build_provider_callbacks(
    llm_config: LLMRoleConfig,
    embedding_config: EmbeddingRoleConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderCallbacks:
    """Build callbacks; wrap embed with Core EmbeddingFunc(supports_asymmetric=True).

    Each call owns and closes its HTTP client, including cancellation paths. No
    automatic retry is performed; persistent application jobs own retry policy.
    """

    async def post(config: Any, role: str, path: str, body: dict[str, Any]) -> Any:
        headers = {}
        if config.api_key is not None:
            headers["Authorization"] = f"Bearer {config.api_key.get_secret_value()}"
        status = None
        try:
            async with asyncio.timeout(config.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=config.timeout_seconds, transport=transport,
                    follow_redirects=False, trust_env=False,
                ) as client:
                    response = await client.post(
                        config.base_url + path, json=body, headers=headers,
                    )
                    status = response.status_code
                    if not 200 <= status < 300:
                        raise _fail(role, config.provider, "http", status)
                    return response.json()
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise _fail(role, config.provider, "timeout", status) from None
        except Exception:
            raise _fail(role, config.provider, "transport_or_json", status) from None

    async def llm(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        error = lambda kind: _fail("llm", llm_config.provider, kind)
        if not isinstance(prompt, str) or (
            system_prompt is not None and not isinstance(system_prompt, str)
        ):
            raise error("input")
        messages = []
        if system_prompt is not None:
            messages.append({"role": "system", "content": system_prompt})
        if history_messages is not None:
            if not isinstance(history_messages, list) or any(
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or not isinstance(message["role"], str)
                or message["role"] not in {"system", "user", "assistant"}
                or not isinstance(message["content"], str)
                for message in history_messages
            ):
                raise error("history")
            messages.extend(deepcopy(history_messages))
        messages.append({"role": "user", "content": prompt})
        options = {key: value for key, value in kwargs.items() if key not in _INTERNAL}
        if options.pop("stream", False) is not False:
            raise error("unsupported_stream")
        keyword = options.pop("keyword_extraction", False)
        entity = options.pop("entity_extraction", False)
        if not isinstance(keyword, bool) or not isinstance(entity, bool):
            raise error("options")
        response_format = options.pop("response_format", None)
        if response_format is None and (keyword or entity):
            response_format = {"type": "json_object"}
        if set(options) - _SAMPLING:
            raise error("unsupported_options")
        for key, value in options.items():
            if key in {"temperature", "top_p"}:
                if type(value) not in {int, float}:
                    raise error("options")
                try:
                    finite = math.isfinite(value)
                except (OverflowError, TypeError, ValueError):
                    raise error("options") from None
                if not finite:
                    raise error("options")
                if value < 0 or (key == "top_p" and value > 1):
                    raise error("options")
            elif key in {"max_tokens", "seed"}:
                if type(value) is not int or (key == "max_tokens" and value <= 0):
                    raise error("options")
            elif key == "stop":
                if not isinstance(value, str) and not (
                    isinstance(value, list) and all(isinstance(item, str) for item in value)
                ):
                    raise error("options")
        body: dict[str, Any] = {"model": llm_config.model, "messages": messages, "stream": False}
        if response_format is not None:
            if not isinstance(response_format, dict):
                raise error("response_format")
            if response_format == {"type": "json_object"}:
                native_format: Any = "json"
            elif (
                response_format.get("type") == "json_schema"
                and set(response_format) == {"type", "json_schema"}
                and isinstance(response_format["json_schema"], dict)
                and isinstance(response_format["json_schema"].get("schema"), dict)
            ):
                native_format = deepcopy(response_format["json_schema"]["schema"])
            else:
                raise error("response_format")
            if (keyword or entity) and response_format.get("type") != "json_object":
                raise error("response_format")
            body["format" if llm_config.provider == "ollama" else "response_format"] = (
                native_format if llm_config.provider == "ollama" else deepcopy(response_format)
            )
        if llm_config.provider == "ollama":
            body["options"] = {
                "num_ctx": llm_config.context_size,
                **{("num_predict" if key == "max_tokens" else key): value
                   for key, value in options.items()},
            }
            data = await post(llm_config, "llm", "/api/chat", body)
            try:
                if data["done"] is not True or data["done_reason"] != "stop":
                    raise ValueError
                message = data["message"]
                if (
                    message.get("role") != "assistant"
                    or message.get("tool_calls")
                    or message.get("function_call")
                ):
                    raise ValueError
                content = message["content"]
            except (KeyError, TypeError, AttributeError, ValueError):
                raise error("incomplete_response") from None
        else:
            body.update(options)
            data = await post(llm_config, "llm", "/chat/completions", body)
            try:
                choices = data["choices"]
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError
                choice = choices[0]
                if choice["finish_reason"] != "stop":
                    raise ValueError
                message = choice["message"]
                if (
                    message.get("role") != "assistant"
                    or message.get("tool_calls")
                    or message.get("function_call")
                    or message.get("refusal")
                ):
                    raise ValueError
                content = message["content"]
            except (KeyError, TypeError, AttributeError, ValueError):
                raise error("incomplete_response") from None
        if not isinstance(content, str) or not content.strip():
            raise error("empty_response")
        return content

    async def embed(
        texts: list[str], *, context: str = "document", **kwargs: Any,
    ) -> np.ndarray:
        error = lambda kind: _fail("embedding", embedding_config.provider, kind)
        options = {key: value for key, value in kwargs.items() if key not in _INTERNAL}
        # Core's optional dimension injection may confirm, never change, the role.
        if "embedding_dim" in options:
            dimension = options.pop("embedding_dim")
            if type(dimension) is not int or dimension != embedding_config.dimension:
                raise error("dimension_override")
        if options:
            raise error("unsupported_options")
        if not isinstance(context, str) or context not in {"query", "document"}:
            raise error("context")
        if not isinstance(texts, (list, tuple)) or any(not isinstance(text, str) for text in texts):
            raise error("input")
        if not texts:
            return np.empty((0, embedding_config.dimension), dtype=np.float64)
        prefix = (embedding_config.query_prefix if context == "query"
                  else embedding_config.document_prefix)
        inputs = [prefix + text for text in texts]
        body: dict[str, Any] = {"model": embedding_config.model}
        if embedding_config.provider == "ollama":
            body.update(input=inputs, truncate=False)
            data = await post(embedding_config, "embedding", "/api/embed", body)
            try:
                vectors = data["embeddings"]
            except (KeyError, TypeError):
                raise error("embedding_response") from None
        else:
            body.update(input=inputs, encoding_format="float")
            if embedding_config.send_dimensions:
                body["dimensions"] = embedding_config.dimension
            data = await post(embedding_config, "embedding", "/embeddings", body)
            try:
                records = data["data"]
                if not isinstance(records, list) or len(records) != len(texts):
                    raise ValueError
                ordered = {}
                for record in records:
                    index = record["index"]
                    if type(index) is not int or index in ordered or not 0 <= index < len(texts):
                        raise ValueError
                    ordered[index] = record["embedding"]
                vectors = [ordered[index] for index in range(len(texts))]
            except (KeyError, TypeError, ValueError):
                raise error("embedding_response") from None
        if not isinstance(vectors, list) or len(vectors) != len(texts) or any(
            not isinstance(vector, list) or len(vector) != embedding_config.dimension
            or any(type(value) not in {int, float} for value in vector)
            for vector in vectors
        ):
            raise error("embedding_shape")
        try:
            result = np.ascontiguousarray(vectors, dtype=np.float64)
            if not np.isfinite(result).all():
                raise ValueError
        except (ValueError, OverflowError, TypeError):
            raise error("embedding_numeric") from None
        return result

    return ProviderCallbacks(llm, embed, llm_config, embedding_config)
