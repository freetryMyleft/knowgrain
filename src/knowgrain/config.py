from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "knowgrain"
    postgres_password: SecretStr = SecretStr("knowgrain-local")
    postgres_database: str = "lightrag"
    knowgrain_postgres_db: str = "knowgrain"
    vault_root: Path = Path("./data/vault")
    vault_parent_dir: Path = Path("./data/vaults")
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=MAX_UPLOAD_BYTES)

    api_host: str = "127.0.0.1"
    api_port: int = 8787
    web_dist_dir: Path = Path("./apps/web/dist")
    tokenizer_cache_dir: Path = Path("./data/tokenizers")

    ollama_host: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3.6:35b"
    llm_context_size: int = 16_384
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dim: int = 1024
    embedding_max_token_size: int = 32_768

    lightrag_working_dir: Path = Path("./data/lightrag")
    lightrag_workspace: str = "knowgrain"

    def configure_lightrag_environment(self, *, workspace: str | None = None) -> None:
        """Set the PostgreSQL variables consumed by LightRAG Core before importing it."""
        import os

        self.configure_lightrag_database_environment()
        selected_workspace = self.lightrag_workspace if workspace is None else workspace
        # LightRAG 1.5.7 reads POSTGRES_WORKSPACE but labels it PG_WORKSPACE in
        # logs. Pin the actual setting and documented alias to the same value.
        os.environ["PG_WORKSPACE"] = selected_workspace
        os.environ["POSTGRES_WORKSPACE"] = selected_workspace

    def configure_lightrag_database_environment(self) -> None:
        """Set connection values without changing the legacy workspace override."""
        import os

        os.environ["POSTGRES_HOST"] = self.postgres_host
        os.environ["POSTGRES_PORT"] = str(self.postgres_port)
        os.environ["POSTGRES_USER"] = self.postgres_user
        os.environ["POSTGRES_PASSWORD"] = self.postgres_password.get_secret_value()
        os.environ["POSTGRES_DATABASE"] = self.postgres_database
