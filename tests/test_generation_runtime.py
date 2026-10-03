"""Boundary checks for the configured same-loop generation callback."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from knowgrain.config import Settings
from knowgrain.lightrag_runtime import LightRAGRuntime
from tests.test_core_lifecycle import install_close_protocol


class GenerationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_binds_stored_parent_and_rejects_unverified_chunks(self):
        revision_id = uuid4()
        query = AsyncMock(return_value={"status": "success", "data": {"chunks": [
            {"chunk_id": "valid", "content": "original", "file_path": "same.txt",
             "source_revision_id": str(uuid4())},
            {"chunk_id": "missing", "content": "original", "file_path": "same.txt"},
            {"chunk_id": "changed", "content": "invented", "file_path": "same.txt"},
            {"chunk_id": "invalid", "content": "original", "file_path": "same.txt"},
        ]}})
        storage = AsyncMock(return_value=[
            {"full_doc_id": str(revision_id), "content": "original"}, None,
            {"full_doc_id": str(revision_id), "content": "original"},
            {"full_doc_id": "not-a-revision", "content": "original"},
        ])
        runtime = LightRAGRuntime(Settings(_env_file=None))
        runtime._rag = SimpleNamespace(aquery_data=query,
                                      text_chunks=SimpleNamespace(get_by_ids=storage))
        result = await runtime.retrieve("topic")
        self.assertEqual(len(result["data"]["chunks"]), 1)
        self.assertEqual(result["data"]["chunks"][0]["source_revision_id"], str(revision_id))
        storage.assert_awaited_once_with(["valid", "missing", "changed", "invalid"])

    async def test_close_drains_cancelled_real_queue_before_storage_finalization(self):
        from lightrag.llm_roles import _RoleLLMMixin

        started, stopped = asyncio.Event(), asyncio.Event()

        async def provider(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        runtime = LightRAGRuntime(Settings(_env_file=None))
        callback = _RoleLLMMixin._wrap_llm_role_func(
            SimpleNamespace(llm_response_cache=None), "query", provider, 1, 30, {},
        )

        async def finalized(name):
            self.assertTrue(stopped.is_set(), "provider must finish before cache storage closes")

        runtime._rag, registry, _ = install_close_protocol(callback=callback, on_finalize=finalized)
        registry_patch = patch("lightrag.kg.postgres_impl.ClientManager._instances", registry)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        generating = asyncio.create_task(runtime.generate_json("system", "synthetic quote"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            generating.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await generating
            await asyncio.wait_for(runtime.close(), 2)
            self.assertTrue(stopped.is_set())
            self.assertIsNone(runtime._rag)
        finally:
            generating.cancel()
            await asyncio.gather(generating, return_exceptions=True)
            await callback.shutdown(graceful=False)

    async def test_pinned_core_role_binds_real_ollama_adapter(self):
        from lightrag.llm.ollama import ollama_model_complete
        from lightrag.llm_roles import _RoleLLMMixin

        runtime = LightRAGRuntime(Settings(_env_file=None))
        cache = SimpleNamespace(global_config={"llm_model_name": "configured-local-model"})
        rag = SimpleNamespace(llm_response_cache=cache)
        callback = _RoleLLMMixin._wrap_llm_role_func(
            rag, "query", ollama_model_complete, 1, 30,
            {"host": "http://configured-local-host:11434"},
        )
        runtime._rag = SimpleNamespace(role_llm_funcs={"query": callback})
        try:
            with patch("lightrag.llm.ollama._ollama_model_if_cache", new_callable=AsyncMock) as provider:
                provider.return_value = '{}'
                self.assertEqual(await runtime.generate_json("system", "quote"), '{}')
                self.assertEqual(provider.call_args.args, ("configured-local-model", "quote"))
                self.assertEqual(provider.call_args.kwargs["host"], "http://configured-local-host:11434")
                self.assertIs(provider.call_args.kwargs["hashing_kv"], cache)
                self.assertEqual(provider.call_args.kwargs["response_format"], {"type": "json_object"})
        finally:
            await callback.shutdown(graceful=True)

    async def test_generation_uses_existing_callback_and_json_mode(self):
        runtime = LightRAGRuntime(Settings(_env_file=None))
        model = AsyncMock(return_value='{"title":"Supported"}')
        runtime._rag = SimpleNamespace(role_llm_funcs={"query": model})
        result = await runtime.generate_json("Use provided evidence only", "Verified excerpt")
        self.assertEqual(result, '{"title":"Supported"}')
        arguments = model.call_args
        self.assertEqual(arguments.args, ("Verified excerpt",))
        self.assertEqual(arguments.kwargs["response_format"], {"type": "json_object"})
        self.assertFalse(arguments.kwargs["stream"])
        self.assertEqual(arguments.kwargs["system_prompt"], "Use provided evidence only")
        self.assertIsNotNone(runtime._event_loop)

    async def test_generation_requires_core_and_rejects_unusable_response(self):
        runtime = LightRAGRuntime(Settings(_env_file=None))
        with self.assertRaises(RuntimeError):
            await runtime.generate_json("system", "prompt")
        for invalid in (None, {}, "  ", "界" * 44_000):
            runtime._rag = SimpleNamespace(role_llm_funcs={"query": AsyncMock(return_value=invalid)})
            with self.subTest(response_type=type(invalid).__name__), self.assertRaises(ValueError):
                await runtime.generate_json("system", "prompt")
