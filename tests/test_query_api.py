from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import unittest
from types import SimpleNamespace
from uuid import UUID

import httpx
from fastapi import FastAPI

from knowgrain.evidence_access import EvidenceFileError
from knowgrain.m3_types import Evidence
from knowgrain.query_api import install_query_routes


EVIDENCE_ID = UUID("a82a8c51-19c9-44f3-a8a7-dd6fefc9a394")
JOB_ID = UUID("e11e1451-2443-421a-921a-13cad8ed9470")


def evidence(filename: str = '資料 "quoted"\r\nX-Evil: true.pdf') -> Evidence:
    excerpt = "原文中的事实。"
    return Evidence(
        evidence_id=EVIDENCE_ID,
        source_id=UUID("c932ef4f-42dc-4a85-aab3-cfbce6dd0082"),
        revision_id=UUID("9a6708b5-d5a6-4f33-8914-4f1cb58d23f9"),
        filename=filename,
        vault_path="Sources/资料.pdf",
        source_sha256="f" * 64,
        parsed_text_sha256="a" * 64,
        chunk_id="chunk-1",
        excerpt=excerpt,
        excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        start=0,
        end=len(excerpt),
        page=1,
        heading=None,
        indexed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


class FakeGenerationRepository:
    def __init__(self, item):
        self.item = item

    async def get_evidence(self, evidence_id):
        return self.item if evidence_id == self.item.evidence_id else None


class FakeEvidenceFiles:
    def __init__(self):
        self.original_error = None
        self.markdown_error = None

    def original(self, item):
        if self.original_error:
            raise self.original_error
        return b"verified original bytes", item.filename

    def markdown(self, item):
        if self.markdown_error:
            raise self.markdown_error
        return b"# verified markdown\n"


class FakeQueries:
    def __init__(self, files):
        self.evidence_files = files
        self.enqueued = []

    async def enqueue(self, question):
        self.enqueued.append(question)
        return {"job_id": str(JOB_ID), "question": question, "state": "queued"}

    async def list_jobs(self, *, limit, offset):
        return [{"job_id": str(JOB_ID), "state": "queued"}]

    async def detail(self, job_id):
        return {"job_id": str(job_id), "state": "succeeded", "result": {"claims": []}}

    async def retry(self, job_id):
        return {"job_id": str(job_id), "state": "queued"}


class QueryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.item = evidence()
        self.files = FakeEvidenceFiles()
        self.queries = FakeQueries(self.files)
        runtime = SimpleNamespace(
            _runtime_lock=asyncio.Lock(),
            database=SimpleNamespace(is_ready=True),
            vault_ready=True,
            queries=self.queries,
            generation=SimpleNamespace(repository=FakeGenerationRepository(self.item)),
        )
        self.app = FastAPI()
        self.app.state.runtime = runtime
        install_query_routes(self.app)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://knowgrain.test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_post_accepts_only_trimmed_question_and_list_omits_results(self):
        response = await self.client.post(
            "/api/v1/queries", json={"question": "  这个事实是什么？  "}
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["question"], "这个事实是什么？")
        self.assertEqual(self.queries.enqueued, ["这个事实是什么？"])
        self.assertEqual(response.headers["location"], f"/api/v1/queries/{JOB_ID}")

        invalid = await self.client.post(
            "/api/v1/queries", json={"question": "问题", "evidence_ids": [str(EVIDENCE_ID)]}
        )
        self.assertEqual(invalid.status_code, 422)
        listing = await self.client.get("/api/v1/queries")
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn("result", listing.json()["jobs"][0])

    async def test_evidence_downloads_are_attachments_with_safe_headers(self):
        original = await self.client.get(f"/api/v1/evidence/{EVIDENCE_ID}/original")
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.content, b"verified original bytes")
        self.assertEqual(original.headers["content-type"], "application/octet-stream")
        disposition = original.headers["content-disposition"]
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)
        self.assertNotIn("%0D", disposition)
        self.assertNotIn("%0A", disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("nosniff", original.headers["x-content-type-options"])

        markdown = await self.client.get(f"/api/v1/evidence/{EVIDENCE_ID}/markdown")
        self.assertEqual(markdown.status_code, 200)
        self.assertIn("text/markdown", markdown.headers["content-type"])
        self.assertEqual(markdown.content, b"# verified markdown\n")

    async def test_missing_and_conflicting_evidence_files_fail_closed(self):
        self.files.original_error = EvidenceFileError("missing")
        missing = await self.client.get(f"/api/v1/evidence/{EVIDENCE_ID}/original")
        self.assertEqual(missing.status_code, 404)

        self.files.markdown_error = EvidenceFileError("conflict")
        conflict = await self.client.get(f"/api/v1/evidence/{EVIDENCE_ID}/markdown")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"]["code"], "evidence_conflict")


if __name__ == "__main__":
    unittest.main()
