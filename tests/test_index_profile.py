import copy
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from knowgrain.index_profile import (
    IndexProfile,
    ModelRevision,
    ProfileMismatchError,
    ProfileValidationError,
)
from knowgrain.providers import (
    EmbeddingRoleConfig,
    LLMRoleConfig,
    ProviderCallbacks,
    build_provider_callbacks,
)
from knowgrain.tokenizer_cache import MAX_RESOURCE_BYTES, TOKENIZER_SHA256, cache_path


class IndexProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lightrag import LightRAG
        from lightrag.utils import EmbeddingFunc

        # Use the local fixed resource; no download or model/storage initialization.
        cls.cache = Path(os.environ.get("TIKTOKEN_CACHE_DIR", "data/tokenizers"))
        try:
            with cache_path(cls.cache).open("rb") as resource:
                content = resource.read(MAX_RESOURCE_BYTES + 1)
        except OSError:
            raise RuntimeError(
                "Tokenizer cache is missing; run make tokenizer before tests"
            ) from None
        if (
            len(content) > MAX_RESOURCE_BYTES
            or hashlib.sha256(content).hexdigest() != TOKENIZER_SHA256
        ):
            raise RuntimeError("Tokenizer cache is invalid; run make tokenizer before tests")
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.environment = patch.dict(os.environ, {"TIKTOKEN_CACHE_DIR": str(cls.cache.resolve())})
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        cls.callbacks = build_provider_callbacks(
            LLMRoleConfig(
                provider="ollama",
                base_url="http://localhost:11434",
                model="llm-test",
                api_key="private-llm-key",
            ),
            EmbeddingRoleConfig(
                provider="ollama",
                base_url="http://localhost:11434",
                model="embed-test",
                dimension=2,
                api_key="private-embed-key",
                document_prefix="D:",
                query_prefix="Q:",
            ),
        )
        cls.rag = LightRAG(
            working_dir=cls.directory.name,
            tiktoken_model_name="gpt-4o",
            llm_model_name=cls.callbacks.llm_config.model,
            llm_model_func=cls.callbacks.llm,
            embedding_func=EmbeddingFunc(
                embedding_dim=2,
                max_token_size=32768,
                supports_asymmetric=True,
                func=cls.callbacks.embed,
            ),
        )

    def capture(self, **kwargs):
        return IndexProfile.capture(self.rag, self.callbacks, self.cache, **kwargs)

    def edit_json(self, profile, edit):
        data = json.loads(profile.to_canonical_json())
        edit(data)
        return IndexProfile.from_canonical_json(json.dumps(data))

    def test_actual_defaults_roundtrip_immutable_and_read_only(self):
        before_env = dict(os.environ)
        before_addon = copy.deepcopy(dict(self.rag.addon_params))
        before_files = sorted(Path(self.directory.name).rglob("*"))
        with (
            patch.object(self.rag, "_build_global_config", side_effect=AssertionError),
            patch.object(self.rag, "initialize_storages", side_effect=AssertionError),
            patch.object(self.rag, "ainsert", side_effect=AssertionError),
        ):
            seal = self.capture()
            seal.assert_matches(self.rag, self.callbacks, self.cache)
        self.assertEqual(before_env, dict(os.environ))
        self.assertEqual(before_addon, self.rag.addon_params)
        self.assertEqual(before_files, sorted(Path(self.directory.name).rglob("*")))
        restored = IndexProfile.from_canonical_json(seal.to_canonical_json())
        self.assertTrue(seal.compare(restored).matches)
        self.assertEqual(seal.snapshot_fingerprint, restored.snapshot_fingerprint)
        with self.assertRaises(FrozenInstanceError):
            seal._canonical_json = "{}"
        data = json.loads(seal.to_canonical_json())
        data["content"]["chunker"]["fixed_token"]["split_by_character"] = "changed"
        self.assertNotIn("changed", seal.to_canonical_json())
        self.assertEqual(data["content"]["tokenizer"]["resource_sha256"], TOKENIZER_SHA256)
        self.assertGreater(data["content"]["legacy_options"]["chunk_token_size"], 0)
        self.assertEqual(data["embedding"]["revision"]["provenance"], "unavailable")
        self.assertNotIn("api_key", seal.to_canonical_json())
        self.assertNotIn("private-", seal.to_canonical_json())
        safe_repr = repr(seal)
        self.assertNotIn("http://localhost:11434", safe_repr)
        self.assertNotIn("private-", safe_repr)
        self.assertNotIn(data["graph"]["prompt"]["entity_types_guidance"], safe_repr)
        public = json.dumps(seal.public_summary())
        self.assertNotIn("entity_types_guidance", public)
        self.assertNotIn(str(self.cache.resolve()), public)

    def test_binding_rejection(self):
        for field, value in (
            ("llm_model_name", "wrong"),
            ("llm_model_func", lambda: None),
            ("chunking_func", lambda: None),
            ("tokenizer", object()),
            ("role_llm_configs", {"extract": {}}),
            ("vlm_process_enable", True),
        ):
            with (
                self.subTest(field=field),
                patch.object(self.rag, field, value),
                self.assertRaises(ProfileValidationError),
            ):
                self.capture()
        for field, value in (
            ("supports_asymmetric", False),
            ("embedding_dim", 3),
            ("max_token_size", 10),
            ("send_dimensions", True),
            ("func", lambda: None),
        ):
            with (
                self.subTest(field=field),
                patch.object(self.rag.embedding_func, field, value),
                self.assertRaises(ProfileValidationError),
            ):
                self.capture()
        with (
            patch.object(self.rag._role_llm_states["extract"], "raw_func", lambda: None),
            self.assertRaises(ProfileValidationError),
        ):
            self.capture()

    def test_factory_provenance_and_embedding_subclass_rejected(self):
        from lightrag.utils import EmbeddingFunc

        class AlternateEmbedding(EmbeddingFunc):
            async def __call__(self, *args, **kwargs):
                raise AssertionError("Custom behavior must never run")

        current = self.rag.embedding_func
        alternate = AlternateEmbedding(
            embedding_dim=current.embedding_dim,
            func=current.func,
            max_token_size=current.max_token_size,
            supports_asymmetric=True,
        )
        with (
            patch.object(self.rag, "embedding_func", alternate),
            self.assertRaises(ProfileValidationError) as error,
        ):
            self.capture()
        self.assertIn("embedding.binding", str(error.exception))
        fake = ProviderCallbacks(
            lambda: None,
            lambda: None,
            self.callbacks.llm_config,
            self.callbacks.embedding_config,
        )
        wrong_config = ProviderCallbacks(
            self.callbacks.llm,
            self.callbacks.embed,
            self.callbacks.llm_config,
            self.callbacks.embedding_config.model_copy(update={"document_prefix": "wrong"}),
        )
        for callbacks in (fake, wrong_config):
            with (
                self.subTest(callbacks=type(callbacks)),
                self.assertRaises(ProfileValidationError) as error,
            ):
                IndexProfile.capture(self.rag, callbacks, self.cache)
            self.assertIn("factory_binding", str(error.exception))

    def test_actual_vector_storage_embedding_binding(self):
        for name in ("chunks_vdb", "entities_vdb", "relationships_vdb"):
            with (
                self.subTest(storage=name),
                patch.object(getattr(self.rag, name), "embedding_func", object()),
                self.assertRaises(ProfileValidationError),
            ):
                self.capture()

    def test_actual_role_wrapper_and_declared_options_agree(self):
        from functools import partial, wraps

        state = self.rag._role_llm_states["extract"]
        with (
            patch.object(state, "wrapped", lambda: None),
            self.assertRaises(ProfileValidationError),
        ):
            self.capture()
        with patch.object(state, "kwargs", {"seed": 42}), self.assertRaises(ProfileValidationError):
            self.capture()
        with (
            patch.object(state, "wrapped", partial(self.callbacks.llm, hashing_kv=object())),
            self.assertRaises(ProfileValidationError),
        ):
            self.capture()
        with patch.object(
            state,
            "wrapped",
            partial(
                self.callbacks.llm, hashing_kv=self.rag.llm_response_cache, api_key="private-key"
            ),
        ):
            with self.assertRaises(ProfileValidationError) as error:
                self.capture()
            self.assertNotIn("private-key", str(error.exception))
        seal = self.capture()

        @wraps(state.wrapped)
        async def replacement(*args, **kwargs):
            raise AssertionError("No model call")

        with (
            patch.object(state, "wrapped", replacement),
            self.assertRaises(ProfileValidationError),
        ):
            seal.assert_matches(self.rag, self.callbacks, self.cache)

    def test_custom_queue_wrapper_rejected_before_capture_and_after_restore(self):
        from functools import wraps

        seal = self.capture()
        restored = IndexProfile.from_canonical_json(seal.to_canonical_json())
        bindings = [(self.rag.embedding_func, "func")]
        bindings.extend((state, "wrapped") for state in self.rag._role_llm_states.values())
        for owner, name in bindings:
            original = getattr(owner, name)

            @wraps(original)
            async def replacement(*args, **kwargs):
                raise AssertionError("Custom behavior must never execute")

            with self.subTest(binding=name), patch.object(owner, name, replacement):
                with self.assertRaises(ProfileValidationError):
                    self.capture()
                with self.assertRaises(ProfileValidationError):
                    restored.assert_matches(self.rag, self.callbacks, self.cache)
        # Mutating declarations does not rewrite a queue's effective closure.
        for field in ("llm_model_max_async", "default_llm_timeout", "embedding_func_max_async"):
            with (
                self.subTest(field=field),
                patch.object(self.rag, field, getattr(self.rag, field) + 1),
                self.assertRaises(ProfileValidationError),
            ):
                self.capture()

    def test_nested_chunk_prompt_and_live_env_drift(self):
        seal = self.capture()
        fixed = self.rag.addon_params["chunker"]["fixed_token"]
        with patch.dict(fixed, {"split_by_character": "|"}):
            comparison = seal.compare(self.capture())
            self.assertIn("content_embedding", comparison.categories)
            self.assertIn("content.chunker.fixed_token.split_by_character", comparison.paths)
        prompt = self.rag._entity_extraction_prompt_profile
        with patch.dict(prompt, {"entity_types_guidance": "private prompt drift"}):
            changed = self.capture()
            comparison = seal.compare(changed)
            self.assertIn("graph_write", comparison.categories)
            self.assertNotIn("content_embedding", comparison.categories)
            with self.assertRaises(ProfileMismatchError) as error:
                seal.assert_matches(changed)
            self.assertNotIn("private prompt drift", str(error.exception))
        with patch.dict(os.environ, {"MAX_EXTRACT_INPUT_TOKENS": "12345"}):
            self.assertIn("graph.live_max_extract_input_tokens", seal.compare(self.capture()).paths)
        with (
            patch.object(self.rag, "_addon_params_dirty", True),
            self.assertRaises(ProfileValidationError),
        ):
            self.capture()

    def test_effective_role_cache_identity_cannot_drift(self):
        seal = self.capture()
        for role in ("extract", "keyword", "query", "vlm"):
            state = self.rag._role_llm_states[role]
            for key, value in (
                ("model", "alternate-cache-model"),
                ("host", "http://private-provider"),
                ("binding", "alternate-provider"),
                ("api_key", "private-secret"),
            ):
                with self.subTest(role=role, key=key), patch.object(state, "metadata", {}):
                    self.rag.set_role_llm_metadata(role, **{key: value})
                    with self.assertRaises(ProfileValidationError) as error:
                        seal.assert_matches(self.rag, self.callbacks, self.cache)
                    self.assertIn("llm.roles." + role + ".cache_identity", str(error.exception))
                    self.assertNotIn(value, str(error.exception))
            for model in (None, "", self.callbacks.llm_config.model):
                with (
                    self.subTest(role=role, model=model),
                    patch.object(
                        state,
                        "metadata",
                        {"model": model, "binding": None, "host": None, "is_cross_provider": False},
                    ),
                ):
                    self.assertTrue(seal.compare(self.capture()).matches)

    def test_classification_operational_sampling_and_prompt(self):
        seal = self.capture()
        operational = self.edit_json(
            seal, lambda d: d["embedding"]["config"].update(timeout_seconds=99)
        )
        self.assertEqual(
            seal.content_embedding_fingerprint, operational.content_embedding_fingerprint
        )
        self.assertEqual(seal.graph_write_fingerprint, operational.graph_write_fingerprint)
        query = self.edit_json(
            seal, lambda d: d["llm"]["roles"]["query"]["sampling"].update(seed=4)
        )
        self.assertEqual(seal.graph_write_fingerprint, query.graph_write_fingerprint)
        extract = self.edit_json(
            seal, lambda d: d["llm"]["roles"]["extract"]["sampling"].update(seed=4)
        )
        self.assertNotEqual(seal.graph_write_fingerprint, extract.graph_write_fingerprint)
        self.assertEqual(seal.content_embedding_fingerprint, extract.content_embedding_fingerprint)
        from lightrag.prompt import PROMPTS

        with patch.dict(PROMPTS, {"rag_response": "query-template-change"}):
            current = self.capture()
            self.assertEqual(
                seal.content_embedding_fingerprint, current.content_embedding_fingerprint
            )
            self.assertEqual(seal.graph_write_fingerprint, current.graph_write_fingerprint)
            self.assertNotEqual(seal.snapshot_fingerprint, current.snapshot_fingerprint)
        with patch.dict(PROMPTS, {"summarize_entity_descriptions": "graph-template-change"}):
            current = self.capture()
            self.assertNotEqual(seal.graph_write_fingerprint, current.graph_write_fingerprint)
            self.assertEqual(
                seal.content_embedding_fingerprint, current.content_embedding_fingerprint
            )

    def test_revision_binding_and_unavailable(self):
        config = self.callbacks.embedding_config
        revision = ModelRevision(
            config.provider, config.base_url, config.model, "sha256:observed", "ollama-tag"
        )
        seal = self.capture(model_revisions={"embedding": revision})
        self.assertIn("sha256:observed", seal.to_canonical_json())
        self.assertNotEqual(
            seal.content_embedding_fingerprint, self.capture().content_embedding_fingerprint
        )
        for wrong in (
            ModelRevision(config.provider, config.base_url, "wrong"),
            ModelRevision(config.provider, config.base_url, config.model, "", "operator"),
            ModelRevision(config.provider, config.base_url, config.model, "secret", "unavailable"),
        ):
            with self.assertRaises(ProfileValidationError):
                self.capture(model_revisions={"embedding": wrong})

    def test_closed_schema_type_literal_and_secret_rejection(self):
        seal = self.capture()
        edits = (
            lambda d: d.update(api_key="secret"),
            lambda d: d["embedding"]["config"].update(api_key="secret"),
            lambda d: d["content"].update(parser_version="unknown"),
            lambda d: d["content"]["legacy_options"].update(chunk_token_size=True),
            lambda d: d["content"]["legacy_options"].update(chunk_token_size=0),
            lambda d: d["llm"]["roles"]["extract"]["sampling"].update(temperature=float("nan")),
            lambda d: d["llm"]["roles"]["extract"]["sampling"].update(top_p=float("inf")),
            lambda d: d["llm"]["roles"]["extract"]["sampling"].update(stop=[1]),
            lambda d: d["embedding"]["revision"].update(provenance="verified"),
            lambda d: d["content"]["chunker"]["fixed_token"].update(secret="private"),
            lambda d: d.pop("graph"),
            lambda d: d["operations"].update(embedding_batch_num=0),
        )
        for edit in edits:
            with self.subTest(edit=edit), self.assertRaises(ProfileValidationError) as error:
                self.edit_json(seal, edit)
            self.assertNotIn("secret", str(error.exception))
            self.assertNotIn("private", str(error.exception))
        for raw in ('{"schema_version":1,"schema_version":1}', "[]", "null", "NaN"):
            with self.assertRaises(ProfileValidationError):
                IndexProfile.from_canonical_json(raw)

    def test_safe_resource_and_invalid_object_errors(self):
        with tempfile.TemporaryDirectory(prefix="private-vault-") as directory:
            with self.assertRaises(ProfileValidationError) as error:
                IndexProfile.capture(self.rag, self.callbacks, Path(directory))
            self.assertNotIn(directory, str(error.exception))
        with self.assertRaises(ProfileValidationError):
            IndexProfile.capture(object(), self.callbacks, self.cache)


if __name__ == "__main__":
    unittest.main()
