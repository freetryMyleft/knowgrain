import asyncio
import hashlib
import json
import os
import socket
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from knowgrain.core_content_manifest import ContentManifestError, build_expected_content_manifest
from knowgrain.index_profile import IndexProfile
from knowgrain.providers import EmbeddingRoleConfig, LLMRoleConfig, build_provider_callbacks
from knowgrain.tokenizer_cache import cache_path
from test_parsers import make_docx_with_paragraph_and_table, make_pdf_with_blank_first_page

REVISION = UUID("00000000-0000-4000-8000-000000000001")


class ContentManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lightrag import LightRAG
        from lightrag.utils import EmbeddingFunc

        cls.cache = Path("data/tokenizers").resolve()
        if not cache_path(cls.cache).is_file():
            raise RuntimeError("Run make tokenizer before these offline tests")
        cls.environment = patch.dict(os.environ, {"TIKTOKEN_CACHE_DIR": str(cls.cache)})
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.callbacks = build_provider_callbacks(
            LLMRoleConfig(provider="ollama", base_url="http://localhost:11434", model="test-llm"),
            EmbeddingRoleConfig(
                provider="ollama",
                base_url="http://localhost:11434",
                model="test-embed",
                dimension=2,
            ),
        )
        # Core constructor only: no initialize, DB, model, or insertion.
        cls.rag = LightRAG(
            working_dir=cls.directory.name,
            tiktoken_model_name="gpt-4o",
            llm_model_name=cls.callbacks.llm_config.model,
            llm_model_func=cls.callbacks.llm,
            embedding_func=EmbeddingFunc(
                embedding_dim=2,
                max_token_size=32768,
                supports_asymmetric=True,
                func=cls.callbacks.embed,
            ),
        )
        cls.profile = IndexProfile.capture(cls.rag, cls.callbacks, cls.cache)
        cls.tokenizer = cls.rag.tokenizer

    def profile_options(
        self,
        *,
        size=1200,
        overlap=100,
        split=None,
        only=False,
        embedding=32768,
        embedding_overlap=0,
    ):
        data = json.loads(self.profile.to_canonical_json())
        data["content"]["chunker"]["fixed_token"].update(
            chunk_token_size=size,
            chunk_overlap_token_size=overlap,
            split_by_character=split,
            split_by_character_only=only,
        )
        data["content"]["legacy_options"].update(
            chunk_token_size=size,
            chunk_overlap_token_size=overlap,
            split_by_character=split,
            split_by_character_only=only,
        )
        data["content"]["limits"].update(
            embedding_token_limit=embedding,
            embedding_chunk_overlap_token_size=embedding_overlap,
        )
        data["embedding"]["config"]["max_token_size"] = embedding
        return IndexProfile.from_canonical_json(json.dumps(data))

    def build(self, text=b"hello", **kwargs):
        arguments = dict(
            profile=self.profile,
            revision_id=REVISION,
            filename="private.txt",
            vault_path="sources/private.txt",
            original_bytes=text,
            expected_source_sha256=hashlib.sha256(text).hexdigest(),
            tokenizer=self.tokenizer,
            tokenizer_cache_dir=self.cache,
        )
        arguments.update(kwargs)
        with (
            patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),
            patch.object(self.rag, "initialize_storages", side_effect=AssertionError),
            patch.object(self.rag, "ainsert", side_effect=AssertionError),
            patch(
                "knowgrain.providers.callbacks._ollama_embedding",
                create=True,
                side_effect=AssertionError,
            ),
        ):
            return build_expected_content_manifest(**arguments)

    def test_text_cleaning_raw_literal_and_independent_hashes(self):
        original = (
            b"\xef\xbb\xbf  # Title\r\n\r\nA&amp;B\x00\x1c\tC\n{{LRdoc}} <|endoftext|> "
            + "中文🧪".encode()
        )
        m = self.build(original, filename="private.md")
        expected_parsed = "# Title\r\n\r\nA&amp;B\x00\x1c\tC\n{{LRdoc}} <|endoftext|> 中文🧪"
        expected_core = "# Title\r\n\r\nA&B\tC\n{{LRdoc}} <|endoftext|> 中文🧪"
        self.assertEqual(m.parsed_text, expected_parsed)
        self.assertEqual(m.core_text, expected_core)
        self.assertEqual(m.parsed_sha256, hashlib.sha256(expected_parsed.encode()).hexdigest())
        self.assertEqual(m.core_sha256, hashlib.sha256(expected_core.encode()).hexdigest())
        self.assertEqual(m.dedup_md5, hashlib.md5(expected_core.encode()).hexdigest())
        self.assertEqual([s.heading for s in m.segments], ["Title", "Title"])
        self.assertEqual(m.raw_format, "raw")
        self.assertEqual(m.chunks[0].id, f"{REVISION}-chunk-000")
        self.assertEqual(m.chunks[0].content, expected_core)
        self.assertEqual(m.chunks[0].tokens, len(self.tokenizer.encode(expected_core)))
        self.assertEqual(m.canonical_file_path, "private.txt")
        self.assertEqual(m.chunks[0].file_path, "private.txt")

    def test_fixed_override_overlap_and_reference_pipeline(self):
        from lightrag.chunker import chunking_by_token_size
        from lightrag.utils import enforce_chunk_token_limit_before_embedding
        from lightrag.utils_pipeline import build_chunks_dict_from_chunking_result

        text = "one two three four five six seven eight nine ten eleven twelve"
        profile = self.profile_options(size=5, overlap=2)
        manifest = self.build(text.encode(), profile=profile)
        tokens = self.tokenizer.encode(text)
        expected = [
            self.tokenizer.decode(tokens[i : i + 5]).strip() for i in range(0, len(tokens), 3)
        ]
        self.assertEqual([c.content for c in manifest.chunks], expected)
        self.assertEqual(
            [c.id for c in manifest.chunks],
            [f"{REVISION}-chunk-{i:03d}" for i in range(len(expected))],
        )
        options = json.loads(manifest.chunk_options)
        self.assertEqual(set(options), {"chunk_token_size", "fixed_token"})
        self.assertEqual(options["fixed_token"]["chunk_token_size"], 5)
        # Separate reference to the pinned pipeline order and six positional args.
        reference = chunking_by_token_size(
            self.tokenizer, text, None, False, 2, 5, _emit_source_span=True
        )
        reference = enforce_chunk_token_limit_before_embedding(
            reference, self.tokenizer, 32768, source_content=text
        )
        stored = build_chunks_dict_from_chunking_result(
            reference, doc_id=str(REVISION), file_path="private.txt"
        )
        self.assertEqual(
            [(c.id, c.content, c.tokens) for c in manifest.chunks],
            [(k, v["content"], v["tokens"]) for k, v in stored.items()],
        )
        self.assertTrue(all("_source_span" not in v for v in stored.values()))

    def test_split_defaults_retained_duplicate_positional_ids(self):
        m = self.build(
            b"same|same|third",
            profile=self.profile_options(size=5, overlap=0, split="|", only=True),
        )
        self.assertEqual([c.content for c in m.chunks], ["same", "same", "third"])
        self.assertEqual(
            [c.id for c in m.chunks],
            [f"{REVISION}-chunk-000", f"{REVISION}-chunk-001", f"{REVISION}-chunk-002"],
        )
        self.assertEqual(
            json.loads(m.chunk_options)["fixed_token"]["split_by_character_only"], True
        )
        with self.assertRaises(ContentManifestError) as failure:
            self.build(
                b"private words words words|short",
                profile=self.profile_options(size=2, overlap=0, split="|", only=True),
            )
        self.assertNotIn("private", str(failure.exception))
        self.assertIsNone(failure.exception.__cause__)

    def test_split_nononly_large_segment(self):
        m = self.build(
            b"one two three four five|last",
            profile=self.profile_options(size=3, overlap=1, split="|"),
        )
        self.assertEqual(
            [c.content for c in m.chunks], ["one two three", "three four five", "five", "last"]
        )
        self.assertTrue(all(c.tokens <= 3 for c in m.chunks))

    def test_embedding_fallback_overlap_continuous_ids(self):
        text = "one two three four five six seven eight nine"
        m = self.build(
            text.encode(),
            profile=self.profile_options(size=20, overlap=0, embedding=4, embedding_overlap=1),
        )
        self.assertEqual(
            [c.content for c in m.chunks],
            ["one two three four", " four five six seven", " seven eight nine"],
        )
        self.assertEqual([c.chunk_order_index for c in m.chunks], [0, 1, 2])
        self.assertEqual([c.split_part for c in m.chunks], [1, 2, 3])
        self.assertEqual([c.split_total for c in m.chunks], [3, 3, 3])
        self.assertTrue(all(c.split_type == "hard_fallback" for c in m.chunks))
        self.assertTrue(
            all(c.tokens == len(self.tokenizer.encode(c.content)) <= 4 for c in m.chunks)
        )
        self.assertEqual([c.id for c in m.chunks], [f"{REVISION}-chunk-{i:03d}" for i in range(3)])

    def test_no_split_tokens_recomputed_after_strip(self):
        m = self.build(
            b"one\n   |two", profile=self.profile_options(size=10, overlap=0, split="|", only=True)
        )
        self.assertEqual([c.content for c in m.chunks], ["one", "two"])
        self.assertEqual([c.tokens for c in m.chunks], [1, 1])

    def test_body_and_location_bound_to_digest(self):
        one = self.build(b"one")
        two = self.build(b"two")
        located = self.build(b"one", vault_path="other/private.txt")
        self.assertEqual(one.chunks[0].id, two.chunks[0].id)
        self.assertNotEqual(one.digest, two.digest)
        self.assertEqual(one.core_sha256, located.core_sha256)
        self.assertNotEqual(one.digest, located.digest)
        self.assertEqual(self.build(b"one").digest, one.digest)

    def test_pdf_page_and_docx_heading_table_order(self):
        pdf = self.build(make_pdf_with_blank_first_page(), filename="private.pdf")
        self.assertEqual([(s.text, s.page) for s in pdf.segments], [("Page two text", 2)])
        docx = self.build(make_docx_with_paragraph_and_table(), filename="private.docx")
        self.assertEqual(
            [s.text for s in docx.segments], ["Project", "A useful paragraph.", "Key | Value"]
        )
        self.assertEqual([s.heading for s in docx.segments], ["Project"] * 3)
        self.assertEqual(docx.parsed_text, "Project\n\nA useful paragraph.\n\nKey | Value")

    def test_invalid_original_sha_and_invalid_document_are_safe(self):
        for sha in ("a" * 64, "A" * 64, "private path"):
            with self.subTest(sha=sha), self.assertRaises(ContentManifestError):
                self.build(expected_source_sha256=sha)
        with self.assertRaises(ContentManifestError) as failure:
            self.build(b"private malformed", filename="private.pdf")
        self.assertNotIn("private", str(failure.exception))

    def test_profile_implementation_mismatch(self):
        for category, key, value in (
            ("packages", "tiktoken", "0.0.0"),
            ("sources", "knowgrain.parsers", "0" * 64),
        ):
            data = json.loads(self.profile.to_canonical_json())
            data["implementation"][category][key] = value
            with self.subTest(category=category), self.assertRaises(ContentManifestError):
                self.build(profile=IndexProfile.from_canonical_json(json.dumps(data)))

    def test_tokenizer_resource_missing_and_corrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            with self.assertRaises(ContentManifestError):
                self.build(tokenizer_cache_dir=cache)
            cache_path(cache).write_bytes(b"private invalid")
            with self.assertRaises(ContentManifestError):
                self.build(tokenizer_cache_dir=cache)

    def test_tokenizer_type_model_encoding_and_method_mismatch(self):
        with self.assertRaises(ContentManifestError):
            self.build(tokenizer=object())
        for instance, name, value in (
            (self.tokenizer, "model_name", "other"),
            (self.tokenizer.tokenizer, "name", "other"),
            (self.tokenizer, "encode", lambda text: [1]),
            (self.tokenizer.tokenizer, "decode", lambda text: "private"),
        ):
            with (
                patch.object(instance, name, value),
                self.subTest(name=name),
                self.assertRaises(ContentManifestError),
            ):
                self.build()

    def test_tokenizer_ranks_special_pattern_mismatch(self):
        encoding = self.tokenizer.tokenizer
        ranks = dict(encoding._mergeable_ranks)
        ranks[next(iter(ranks))] += 1
        for name, value in (
            ("_mergeable_ranks", ranks),
            ("_special_tokens", {}),
            ("_pat_str", "private"),
        ):
            with (
                patch.object(encoding, name, value),
                self.subTest(name=name),
                self.assertRaises(ContentManifestError),
            ):
                self.build()

    def test_immutable_privacy_no_environment_or_files_changed(self):
        environment = dict(os.environ)
        files = sorted(Path(self.directory.name).rglob("*"))
        m = self.build(b"private contents")
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(sorted(Path(self.directory.name).rglob("*")), files)
        for target, name, value in (
            (m, "core_text", "changed"),
            (m.chunks[0], "content", "changed"),
            (m.segments[0], "text", "changed"),
        ):
            with self.assertRaises(FrozenInstanceError):
                setattr(target, name, value)
        self.assertIsInstance(m.chunks, tuple)
        self.assertIsInstance(m.segments, tuple)
        for representation in (repr(m), repr(m.chunks[0]), repr(m.segments[0])):
            for private in ("private", "http://localhost", "entity_types_guidance"):
                self.assertNotIn(private, representation)

    def test_base_exceptions_propagate(self):
        for exception in (KeyboardInterrupt, asyncio.CancelledError):
            with (
                patch("knowgrain.core_content_manifest.parse_document", side_effect=exception),
                self.assertRaises(exception),
            ):
                self.build()
