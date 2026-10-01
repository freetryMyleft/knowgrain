"""Local-file tests for evidence collection and revalidation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from unittest.mock import patch

from knowgrain.m3_types import EligibleRevision, EvidenceUnavailableError
from knowgrain.parsers import parse_document
from knowgrain.provenance import ProvenanceService
from knowgrain.vault import VaultStore


class RepositoryStub:
    def __init__(self, revisions=()):
        self.by_path = {revision.vault_path: revision for revision in revisions}
        self.by_id = {revision.revision_id: revision for revision in revisions}
        self.path_calls = []
        self.id_calls = []

    async def eligible_by_paths(self, paths):
        self.path_calls.append(list(paths))
        return {path: self.by_path[path] for path in paths if path in self.by_path}

    async def eligible_by_ids(self, ids):
        self.id_calls.append(list(ids))
        return {identity: self.by_id[identity] for identity in ids if identity in self.by_id}


class ProvenanceServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = VaultStore(Path(self.temporary.name) / "vault")
        self.vault.initialize()
        self.source_id = uuid4()
        self.revision_id = uuid4()
        self.indexed_at = datetime(2026, 1, 2, tzinfo=UTC)

    def make_revision(self, filename: str, content: bytes) -> EligibleRevision:
        vault_path = self.vault.write_source(
            self.source_id, self.revision_id, filename, content
        )
        parsed = parse_document(filename, content)
        return EligibleRevision(
            source_id=self.source_id,
            revision_id=self.revision_id,
            filename=filename,
            vault_path=vault_path,
            sha256=hashlib.sha256(content).hexdigest(),
            parsed_text_sha256=hashlib.sha256(parsed.text.encode("utf-8")).hexdigest(),
            indexed_at=self.indexed_at,
        )

    @staticmethod
    def response(*chunks):
        return {
            "status": "success",
            "data": {
                "chunks": list(chunks),
                # Entity and relation descriptions are deliberately not quotes.
                "entities": [{"description": "must not become evidence"}],
                "relationships": [{"description": "must not become evidence"}],
            },
        }

    async def test_collect_proves_chunk_offset_location_and_validates(self):
        content = "# Project\n\nVerified sentence.\n\n## Details\n\nAnother fact.".encode()
        revision = self.make_revision("notes.md", content)
        repository = RepositoryStub([revision])
        service = ProvenanceService(repository, self.vault)

        items = await service.collect(
            self.response(
                {
                    "file_path": revision.vault_path,
                    "chunk_id": "chunk-1",
                    "content": "Verified sentence.",
                }
            )
        )

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.start, content.decode().index("Verified sentence."))
        self.assertEqual(item.end - item.start, len(item.excerpt))
        self.assertEqual(item.excerpt, "Verified sentence.")
        self.assertEqual(item.heading, "Project")
        self.assertIsNone(item.page)
        self.assertEqual(repository.path_calls, [[revision.vault_path]])
        await service.validate(items)
        self.assertEqual(repository.id_calls, [[revision.revision_id]])

    async def test_entities_unknown_paths_and_malformed_chunks_are_ignored(self):
        content = b"A source-backed sentence."
        revision = self.make_revision("notes.txt", content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)

        items = await service.collect(
            self.response(
                {"description": "entity description"},
                {"file_path": "Sources/Files/unknown.txt", "chunk_id": "x", "content": "x"},
                {
                    "file_path": revision.vault_path,
                    "chunk_id": "real-chunk",
                    "content": "A source-backed sentence.",
                },
            )
        )

        self.assertEqual([item.excerpt for item in items], ["A source-backed sentence."])

    async def test_duplicate_quote_is_not_assigned_an_arbitrary_offset(self):
        content = b"Repeated evidence.\n\nRepeated evidence."
        revision = self.make_revision("notes.txt", content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)

        with self.assertRaises(EvidenceUnavailableError):
            await service.collect(
                self.response(
                    {
                        "file_path": revision.vault_path,
                        "chunk_id": "duplicate",
                        "content": "Repeated evidence.",
                    }
                )
            )

    async def test_crlf_quote_requires_the_exact_parser_output(self):
        content = b"alpha\r\n\r\nbeta"
        revision = self.make_revision("notes.txt", content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)

        items = await service.collect(
            self.response(
                {
                    "file_path": revision.vault_path,
                    "chunk_id": "crlf-exact",
                    "content": "alpha\r\n\r\nbeta",
                }
            )
        )
        self.assertEqual(items[0].excerpt, "alpha\r\n\r\nbeta")
        self.assertEqual(items[0].start, 0)

        with self.assertRaises(EvidenceUnavailableError):
            await service.collect(
                self.response(
                    {
                        "file_path": revision.vault_path,
                        "chunk_id": "normalized-is-not-exact",
                        "content": "alpha\n\nbeta",
                    }
                )
            )

    async def test_rejects_original_or_parsed_hash_drift_without_echoing_text(self):
        private_text = "private source text"
        content = private_text.encode()
        revision = self.make_revision("notes.txt", content)
        source_path = self.vault.resolve(revision.vault_path)
        source_path.write_bytes(b"changed source bytes")
        service = ProvenanceService(RepositoryStub([revision]), self.vault)

        with self.assertRaises(EvidenceUnavailableError) as captured:
            await service.collect(
                self.response(
                    {
                        "file_path": revision.vault_path,
                        "chunk_id": "chunk",
                        "content": private_text,
                    }
                )
            )
        self.assertNotIn(private_text, str(captured.exception))

        source_path.write_bytes(content)
        parsed_drift = replace(revision, parsed_text_sha256="0" * 64)
        service = ProvenanceService(RepositoryStub([parsed_drift]), self.vault)
        with self.assertRaises(EvidenceUnavailableError):
            await service.collect(
                self.response(
                    {
                        "file_path": revision.vault_path,
                        "chunk_id": "chunk",
                        "content": private_text,
                    }
                )
            )

    async def test_validate_rejects_tampered_excerpt_offset_and_changed_vault(self):
        content = b"A reliable original quotation."
        revision = self.make_revision("notes.txt", content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)
        evidence = await service.collect(
            self.response(
                {
                    "file_path": revision.vault_path,
                    "chunk_id": "chunk",
                    "content": "A reliable original quotation.",
                }
            )
        )

        with self.assertRaises(EvidenceUnavailableError):
            await service.validate((replace(evidence[0], start=1),))
        with self.assertRaises(EvidenceUnavailableError):
            await service.validate((replace(evidence[0], excerpt="altered quote"),))

        self.vault.resolve(revision.vault_path).write_bytes(b"Changed after inference.")
        with self.assertRaises(EvidenceUnavailableError):
            await service.validate(evidence)

    async def test_excerpt_and_total_evidence_character_limits_are_enforced(self):
        excerpts = [f"[{index:02}]" + chr(65 + index) * 5_996 for index in range(9)]
        content = "\n\n".join(excerpts).encode()
        revision = self.make_revision("long.txt", content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)
        response = self.response(
            *[
                {
                    "file_path": revision.vault_path,
                    "chunk_id": f"chunk-{index}",
                    "content": excerpt,
                }
                for index, excerpt in enumerate(excerpts)
            ]
        )

        items = await service.collect(response)
        self.assertEqual(len(items), 8)
        self.assertTrue(all(len(item.excerpt) == 6_000 for item in items))
        self.assertEqual(sum(len(item.excerpt) for item in items), 48_000)
        await service.validate(items)

    async def test_original_read_budget_rejects_a_file_before_reading_it(self):
        content = b"a file larger than the test budget"
        revision = self.make_revision("large.txt", content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)

        with patch("knowgrain.provenance._MAX_ORIGINAL_BYTES", 8):
            with self.assertRaises(EvidenceUnavailableError):
                await service.collect(
                    self.response(
                        {
                            "file_path": revision.vault_path,
                            "chunk_id": "chunk",
                            "content": content.decode(),
                        }
                    )
                )

    async def test_oversized_retrieval_chunk_is_ignored_before_excerpting(self):
        content = ("z" * 1_000_001).encode()
        revision = self.make_revision("large.txt", content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)

        with self.assertRaises(EvidenceUnavailableError):
            await service.collect(
                self.response(
                    {
                        "file_path": revision.vault_path,
                        "chunk_id": "chunk",
                        "content": content.decode(),
                    }
                )
            )

    async def test_only_first_fifty_retrieval_candidates_are_examined(self):
        content = b"Candidate beyond the retrieval limit."
        revision = self.make_revision("notes.txt", content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)
        response = self.response(
            *([{"malformed": True}] * 50),
            {
                "file_path": revision.vault_path,
                "chunk_id": "candidate-51",
                "content": content.decode(),
            },
        )

        with self.assertRaises(EvidenceUnavailableError):
            await service.collect(response)

    async def test_append_after_initial_file_stat_is_not_accepted_as_evidence(self):
        content = b"A stable original before the concurrent append."
        revision = self.make_revision("append-race.txt", content)
        source_path = self.vault.resolve(revision.vault_path)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)
        original_fstat = os.fstat
        injected = False

        def append_after_stat(descriptor):
            nonlocal injected
            metadata = original_fstat(descriptor)
            if not injected:
                injected = True
                with source_path.open("ab") as source:
                    source.write(b" appended after the initial stat")
            return metadata

        with patch("knowgrain.provenance.os.fstat", side_effect=append_after_stat):
            with self.assertRaises(EvidenceUnavailableError):
                await service.collect(
                    self.response(
                        {
                            "file_path": revision.vault_path,
                            "chunk_id": "append-race",
                            "content": content.decode(),
                        }
                    )
                )

        self.assertTrue(injected)
        self.assertEqual(source_path.read_bytes(), content + b" appended after the initial stat")

    async def test_atomic_path_replacement_during_read_is_not_accepted_as_evidence(self):
        content = b"The replacement has identical bytes but a different file identity."
        revision = self.make_revision("replace-race.txt", content)
        source_path = self.vault.resolve(revision.vault_path)
        replacement_path = source_path.with_name("replacement.tmp")
        replacement_path.write_bytes(content)
        service = ProvenanceService(RepositoryStub([revision]), self.vault)
        original_stat = os.stat
        original_fstat = os.fstat
        descriptor_was_statted = False
        replaced = False

        def arm_after_descriptor_stat(descriptor):
            nonlocal descriptor_was_statted
            metadata = original_fstat(descriptor)
            descriptor_was_statted = True
            return metadata

        def replace_before_path_stat(path, *args, **kwargs):
            nonlocal replaced
            if (
                descriptor_was_statted
                and kwargs.get("follow_symlinks") is False
                and os.fspath(path) == os.fspath(source_path)
                and not replaced
            ):
                replaced = True
                os.replace(replacement_path, source_path)
            return original_stat(path, *args, **kwargs)

        with (
            patch("knowgrain.provenance.os.fstat", side_effect=arm_after_descriptor_stat),
            patch("knowgrain.provenance.os.stat", side_effect=replace_before_path_stat),
        ):
            with self.assertRaises(EvidenceUnavailableError):
                await service.collect(
                    self.response(
                        {
                            "file_path": revision.vault_path,
                            "chunk_id": "replacement-race",
                            "content": content.decode(),
                        }
                    )
                )

        self.assertTrue(replaced)
        self.assertEqual(source_path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
