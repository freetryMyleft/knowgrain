from dataclasses import FrozenInstanceError
from contextlib import chdir
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from knowgrain.config import Settings
from knowgrain.index_identity import CoreIndexIdentity
from knowgrain.lightrag_runtime import LightRAGRuntime

_STORAGE_ATTRIBUTES = (
    "full_docs",
    "text_chunks",
    "full_entities",
    "full_relations",
    "entity_chunks",
    "relation_chunks",
    "entities_vdb",
    "relationships_vdb",
    "chunks_vdb",
    "chunk_entity_relation_graph",
    "llm_response_cache",
    "doc_status",
)


class CoreIndexIdentityTests(unittest.TestCase):
    def test_legacy_identity_uses_current_settings_without_a_vector_suffix(self):
        settings = Settings(
            _env_file=None,
            lightrag_workspace="knowgrain_setup_acceptance",
            lightrag_working_dir=Path("./data/lightrag-legacy"),
        )

        identity = CoreIndexIdentity.from_settings(settings)

        self.assertEqual(identity.workspace, "knowgrain_setup_acceptance")
        self.assertEqual(identity.working_dir, settings.lightrag_working_dir)
        self.assertIsNone(identity.vector_model_name)

    def test_workspace_keeps_upstream_safe_dots_hyphens_and_unicode(self):
        identity = CoreIndexIdentity("知识-v1.0", Path("./core"))

        self.assertEqual(identity.workspace, "知识-v1.0")

    def test_invalid_workspace_values_fail_before_runtime_startup(self):
        invalid_workspaces = (
            "",
            " \t ",
            "x" * 129,
            "../outside",
            "a/b",
            "a\\b",
            ".",
            "..",
            "name\nother",
            "invalid\ud800unicode",
        )
        for workspace in invalid_workspaces:
            with self.subTest(workspace=repr(workspace)):
                with self.assertRaises((TypeError, ValueError)):
                    CoreIndexIdentity(workspace, Path("./core"))

    def test_vector_model_name_accepts_only_full_canonical_tokens(self):
        valid = CoreIndexIdentity("kg", Path("./core"), "kg_" + "a" * 24)
        self.assertEqual(valid.vector_model_name, "kg_" + "a" * 24)

        for model_name in (
            "",
            "raw-provider-model",
            "kg_" + "A" * 24,
            "kg_" + "a" * 23,
            "kg_" + "a" * 25,
        ):
            with self.subTest(model_name=model_name):
                with self.assertRaises(ValueError):
                    CoreIndexIdentity("kg", Path("./core"), model_name)

    def test_working_dir_must_be_a_path_and_identity_is_frozen(self):
        with self.assertRaises(TypeError):
            CoreIndexIdentity("kg", "./core")  # type: ignore[arg-type]

        identity = CoreIndexIdentity("kg", Path("./core"))
        with self.assertRaises(FrozenInstanceError):
            identity.workspace = "other"  # type: ignore[misc]

    def test_settings_explicitly_overwrite_both_upstream_workspace_environment_names(self):
        settings = Settings(_env_file=None, lightrag_workspace="settings-workspace")
        original_db_config = {
            "POSTGRES_HOST": "127.0.0.9",
            "POSTGRES_PORT": "15432",
            "POSTGRES_USER": "ambient-user",
            "POSTGRES_PASSWORD": "ambient-password",
            "POSTGRES_DATABASE": "ambient-database",
            "PG_WORKSPACE": "ambient-pg-workspace",
            "POSTGRES_WORKSPACE": "ambient-postgres-workspace",
        }
        with patch.dict(os.environ, original_db_config):
            settings.configure_lightrag_environment(workspace="identity-workspace")

            self.assertEqual(os.environ["PG_WORKSPACE"], "identity-workspace")
            self.assertEqual(os.environ["POSTGRES_WORKSPACE"], "identity-workspace")
            self.assertEqual(os.environ["POSTGRES_HOST"], settings.postgres_host)
            self.assertEqual(os.environ["POSTGRES_PORT"], str(settings.postgres_port))
            self.assertEqual(os.environ["POSTGRES_USER"], settings.postgres_user)
            self.assertEqual(
                os.environ["POSTGRES_PASSWORD"],
                settings.postgres_password.get_secret_value(),
            )
            self.assertEqual(os.environ["POSTGRES_DATABASE"], settings.postgres_database)


class LightRAGIdentityStartupTests(unittest.IsolatedAsyncioTestCase):
    async def start_real_core_without_transport(
        self,
        identity,
        *,
        fault=None,
        embedding_dim=1024,
        default_identity=False,
        postgres_workspace="ambient-postgres-workspace",
    ):
        settings = Settings(
            _env_file=None,
            lightrag_workspace=(
                identity.workspace if default_identity else "legacy-settings-workspace"
            ),
            lightrag_working_dir=(
                identity.working_dir
                if default_identity
                else Path("./ignored-settings-working-dir")
            ),
            tokenizer_cache_dir=Path("./unused-tokenizer-cache"),
            embedding_dim=embedding_dim,
        )
        runtime = LightRAGRuntime(settings, index_identity=None if default_identity else identity)
        runtime._probe_postgres = AsyncMock(return_value=(True, None))
        runtime.validate_model_configuration = AsyncMock()

        from lightrag import LightRAG

        created: list[object] = []
        finalized: dict[str, AsyncMock] = {}

        async def initialize_without_transport(rag):
            created.append(rag)
            # This mirrors PGKVStorage.initialize's workspace precedence after
            # PostgreSQLDB has read its configuration, while avoiding DB I/O.
            for attribute in _STORAGE_ATTRIBUTES:
                storage = getattr(rag, attribute)
                storage.workspace = os.environ["POSTGRES_WORKSPACE"]
                finalizer = AsyncMock()
                storage.finalize = finalizer
                finalized[attribute] = finalizer
            if fault == "workspace_mismatch":
                rag.chunks_vdb.workspace = "ambient-workspace"
            elif fault == "missing_workspace":
                del rag.doc_status.workspace
            elif fault == "missing_storage":
                del rag.text_chunks

        with patch.object(
            LightRAG,
            "initialize_storages",
            autospec=True,
            side_effect=initialize_without_transport,
        ), patch(
            "lightrag.lightrag.TiktokenTokenizer",
            return_value=type("LocalTokenizer", (), {"encode": lambda self, text: []})(),
        ), patch(
            "knowgrain.tokenizer_cache.require_tokenizer_cache"
        ) as require_cache:
            with patch.dict(
                os.environ,
                {
                    "PG_WORKSPACE": "ambient-pg-workspace",
                    "POSTGRES_WORKSPACE": postgres_workspace or "",
                },
            ):
                if postgres_workspace is None:
                    os.environ.pop("POSTGRES_WORKSPACE", None)
                try:
                    await runtime._start_unlocked()
                except BaseException as exc:
                    if default_identity and not created:
                        require_cache.assert_not_called()
                        runtime._probe_postgres.assert_not_awaited()
                        runtime.validate_model_configuration.assert_not_awaited()
                        self.assertEqual(os.environ.get("POSTGRES_WORKSPACE"), postgres_workspace)
                    else:
                        require_cache.assert_called_once_with(settings.tokenizer_cache_dir)
                    return runtime, created[0] if created else None, finalized, exc
                require_cache.assert_called_once_with(settings.tokenizer_cache_dir)

        return runtime, created[0] if created else None, finalized, None

    async def test_default_identity_rejects_ambient_legacy_workspace_before_dependencies(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            identity = CoreIndexIdentity("knowgrain", Path(directory) / "core")
            runtime, rag, finalized, error = await self.start_real_core_without_transport(
                identity, default_identity=True, postgres_workspace="existing-private-workspace"
            )

            self.assertIsInstance(error, ValueError)
            self.assertIn("LIGHTRAG_WORKSPACE", str(error))
            self.assertIn("POSTGRES_WORKSPACE", str(error))
            self.assertNotIn("existing-private-workspace", str(error))
            self.assertFalse(runtime.restart_required)
            self.assertIsNone(rag)
            self.assertEqual(finalized, {})
            self.assertFalse(identity.working_dir.exists())

    async def test_default_identity_accepts_matching_ambient_legacy_workspace(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            identity = CoreIndexIdentity("existing", Path(directory) / "core")
            runtime, rag, _, error = await self.start_real_core_without_transport(
                identity, default_identity=True, postgres_workspace="existing"
            )
            self.assertIsNone(error)
            self.assertTrue(runtime.is_ready)
            self.assertEqual(rag.workspace, "existing")

    async def test_default_identity_respects_upstream_config_ini_workspace(self):
        for config_workspace in ("existing", "different"):
            with self.subTest(workspace=config_workspace):
                with tempfile.TemporaryDirectory() as directory, chdir(directory):
                    Path("config.ini").write_text(
                        f"[postgres]\nworkspace={config_workspace}\n", encoding="utf-8"
                    )
                    identity = CoreIndexIdentity("existing", Path(directory) / "core")
                    runtime, rag, _, error = await self.start_real_core_without_transport(
                        identity, default_identity=True, postgres_workspace=None
                    )
                    if config_workspace == "existing":
                        self.assertIsNone(error)
                        self.assertTrue(runtime.is_ready)
                    else:
                        self.assertIsInstance(error, ValueError)
                        self.assertIsNone(rag)
                        self.assertFalse(runtime.restart_required)

    async def test_explicit_identity_overrides_old_environment_and_config_without_vector_token(self):
        for old_environment in ("old-environment", None):
            with self.subTest(old_environment=old_environment):
                with tempfile.TemporaryDirectory() as directory, chdir(directory):
                    Path("config.ini").write_text(
                        "[postgres]\nworkspace=old-config\n", encoding="utf-8"
                    )
                    identity = CoreIndexIdentity("target", Path(directory) / "core")
                    runtime, rag, _, error = await self.start_real_core_without_transport(
                        identity, postgres_workspace=old_environment
                    )
                    self.assertIsNone(error)
                    self.assertTrue(runtime.is_ready)
                    self.assertIsNone(rag.embedding_func.model_name)
                    self.assertEqual(rag.workspace, "target")

    async def test_default_identity_uses_environment_before_config_ini(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            Path("config.ini").write_text("[postgres]\nworkspace=old-config\n", encoding="utf-8")
            identity = CoreIndexIdentity("existing", Path(directory) / "core")
            runtime, rag, _, error = await self.start_real_core_without_transport(
                identity, default_identity=True, postgres_workspace="existing"
            )
            self.assertIsNone(error)
            self.assertTrue(runtime.is_ready)
            self.assertEqual(rag.workspace, "existing")

    async def test_target_token_reaches_real_vector_storages_and_all_workspaces(self):
        identity = CoreIndexIdentity(
            "知识-v1.0",
            Path(tempfile.gettempdir()) / "knowgrain-core-identity-target",
            "kg_" + "b" * 24,
        )
        runtime, rag, _, startup_error = await self.start_real_core_without_transport(identity)

        self.assertIsNone(startup_error)
        self.assertIsInstance(rag, __import__("lightrag").LightRAG)
        self.assertTrue(runtime.is_ready)
        self.assertIs(runtime.index_identity, identity)
        with self.assertRaises(AttributeError):
            runtime.index_identity = identity  # type: ignore[misc]
        self.assertEqual(Path(rag.working_dir), identity.working_dir)
        self.assertEqual(rag.workspace, identity.workspace)

        for attribute in _STORAGE_ATTRIBUTES:
            with self.subTest(storage=attribute):
                self.assertEqual(getattr(rag, attribute).workspace, identity.workspace)

        expected_suffix = f"{identity.vector_model_name}_{rag.embedding_func.embedding_dim}d"
        for attribute in ("entities_vdb", "relationships_vdb", "chunks_vdb"):
            storage = getattr(rag, attribute)
            with self.subTest(vector_storage=attribute):
                self.assertEqual(storage.embedding_func.model_name, identity.vector_model_name)
                self.assertEqual(storage.model_suffix, expected_suffix)
                self.assertTrue(storage.table_name.endswith(expected_suffix))
                self.assertLessEqual(len(storage.table_name), 63)

    async def test_dimension_that_exceeds_upstream_table_limit_fails_before_core_creation(self):
        identity = CoreIndexIdentity(
            "workspace-current",
            Path("./must-not-be-created"),
            "kg_" + "c" * 24,
        )
        runtime, rag, finalized, startup_error = await self.start_real_core_without_transport(
            identity,
            embedding_dim=10**12,
        )

        self.assertIsInstance(startup_error, ValueError)
        self.assertIn("exceed PostgreSQL's 63-character", str(startup_error))
        self.assertIsNone(rag)
        self.assertFalse(runtime.is_ready)
        self.assertFalse(runtime.restart_required)
        self.assertIsNone(runtime._partial_rag)
        self.assertEqual(finalized, {})

    async def test_legacy_startup_keeps_the_existing_unsuffixed_vector_tables(self):
        identity = CoreIndexIdentity(
            "knowgrain_setup_acceptance",
            Path(tempfile.gettempdir()) / "knowgrain-core-identity-legacy",
        )
        runtime, rag, _, startup_error = await self.start_real_core_without_transport(identity)

        self.assertIsNone(startup_error)
        self.assertTrue(runtime.is_ready)
        self.assertIsNone(rag.embedding_func.model_name)
        for attribute in ("entities_vdb", "relationships_vdb", "chunks_vdb"):
            storage = getattr(rag, attribute)
            with self.subTest(vector_storage=attribute):
                self.assertIsNone(storage.model_suffix)
                self.assertEqual(storage.table_name, storage.legacy_table_name)

    async def test_workspace_mismatch_cleans_up_and_requires_restart(self):
        identity = CoreIndexIdentity("workspace-current", Path("./ignored"))
        runtime, rag, finalized, startup_error = await self.start_real_core_without_transport(
            identity, fault="workspace_mismatch"
        )

        self.assertIsInstance(startup_error, RuntimeError)
        self.assertIn("chunks_vdb (workspace mismatch)", str(startup_error))
        self.assertFalse(runtime.is_ready)
        self.assertTrue(runtime.restart_required)
        self.assertIsNone(runtime._partial_rag)
        self.assertIn("identity validation failed", runtime.restart_required_detail)
        self.assertEqual(len(finalized), len(_STORAGE_ATTRIBUTES))
        for finalizer in finalized.values():
            finalizer.assert_awaited_once()

    async def test_missing_storage_workspace_is_rejected_and_cleaned_up(self):
        identity = CoreIndexIdentity("workspace-current", Path("./ignored"))
        runtime, _, finalized, startup_error = await self.start_real_core_without_transport(
            identity, fault="missing_workspace"
        )

        self.assertIsInstance(startup_error, RuntimeError)
        self.assertIn("doc_status (missing workspace)", str(startup_error))
        self.assertFalse(runtime.is_ready)
        self.assertTrue(runtime.restart_required)
        self.assertIsNone(runtime._partial_rag)
        self.assertIn("identity validation failed", runtime.restart_required_detail)
        for finalizer in finalized.values():
            finalizer.assert_awaited_once()

    async def test_missing_storage_fails_closed_and_finalizes_remaining_storages(self):
        identity = CoreIndexIdentity("workspace-current", Path("./ignored"))
        runtime, _, finalized, startup_error = await self.start_real_core_without_transport(
            identity, fault="missing_storage"
        )

        self.assertIsInstance(startup_error, RuntimeError)
        self.assertIn("text_chunks (missing storage)", str(startup_error))
        self.assertFalse(runtime.is_ready)
        self.assertTrue(runtime.restart_required)
        self.assertIsNotNone(runtime._partial_rag)
        self.assertIn("text_chunks (AttributeError)", runtime.restart_required_detail)
        self.assertIn("best-effort storage cleanup failed", runtime.restart_required_detail)
        self.assertEqual(len(finalized), len(_STORAGE_ATTRIBUTES))
        for attribute, finalizer in finalized.items():
            if attribute == "text_chunks":
                finalizer.assert_not_awaited()
            else:
                finalizer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
