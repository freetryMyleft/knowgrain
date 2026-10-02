from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from knowgrain.entity_mapping_api import install_entity_mapping_routes
from knowgrain.entity_mapping_service import (
    EntityMappingNotFoundError,
    EntityMappingUnavailableError,
    EntityMappingValidationError,
)


class FakeEntityMapping:
    async def page_entities(self, page_id):
        if page_id == "malformed":
            raise EntityMappingNotFoundError("missing")
        return {"page_id": page_id, "entities": []}

    async def entity_pages(self, name):
        if "\u200b" in name:
            raise EntityMappingValidationError("invalid name")
        if name == "slow":
            await asyncio.sleep(1)
        if name == "unavailable":
            raise EntityMappingUnavailableError("private detail is not exposed")
        return {"name": name, "pages": [], "truncated": False}


class EntityMappingAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        runtime = SimpleNamespace(
            _runtime_lock=asyncio.Lock(),
            database=SimpleNamespace(is_ready=True),
            vault_ready=True,
            entity_mapping=FakeEntityMapping(),
        )
        self.app = FastAPI()
        self.app.state.runtime = runtime
        install_entity_mapping_routes(self.app)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://knowgrain.test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_page_id_errors_and_exact_name_validation(self):
        missing = await self.client.get("/api/v1/wiki/pages/malformed/entities")
        self.assertEqual(missing.status_code, 404)

        invalid = await self.client.get(
            "/api/v1/graph/entity-pages", params={"name": "bad\u200bname"}
        )
        self.assertEqual(invalid.status_code, 422)

        too_long = await self.client.get(
            "/api/v1/graph/entity-pages", params={"name": "x" * 513}
        )
        self.assertEqual(too_long.status_code, 422)

    async def test_storage_failures_and_timeout_are_safe_503(self):
        unavailable = await self.client.get(
            "/api/v1/graph/entity-pages", params={"name": "unavailable"}
        )
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("private detail", unavailable.text)

        with patch("knowgrain.entity_mapping_api._ENTITY_MAPPING_TIMEOUT_SECONDS", 0.01):
            timed_out = await self.client.get(
                "/api/v1/graph/entity-pages", params={"name": "slow"}
            )
        self.assertEqual(timed_out.status_code, 503)


if __name__ == "__main__":
    unittest.main()
