"""PostgreSQL predicate checks against an explicitly selected disposable DB."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import os
import unittest
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text, update

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.models import SourceDocument, SourceRevision
from knowgrain.provenance_repository import ProvenanceRepository


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@unittest.skipUnless(
    TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected"
)
class ProvenancePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(
            _env_file=None,
            postgres_host="127.0.0.1",
            postgres_port=int(os.environ.get("KNOWGRAIN_TEST_POSTGRES_PORT", "55432")),
            postgres_user="knowgrain",
            postgres_database=TEST_DATABASE,
            knowgrain_postgres_db=TEST_DATABASE,
        )
        self.database = ApplicationDatabase(self.settings)
        self.source_ids: set[UUID] = set()
        async with self.database.engine.connect() as connection:
            version = await connection.execute(text("SELECT version_num FROM alembic_version"))
            self.assertTrue(version.scalars().first())
            await connection.execute(text("SELECT 1 FROM source_revision LIMIT 0"))
        self.repository = ProvenanceRepository(self.database)

    async def asyncTearDown(self):
        if self.source_ids:
            async with self.database.session_factory() as session, session.begin():
                ids = list(self.source_ids)
                await session.execute(
                    update(SourceDocument)
                    .where(SourceDocument.id.in_(ids))
                    .values(latest_revision_id=None, current_revision_id=None)
                )
                await session.execute(
                    delete(SourceRevision).where(SourceRevision.source_id.in_(ids))
                )
                await session.execute(
                    delete(SourceDocument).where(SourceDocument.id.in_(ids))
                )
        await self.database.close()

    async def add_source(
        self,
        name: str,
        *,
        state: str = "active",
        revisions: tuple[str, ...] = ("ready",),
        current_index: int = 0,
        latest_index: int = 0,
        shared_path: str | None = None,
    ) -> tuple[UUID, list[UUID], list[str]]:
        source_id = uuid4()
        revision_ids = [uuid4() for _ in revisions]
        paths: list[str] = []
        async with self.database.session_factory() as session, session.begin():
            source = SourceDocument(
                id=source_id,
                filename=f"{name}.txt",
                state=state,
            )
            session.add(source)
            await session.flush()
            for index, index_state in enumerate(revisions):
                original_hash = hashlib.sha256(f"{name}:{index}".encode()).hexdigest()
                parsed_hash = hashlib.sha256(f"parsed:{name}:{index}".encode()).hexdigest()
                path = shared_path or f"Sources/Files/{source_id}/{revision_ids[index]}.txt"
                paths.append(path)
                session.add(
                    SourceRevision(
                        id=revision_ids[index],
                        source_id=source_id,
                        filename=f"{name}.txt",
                        sha256=original_hash,
                        vault_path=path,
                        media_type="text/plain",
                        index_state=index_state,
                        parsed_text_sha256=parsed_hash if index_state == "ready" else None,
                        indexed_at=datetime.now(UTC) if index_state == "ready" else None,
                    )
                )
            await session.flush()
            source.current_revision_id = revision_ids[current_index]
            source.latest_revision_id = revision_ids[latest_index]
            await session.flush()
        self.source_ids.add(source_id)
        return source_id, revision_ids, paths

    async def test_paths_and_ids_require_one_active_current_latest_ready_revision(self):
        current_source, current_ids, current_paths = await self.add_source("current")
        _, old_ids, old_paths = await self.add_source(
            "old", revisions=("ready", "ready"), current_index=0, latest_index=1
        )
        _, queued_ids, queued_paths = await self.add_source(
            "latest-queued", revisions=("ready", "queued"), current_index=0, latest_index=1
        )
        _, deleted_ids, deleted_paths = await self.add_source(
            "deleted", state="deleted"
        )

        requested_paths = [
            current_paths[0],
            old_paths[0],
            old_paths[1],
            queued_paths[0],
            queued_paths[1],
            deleted_paths[0],
        ]
        by_path = await self.repository.eligible_by_paths(requested_paths)
        self.assertEqual(set(by_path), {current_paths[0]})
        self.assertEqual(by_path[current_paths[0]].source_id, current_source)
        self.assertEqual(by_path[current_paths[0]].revision_id, current_ids[0])

        requested_ids = [
            current_ids[0],
            *old_ids,
            *queued_ids,
            deleted_ids[0],
        ]
        by_id = await self.repository.eligible_by_ids(requested_ids)
        self.assertEqual(set(by_id), {current_ids[0]})
        self.assertEqual(by_id[current_ids[0]].source_id, current_source)

    async def test_path_lookup_rejects_duplicate_vault_path_records(self):
        shared_path = f"Sources/Files/shared/{uuid4()}.txt"
        _, first_ids, _ = await self.add_source(
            "ambiguous-first", shared_path=shared_path
        )
        _, second_ids, _ = await self.add_source(
            "ambiguous-second", shared_path=shared_path
        )

        self.assertEqual(await self.repository.eligible_by_paths([shared_path]), {})
        # ID validation applies the same unambiguous-path predicate.
        by_id = await self.repository.eligible_by_ids([first_ids[0], second_ids[0]])
        self.assertEqual(by_id, {})


if __name__ == "__main__":
    unittest.main()
