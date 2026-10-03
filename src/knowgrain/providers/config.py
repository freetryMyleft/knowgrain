"""Immutable server-side role configuration; never a complete index fingerprint.

Parser, tokenizer and chunker identity must be sealed separately by the future
index-generation coordinator. Credentials are intentionally absent from metadata.
"""

import hashlib
import json
import re
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

if TYPE_CHECKING:
    from knowgrain.config import Settings

Provider = Literal["ollama", "openai-compatible"]


class _RoleConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider: Provider
    base_url: str
    model: str = Field(min_length=1)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    timeout_seconds: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if not value or re.search(r"[\s\\\x00-\x1f\x7f]", value):
            raise ValueError("Invalid provider endpoint")
        try:
            parts = urlsplit(value)
            # Access port to validate its syntax/range, including malformed IPv6.
            parts.port
            host = parts.hostname
            if (
                parts.scheme.lower() not in {"http", "https"}
                or not host
                or parts.username is not None
                or parts.password is not None
                or "?" in value
                or "#" in value
                or re.search(r"[^a-zA-Z0-9.\-:\[\]]", host)
                or host.startswith(".")
                or ".." in host
                or parts.netloc.endswith(":")
            ):
                raise ValueError
        except ValueError:
            raise ValueError("Invalid provider endpoint") from None
        # Canonicalize case and default ports while retaining deployment paths.
        import httpx

        url = httpx.URL(value)
        if (url.scheme, url.port) in {("http", 80), ("https", 443)}:
            url = url.copy_with(port=None)
        return str(url).rstrip("/")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid provider model")
        return value

    def canonical_config(self) -> dict[str, object]:
        """Return a fresh secret-free representation, safe to persist as metadata."""
        return self.model_dump(mode="json")

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_config(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class LLMRoleConfig(_RoleConfig):
    context_size: int = Field(default=16_384, gt=0, strict=True)


class EmbeddingRoleConfig(_RoleConfig):
    dimension: int = Field(gt=0, le=16_000, strict=True)
    max_token_size: int = Field(default=32_768, gt=0, strict=True)
    document_prefix: str = ""
    query_prefix: str = ""
    send_dimensions: bool = Field(default=False, strict=True)


def role_configs_from_settings(settings: "Settings") -> tuple[LLMRoleConfig, EmbeddingRoleConfig]:
    """Adapt the existing local Ollama defaults without reading ambient cloud settings."""
    return (
        LLMRoleConfig(
            provider="ollama", base_url=settings.ollama_host, model=settings.llm_model,
            context_size=settings.llm_context_size,
        ),
        EmbeddingRoleConfig(
            provider="ollama", base_url=settings.ollama_host, model=settings.embedding_model,
            dimension=settings.embedding_dim, max_token_size=settings.embedding_max_token_size,
        ),
    )
