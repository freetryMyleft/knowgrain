"""Real-filesystem tests for explicit Wiki review application and recovery."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from knowgrain.review_files import ReviewFileStore
from knowgrain.vault import VaultPathError, VaultStore
from knowgrain.wiki_files import (
    WikiConflictError,
    WikiFileStore,
    WikiValidationError,
    parse_wiki,
)


def markdown(page_id, *, status: str, body: str) -> str:
    return (
        f"---\nkg_id: {page_id}\nkg_kind: wiki\nkg_status: {status}\n"
        f'title: "Review fixture"\n---\n\n{body}'
    )


class ReviewFileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "vault"
        self.vault = VaultStore(self.root)
        self.vault.initialize()
        self.wiki = WikiFileStore(self.vault)
        self.files = ReviewFileStore(self.wiki)

    def create_draft(self, body: str = "Generated draft.\n"):
        page_id = uuid4()
        content = markdown(page_id, status="draft", body=body)
        parsed = parse_wiki(content, f"Wiki/Drafts/{page_id}.md")
        path = self.vault.resolve(parsed.vault_path)
        self.wiki._ensure_parent(parsed.vault_path)
        self.wiki._publish_exclusive(path, content.encode("utf-8"))
        return page_id, parsed, content

    def make_page_file(self, page_id, relative: str, content: str) -> None:
        path = self.vault.resolve(relative)
        self.wiki._ensure_parent(relative)
        self.wiki._publish_exclusive(path, content.encode("utf-8"))

    def test_review_moves_draft_to_pages_and_retry_is_idempotent(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Reviewed and approved.\n")
        operation_id = uuid4()

        result = self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        destination = self.vault.resolve(f"Wiki/Pages/{page_id}.md")
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        self.assertEqual(result.vault_path, f"Wiki/Pages/{page_id}.md")
        self.assertEqual(destination.read_text(), reviewed)
        self.assertFalse(old_path.exists())

        recovery = self.vault.resolve(
            f".knowgrain/wiki-recovery/{page_id}/{original.content_sha256}.md"
        )
        self.assertEqual(recovery.read_text(), old_markdown)
        intent_path = self.vault.resolve(
            f".knowgrain/review-operations/{operation_id}/intent.json"
        )
        intent_before = intent_path.read_bytes()
        with patch.object(self.files, "_publish_move_destination", side_effect=AssertionError):
            retried = self.files.commit(
                operation_id, page_id, original.content_sha256, reviewed
            )
        self.assertEqual(retried, result)
        self.assertEqual(intent_path.read_bytes(), intent_before)

    def test_same_path_review_replaces_atomically_and_retry_does_not_reapply(self) -> None:
        page_id = uuid4()
        old_markdown = markdown(page_id, status="draft", body="Old body.\n")
        relative = "Wiki/Pages/Stable-path.md"
        self.make_page_file(page_id, relative, old_markdown)
        original = parse_wiki(old_markdown, relative)
        reviewed = markdown(page_id, status="reviewed", body="Final body.\n")
        operation_id = uuid4()

        result = self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        self.assertEqual(result.vault_path, relative)
        self.assertEqual(self.vault.resolve(relative).read_text(), reviewed)
        with patch.object(self.files, "_replace_same_path", side_effect=AssertionError):
            retry = self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        self.assertEqual(retry, result)

    def test_interruption_after_destination_publication_recovers_exact_partial_move(self) -> None:
        page_id, original, _ = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Recovered review.\n")
        operation_id = uuid4()

        with patch.object(
            self.files,
            "_remove_old_after_move",
            side_effect=OSError("simulated interruption"),
        ):
            with self.assertRaises(WikiConflictError) as raised:
                self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        self.assertEqual(raised.exception.code, "review_write_failed")
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        new_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")
        self.assertTrue(old_path.exists())
        self.assertEqual(new_path.read_text(), reviewed)

        retried = self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        self.assertEqual(retried.markdown, reviewed)
        self.assertFalse(old_path.exists())
        self.assertEqual(new_path.read_text(), reviewed)

    def test_retry_intent_validation_returns_false_when_journal_is_absent(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="No journal yet.\n")
        operation_id = uuid4()
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        new_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")

        valid = self.files.validate_retry_intent(
            operation_id, page_id, original.content_sha256, reviewed
        )

        self.assertFalse(valid)
        self.assertEqual(old_path.read_text(), old_markdown)
        self.assertFalse(new_path.exists())
        self.assertFalse(
            self.vault.resolve(
                f".knowgrain/review-operations/{operation_id}/intent.json"
            ).exists()
        )

    def test_retry_intent_validation_accepts_only_its_exact_partial_move_read_only(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Own partial review.\n")
        operation_id = uuid4()
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        new_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")

        with patch.object(
            self.files,
            "_remove_old_after_move",
            side_effect=OSError("simulated interruption"),
        ):
            with self.assertRaises(WikiConflictError):
                self.files.commit(operation_id, page_id, original.content_sha256, reviewed)

        old_before = old_path.read_bytes()
        new_before = new_path.read_bytes()
        valid = self.files.validate_retry_intent(
            operation_id, page_id, original.content_sha256, reviewed
        )

        self.assertTrue(valid)
        self.assertEqual(old_path.read_bytes(), old_before)
        self.assertEqual(new_path.read_bytes(), new_before)
        self.assertEqual(old_before.decode("utf-8"), old_markdown)
        self.assertEqual(new_before.decode("utf-8"), reviewed)

    def test_retry_intent_validation_rejects_unrelated_duplicate_without_mutation(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Own partial review.\n")
        operation_id = uuid4()
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        new_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")

        with patch.object(
            self.files,
            "_remove_old_after_move",
            side_effect=OSError("simulated interruption"),
        ):
            with self.assertRaises(WikiConflictError):
                self.files.commit(operation_id, page_id, original.content_sha256, reviewed)

        unrelated_path = self.vault.resolve(f"Wiki/Pages/duplicate-{page_id}.md")
        self.wiki._publish_exclusive(unrelated_path, old_markdown.encode("utf-8"))
        before = {
            path: path.read_bytes()
            for path in (old_path, new_path, unrelated_path)
        }

        with self.assertRaises(WikiConflictError) as raised:
            self.files.validate_retry_intent(
                operation_id, page_id, original.content_sha256, reviewed
            )

        self.assertEqual(raised.exception.code, "duplicate_id")
        self.assertEqual(
            {path: path.read_bytes() for path in before},
            before,
        )

    def test_retry_intent_validation_rejects_mismatch_or_corrupt_journal_read_only(self) -> None:
        page_id, original, _ = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Journaled review.\n")
        operation_id = uuid4()
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        new_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")
        intent_path = self.vault.resolve(
            f".knowgrain/review-operations/{operation_id}/intent.json"
        )

        with patch.object(
            self.files,
            "_remove_old_after_move",
            side_effect=OSError("simulated interruption"),
        ):
            with self.assertRaises(WikiConflictError):
                self.files.commit(operation_id, page_id, original.content_sha256, reviewed)

        old_before = old_path.read_bytes()
        new_before = new_path.read_bytes()
        changed_request = markdown(page_id, status="reviewed", body="Different request.\n")
        with self.assertRaises(WikiConflictError) as mismatch:
            self.files.validate_retry_intent(
                operation_id, page_id, original.content_sha256, changed_request
            )
        self.assertEqual(mismatch.exception.code, "review_intent_mismatch")
        self.assertEqual(old_path.read_bytes(), old_before)
        self.assertEqual(new_path.read_bytes(), new_before)

        intent_path.write_bytes(b"not a valid immutable intent")
        with self.assertRaises(WikiConflictError) as corrupt:
            self.files.validate_retry_intent(
                operation_id, page_id, original.content_sha256, reviewed
            )
        self.assertEqual(corrupt.exception.code, "review_intent_invalid")
        self.assertEqual(old_path.read_bytes(), old_before)
        self.assertEqual(new_path.read_bytes(), new_before)

    def test_journal_directory_fsync_failure_blocks_wiki_mutation_and_retry_resyncs_chain(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Durably reviewed.\n")
        operation_id = uuid4()
        intent_relative = f".knowgrain/review-operations/{operation_id}/intent.json"
        intent_path = self.vault.resolve(intent_relative)
        operation_directory = intent_path.parent
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        new_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")
        sync_events: list[str] = []
        wiki_mutations: list[str] = []
        failed_once = False
        real_sync_directory = self.files._strict_fsync_directory
        real_publish_exclusive = self.files._publish_exclusive

        def sync_directory(path: Path) -> None:
            nonlocal failed_once
            relative = (
                "<vault>"
                if path == self.vault.root
                else path.relative_to(self.vault.root).as_posix()
            )
            sync_events.append(relative)
            # Fail after the hard link is visible, emulating an interrupted
            # operation directory fsync. The retry must resync every ancestor.
            if path == operation_directory and intent_path.exists() and not failed_once:
                failed_once = True
                raise OSError("simulated journal directory fsync failure")
            real_sync_directory(path)

        def publish_exclusive(destination: Path, content: bytes) -> None:
            wiki_mutations.append(destination.relative_to(self.vault.root).as_posix())
            real_publish_exclusive(destination, content)

        with (
            patch.object(self.files, "_strict_fsync_directory", side_effect=sync_directory),
            patch.object(self.files, "_publish_exclusive", side_effect=publish_exclusive),
        ):
            with self.assertRaisesRegex(OSError, "journal directory fsync"):
                self.files.commit(
                    operation_id, page_id, original.content_sha256, reviewed
                )

            self.assertEqual(old_path.read_text(), old_markdown)
            self.assertFalse(new_path.exists())
            self.assertEqual(wiki_mutations, [])
            self.assertTrue(intent_path.exists())

            # Keep the published-but-previously-unsynced journal. The normal
            # retry path must make its full directory chain durable first.
            sync_events.clear()
            result = self.files.commit(
                operation_id, page_id, original.content_sha256, reviewed
            )

        expected_chain = [
            "<vault>",
            ".knowgrain",
            ".knowgrain/review-operations",
            f".knowgrain/review-operations/{operation_id}",
        ]
        self.assertEqual(sync_events[: len(expected_chain)], expected_chain)
        self.assertEqual(len(wiki_mutations), 1)
        self.assertEqual(wiki_mutations[0], f"Wiki/Pages/{page_id}.md")
        self.assertEqual(result.markdown, reviewed)
        self.assertFalse(old_path.exists())
        self.assertEqual(new_path.read_text(), reviewed)

    def test_external_old_edit_after_partial_move_is_preserved(self) -> None:
        page_id, original, _ = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Approved content.\n")
        operation_id = uuid4()
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        new_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")
        external_old = b"External edit to original, retained for recovery.\n"

        def edit_old_then_interrupt(intent):
            old_path.write_bytes(external_old)
            raise OSError("simulated interruption")

        with patch.object(
            self.files, "_remove_old_after_move", side_effect=edit_old_then_interrupt
        ):
            with self.assertRaises(WikiConflictError):
                self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        self.assertEqual(old_path.read_bytes(), external_old)
        self.assertEqual(new_path.read_text(), reviewed)

        with self.assertRaises(WikiConflictError) as raised:
            self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        self.assertEqual(raised.exception.code, "review_state_changed")
        self.assertEqual(old_path.read_bytes(), external_old)
        self.assertEqual(new_path.read_text(), reviewed)

    def test_external_new_edit_after_partial_move_is_preserved(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Approved content.\n")
        operation_id = uuid4()
        old_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        new_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")

        with patch.object(
            self.files, "_remove_old_after_move", side_effect=OSError("interruption")
        ):
            with self.assertRaises(WikiConflictError):
                self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        external_new = b"Someone edited the published destination.\n"
        new_path.write_bytes(external_new)

        with self.assertRaises(WikiConflictError) as raised:
            self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        self.assertEqual(raised.exception.code, "review_state_changed")
        self.assertEqual(old_path.read_text(), old_markdown)
        self.assertEqual(new_path.read_bytes(), external_new)

    def test_retry_with_a_different_request_rejects_existing_journal(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        reviewed = markdown(page_id, status="reviewed", body="Approved content.\n")
        operation_id = uuid4()

        with patch.object(self.files, "_publish_exclusive", side_effect=OSError("interruption")):
            with self.assertRaises(WikiConflictError):
                self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        journal = self.vault.resolve(
            f".knowgrain/review-operations/{operation_id}/intent.json"
        )
        original_intent = journal.read_bytes()
        changed_request = markdown(page_id, status="reviewed", body="Different review.\n")

        with self.assertRaises(WikiConflictError) as raised:
            self.files.commit(operation_id, page_id, original.content_sha256, changed_request)
        self.assertEqual(raised.exception.code, "review_intent_mismatch")
        self.assertEqual(journal.read_bytes(), original_intent)
        self.assertEqual(
            self.vault.resolve(f"Wiki/Drafts/{page_id}.md").read_text(), old_markdown
        )
        self.assertFalse(self.vault.resolve(f"Wiki/Pages/{page_id}.md").exists())

    def test_initial_external_source_change_is_not_journaled_or_overwritten(self) -> None:
        page_id, original, _ = self.create_draft()
        external = b"External replacement remains authoritative.\n"
        self.vault.resolve(f"Wiki/Drafts/{page_id}.md").write_bytes(external)
        reviewed = markdown(page_id, status="reviewed", body="Approved content.\n")
        operation_id = uuid4()

        with self.assertRaises(WikiConflictError):
            self.files.commit(operation_id, page_id, original.content_sha256, reviewed)
        self.assertEqual(self.vault.resolve(f"Wiki/Drafts/{page_id}.md").read_bytes(), external)
        self.assertFalse(
            self.vault.resolve(f".knowgrain/review-operations/{operation_id}/intent.json").exists()
        )

    def test_duplicate_page_identity_blocks_review_without_file_changes(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        duplicate = self.vault.resolve("Wiki/Pages/duplicate.md")
        self.make_page_file(page_id, "Wiki/Pages/duplicate.md", old_markdown)
        reviewed = markdown(page_id, status="reviewed", body="Approved content.\n")

        with self.assertRaises(WikiConflictError) as raised:
            self.files.commit(uuid4(), page_id, original.content_sha256, reviewed)
        self.assertEqual(raised.exception.code, "duplicate_id")
        self.assertEqual(self.vault.resolve(f"Wiki/Drafts/{page_id}.md").read_text(), old_markdown)
        self.assertEqual(duplicate.read_text(), old_markdown)

    def test_managed_symlink_blocks_review_without_touching_external_file(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        outside = self.root.parent / "outside.md"
        outside.write_text("external data stays untouched")
        symlink = self.vault.resolve(f"Wiki/Pages/{page_id}.md")
        symlink.symlink_to(outside)
        reviewed = markdown(page_id, status="reviewed", body="Approved content.\n")

        with self.assertRaises(WikiConflictError) as raised:
            self.files.commit(uuid4(), page_id, original.content_sha256, reviewed)
        self.assertEqual(raised.exception.code, "unsafe_path")
        self.assertEqual(self.vault.resolve(f"Wiki/Drafts/{page_id}.md").read_text(), old_markdown)
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(outside.read_text(), "external data stays untouched")

    def test_symlinked_journal_parent_is_rejected(self) -> None:
        page_id, original, old_markdown = self.create_draft()
        outside = self.root.parent / "outside-journal"
        outside.mkdir()
        journal_root = self.vault.resolve(".knowgrain/review-operations")
        journal_root.parent.mkdir(exist_ok=True)
        journal_root.symlink_to(outside, target_is_directory=True)
        reviewed = markdown(page_id, status="reviewed", body="Approved content.\n")

        with self.assertRaises(VaultPathError):
            self.files.commit(uuid4(), page_id, original.content_sha256, reviewed)
        self.assertEqual(self.vault.resolve(f"Wiki/Drafts/{page_id}.md").read_text(), old_markdown)
        self.assertEqual(list(outside.iterdir()), [])

    def test_new_markdown_must_match_page_id_and_reviewed_status(self) -> None:
        page_id, original, _ = self.create_draft()
        with self.assertRaises(WikiValidationError):
            self.files.commit(
                uuid4(), page_id, original.content_sha256,
                markdown(uuid4(), status="reviewed", body="Wrong page.\n"),
            )
        with self.assertRaises(WikiValidationError):
            self.files.commit(
                uuid4(), page_id, original.content_sha256,
                markdown(page_id, status="draft", body="Still a draft.\n"),
            )


if __name__ == "__main__":
    unittest.main()
