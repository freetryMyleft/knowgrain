"""Real transaction tests; require an explicitly selected disposable test database."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, select, text, update

from knowgrain.api import create_app
from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.models import Job, SourceDocument, SourceRevision, VaultBinding
from knowgrain.source_repository import SourceConflictError, SourceRepository
from knowgrain.source_service import SourceService
from knowgrain.vault import VaultStore


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresSourcesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(
            _env_file=None,
            postgres_host="127.0.0.1",
            postgres_port=int(os.environ.get("KNOWGRAIN_TEST_POSTGRES_PORT", "55432")),
            postgres_user="knowgrain",
            postgres_database=TEST_DATABASE,
            knowgrain_postgres_db=TEST_DATABASE,
            vault_root=Path(self.temporary.name) / "vault",
            vault_parent_dir=Path(self.temporary.name) / "vaults",
            llm_model="knowgrain-test-missing-model",
            embedding_model="knowgrain-test-missing-embedding",
        )
        self.database = ApplicationDatabase(self.settings)
        self.assertTrue(await self.database.initialize(), self.database.last_error)
        binding, source_count = await self.database.get_vault_state()
        if binding is not None or source_count:
            await self.database.close()
            self.skipTest("fixture database must start without source rows or a Vault binding")
        self.repository = SourceRepository(self.database)
        self.vault = VaultStore(self.settings.vault_root)
        self.vault.initialize()
        self.service = SourceService(self.settings, self.repository, self.vault)
        self.source_ids = set()
        self.fixture_binding_id = None

    async def asyncTearDown(self):
        # Delete only this fixture's rows, inside the explicitly opted-in test DB.
        async with self.database.session_factory() as session, session.begin():
            ids = list(self.source_ids)
            revisions = select(SourceRevision.id).where(SourceRevision.source_id.in_(ids))
            await session.execute(
                update(SourceDocument).where(SourceDocument.id.in_(ids)).values(
                    latest_revision_id=None, current_revision_id=None
                )
            )
            await session.execute(delete(Job).where(Job.revision_id.in_(revisions)))
            await session.execute(delete(SourceRevision).where(SourceRevision.source_id.in_(ids)))
            await session.execute(delete(SourceDocument).where(SourceDocument.id.in_(ids)))
            if self.fixture_binding_id is not None:
                await session.execute(
                    delete(VaultBinding).where(
                        VaultBinding.id == 1,
                        VaultBinding.binding_id == self.fixture_binding_id,
                    )
                )
        await self.database.close()

    async def upload(self, filename="file.md", content=None, source_id=None):
        content = content or f"fixture {uuid4()}".encode()
        result = await self.service.import_file(filename, content, source_id=source_id)
        self.source_ids.add(result.source_id)
        return result

    async def test_concurrent_duplicate_uploads_share_revision_and_job(self):
        content = f"duplicate fixture {uuid4()}".encode()
        first, second = await asyncio.gather(
            self.upload(content=content), self.upload(content=content)
        )
        self.assertEqual(first.revision_id, second.revision_id)
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual({first.duplicate, second.duplicate}, {False, True})
        self.assertEqual(self.vault.read_bytes(first.vault_path), content)

    async def test_only_one_owner_claims_a_job(self):
        result = await self.upload()
        claims = await asyncio.gather(
            self.repository.claim_job(uuid4()), self.repository.claim_job(uuid4())
        )
        claimed = [claim for claim in claims if claim is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["job_id"], str(result.job_id))

    async def test_old_completion_cannot_replace_new_revision_and_filename(self):
        first = await self.upload(filename="first.md")
        first_owner = uuid4()
        first_job = await self.repository.claim_job(first_owner)
        second = await self.upload(filename="second.txt", source_id=first.source_id)
        old_revision = await self.repository.get_revision(first.revision_id)
        self.assertEqual(old_revision["filename"], "first.md")
        self.assertTrue(await self.repository.complete_job(
            UUID(first_job["job_id"]), first_owner, "a" * 64, []
        ))
        source = await self.repository.get_source(first.source_id)
        self.assertIsNone(source["current_revision_id"])
        second_owner = uuid4()
        new_job = await self.repository.claim_job(second_owner)
        self.assertEqual(new_job["filename"], "second.txt")
        self.assertTrue(await self.repository.complete_job(
            UUID(new_job["job_id"]), second_owner, "b" * 64, []
        ))
        source = await self.repository.get_source(first.source_id)
        self.assertEqual(source["current_revision_id"], str(second.revision_id))

    async def test_list_sources_paginates_and_preserves_latest_and_current_snapshots(self):
        first = await self.upload(filename="before.txt", content=b"before revision")
        indexed_at = datetime.now(UTC)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceRevision)
                .where(SourceRevision.id == first.revision_id)
                .values(
                    index_state="ready",
                    parsed_text_sha256=hashlib.sha256(b"indexed text").hexdigest(),
                    indexed_at=indexed_at,
                )
            )
            await session.execute(
                update(Job)
                .where(Job.id == first.job_id)
                .values(state="succeeded", updated_at=indexed_at)
            )
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id == first.source_id)
                .values(current_revision_id=first.revision_id)
            )

        latest = await self.upload(
            filename="after.txt", content=b"after revision", source_id=first.source_id
        )
        other = await self.upload(filename="other.md", content=b"another source")
        third = await self.upload(filename="third.md", content=b"third source")

        with self.assertRaises(ValueError):
            await self.repository.list_sources(limit=0)
        with self.assertRaises(ValueError):
            await self.repository.list_sources(limit=501)
        with self.assertRaises(ValueError):
            await self.repository.list_sources(offset=-1)

        all_sources = await self.repository.list_sources(limit=500)
        self.assertLessEqual(len(all_sources), 500)
        snapshots_by_id = {snapshot["source_id"]: snapshot for snapshot in all_sources}
        self.assertTrue(
            {str(first.source_id), str(other.source_id), str(third.source_id)}
            <= snapshots_by_id.keys()
        )

        paginated = []
        for offset in range(0, len(all_sources), 2):
            page = await self.repository.list_sources(limit=2, offset=offset)
            self.assertLessEqual(len(page), 2)
            paginated.extend(page)
        self.assertEqual(paginated, all_sources)

        snapshot = snapshots_by_id[str(first.source_id)]
        self.assertEqual(snapshot, await self.repository.get_source(first.source_id))
        self.assertEqual(snapshot["latest_revision_id"], str(latest.revision_id))
        self.assertEqual(snapshot["current_revision_id"], str(first.revision_id))
        self.assertEqual(snapshot["revision_status"], "queued")
        self.assertEqual(snapshot["latest_revision"]["state"], "queued")
        self.assertEqual(snapshot["current_revision"]["index_state"], "ready")
        self.assertEqual(snapshot["lifecycle_version"], 0)
        self.assertEqual(
            set(snapshot),
            {
                "id",
                "source_id",
                "filename",
                "state",
                "lifecycle_version",
                "latest_revision_id",
                "current_revision_id",
                "revision_status",
                "sha256",
                "vault_path",
                "error",
                "latest_revision",
                "current_revision",
                "created_at",
            },
        )
        revision_fields = {
            "id",
            "revision_id",
            "source_id",
            "filename",
            "sha256",
            "vault_path",
            "media_type",
            "state",
            "index_state",
            "parsed_text_sha256",
            "error",
            "created_at",
            "indexed_at",
        }
        self.assertEqual(set(snapshot["latest_revision"]), revision_fields)
        self.assertEqual(set(snapshot["current_revision"]), revision_fields)

    async def test_failure_and_retry_preserve_revision_and_original(self):
        content = f"retained fixture {uuid4()}".encode()
        result = await self.upload(content=content)
        with self.assertRaises(SourceConflictError):
            await self.repository.retry_source(result.source_id)
        owner = uuid4()
        job = await self.repository.claim_job(owner)
        self.assertFalse(await self.repository.complete_job(UUID(job["job_id"]), uuid4(), "f" * 64, []))
        self.assertTrue(await self.repository.fail_job(result.job_id, owner, "model unavailable"))
        self.assertEqual((await self.repository.get_job(result.job_id))["state"], "failed")
        self.assertEqual(await self.repository.retry_source(result.source_id), result.job_id)
        self.assertEqual(self.vault.read_bytes(result.vault_path), content)

    async def test_list_sources_filters_before_pagination(self):
        first = await self.upload(filename="filter-one.txt", content=b"filter one")
        second = await self.upload(filename="filter-two.txt", content=b"filter two")
        active = await self.upload(filename="filter-active.txt", content=b"filter active")
        for result in (first, second):
            await self.repository.soft_delete_source(
                result.source_id,
                expected_lifecycle_version=0,
                expected_latest_revision_id=result.revision_id,
            )

        deleted = await self.repository.list_sources(state="deleted", limit=500)
        active_sources = await self.repository.list_sources(state="active", limit=500)
        first_page = await self.repository.list_sources(state="deleted", limit=1, offset=0)
        second_page = await self.repository.list_sources(state="deleted", limit=1, offset=1)
        self.assertEqual(len(deleted), 2)
        self.assertEqual(first_page + second_page, deleted)
        self.assertEqual({item["source_id"] for item in active_sources}, {str(active.source_id)})
        with self.assertRaises(ValueError):
            await self.repository.list_sources(state="archived")

    async def test_same_parse_reindex_preserves_evidence_timestamp(self):
        result = await self.upload(filename="stable.txt", content=b"Stable original")
        digest = hashlib.sha256(b"Stable original").hexdigest()
        owner = uuid4()
        await self.repository.claim_job(owner)
        self.assertTrue(await self.repository.complete_job(result.job_id, owner, digest, []))
        first = await self.repository.get_revision(result.revision_id)
        self.assertEqual(await self.repository.retry_source(result.source_id), result.job_id)
        owner = uuid4()
        await self.repository.claim_job(owner)
        self.assertTrue(await self.repository.complete_job(result.job_id, owner, digest, []))
        second = await self.repository.get_revision(result.revision_id)
        self.assertEqual(second["indexed_at"], first["indexed_at"])
        self.assertEqual(second["parsed_text_sha256"], first["parsed_text_sha256"])
        self.assertEqual(second["state"], "ready")

    async def test_delete_restore_are_versioned_idempotent_and_aba_safe(self):
        result = await self.upload(filename="lifecycle.txt", content=b"Lifecycle source")
        owner = uuid4()
        await self.repository.claim_job(owner)
        digest = hashlib.sha256(b"parsed source").hexdigest()
        self.assertTrue(await self.repository.complete_job(result.job_id, owner, digest, []))

        deleted = await self.repository.soft_delete_source(
            result.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=result.revision_id,
        )
        self.assertEqual((deleted["state"], deleted["lifecycle_version"]), ("deleted", 1))
        self.assertEqual(deleted["current_revision_id"], str(result.revision_id))
        self.assertEqual(
            await self.repository.soft_delete_source(
                result.source_id,
                expected_lifecycle_version=0,
                expected_latest_revision_id=result.revision_id,
            ),
            deleted,
        )

        restored = await self.repository.restore_source(
            result.source_id,
            expected_lifecycle_version=1,
            expected_latest_revision_id=result.revision_id,
            verified_current_revision_id=result.revision_id,
        )
        self.assertEqual((restored["state"], restored["lifecycle_version"]), ("active", 2))
        self.assertEqual(
            await self.repository.restore_source(
                result.source_id,
                expected_lifecycle_version=1,
                expected_latest_revision_id=result.revision_id,
                verified_current_revision_id=result.revision_id,
            ),
            restored,
        )
        with self.assertRaises(SourceConflictError):
            await self.repository.soft_delete_source(
                result.source_id,
                expected_lifecycle_version=0,
                expected_latest_revision_id=result.revision_id,
            )

    async def test_lifecycle_latest_revision_compare_and_set_rejects_new_upload(self):
        first = await self.upload(filename="first.txt", content=b"first")
        second = await self.upload(
            filename="second.txt", content=b"second", source_id=first.source_id
        )
        with self.assertRaises(SourceConflictError):
            await self.repository.soft_delete_source(
                first.source_id,
                expected_lifecycle_version=0,
                expected_latest_revision_id=first.revision_id,
            )
        snapshot = await self.repository.get_source(first.source_id)
        self.assertEqual(snapshot["latest_revision_id"], str(second.revision_id))
        self.assertEqual(snapshot["lifecycle_version"], 0)

    async def test_deleted_source_cancels_running_index_job_and_rejects_late_worker(self):
        result = await self.upload(content=b"running source")
        owner = uuid4()
        await self.repository.claim_job(owner)
        deleted = await self.repository.soft_delete_source(
            result.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=result.revision_id,
        )
        self.assertEqual(deleted["lifecycle_version"], 1)
        job = await self.repository.get_job(result.job_id)
        revision = await self.repository.get_revision(result.revision_id)
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(job["lease_owner"])
        self.assertIsNone(job["lease_until"])
        self.assertEqual(revision["state"], "failed")
        self.assertIsNone(await self.repository.claim_job(uuid4()))
        self.assertFalse(
            await self.repository.complete_job(result.job_id, owner, "c" * 64, [])
        )
        self.assertFalse(await self.repository.fail_job(result.job_id, owner, "late error"))
        self.assertFalse(await self.repository.renew_lease(result.job_id, owner))

    async def test_expired_lease_cannot_be_renewed(self):
        result = await self.upload(content=b"expired lease")
        owner = uuid4()
        await self.repository.claim_job(owner)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == result.job_id)
                .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
            )
        self.assertFalse(await self.repository.renew_lease(result.job_id, owner))

    async def test_lease_expiring_while_waiting_for_source_lock_cannot_complete(self):
        result = await self.upload(content=b"wait for source lock")
        owner = uuid4()
        await self.repository.claim_job(owner)
        async with self.database.session_factory() as session, session.begin():
            await session.scalar(
                select(SourceDocument)
                .where(SourceDocument.id == result.source_id)
                .with_for_update()
            )
            await session.execute(
                update(Job)
                .where(Job.id == result.job_id)
                .values(lease_until=func.clock_timestamp() + text("INTERVAL '200 milliseconds'"))
            )
            completion = asyncio.create_task(
                self.repository.complete_job(result.job_id, owner, "d" * 64, [])
            )
            await asyncio.sleep(0.35)
            self.assertFalse(completion.done())
        self.assertFalse(await completion)

    async def test_release_owner_does_not_resurrect_deleted_running_work(self):
        result = await self.upload(content=b"release deleted work")
        owner = uuid4()
        await self.repository.claim_job(owner)
        await self.repository.soft_delete_source(
            result.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=result.revision_id,
        )
        await self.repository.release_owner(owner)
        job = await self.repository.get_job(result.job_id)
        revision = await self.repository.get_revision(result.revision_id)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(revision["state"], "failed")

    async def test_upload_api_persists_job_even_when_models_unavailable(self):
        app = create_app(self.settings)
        async with app.router.lifespan_context(app):
            binding, _ = await self.database.get_vault_state()
            original_binding_id = binding["binding_id"]
            self.fixture_binding_id = original_binding_id
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
            ) as client:
                status = await client.get("/api/v1/system/vault")
                self.assertEqual(status.status_code, 200, status.text)
                self.assertTrue(status.json()["ready"])
                self.assertEqual(status.json()["root"], str(self.vault.root))
                preview = await client.post(
                    "/api/v1/system/vault/preview", json={"name": "Selected"}
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(
                    preview.json()["create_directories"],
                    ["Sources/Files", "Sources/Evidence", "Wiki/Drafts", "Wiki/Pages"],
                )
                self.assertFalse((self.settings.vault_parent_dir / "Selected").exists())
                selected = await client.post(
                    "/api/v1/system/vault/select",
                    json={
                        "name": "Selected",
                        "expected_binding_id": str(original_binding_id),
                        "expected_root": status.json()["root"],
                    },
                )
                self.assertEqual(selected.status_code, 200, selected.text)
                self.assertTrue(selected.json()["ready"])
                self.fixture_binding_id = UUID(selected.json()["binding_id"])
                self.assertEqual(
                    selected.json()["root"],
                    str(self.settings.vault_parent_dir.resolve() / "Selected"),
                )
                stale = await client.post(
                    "/api/v1/system/vault/select",
                    json={
                        "name": "Another",
                        "expected_binding_id": str(original_binding_id),
                        "expected_root": status.json()["root"],
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                unsafe = await client.post(
                    "/api/v1/system/vault/preview", json={"name": "../outside"}
                )
                self.assertEqual(unsafe.status_code, 422, unsafe.text)

                content = f"API evidence {uuid4()}".encode()
                response = await client.post(
                    "/api/v1/sources", files={"file": ("example.md", content, "text/markdown")}
                )
                self.assertEqual(response.status_code, 202, response.text)
                data = response.json()
                self.source_ids.add(UUID(data["source_id"]))
                locked = await client.post(
                    "/api/v1/system/vault/preview", json={"name": "Other"}
                )
                self.assertEqual(locked.status_code, 409, locked.text)
                source = await client.get(f"/api/v1/sources/{data['source_id']}")
                self.assertEqual(source.status_code, 200)
                job = await client.get(f"/api/v1/jobs/{data['job_id']}")
                self.assertEqual(job.json()["state"], "queued")
                selected_vault = VaultStore(Path(selected.json()["root"]))
                self.assertEqual(selected_vault.read_bytes(data["vault_path"]), content)
                rejected = await client.post(
                    "/api/v1/sources", headers={"origin": "https://example.invalid"},
                    files={"file": ("example.md", content, "text/markdown")},
                )
                self.assertEqual(rejected.status_code, 403)
                unsafe = await client.post(
                    "/api/v1/sources", files={"file": ("../unsafe.md", content)}
                )
                self.assertEqual(unsafe.status_code, 422)
