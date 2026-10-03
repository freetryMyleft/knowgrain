import asyncio
from copy import deepcopy
import hashlib
import json
import os
import unittest
from unittest.mock import patch

import httpx
import numpy as np
from pydantic import ValidationError

from knowgrain.config import Settings
from knowgrain.providers import (
    EmbeddingRoleConfig, LLMRoleConfig, ProviderError, build_provider_callbacks,
    role_configs_from_settings,
)


def configs(llm_provider="ollama", embed_provider="ollama", **embed_options):
    return (
        LLMRoleConfig(
            provider=llm_provider, base_url="https://llm.example/deploy/v1/",
            model="llm-real", api_key="llm-secret", context_size=4096,
        ),
        EmbeddingRoleConfig(
            provider=embed_provider, base_url="https://embed.example/other/v1/",
            model="embedding-real", api_key="embedding-secret", dimension=2,
            document_prefix="D: ", query_prefix="Q: ", **embed_options,
        ),
    )


def llm_response(provider, text="answer"):
    if provider == "ollama":
        return {"done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": text}}
    return {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


class ConfigTests(unittest.TestCase):
    def test_immutable_secret_free_canonical_metadata(self):
        llm, embedding = configs()
        for config in (llm, embedding):
            self.assertNotIn("secret", repr(config))
            self.assertNotIn("api_key", config.model_dump())
            self.assertNotIn("secret", config.model_dump_json())
            self.assertEqual(config.fingerprint(), hashlib.sha256(
                config.canonical_json().encode("utf-8")).hexdigest())
            with self.assertRaises(ValidationError):
                config.model = "changed"
            metadata = config.canonical_config()
            metadata["model"] = "changed"
            self.assertNotEqual(config.model, "changed")
        self.assertEqual(llm.base_url, "https://llm.example/deploy/v1")
        same = embedding.model_copy(update={"api_key": None})
        self.assertEqual(same.fingerprint(), embedding.fingerprint())
        for field, value in {
            "provider": "openai-compatible", "base_url": "https://different.example/v1",
            "model": "different", "dimension": 3, "max_token_size": 128,
            "document_prefix": "new", "query_prefix": "new", "send_dimensions": True,
        }.items():
            with self.subTest(field=field):
                self.assertNotEqual(
                    embedding.model_copy(update={field: value}).fingerprint(),
                    embedding.fingerprint(),
                )
        unicode_config = embedding.model_copy(update={"query_prefix": "检索："})
        self.assertIn("检索：", unicode_config.canonical_json())

    def test_invalid_endpoints_and_bounded_values(self):
        for url in ["", "https://", "ftp://valid.example", "https://user:secret@host",
                    "https://host?key=secret", "https://host#fragment", "https://host?",
                    "https://host#", "http://host\\path", "http://host\n/api",
                    "http://bad host", "http://.host", "http://bad..host",
                    "http://host:70000", "http://host:", "http://[broken",
                    "http://host%20evil"]:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                LLMRoleConfig(provider="ollama", base_url=url, model="test")
        for timeout in [0, -1, 601, float("nan"), float("inf")]:
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                LLMRoleConfig(provider="ollama", base_url="http://localhost", model="test",
                              timeout_seconds=timeout)
        for dimension in [0, -1, 16001, True, "2"]:
            with self.subTest(dimension=dimension), self.assertRaises(ValidationError):
                EmbeddingRoleConfig(provider="ollama", base_url="http://localhost",
                                    model="test", dimension=dimension)
        with self.assertRaises(ValidationError) as error:
            LLMRoleConfig(provider="ollama", base_url="http://localhost", model="test",
                          api_key={"sensitive": "raw-secret"})
        self.assertNotIn("raw-secret", str(error.exception))
        self.assertEqual(LLMRoleConfig(
            provider="ollama", base_url="HTTP://LOCALHOST:80/custom/", model="test",
        ).base_url, "http://localhost/custom")
        for original, canonical in [
            ("https://HOST:443/deploy/v1/", "https://host/deploy/v1"),
            ("http://[::1]:80/v1/", "http://[::1]/v1"),
            ("http://[::1]:11434/v1/", "http://[::1]:11434/v1"),
        ]:
            with self.subTest(original=original):
                self.assertEqual(LLMRoleConfig(
                    provider="ollama", base_url=original, model="test",
                ).base_url, canonical)

    def test_settings_factory_uses_only_existing_local_settings(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-key"}):
            settings = Settings(_env_file=None)
            llm, embed = role_configs_from_settings(settings)
        self.assertEqual((llm.provider, embed.provider), ("ollama", "ollama"))
        self.assertEqual(llm.model, settings.llm_model)
        self.assertEqual(embed.dimension, settings.embedding_dim)
        self.assertEqual(llm.context_size, settings.llm_context_size)
        self.assertIsNone(llm.api_key)
        self.assertIsNone(embed.api_key)


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_four_role_combinations_exact_wires(self):
        for llm_provider in ["ollama", "openai-compatible"]:
            for embed_provider in ["ollama", "openai-compatible"]:
                with self.subTest(llm=llm_provider, embedding=embed_provider):
                    llm, embed = configs(llm_provider, embed_provider, send_dimensions=True)
                    seen = []

                    def handler(request):
                        body = json.loads(request.content)
                        seen.append((request, body))
                        if request.url.host == "llm.example":
                            return httpx.Response(200, json=llm_response(llm_provider))
                        return httpx.Response(200, json=(
                            {"embeddings": [[1, 2], [3, 4]]} if embed_provider == "ollama"
                            else {"data": [{"index": 1, "embedding": [3, 4]},
                                           {"index": 0, "embedding": [1, 2]}]}
                        ))

                    callbacks = build_provider_callbacks(llm, embed, transport=httpx.MockTransport(handler))
                    history = [{"role": "user", "content": "previous"},
                               {"role": "assistant", "content": "past answer"}]
                    before = deepcopy(history)
                    text = await callbacks.llm(
                        "current", system_prompt="system", history_messages=history,
                        max_tokens=10, temperature=0.2, top_p=0.8, seed=42, stop=["END"],
                        response_format={"type": "json_object"}, hashing_kv=object(),
                        token_tracker=object(), _priority=2, enable_llm_cache=True,
                    )
                    self.assertEqual(text, "answer")
                    self.assertEqual(history, before)
                    matrix = await callbacks.embed(["one", "two"], context="query")
                    np.testing.assert_array_equal(matrix, [[1, 2], [3, 4]])
                    self.assertTrue(matrix.flags.c_contiguous)
                    self.assertEqual(matrix.dtype.kind, "f")
                    request, body = seen[0]
                    self.assertEqual(str(request.url), "https://llm.example/deploy/v1" + (
                        "/api/chat" if llm_provider == "ollama" else "/chat/completions"))
                    self.assertEqual(request.headers["authorization"], "Bearer llm-secret")
                    self.assertEqual(body["model"], "llm-real")
                    self.assertEqual(body["messages"], [{"role": "system", "content": "system"},
                                                      *before, {"role": "user", "content": "current"}])
                    self.assertFalse(body["stream"])
                    if llm_provider == "ollama":
                        self.assertEqual(body["format"], "json")
                        self.assertEqual(body["options"], {"num_ctx": 4096, "num_predict": 10,
                                                          "temperature": 0.2, "top_p": 0.8,
                                                          "seed": 42, "stop": ["END"]})
                    else:
                        self.assertEqual(body["response_format"], {"type": "json_object"})
                        self.assertEqual(body["max_tokens"], 10)
                    request, body = seen[1]
                    self.assertEqual(str(request.url), "https://embed.example/other/v1" + (
                        "/api/embed" if embed_provider == "ollama" else "/embeddings"))
                    self.assertEqual(request.headers["authorization"], "Bearer embedding-secret")
                    self.assertEqual(body["model"], "embedding-real")
                    self.assertEqual(body["input"], ["Q: one", "Q: two"])
                    if embed_provider == "ollama":
                        self.assertFalse(body["truncate"])
                        self.assertNotIn("dimensions", body)
                    else:
                        self.assertEqual(body["dimensions"], 2)
                        self.assertEqual(body["encoding_format"], "float")
                    self.assertNotIn("api_key", callbacks.llm_metadata)
                    self.assertEqual(callbacks.embedding_role_fingerprint, embed.fingerprint())

    async def test_no_ambient_or_cross_role_credential_and_dimensions_opt_in(self):
        llm, embed = configs("openai-compatible", "openai-compatible")
        embed = embed.model_copy(update={"api_key": None})
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})

        with patch.dict(os.environ, {"OPENAI_API_KEY": "ambient", "HTTPS_PROXY": "http://bad.proxy"}):
            callbacks = build_provider_callbacks(llm, embed, transport=httpx.MockTransport(handler))
            await callbacks.embed(["text"])
        self.assertNotIn("authorization", seen[0].headers)
        self.assertNotIn("dimensions", json.loads(seen[0].content))
        self.assertEqual(json.loads(seen[0].content)["input"], ["D: text"])

    async def test_actual_core_embedding_wrapper_keeps_storage_token_separate(self):
        from lightrag.utils import EmbeddingFunc

        llm, embed = configs("ollama", "openai-compatible", send_dimensions=True)
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})

        callbacks = build_provider_callbacks(llm, embed, transport=httpx.MockTransport(handler))
        wrapper = EmbeddingFunc(
            embedding_dim=2, max_token_size=embed.max_token_size, func=callbacks.embed,
            supports_asymmetric=True, send_dimensions=True, model_name="kg_0123456789abcdef01234567",
        )
        await wrapper(["query"], context="query")
        await wrapper(["document"], context="document")
        self.assertEqual(seen[0]["input"], ["Q: query"])
        self.assertEqual(seen[1]["input"], ["D: document"])
        self.assertEqual(seen[0]["model"], "embedding-real")
        self.assertEqual(wrapper.model_name, "kg_0123456789abcdef01234567")
        self.assertEqual(wrapper.max_token_size, embed.max_token_size)

    async def test_empty_embeddings_and_invalid_inputs_never_send(self):
        def handler(request):
            self.fail("Unexpected HTTP request")

        callbacks = build_provider_callbacks(*configs(), transport=httpx.MockTransport(handler))
        self.assertEqual((await callbacks.embed([])).shape, (0, 2))
        for kwargs in [{"context": "invalid"}, {"model": "override"}, {"embedding_dim": 3},
                       {"truncate": True}, {"api_key": "override"}, {"base_url": "override"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ProviderError):
                await callbacks.embed(["text"], **kwargs)
        with self.assertRaises(ProviderError):
            await callbacks.embed([1])
        for kwargs in [{"stream": True}, {"model": "override"}, {"api_key": "override"},
                       {"host": "override"}, {"options": {"num_ctx": 1}},
                       {"response_format": "json"}, {"temperature": float("nan")},
                       {"history_messages": [{"role": "tool", "content": "secret"}]}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ProviderError):
                await callbacks.llm("text", **kwargs)

    async def test_keyword_and_schema_formats(self):
        for provider in ["ollama", "openai-compatible"]:
            seen = []

            def handler(request):
                seen.append(json.loads(request.content))
                return httpx.Response(200, json=llm_response(provider, "{}"))

            callbacks = build_provider_callbacks(*configs(provider), transport=httpx.MockTransport(handler))
            await callbacks.llm("text", keyword_extraction=True)
            schema = {"type": "json_schema", "json_schema": {
                "name": "object", "strict": True, "schema": {"type": "object"}}}
            await callbacks.llm("text", response_format=schema)
            self.assertEqual(seen[0].get("format") if provider == "ollama"
                             else seen[0].get("response_format"),
                             "json" if provider == "ollama" else {"type": "json_object"})
            self.assertEqual(seen[1].get("format") if provider == "ollama"
                             else seen[1].get("response_format"),
                             schema["json_schema"]["schema"] if provider == "ollama" else schema)

    async def test_malformed_embedding_responses_are_rejected(self):
        malformed = [
            {}, {"data": []},
            {"data": [{"index": 0, "embedding": [1, 2]}, {"index": 0, "embedding": [1, 2]}]},
            {"data": [{"index": 0, "embedding": [1, 2]}, {"index": 2, "embedding": [1, 2]}]},
            {"data": [{"index": True, "embedding": [1, 2]}, {"index": 0, "embedding": [1, 2]}]},
            {"data": [{"index": "0", "embedding": [1, 2]}, {"index": 1, "embedding": [1, 2]}]},
        ]
        for provider in ["ollama", "openai-compatible"]:
            for vectors in [[], [[1, 2]], [[1], [2]], [[True, 2], [1, 2]],
                            [["1", 2], [1, 2]], [[float("inf"), 2], [1, 2]],
                            [[float("nan"), 2], [1, 2]], [None, [1, 2]]]:
                data = {"embeddings": vectors} if provider == "ollama" else {
                    "data": [{"index": index, "embedding": vector}
                             for index, vector in enumerate(vectors)]}
                malformed_provider = [data]
                if provider == "openai-compatible":
                    malformed_provider += malformed
                for bad in malformed_provider:
                    # Construct raw JSON because HTTPX's JSON helper rejects nonfinite values.
                    transport = httpx.MockTransport(lambda request: httpx.Response(
                        200, content=json.dumps(bad).encode(), headers={"content-type": "application/json"}))
                    callbacks = build_provider_callbacks(*configs(embed_provider=provider), transport=transport)
                    with self.subTest(provider=provider, bad=bad), self.assertRaises(ProviderError):
                        await callbacks.embed(["private one", "private two"])

    async def test_incomplete_llm_responses_rejected(self):
        for provider in ["ollama", "openai-compatible"]:
            invalid = [{}, llm_response(provider, ""), llm_response(provider, None),
                       llm_response(provider, "   "), llm_response(provider, ["text"])]
            truncated = llm_response(provider)
            if provider == "ollama":
                truncated["done_reason"] = "length"
            else:
                truncated["choices"][0]["finish_reason"] = "length"
            invalid.append(truncated)
            tool = llm_response(provider)
            (tool["message"] if provider == "ollama" else tool["choices"][0]["message"])["tool_calls"] = [{}]
            invalid.append(tool)
            for data in invalid:
                callbacks = build_provider_callbacks(*configs(provider), transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=data)))
                with self.subTest(provider=provider, data=data), self.assertRaises(ProviderError):
                    await callbacks.llm("private prompt")

    async def test_safe_status_json_timeout_transport_errors_no_redirect(self):
        for role in ["llm", "embedding"]:
            for kind in ["status", "redirect", "json", "timeout", "transport"]:
                seen = []

                def handler(request):
                    seen.append(request)
                    if kind == "timeout":
                        raise httpx.ReadTimeout("private prompt embedding-secret", request=request)
                    if kind == "transport":
                        raise httpx.ConnectError("https://llm.example llm-secret", request=request)
                    if kind == "json":
                        return httpx.Response(200, content=b"private prompt invalid json")
                    return httpx.Response(503 if kind == "status" else 302,
                                          content=b"embedding-secret private prompt",
                                          headers={"location": "https://other.example"})

                callbacks = build_provider_callbacks(*configs(), transport=httpx.MockTransport(handler))
                with self.subTest(role=role, kind=kind), self.assertRaises(ProviderError) as caught:
                    await (callbacks.llm("private prompt") if role == "llm"
                           else callbacks.embed(["private prompt"]))
                message = str(caught.exception)
                for secret in ["private prompt", "llm-secret", "embedding-secret", "example"]:
                    self.assertNotIn(secret, message)
                self.assertIn("role=" + role, message)
                self.assertTrue(caught.exception.__suppress_context__ or kind in {"status", "redirect"})
                self.assertEqual(len(seen), 1)

    async def test_invalid_unhashable_inputs_fail_safely_before_request(self):
        def unexpected_request(request):
            self.fail("Invalid input must fail before HTTP")

        callbacks = build_provider_callbacks(*configs(), transport=httpx.MockTransport(unexpected_request))
        for context in [[], {}, None, 1]:
            with self.subTest(context=context), self.assertRaisesRegex(ProviderError, "type=context"):
                await callbacks.embed(["private prompt"], context=context)
        for role in [[], {}, None, 1]:
            with self.subTest(role=role), self.assertRaisesRegex(ProviderError, "type=history"):
                await callbacks.llm("private prompt", history_messages=[{"role": role, "content": "private"}])

    async def test_overflowing_sampling_values_are_safe_errors(self):
        for key in ["temperature", "top_p"]:
            callbacks = build_provider_callbacks(*configs(), transport=httpx.MockTransport(
                lambda request: self.fail("Invalid sampling must fail before HTTP")))
            for value in [10 ** 1000, float("nan"), float("inf"), True, "private-secret"]:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ProviderError, "type=options"):
                    await callbacks.llm("private prompt", **{key: value})

    async def test_refusal_and_deprecated_function_call_cannot_be_success_text(self):
        for provider in ["ollama", "openai-compatible"]:
            fields = {"function_call": {"name": "tool", "arguments": "{}"}}
            if provider == "openai-compatible":
                fields["refusal"] = "private refusal response"
            for field, value in fields.items():
                data = llm_response(provider, "partial text")
                message = data["message"] if provider == "ollama" else data["choices"][0]["message"]
                message[field] = value
                callbacks = build_provider_callbacks(*configs(provider), transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=data)))
                with self.subTest(provider=provider, field=field), self.assertRaises(ProviderError) as caught:
                    await callbacks.llm("private prompt")
                self.assertNotIn("private", str(caught.exception))

    async def test_cancellation_and_deadline_close_client(self):
        class WaitingTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.started = asyncio.Event()
                self.closed = False

            async def handle_async_request(self, request):
                self.started.set()
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        for cancel in [True, False]:
            transport = WaitingTransport()
            llm, embed = configs()
            llm = llm.model_copy(update={"timeout_seconds": 0.03})
            callbacks = build_provider_callbacks(llm, embed, transport=transport)
            task = asyncio.create_task(callbacks.llm("private prompt"))
            await transport.started.wait()
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                with self.assertRaisesRegex(ProviderError, "type=timeout"):
                    await task
            self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
