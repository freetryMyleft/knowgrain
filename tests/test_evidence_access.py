from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from knowgrain.config import MAX_UPLOAD_BYTES
from knowgrain.evidence_access import EvidenceAccess, EvidenceFileError
from knowgrain.generation_contract import render_evidence
from knowgrain.m3_types import Evidence, evidence_identity
from knowgrain.vault import VaultStore


class EvidenceAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = VaultStore(Path(self.temporary.name) / "vault")
        self.vault.initialize()
        self.files = EvidenceAccess(self.vault)

        self.source_id = uuid4()
        self.revision_id = uuid4()
        self.source_bytes = "First fact. Second quoted fact.".encode("utf-8")
        self.vault_path = self.vault.write_source(
            self.source_id, self.revision_id, "source.txt", self.source_bytes
        )
        self.excerpt = "Second quoted fact."
        self.excerpt_hash = hashlib.sha256(self.excerpt.encode()).hexdigest()
        self.evidence = Evidence(
            evidence_id=evidence_identity(self.revision_id, "chunk-1", self.excerpt_hash),
            source_id=self.source_id,
            revision_id=self.revision_id,
            filename="source.txt",
            vault_path=self.vault_path,
            source_sha256=hashlib.sha256(self.source_bytes).hexdigest(),
            parsed_text_sha256=hashlib.sha256(self.source_bytes).hexdigest(),
            chunk_id="chunk-1",
            excerpt=self.excerpt,
            excerpt_sha256=self.excerpt_hash,
            start=len("First fact. "),
            end=len("First fact. ") + len(self.excerpt),
            page=None,
            heading=None,
            indexed_at=datetime.now(UTC),
        )

    def test_publish_original_and_markdown_return_captured_canonical_bytes(self) -> None:
        self.files.publish((self.evidence,))
        expected_markdown = render_evidence(self.evidence).encode("utf-8")

        original, filename = self.files.original(self.evidence)

        self.assertEqual((original, filename), (self.source_bytes, "source.txt"))
        self.assertEqual(self.files.markdown(self.evidence), expected_markdown)
        self.files.publish((self.evidence,))  # Exact canonical retries are idempotent.

    def test_edited_markdown_and_original_fail_closed(self) -> None:
        self.files.publish((self.evidence,))
        markdown_path = self.vault.resolve(f"Sources/Evidence/{self.evidence.evidence_id}.md")
        markdown_path.write_bytes(b"externally edited evidence")
        with self.assertRaises(EvidenceFileError) as markdown_error:
            self.files.markdown(self.evidence)
        self.assertEqual(markdown_error.exception.code, "conflict")
        with self.assertRaises(EvidenceFileError) as publish_error:
            self.files.publish((self.evidence,))
        self.assertEqual(publish_error.exception.code, "conflict")

        original_path = self.vault.resolve(self.vault_path)
        original_path.write_bytes(b"changed source")
        with self.assertRaises(EvidenceFileError) as original_error:
            self.files.original(self.evidence)
        self.assertEqual(original_error.exception.code, "conflict")

    def test_missing_and_symlinked_originals_are_rejected(self) -> None:
        original_path = self.vault.resolve(self.vault_path)
        original_path.unlink()
        with self.assertRaises(EvidenceFileError) as missing_error:
            self.files.original(self.evidence)
        self.assertEqual(missing_error.exception.code, "missing")

        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_bytes(self.source_bytes)
        original_path.symlink_to(outside)
        with self.assertRaises(EvidenceFileError) as symlink_error:
            self.files.original(self.evidence)
        self.assertEqual(symlink_error.exception.code, "unavailable")

    def test_parent_directory_symlink_is_rejected(self) -> None:
        original_path = self.vault.resolve(self.vault_path)
        parent = original_path.parent
        parked_parent = parent.with_name(f"{parent.name}-saved")
        parent.rename(parked_parent)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / original_path.name).write_bytes(self.source_bytes)
        parent.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(EvidenceFileError) as error:
            self.files.original(self.evidence)
        self.assertEqual(error.exception.code, "unavailable")

    def test_append_during_read_is_rejected_as_a_file_race(self) -> None:
        original_path = self.vault.resolve(self.vault_path)
        real_read = os.read
        appended = False

        def read_then_append(descriptor: int, size: int) -> bytes:
            nonlocal appended
            data = real_read(descriptor, size)
            if not appended:
                appended = True
                with original_path.open("ab") as changed:
                    changed.write(b" appended during read")
                    changed.flush()
            return data

        with patch("knowgrain.evidence_access.os.read", side_effect=read_then_append):
            with self.assertRaises(EvidenceFileError) as error:
                self.files.original(self.evidence)
        self.assertEqual(error.exception.code, "conflict")

    def test_returns_verified_capture_if_path_changes_after_validation(self) -> None:
        original_path = self.vault.resolve(self.vault_path)
        replacement_path = original_path.with_name("replacement.txt")
        real_same_stat = EvidenceAccess._same_stat
        replaced = False

        def validate_then_replace(before, after) -> bool:
            nonlocal replaced
            result = real_same_stat(before, after)
            if result and not replaced:
                replaced = True
                replacement_path.write_bytes(b"unsafe replacement bytes")
                replacement_path.replace(original_path)
            return result

        with patch.object(EvidenceAccess, "_same_stat", side_effect=validate_then_replace):
            captured, filename = self.files.original(self.evidence)

        self.assertTrue(replaced)
        self.assertEqual((captured, filename), (self.source_bytes, "source.txt"))
        self.assertEqual(original_path.read_bytes(), b"unsafe replacement bytes")

    def test_invalid_or_escaping_evidence_path_is_rejected(self) -> None:
        self.files.publish((self.evidence,))
        invalid = replace(self.evidence, vault_path="../outside.txt")
        with self.assertRaises(EvidenceFileError) as error:
            self.files.original(invalid)
        self.assertEqual(error.exception.code, "unavailable")

    def test_original_revision_returns_only_hash_verified_bounded_bytes(self) -> None:
        self.assertEqual(
            self.files.original_revision(
                self.vault_path, hashlib.sha256(self.source_bytes).hexdigest()
            ),
            self.source_bytes,
        )
        with self.assertRaises(EvidenceFileError) as changed:
            self.files.original_revision(self.vault_path, "0" * 64)
        self.assertEqual(changed.exception.code, "conflict")
        with self.assertRaises(EvidenceFileError) as malformed_hash:
            self.files.original_revision(self.vault_path, "A" * 64)
        self.assertEqual(malformed_hash.exception.code, "unavailable")

    def test_original_revision_rejects_symlinks_and_fifos(self) -> None:
        original_path = self.vault.resolve(self.vault_path)
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_bytes(self.source_bytes)
        original_path.unlink()
        original_path.symlink_to(outside)
        with self.assertRaises(EvidenceFileError) as symlink_error:
            self.files.original_revision(
                self.vault_path, hashlib.sha256(self.source_bytes).hexdigest()
            )
        self.assertEqual(symlink_error.exception.code, "unavailable")

        original_path.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(original_path)
            with self.assertRaises(EvidenceFileError) as fifo_error:
                self.files.original_revision(
                    self.vault_path, hashlib.sha256(self.source_bytes).hexdigest()
                )
            self.assertEqual(fifo_error.exception.code, "unavailable")

    def test_canonical_original_reads_only_its_exact_archived_path(self) -> None:
        original_path = self.vault.resolve(self.vault_path)
        archived_path = self.vault.resolve(
            self.vault_path.replace("Sources/Files", "Trash/Files", 1)
        )
        archived_path.parent.mkdir(parents=True)
        original_path.replace(archived_path)
        digest = hashlib.sha256(self.source_bytes).hexdigest()

        captured = self.files.original_revision(self.vault_path, digest)

        self.assertEqual(captured, self.source_bytes)
        with self.assertRaises(EvidenceFileError) as disabled:
            self.files.original_revision(
                self.vault_path, digest, allow_archived=False
            )
        self.assertEqual(disabled.exception.code, "missing")
        with self.assertRaises(EvidenceFileError) as wrong_hash:
            self.files.original_revision(self.vault_path, "0" * 64)
        self.assertEqual(wrong_hash.exception.code, "conflict")

    def test_original_symlink_cannot_redirect_capture_to_trash(self) -> None:
        original_path = self.vault.resolve(self.vault_path)
        archived_path = self.vault.resolve(
            self.vault_path.replace("Sources/Files", "Trash/Files", 1)
        )
        archived_path.parent.mkdir(parents=True)
        archived_path.write_bytes(self.source_bytes)
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_bytes(self.source_bytes)
        original_path.unlink()
        original_path.symlink_to(outside)

        with self.assertRaises(EvidenceFileError) as error:
            self.files.original_revision(
                self.vault_path,
                hashlib.sha256(self.source_bytes).hexdigest(),
            )

        self.assertEqual(error.exception.code, "unavailable")

    def test_conflicting_canonical_original_never_falls_back_to_trash(self) -> None:
        original_path = self.vault.resolve(self.vault_path)
        archived_path = self.vault.resolve(
            self.vault_path.replace("Sources/Files", "Trash/Files", 1)
        )
        archived_path.parent.mkdir(parents=True)
        archived_path.write_bytes(self.source_bytes)
        original_path.write_bytes(b"changed canonical bytes")

        with self.assertRaises(EvidenceFileError) as error:
            self.files.original_revision(
                self.vault_path,
                hashlib.sha256(self.source_bytes).hexdigest(),
            )

        self.assertEqual(error.exception.code, "conflict")

    def test_evidence_markdown_does_not_use_trash_fallback(self) -> None:
        self.files.publish((self.evidence,))
        evidence_path = self.vault.resolve(
            f"Sources/Evidence/{self.evidence.evidence_id}.md"
        )
        evidence_path.unlink()
        trash_path = self.vault.resolve(
            f"Trash/Files/{self.evidence.source_id}/{self.evidence.evidence_id}.md"
        )
        trash_path.parent.mkdir(parents=True)
        trash_path.write_bytes(render_evidence(self.evidence).encode("utf-8"))

        with self.assertRaises(EvidenceFileError) as error:
            self.files.markdown(self.evidence)

        self.assertEqual(error.exception.code, "missing")

    def test_capture_original_rejects_invalid_budgets_and_still_rejects_fifos(self) -> None:
        for budget in (0, True, MAX_UPLOAD_BYTES + 1, 1.5):
            with self.subTest(budget=budget), self.assertRaises(EvidenceFileError):
                self.files.capture_original(self.vault_path, budget)

        original_path = self.vault.resolve(self.vault_path)
        original_path.unlink()
        os.mkfifo(original_path)
        with self.assertRaises(EvidenceFileError) as error:
            self.files.capture_original(self.vault_path, 1024)
        self.assertEqual(error.exception.code, "unavailable")

    def test_noncanonical_legacy_path_is_readable_but_never_gets_trash_fallback(self) -> None:
        legacy_relative = "Sources/Files/source/revision.txt"
        legacy_path = self.vault.resolve(legacy_relative)
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_bytes(self.source_bytes)
        digest = hashlib.sha256(self.source_bytes).hexdigest()

        self.assertEqual(
            self.files.original_revision(legacy_relative, digest), self.source_bytes
        )
        legacy_path.unlink()
        archived_legacy = self.vault.resolve(
            "Trash/Files/source/revision.txt"
        )
        archived_legacy.parent.mkdir(parents=True)
        archived_legacy.write_bytes(self.source_bytes)
        with self.assertRaises(EvidenceFileError) as missing:
            self.files.original_revision(legacy_relative, digest)
        self.assertEqual(missing.exception.code, "missing")

    def test_large_registered_original_remains_readable_after_upload_limit_changes(self) -> None:
        # A retained revision can exceed today's default 20 MiB upload limit.
        size = 65 * 1024 * 1024
        original_path = self.vault.resolve(self.vault_path)
        with original_path.open("wb") as target:
            target.truncate(size)
        digest = hashlib.sha256()
        block = bytes(1024 * 1024)
        for _ in range(65):
            digest.update(block)
        content = self.files.original_revision(self.vault_path, digest.hexdigest())
        self.assertEqual(len(content), size)
        self.assertEqual(hashlib.sha256(content).hexdigest(), digest.hexdigest())
        with original_path.open("wb") as target:
            target.truncate(101 * 1024 * 1024)
        with self.assertRaises(EvidenceFileError) as oversized:
            self.files.original_revision(self.vault_path, digest.hexdigest())
        self.assertEqual(oversized.exception.code, "unavailable")


if __name__ == "__main__":
    unittest.main()
