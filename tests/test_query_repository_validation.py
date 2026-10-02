from __future__ import annotations

import hashlib
from datetime import UTC, datetime
import unittest
from uuid import uuid4

from knowgrain.m3_types import Evidence, evidence_identity
from knowgrain.query_repository import QueryRepository


class QueryRepositoryResultValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.revision_id = uuid4()
        self.excerpt = "A supported sentence."
        excerpt_hash = hashlib.sha256(self.excerpt.encode()).hexdigest()
        self.evidence = Evidence(
            evidence_id=evidence_identity(self.revision_id, "chunk-1", excerpt_hash),
            source_id=uuid4(),
            revision_id=self.revision_id,
            filename="source.txt",
            vault_path="Sources/Files/source.txt",
            source_sha256="a" * 64,
            parsed_text_sha256="b" * 64,
            chunk_id="chunk-1",
            excerpt=self.excerpt,
            excerpt_sha256=excerpt_hash,
            start=0,
            end=len(self.excerpt),
            page=None,
            heading=None,
            indexed_at=datetime.now(UTC),
        )

    def result(self, text: str, key: str = "claim.fact") -> dict:
        return {
            "status": "answered",
            "message": "",
            "claims": [
                {
                    "key": key,
                    "text": text,
                    "evidence_ids": [str(self.evidence.evidence_id)],
                }
            ],
            "model": {
                "name": "test-model",
                "provider": "test-provider",
                "generated_at": datetime.now(UTC).isoformat(),
            },
        }

    def test_accepts_period_in_claim_key(self) -> None:
        _, retained = QueryRepository._normalize_result(
            self.result("A supported answer."), (self.evidence,)
        )
        self.assertEqual(retained["claims"][0]["key"], "claim.fact")

    def test_rejects_claim_text_over_contract_limit(self) -> None:
        with self.assertRaises(ValueError):
            QueryRepository._normalize_result(
                self.result("x" * 1001), (self.evidence,)
            )


if __name__ == "__main__":
    unittest.main()
