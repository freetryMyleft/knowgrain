from io import BytesIO
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from knowgrain.tokenizer_cache import cache_path, prepare_tokenizer_cache, require_tokenizer_cache


class TokenizerCacheTests(unittest.TestCase):
    def test_startup_missing_or_corrupt_cache_never_downloads(self):
        with TemporaryDirectory() as temporary, patch("knowgrain.tokenizer_cache.urlopen") as download:
            root = Path(temporary)
            with self.assertRaises(RuntimeError):
                require_tokenizer_cache(root)
            cache_path(root).write_bytes(b"corrupt")
            with self.assertRaises(RuntimeError):
                require_tokenizer_cache(root)
            download.assert_not_called()

    def test_verified_download_is_reused_offline(self):
        content = b"synthetic tokenizer fixture"
        with (
            TemporaryDirectory() as temporary,
            patch("knowgrain.tokenizer_cache.TOKENIZER_SHA256", hashlib.sha256(content).hexdigest()),
            patch("knowgrain.tokenizer_cache.urlopen", return_value=BytesIO(content)) as download,
            patch.dict(os.environ),
        ):
            root = Path(temporary)
            path = prepare_tokenizer_cache(root)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(prepare_tokenizer_cache(root), path)
            require_tokenizer_cache(root)
            self.assertEqual(os.environ["TIKTOKEN_CACHE_DIR"], str(root.resolve()))
            download.assert_called_once()
            self.assertEqual([entry.resolve() for entry in root.iterdir()], [path])

    def test_invalid_download_does_not_replace_existing_cache(self):
        with TemporaryDirectory() as temporary, patch("knowgrain.tokenizer_cache.urlopen", return_value=BytesIO(b"wrong")):
            root = Path(temporary)
            path = cache_path(root)
            path.write_bytes(b"previous cache")
            with self.assertRaises(ValueError):
                prepare_tokenizer_cache(root)
            self.assertEqual(path.read_bytes(), b"previous cache")
