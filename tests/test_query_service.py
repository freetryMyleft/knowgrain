from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import threading
import unittest
from uuid import UUID

from knowgrain.config import Settings
from knowgrain.m3_types import Evidence, EvidenceUnavailableError
from knowgrain.query_service import QueryService


JOB_ID = UUID("e11e1451-2443-421a-921a-13cad8ed9470")
EVIDENCE_ID = UUID("a82a8c51-19c9-44f3-a8a7-dd6fefc9a394")


def evidence(identity: UUID = EVIDENCE_ID, excerpt: str = "原文中的事实。") -> Evidence:
    return Evidence(
        evidence_id=identity,
        source_id=UUID("c932ef4f-42dc-4a85-aab3-cfbce6dd0082"),
        revision_id=UUID("9a6708b5-d5a6-4f33-8914-4f1cb58d23f9"),
        filename="资料.pdf",
        vault_path="Sources/资料.pdf",
        source_sha256="f" * 64,
        parsed_text_sha256="a" * 64,
        chunk_id=f"chunk-{identity}",
        excerpt=excerpt,
        excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        start=0,
        end=len(excerpt),
        page=1,
        heading=None,
        indexed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


class FakeRepository:
    def __init__(self):
        self.completions = []
        self.failures = []
        self.job = None

    async def complete(self, job_id, owner, result, evidence_items):
        self.completions.append((job_id, result, tuple(evidence_items)))
        return True

    async def fail(self, job_id, owner, error):
        self.failures.append((job_id, error))

    async def get_job(self, job_id):
        return self.job


class FakeProvenance:
    def __init__(self, items=(), *, fail_on_validation=()):
        self.items = tuple(items)
        self.fail_on_validation = set(fail_on_validation)
        self.validation_count = 0

    async def collect(self, raw):
        if not self.items:
            raise EvidenceUnavailableError("no current evidence")
        return self.items

    async def validate(self, items):
        self.validation_count += 1
        if self.validation_count in self.fail_on_validation:
            raise EvidenceUnavailableError("no longer current")


class FakeLightRAG:
    is_ready = True
    restart_required = False
    model_validation_error = None

    def __init__(self, response):
        self.response = response
        self.retrieve_calls = 0
        self.generate_calls = 0
        self.prompt = None

    async def retrieve(self, question, *, mode):
        self.retrieve_calls += 1
        self.question = question
        return {"data": {"chunks": []}}

    async def generate_json(self, system, prompt):
        self.generate_calls += 1
        self.prompt = prompt
        return self.response


class FakeEvidenceFiles:
    def __init__(self):
        self.published = []

    def publish(self, items):
        self.published.append(tuple(items))


class QueryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.item = evidence()
        self.job = {"job_id": JOB_ID, "question": "  这个事实是什么？  "}
        self.repo = FakeRepository()
        self.rag = FakeLightRAG(json.dumps({
            "status": "answered",
            "claims": [{
                "key": "claim-1", "text": "原文支持的事实。", "evidence_ids": [str(EVIDENCE_ID)]
            }],
        }, ensure_ascii=False))
        self.provenance = FakeProvenance((self.item,))
        self.files = FakeEvidenceFiles()
        self.service = QueryService(
            Settings(llm_model="local-test"), self.repo, self.provenance, self.rag, self.files
        )

    async def test_answer_persists_claims_and_only_cited_evidence(self):
        await self.service._process(self.job)
        self.assertEqual(self.rag.retrieve_calls, 1)
        self.assertEqual(self.rag.generate_calls, 1)
        job_id, result, stored_evidence = self.repo.completions[0]
        self.assertEqual(job_id, JOB_ID)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["claims"][0]["text"], "原文支持的事实。")
        self.assertEqual(stored_evidence, (self.item,))
        self.assertEqual(self.files.published, [(self.item,)])
        self.assertEqual(self.provenance.validation_count, 2)

    async def test_empty_current_evidence_completes_insufficient_without_model_call(self):
        self.service.provenance = FakeProvenance()
        await self.service._process(self.job)
        self.assertEqual(self.rag.generate_calls, 0)
        _, result, stored_evidence = self.repo.completions[0]
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["message"], "无法核实：当前资料不足以支持该问题。")
        self.assertEqual(result["claims"], [])
        self.assertEqual(stored_evidence, ())

    async def test_evidence_becoming_stale_after_publication_completes_insufficient(self):
        self.service.provenance = FakeProvenance((self.item,), fail_on_validation=(2,))
        await self.service._process(self.job)
        _, result, stored_evidence = self.repo.completions[0]
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(stored_evidence, ())
        self.assertEqual(self.files.published, [(self.item,)])

    async def test_invalid_model_citations_fail_the_job_without_persisting_result(self):
        self.rag.response = json.dumps({
            "status": "answered",
            "claims": [{"key": "claim-1", "text": "fabricated", "evidence_ids": [str(UUID(int=1))]}],
        })
        await self.service._execute(self.job)
        self.assertEqual(self.repo.completions, [])
        self.assertEqual(len(self.repo.failures), 1)
        self.assertIn("引用规则", self.repo.failures[0][1])

    async def test_detail_revalidates_retained_evidence_on_each_read(self):
        self.repo.job = {
            "job_id": JOB_ID,
            "state": "succeeded",
            "result": {
                "status": "answered",
                "message": "",
                "claims": [],
                "evidence": [self.item.snapshot()],
                "model": {"name": "local", "provider": "ollama", "generated_at": "now"},
            },
        }
        self.service.provenance = FakeProvenance((self.item,), fail_on_validation=(1,))
        detail = await self.service.detail(JOB_ID)
        self.assertFalse(detail["result"]["evidence_current"])
        self.assertFalse(detail["result"]["evidence"][0]["current"])

    async def test_publish_thread_is_joined_before_cancellation_returns(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowFiles:
            def publish(self, items):
                entered.set()
                release.wait(timeout=3)

        service = QueryService(
            Settings(), self.repo, self.provenance, self.rag, SlowFiles()
        )
        task = asyncio.create_task(service._publish_drained((self.item,)))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
