from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from uuid import UUID
from unittest.mock import patch

from knowgrain.vault import VaultStore
from knowgrain.wiki_files import (
    MAX_DIFF_BYTES,
    MAX_FRONTMATTER_BYTES,
    MAX_PAGE_BYTES,
    WikiConflictError,
    WikiFileStore,
    WikiNotFoundError,
    WikiValidationError,
    parse_wiki,
    parse_wikilinks,
)


PAGE_ID = "a93458ae-8f65-4df5-8650-7de9b0579d68"


def page(markdown_body: str = "# Hello\n", *, page_id: str = PAGE_ID, status: str = "draft") -> str:
    return (
        "---\n"
        f"kg_id: {page_id}\n"
        "kg_kind: wiki\n"
        f"kg_status: {status}\n"
        "---\n"
        f"{markdown_body}"
    )


class WikiFileParserTests(unittest.TestCase):
    def test_parse_preserves_lf_crlf_document_and_hashes_entire_markdown(self) -> None:
        markdown = (
            f"---\r\nkg_id: {PAGE_ID}\r\nkg_kind: wiki\r\nkg_status: draft\r\n"
            "title: 'Title'\r\n---\r\n"
            "\r\n# Heading\r\n[[Other#Part|label]]\r\n"
        )
        parsed = parse_wiki(markdown, "Wiki/Drafts/a.md")
        self.assertEqual(parsed.markdown, markdown)
        self.assertEqual(parsed.title, "Title")
        self.assertEqual(parsed.content_sha256, hashlib.sha256(markdown.encode()).hexdigest())
        self.assertEqual(parsed.links[0].line, 9)

        no_metadata_title = parse_wiki(page("#  First heading ##\n"), "Wiki/Pages/fallback.md")
        self.assertEqual(no_metadata_title.title, "First heading")
        blank_title_markdown = page("# Fallback heading\n").replace(
            "kg_status: draft\n", 'kg_status: draft\ntitle: "  "\n'
        )
        empty_metadata_title = parse_wiki(
            blank_title_markdown,
            "Wiki/Pages/empty-title.md",
        )
        self.assertEqual(empty_metadata_title.title, "Fallback heading")

    def test_parse_rejects_malformed_identity_metadata_and_unsafe_yaml(self) -> None:
        invalid_documents = [
            "# no frontmatter\n",
            page().replace("kg_kind: wiki", "kg_kind: source"),
            page().replace("kg_status: draft", "kg_status: pending"),
            page().replace(f"kg_id: {PAGE_ID}", "kg_id: not-a-uuid"),
            page().replace("kg_status: draft\n", "kg_status: draft\nkg_status: reviewed\n"),
            page().replace("kg_status: draft", "kg_status: !!python/object/apply:os.system ['id']"),
            page().replace("kg_status: draft", "kg_status: draft\nextra: &shared x\ncopy: *shared"),
        ]
        for document in invalid_documents:
            with self.subTest(document=document[:80]), self.assertRaises(WikiValidationError):
                parse_wiki(document, "Wiki/Drafts/sample.md")

    def test_parse_rejects_nul_in_markdown_or_yaml_decoded_title(self) -> None:
        yaml_nul = (
            f'---\nkg_id: {PAGE_ID}\nkg_kind: wiki\nkg_status: draft\n'
            'title: "bad\\0title"\n---\n# Heading\n'
        )
        documents = (page("body\x00text\n"), yaml_nul, page("[[target\x00|label]]\n"))
        for document in documents:
            with self.subTest(document=repr(document)), self.assertRaisesRegex(
                WikiValidationError, "NUL"
            ):
                parse_wiki(document, "Wiki/Drafts/nul.md")

    def test_page_link_limit_rejects_the_page_without_partial_links(self) -> None:
        markdown = page("[[one]]\n[[two]]\n[[three]]\n")
        with patch("knowgrain.wiki_files.MAX_PAGE_LINKS", 2):
            with self.assertRaisesRegex(WikiValidationError, "wikilink limit"):
                parse_wiki(markdown, "Wiki/Drafts/too-many-links.md")

    def test_parse_enforces_page_frontmatter_and_yaml_complexity_bounds(self) -> None:
        too_large_page = page("x" * MAX_PAGE_BYTES)
        with self.assertRaisesRegex(WikiValidationError, "2 MiB"):
            parse_wiki(too_large_page, "Wiki/Drafts/large.md")

        too_large_frontmatter = (
            f"---\nkg_id: {PAGE_ID}\nkg_kind: wiki\nkg_status: draft\n"
            f"notes: {'x' * MAX_FRONTMATTER_BYTES}\n---\n"
        )
        with self.assertRaisesRegex(WikiValidationError, "32 KiB"):
            parse_wiki(too_large_frontmatter, "Wiki/Drafts/large-frontmatter.md")

        nested = "value\n"
        for _ in range(40):
            nested = "-\n  " + nested.replace("\n", "\n  ")
        too_deep = (
            f"---\nkg_id: {PAGE_ID}\nkg_kind: wiki\nkg_status: draft\nextra: {nested}---\n"
        )
        with self.assertRaises(WikiValidationError):
            parse_wiki(too_deep, "Wiki/Drafts/deep.md")

    def test_wikilinks_skip_frontmatter_fences_inline_code_comments_and_escapes(self) -> None:
        markdown = (
            "---\n"
            f"kg_id: {PAGE_ID}\nkg_kind: wiki\nkg_status: draft\nlinks: [[frontmatter]]\n"
            "---\n"
            "A [[Page#Heading|Shown]] and ![[Embedded#^block|preview]].\n"
            "A local [[#Local heading]] anchor.\n"
            "Escaped \\[[Nope]] and double \\\\[[Yes]].\n"
            "Inline `[[Code]]` plus `` `[[Also code]] ``.\n"
            "Inline comment tokens `<!-- [[Also code]] -->` then [[After code]].\n"
            "<!-- [[Comment]]\n[[Still comment]] -->\n"
            "```md\n[[Fenced]]\n```\n"
            "~~~\n![[Tilde fenced]]\n~~~\n"
            "After [[Kept]].\n"
        )
        links = parse_wikilinks(markdown)
        self.assertEqual(
            [(link.target, link.anchor, link.label, link.embed) for link in links],
            [
                ("Page", "Heading", "Shown", False),
                ("Embedded", "^block", "preview", True),
                ("", "Local heading", None, False),
                ("Yes", None, None, False),
                ("After code", None, None, False),
                ("Kept", None, None, False),
            ],
        )
        self.assertEqual([link.line for link in links], [7, 7, 8, 9, 11, 20])

    def test_links_without_frontmatter_retain_original_line_numbers(self) -> None:
        links = parse_wikilinks("first\nsecond [[Page]]\n")
        self.assertEqual(links[0].line, 2)

    def test_escaped_backticks_and_fences_inside_comments_do_not_hide_links(self) -> None:
        markdown = (
            r"Escaped \` [[Visible]] \` and [[Outside]]." + "\n"
            "<!--\n```\ncomment content\n-->\n"
            "[[After comment]]\n"
        )
        self.assertEqual(
            [link.target for link in parse_wikilinks(markdown)],
            ["Visible", "Outside", "After comment"],
        )

    def test_large_link_input_keeps_line_numbers_and_unclosed_openers_are_linear(self) -> None:
        many_links = "\n".join("[[Page]]" for _ in range(10_000))
        links = parse_wikilinks(many_links)
        self.assertEqual(len(links), 10_000)
        self.assertEqual(links[0].line, 1)
        self.assertEqual(links[-1].line, 10_000)
        self.assertEqual(parse_wikilinks("[[a" * 10_000), ())


class WikiFileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "vault"
        self.vault = VaultStore(self.root)
        self.vault.initialize()
        self.store = WikiFileStore(self.vault)

    def test_create_save_and_recovery_snapshot_use_real_files(self) -> None:
        created = self.store.create("A title: with quotes 'ok'", "# Body\n\n[[Other]]\n")
        path = self.root / created.vault_path
        self.assertTrue(path.is_file())
        self.assertEqual(created.status, "draft")
        self.assertEqual(created.title, "A title: with quotes 'ok'")
        self.assertEqual(self.store.scan().pages, (created,))

        edited_markdown = created.markdown.replace("# Body", "# Edited")
        saved = self.store.save(created.page_id, edited_markdown, created.content_sha256)
        self.assertEqual(saved.markdown, edited_markdown)
        self.assertEqual(path.read_text(encoding="utf-8"), edited_markdown)
        snapshot = (
            self.root
            / ".knowgrain"
            / "wiki-recovery"
            / str(created.page_id)
            / f"{created.content_sha256}.md"
        )
        self.assertEqual(snapshot.read_bytes(), created.markdown.encode("utf-8"))

    def test_stale_save_returns_current_markdown_and_bounded_diff_without_overwrite(self) -> None:
        created = self.store.create("Stale test", "original body\n")
        destination = self.root / created.vault_path
        external_markdown = created.markdown.replace("original body", "Obsidian edit")
        destination.write_bytes(external_markdown.encode("utf-8"))
        web_markdown = created.markdown.replace("original body", "Web edit")

        with self.assertRaises(WikiConflictError) as raised:
            self.store.save(created.page_id, web_markdown, created.content_sha256)

        conflict = raised.exception
        self.assertEqual(conflict.code, "stale_content")
        self.assertEqual(conflict.current.markdown, external_markdown)
        self.assertIn("Obsidian edit", conflict.diff)
        self.assertIn("Web edit", conflict.diff)
        self.assertEqual(destination.read_bytes(), external_markdown.encode("utf-8"))
        self.assertLessEqual(len(conflict.diff.encode("utf-8")), MAX_DIFF_BYTES)

    def test_stale_diff_is_capped_at_64_kib(self) -> None:
        created = self.store.create("Large diff", "initial\n")
        destination = self.root / created.vault_path
        external_markdown = created.markdown.replace("initial", "external" + "x" * 100_000)
        destination.write_text(external_markdown, encoding="utf-8")
        web_markdown = created.markdown.replace("initial", "submitted" + "y" * 100_000)

        with self.assertRaises(WikiConflictError) as raised:
            self.store.save(created.page_id, web_markdown, created.content_sha256)

        self.assertLessEqual(len(raised.exception.diff.encode("utf-8")), MAX_DIFF_BYTES)
        self.assertTrue(raised.exception.diff.endswith("[diff truncated]\n"))

    def test_rename_preserves_identity_and_save_targets_new_path(self) -> None:
        created = self.store.create("Rename", "body\n")
        old_path = self.root / created.vault_path
        new_relative = f"Wiki/Pages/renamed-{created.page_id}.md"
        new_path = self.root / new_relative
        old_path.rename(new_path)

        scan = self.store.scan()
        self.assertEqual(len(scan.pages), 1)
        self.assertEqual(scan.pages[0].page_id, created.page_id)
        self.assertEqual(scan.pages[0].vault_path, new_relative)
        saved_markdown = created.markdown.replace("body", "after rename")
        saved = self.store.save(created.page_id, saved_markdown, created.content_sha256)
        self.assertEqual(saved.vault_path, new_relative)
        self.assertEqual(new_path.read_text(encoding="utf-8"), saved_markdown)
        self.assertFalse(old_path.exists())

    def test_duplicate_id_pages_are_excluded_and_cannot_be_saved(self) -> None:
        self._write("Wiki/Drafts/a.md", page("a\n"))
        self._write("Wiki/Pages/b.md", page("b\n"))
        scan = self.store.scan()
        self.assertEqual(scan.pages, ())
        self.assertEqual({issue.code for issue in scan.issues}, {"duplicate_id"})
        with self.assertRaises(WikiConflictError) as raised:
            self.store.save(UUID(PAGE_ID), page("edit\n"), "0" * 64)
        self.assertEqual(raised.exception.code, "duplicate_id")

    def test_save_requires_identity_and_status_to_remain_unchanged(self) -> None:
        created = self.store.create("Status", "body\n")
        status_changed = created.markdown.replace("kg_status: draft", "kg_status: reviewed")
        with self.assertRaisesRegex(WikiValidationError, "status"):
            self.store.save(created.page_id, status_changed, created.content_sha256)
        identity_changed = created.markdown.replace(str(created.page_id), PAGE_ID)
        with self.assertRaisesRegex(WikiValidationError, "identity"):
            self.store.save(created.page_id, identity_changed, created.content_sha256)
        self.assertEqual((self.root / created.vault_path).read_text(), created.markdown)

    def test_scan_reports_invalid_utf8_malformed_and_oversized_files(self) -> None:
        self._write("Wiki/Drafts/invalid.md", b"\xff")
        self._write("Wiki/Drafts/malformed.md", "---\nnot: [yaml\n")
        self._write(
            "Wiki/Drafts/nul-title.md",
            f'---\nkg_id: {PAGE_ID}\nkg_kind: wiki\nkg_status: draft\ntitle: "bad\\0title"\n---\n',
        )
        self._write("Wiki/Pages/large.md", b"x" * (MAX_PAGE_BYTES + 1))
        scan = self.store.scan()
        self.assertEqual(scan.pages, ())
        codes = {issue.vault_path: issue.code for issue in scan.issues}
        self.assertEqual(codes["Wiki/Drafts/invalid.md"], "invalid_utf8")
        self.assertEqual(codes["Wiki/Drafts/malformed.md"], "invalid_frontmatter")
        self.assertEqual(codes["Wiki/Drafts/nul-title.md"], "invalid_frontmatter")
        self.assertEqual(codes["Wiki/Pages/large.md"], "oversized")
        self.assertTrue(scan.complete)

    def test_scan_stops_after_the_candidate_file_bound(self) -> None:
        directory = self.root / "Wiki/Drafts"
        for index in range(10_001):
            (directory / f"candidate-{index:05d}.md").touch()
        known_page = page("body\n")
        (directory / "candidate-00000.md").write_text(known_page, encoding="utf-8")

        scan = self.store.scan()

        self.assertEqual(scan.pages, ())
        self.assertEqual(len(scan.issues), 10_000)
        self.assertEqual(sum(issue.code == "scan_limit" for issue in scan.issues), 1)
        self.assertFalse(scan.complete)
        with self.assertRaises(WikiConflictError) as raised:
            self.store.save(
                UUID(PAGE_ID), known_page, hashlib.sha256(known_page.encode()).hexdigest()
            )
        self.assertEqual(raised.exception.code, "scan_limit")

    def test_scan_entry_and_read_byte_limits_withhold_partial_pages(self) -> None:
        first = page("first\n")
        second = page("second\n", page_id="a93458ae-8f65-4df5-8650-7de9b0579d69")
        self._write("Wiki/Pages/one.md", first)
        self._write("Wiki/Drafts/two.md", second)

        with patch("knowgrain.wiki_files.MAX_SCAN_ENTRIES", 1):
            entry_scan = self.store.scan()
        self.assertEqual(entry_scan.pages, ())
        self.assertIn("scan_limit", {issue.code for issue in entry_scan.issues})
        self.assertFalse(entry_scan.complete)

        byte_budget = len(first.encode()) + len(second.encode()) - 1
        with patch("knowgrain.wiki_files.MAX_SCAN_BYTES", byte_budget):
            byte_scan = self.store.scan()
        self.assertEqual(byte_scan.pages, ())
        self.assertIn("scan_limit", {issue.code for issue in byte_scan.issues})
        self.assertFalse(byte_scan.complete)

    def test_scan_total_link_limit_withholds_partial_projection(self) -> None:
        self._write("Wiki/Drafts/linked.md", page("[[one]]\n[[two]]\n"))
        with patch("knowgrain.wiki_files.MAX_SCAN_LINKS", 1):
            scan = self.store.scan()

        self.assertEqual(scan.pages, ())
        self.assertFalse(scan.complete)
        self.assertIn("scan_limit", {issue.code for issue in scan.issues})

    def test_directory_scan_error_marks_the_projection_incomplete(self) -> None:
        self._write("Wiki/Drafts/valid.md", page("valid\n"))
        with patch("knowgrain.wiki_files.os.scandir", side_effect=PermissionError("denied")):
            scan = self.store.scan()

        self.assertEqual(scan.pages, ())
        self.assertFalse(scan.complete)
        self.assertIn("scan_error", {issue.code for issue in scan.issues})

    def test_create_rejects_empty_and_nul_titles(self) -> None:
        for title in ("", "  \t", "bad\x00title"):
            with self.subTest(title=repr(title)), self.assertRaises(WikiValidationError):
                self.store.create(title, "body\n")

    def test_scan_rejects_symlinks_and_special_nodes_without_blocking_on_fifo(self) -> None:
        outside = Path(self.temporary_directory.name) / "outside.md"
        outside.write_text(page("outside\n"), encoding="utf-8")
        (self.root / "Wiki/Drafts/symlink.md").symlink_to(outside)
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "Wiki/Drafts/wait.md")

        scan = self.store.scan()
        by_path = {issue.vault_path: issue.code for issue in scan.issues}
        self.assertEqual(by_path["Wiki/Drafts/symlink.md"], "unsafe_path")
        if hasattr(os, "mkfifo"):
            self.assertEqual(by_path["Wiki/Drafts/wait.md"], "unsafe_path")
        self.assertEqual(scan.pages, ())
        self.assertTrue(scan.complete)

    def test_scan_ignores_files_outside_managed_roots_and_rejects_bad_paths(self) -> None:
        self._write("Wiki/Other/page.md", page("ignored\n"))
        self._write("Wiki/Drafts/not-markdown.txt", "ordinary text")
        self._write("Wiki/Drafts/subdir/page.md", page("included\n"))
        scan = self.store.scan()
        self.assertEqual([item.vault_path for item in scan.pages], ["Wiki/Drafts/subdir/page.md"])
        with self.assertRaises(WikiValidationError):
            parse_wiki(page(), "Wiki/Other/page.md")
        with self.assertRaises(WikiValidationError):
            parse_wiki(page(), "Wiki/Drafts/../Pages/page.md")

    def test_external_removal_is_not_replaced(self) -> None:
        created = self.store.create("Removed", "body\n")
        (self.root / created.vault_path).unlink()
        with self.assertRaises(WikiNotFoundError):
            self.store.save(created.page_id, created.markdown, created.content_sha256)
        self.assertFalse((self.root / created.vault_path).exists())

    def _write(self, relative: str, content: str | bytes) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)


if __name__ == "__main__":
    unittest.main()
