from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from knowgrain.models import (
    GenerationJob,
    QueryJob,
    SourceDocument,
    SourceRevision,
    VaultBinding,
    WikiPage,
)


EXPECTED_SCHEMA_REVISION = "0010_m5_source_lifecycle"
_VAULT_BINDING_LOCK_KEYS = (1263420247, 1196575049)


class VaultBindingConflict(RuntimeError):
    """The binding changed since the caller previewed it or sources lock it."""


class ApplicationDatabase:
    """Async connection pool and readiness probe for the application database."""

    def __init__(self, settings: Any) -> None:
        password = settings.postgres_password
        if hasattr(password, "get_secret_value"):
            password = password.get_secret_value()
        url = URL.create(
            "postgresql+asyncpg",
            username=settings.postgres_user,
            password=password,
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=getattr(settings, "knowgrain_postgres_db", "knowgrain"),
        )
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.is_ready = False
        self.last_error: str | None = None

    async def initialize(self) -> bool:
        """Probe the database and require the current Alembic schema revision.

        Readiness failures are recorded instead of raised so the API can remain available
        and report 503 until PostgreSQL is reachable and migrations have been applied.
        """
        self.is_ready, self.last_error = await self.probe_readiness()
        return self.is_ready

    async def probe_readiness(self) -> tuple[bool, str | None]:
        try:
            async with asyncio.timeout(3):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                    result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                    versions = set(result.scalars().all())
            if versions != {EXPECTED_SCHEMA_REVISION}:
                self.is_ready = False
                self.last_error = "Application database migrations are missing or out of date."
                return False, self.last_error
            self.is_ready = True
            self.last_error = None
            return True, None
        except TimeoutError:
            self.is_ready = False
            self.last_error = "Application database readiness check timed out."
            return False, self.last_error
        except Exception:
            self.is_ready = False
            self.last_error = "Application database is unavailable or not migrated."
            return False, self.last_error

    async def get_vault_state(self) -> tuple[dict[str, Any] | None, int]:
        """Return the singleton binding and managed-content count from one DB session.

        The count includes persistent sources, Wiki identities (including absent
        pages), and generation jobs. Pending jobs also lock the binding so their
        reserved output cannot be published into a different Vault.
        """
        async with self.session_factory() as session:
            binding = await session.get(VaultBinding, 1)
            return (
                self._binding_snapshot(binding) if binding else None,
                await self._managed_content_count(session),
            )

    async def list_source_originals(self) -> list[tuple[str, str]]:
        """Return only Vault-relative paths and hashes needed for legacy adoption."""
        async with self.session_factory() as session:
            rows = await session.execute(
                select(SourceRevision.vault_path, SourceRevision.sha256).order_by(
                    SourceRevision.created_at, SourceRevision.id
                )
            )
            return [(str(path), str(digest)) for path, digest in rows]

    async def compare_and_set_vault_binding(
        self,
        *,
        expected_binding_id: UUID | None,
        expected_root: str,
        target_root: str,
    ) -> dict[str, Any]:
        """Serialize singleton creation and apply root changes with a database lock."""
        async with self.session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_a, :lock_b)"),
                {"lock_a": _VAULT_BINDING_LOCK_KEYS[0], "lock_b": _VAULT_BINDING_LOCK_KEYS[1]},
            )
            binding = await session.scalar(
                select(VaultBinding).where(VaultBinding.id == 1).with_for_update()
            )
            managed_content_count = await self._managed_content_count(session)
            current_id = binding.binding_id if binding else None
            current_root = binding.root_path if binding else expected_root
            if current_id != expected_binding_id or current_root != expected_root:
                raise VaultBindingConflict("Vault selection changed; refresh the preview")
            if managed_content_count and target_root != expected_root:
                raise VaultBindingConflict(
                    "The Vault is locked because managed content already exists"
                )
            if binding is not None:
                if binding.root_path != target_root:
                    binding.root_path = target_root
                    binding.binding_id = uuid4()
                    binding.updated_at = func.now()
                await session.flush()
                await session.refresh(binding, attribute_names=["created_at", "updated_at"])
                return self._binding_snapshot(binding)

            binding = VaultBinding(
                id=1,
                binding_id=uuid4(),
                root_path=target_root,
            )
            session.add(binding)
            await session.flush()
            await session.refresh(binding, attribute_names=["created_at", "updated_at"])
            return self._binding_snapshot(binding)

    @staticmethod
    async def _managed_content_count(session: Any) -> int:
        source_count = select(func.count()).select_from(SourceDocument).scalar_subquery()
        wiki_count = select(func.count()).select_from(WikiPage).scalar_subquery()
        generation_count = select(func.count()).select_from(GenerationJob).scalar_subquery()
        query_count = select(func.count()).select_from(QueryJob).scalar_subquery()
        return int(
            await session.scalar(
                select(source_count + wiki_count + generation_count + query_count)
            )
            or 0
        )

    @staticmethod
    def _binding_snapshot(binding: VaultBinding) -> dict[str, Any]:
        return {
            "binding_id": binding.binding_id,
            "root_path": binding.root_path,
            "created_at": binding.created_at,
            "updated_at": binding.updated_at,
        }

    async def close(self) -> None:
        self.is_ready = False
        self.last_error = None
        await self.engine.dispose()
