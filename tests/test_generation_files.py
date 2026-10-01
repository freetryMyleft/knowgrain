"""Generated publication uses real files and preserves external edits."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from knowgrain.generation_files import GenerationFileStore
from knowgrain.vault import VaultStore
from knowgrain.wiki_files import WikiConflictError, WikiFileStore


class GenerationFileTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = VaultStore(Path(temporary.name) / "vault")
        self.vault.initialize()
        self.wiki = WikiFileStore(self.vault)
        self.files = GenerationFileStore(self.wiki)
        self.page_id, self.evidence_id = uuid4(), uuid4()
        self.markdown = (
            f"---\nkg_id: {self.page_id}\nkg_kind: wiki\nkg_status: draft\n"
            "title: Supported draft\n---\n\nA claim with evidence.\n"
        )
        self.evidence = f"---\nkg_kind: evidence\nkg_id: {self.evidence_id}\n---\n\nVerified quote.\n"

    def test_retry_retains_identity_and_discovers_external_move(self):
        first = self.files.publish(self.page_id, self.markdown, [(self.evidence_id, self.evidence)])
        old = self.vault.resolve(first.vault_path)
        moved = self.vault.resolve("Wiki/Drafts/Moved.md")
        old.rename(moved)
        retried = self.files.publish(self.page_id, self.markdown, [(self.evidence_id, self.evidence)])
        self.assertEqual(retried.page_id, self.page_id)
        self.assertEqual(retried.vault_path, "Wiki/Drafts/Moved.md")
        self.assertFalse(old.exists())
        self.assertEqual(moved.read_text(), self.markdown)

    def test_retry_cannot_overwrite_manually_edited_or_reviewed_page(self):
        first = self.files.publish(self.page_id, self.markdown, [])
        path = self.vault.resolve(first.vault_path)
        for changed in (self.markdown + "Human edit.\n", self.markdown.replace("kg_status: draft", "kg_status: reviewed")):
            path.write_text(changed)
            with self.assertRaises(WikiConflictError):
                self.files.publish(self.page_id, self.markdown, [])
            self.assertEqual(path.read_text(), changed)

    def test_changed_evidence_blocks_publication_without_overwrite(self):
        relative = f"Sources/Evidence/{self.evidence_id}.md"
        path = self.vault.resolve(relative)
        path.write_text("Externally changed derivative evidence")
        with self.assertRaises(WikiConflictError):
            self.files.publish(self.page_id, self.markdown, [(self.evidence_id, self.evidence)])
        self.assertEqual(path.read_text(), "Externally changed derivative evidence")
        self.assertFalse(self.vault.resolve(f"Wiki/Drafts/{self.page_id}.md").exists())
