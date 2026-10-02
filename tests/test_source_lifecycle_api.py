from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from knowgrain.source_repository import SourceConflictError, SourceNotFoundError
from knowgrain.source_service import SourceLifecycleUnavailableError
from knowgrain.source_lifecycle_api import install_source_lifecycle_routes


SOURCE_ID = UUID("90b7eef4-d817-423b-88b8-a9ebf0388146")
LATEST_ID = UUID("9f4d0c94-22af-435f-8c81-cc895fc1c560")


class FakeSources:
    def __init__(self):
        self.calls = []
        self.error: Exception | None = None
        self.entered: asyncio.Event | None = None
        self.block: asyncio.Event | None = None

    async def _call(self, name, source_id, **kwargs):
        if self.entered is not None:
            self.entered.set()
        if self.block is not None:
            await self.block.wait()
        if self.error is not None:
            raise self.error
        self.calls.append((name, source_id, kwargs))
        return {
            "id": str(source_id),
            "state": "deleted" if name == "soft_delete_source" else "active",
            "lifecycle_version": kwargs["expected_lifecycle_version"] + 1,
        }

    async def soft_delete_source(self, source_id, **kwargs):
        return await self._call("soft_delete_source", source_id, **kwargs)

    async def restore_source(self, source_id, **kwargs):
        return await self._call("restore_source", source_id, **kwargs)


class SourceLifecycleAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sources = FakeSources()
        self.runtime = SimpleNamespace(
            _runtime_lock=asyncio.Lock(),
            database=SimpleNamespace(is_ready=True),
            vault_ready=True,
            sources=self.sources,
        )
        self.app = FastAPI()
        self.app.state.runtime = self.runtime
        install_source_lifecycle_routes(self.app)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://knowgrain.test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    def _url(self, action: str | None = None) -> str:
        base = f"/api/v1/sources/{SOURCE_ID}"
        return base if action is None else f"{base}/{action}"

    async def test_delete_and_restore_forward_required_cas_values(self):
        body = {
            "expected_lifecycle_version": 4,
            "expected_latest_revision_id": str(LATEST_ID),
        }
        deleted = await self.client.request("DELETE", self._url(), json=body)
        restored = await self.client.post(self._url("restore"), json=body)

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["state"], "deleted")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["state"], "active")
        self.assertEqual(
            self.sources.calls,
            [
                (
                    "soft_delete_source",
                    SOURCE_ID,
                    {
                        "expected_lifecycle_version": 4,
                        "expected_latest_revision_id": LATEST_ID,
                    },
                ),
                (
                    "restore_source",
                    SOURCE_ID,
                    {
                        "expected_lifecycle_version": 4,
                        "expected_latest_revision_id": LATEST_ID,
                    },
                ),
            ],
        )

    async def test_lifecycle_body_rejects_bool_string_missing_and_extra_fields(self):
        valid_latest = str(LATEST_ID)
        invalid_bodies = (
            {"expected_lifecycle_version": True, "expected_latest_revision_id": valid_latest},
            {"expected_lifecycle_version": "4", "expected_latest_revision_id": valid_latest},
            {"expected_lifecycle_version": 4},
            {
                "expected_lifecycle_version": 4,
                "expected_latest_revision_id": valid_latest,
                "extra": "forbidden",
            },
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                deleted = await self.client.request("DELETE", self._url(), json=body)
                restored = await self.client.post(self._url("restore"), json=body)
                self.assertEqual(deleted.status_code, 422)
                self.assertEqual(restored.status_code, 422)
        self.assertFalse(self.sources.calls)

    async def test_lifecycle_body_requires_nullable_latest_field_explicitly(self):
        body = {"expected_lifecycle_version": 0, "expected_latest_revision_id": None}
        response = await self.client.request("DELETE", self._url(), json=body)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.sources.calls[0][2]["expected_latest_revision_id"])

    async def test_readiness_is_rechecked_after_runtime_lock_is_acquired(self):
        await self.runtime._runtime_lock.acquire()
        request = asyncio.create_task(self.client.post(
            self._url("restore"),
            json={
                "expected_lifecycle_version": 0,
                "expected_latest_revision_id": None,
            },
        ))
        await asyncio.sleep(0.02)
        self.runtime.vault_ready = False
        self.runtime._runtime_lock.release()

        response = await request
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.sources.calls)

    async def test_service_failures_map_to_documented_statuses(self):
        body = {
            "expected_lifecycle_version": 0,
            "expected_latest_revision_id": None,
        }
        for error, status_code in (
            (SourceNotFoundError(), 404),
            (SourceConflictError("Vault 中的原件已变化"), 409),
            (SourceLifecycleUnavailableError("Vault 原件暂不可用"), 503),
        ):
            with self.subTest(status_code=status_code):
                self.sources.error = error
                response = await self.client.post(self._url("restore"), json=body)
                self.assertEqual(response.status_code, status_code)
        self.sources.error = None


if __name__ == "__main__":
    unittest.main()
