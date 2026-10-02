import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from knowgrain.api import ApplicationRuntime
from knowgrain.config import Settings
from knowgrain.vault import VaultStore
from knowgrain.vault_setup import VaultPathSetupError


class ApplicationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_vault_failure_pauses_claims_and_retry_recovers(self):
        with TemporaryDirectory() as temporary:
            runtime = ApplicationRuntime(Settings(
                _env_file=None, vault_root=Path(temporary) / "vault"
            ))
            runtime.lightrag._rag = object()
            claimed = asyncio.Event()

            async def initialize_database():
                runtime.database.is_ready = True
                return True

            async def claim(owner):
                claimed.set()
                return None

            async def initialize_vault():
                return VaultStore(Path(temporary) / "vault"), {"binding_id": uuid4()}

            # Keep the real runner; only replace external dependencies.
            with (
                patch.object(runtime.database, "initialize", side_effect=initialize_database),
                patch.object(
                    runtime.vault_setup,
                    "initialize",
                    side_effect=VaultPathSetupError("Vault unavailable"),
                ),
                patch.object(runtime.repository, "claim_job", side_effect=claim) as claim_job,
                patch.object(runtime.repository, "release_owner", new_callable=AsyncMock),
                patch.object(runtime.repository, "release_maintenance_owner", new_callable=AsyncMock),
                patch.object(runtime.lightrag, "start", new_callable=AsyncMock),
            ):
                await runtime.initialize()
                self.assertFalse(runtime.vault_ready)
                self.assertIsNone(runtime.jobs._task)
                claim_job.assert_not_awaited()

            try:
                with (
                    patch.object(runtime.database, "initialize", side_effect=initialize_database),
                    patch.object(runtime.vault_setup, "initialize", side_effect=initialize_vault),
                    patch.object(runtime.repository, "claim_job", side_effect=claim),
                    patch.object(runtime.repository, "release_owner", new_callable=AsyncMock),
                    patch.object(runtime.repository, "claim_maintenance", new_callable=AsyncMock, return_value=None),
                    patch.object(runtime.repository, "release_maintenance_owner", new_callable=AsyncMock),
                    patch.object(runtime.lightrag, "start", new_callable=AsyncMock),
                ):
                    # A usable Core lets the runner claim once Vault is repaired.
                    runtime.lightrag._rag = object()
                    await runtime.initialize()
                    self.assertTrue(runtime.vault_ready)
                    await asyncio.wait_for(claimed.wait(), timeout=1)

                    # A later failed reinitialization must also stop an existing runner.
                    with patch.object(
                        runtime.vault_setup,
                        "initialize",
                        side_effect=VaultPathSetupError("Vault unavailable"),
                    ):
                        await runtime.initialize()
                    self.assertFalse(runtime.vault_ready)
                    self.assertIsNone(runtime.jobs._task)
                    self.assertIsNone(runtime.maintenance._task)
            finally:
                runtime.lightrag._rag = None
                runtime.database.is_ready = False
                await runtime.close()

    async def test_failed_same_root_selection_disables_imports_and_stops_claims(self):
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            runtime = ApplicationRuntime(Settings(
                _env_file=None,
                vault_root=Path(temporary) / "vault",
                vault_parent_dir=Path(temporary) / "vaults",
            ))
            runtime.database.is_ready = True
            runtime.vault_ready = True
            runtime.lightrag._rag = object()
            expected_binding_id = uuid4()
            stop = AsyncMock()
            with (
                patch.object(runtime.jobs, "stop", stop),
                patch.object(runtime.maintenance, "stop", new_callable=AsyncMock),
                patch.object(
                    runtime.vault_setup,
                    "validate_selection",
                    new_callable=AsyncMock,
                    return_value=(Path(temporary) / "vaults" / "Vault", {}, 1),
                ),
                patch.object(
                    runtime.vault_setup,
                    "commit_selection",
                    new_callable=AsyncMock,
                    side_effect=VaultPathSetupError("registered original changed"),
                ),
            ):
                with self.assertRaises(VaultPathSetupError):
                    await runtime.select_vault(
                        name="Vault",
                        expected_binding_id=expected_binding_id,
                        expected_root=str(Path(temporary) / "vault"),
                    )
            self.assertFalse(runtime.vault_ready)
            stop.assert_awaited_once()
            runtime.lightrag._rag = None
            runtime.database.is_ready = False
            await runtime.close()

    async def test_shutdown_drains_maintenance_and_indexing_before_core_finalization(self):
        runtime = ApplicationRuntime(Settings(_env_file=None))
        events = []

        def stopped(name):
            async def call():
                events.append(name)
            return call

        with (
            patch.object(runtime.queries, "stop", side_effect=stopped("queries")),
            patch.object(runtime.generation, "stop", side_effect=stopped("generation")),
            patch.object(runtime.maintenance, "stop", side_effect=stopped("maintenance")),
            patch.object(runtime.jobs, "stop", side_effect=stopped("index")),
            patch.object(runtime.wiki, "stop", side_effect=stopped("wiki")),
            patch.object(runtime.lightrag, "close", side_effect=stopped("core")),
            patch.object(runtime.database, "close", side_effect=stopped("database")),
        ):
            await runtime.close()
        self.assertEqual(events, ["queries", "generation", "maintenance", "index", "wiki", "core", "database"])
