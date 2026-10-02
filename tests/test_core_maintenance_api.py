import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
import httpx
from sqlalchemy.exc import SQLAlchemyError

from knowgrain.core_maintenance_api import install_core_maintenance_routes
from knowgrain.source_repository import SourceConflictError, SourceNotFoundError


class CoreMaintenanceAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source_id = uuid4()
        self.job_id = uuid4()
        self.repository = SimpleNamespace(
            get_source=AsyncMock(return_value={"id": str(self.source_id)}),
            list_maintenance=AsyncMock(return_value=[]),
            retry_maintenance=AsyncMock(return_value={"job_id": str(self.job_id), "state": "queued"}),
        )
        self.runtime = SimpleNamespace(
            _runtime_lock=asyncio.Lock(), database=SimpleNamespace(is_ready=True),
            vault_ready=True, repository=self.repository,
        )
        app = FastAPI()
        app.state.runtime = self.runtime
        install_core_maintenance_routes(app)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://knowgrain.test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_list_requires_existing_source_and_bounded_limit(self):
        url = f"/api/v1/sources/{self.source_id}/maintenance"
        response = await self.client.get(url, params={"limit": 7})
        self.assertEqual(response.status_code, 200)
        self.repository.list_maintenance.assert_awaited_once_with(self.source_id, limit=7)
        for limit in (0, 101):
            self.assertEqual((await self.client.get(url, params={"limit": limit})).status_code, 422)
        self.repository.get_source.return_value = None
        self.assertEqual((await self.client.get(url)).status_code, 404)
        self.assertEqual(self.repository.list_maintenance.await_count, 1)

    async def test_retry_maps_conflicts_and_hides_private_error_details(self):
        url = f"/api/v1/maintenance/{self.job_id}/retry"
        response = await self.client.post(url)
        self.assertEqual(response.status_code, 202)
        self.repository.retry_maintenance.assert_awaited_once_with(self.job_id)
        for error, status in (
            (SourceNotFoundError(), 404),
            (SourceConflictError("private source contents"), 409),
            (SQLAlchemyError("private source contents"), 503),
        ):
            self.repository.retry_maintenance.side_effect = error
            response = await self.client.post(url)
            self.assertEqual(response.status_code, status)
            self.assertNotIn("private source contents", response.text)

    async def test_unready_runtime_never_touches_repository(self):
        await self.runtime._runtime_lock.acquire()
        task = asyncio.create_task(self.client.post(f"/api/v1/maintenance/{self.job_id}/retry"))
        await asyncio.sleep(0)
        self.runtime.vault_ready = False
        self.runtime._runtime_lock.release()
        self.assertEqual((await task).status_code, 503)
        self.repository.retry_maintenance.assert_not_awaited()
