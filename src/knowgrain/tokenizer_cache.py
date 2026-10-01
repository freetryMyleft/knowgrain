"""Explicit preparation of the fixed tokenizer resource used by LightRAG 1.5.7."""

import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen

TOKENIZER_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
TOKENIZER_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
MAX_RESOURCE_BYTES = 8 * 1024 * 1024


def cache_path(directory: Path) -> Path:
    # tiktoken uses the URL's SHA-1 as a cache filename, SHA-256 for content integrity.
    return directory.expanduser().resolve() / hashlib.sha1(TOKENIZER_URL.encode()).hexdigest()


def _valid_resource(path: Path) -> bool:
    try:
        with path.open("rb") as resource:
            content = resource.read(MAX_RESOURCE_BYTES + 1)
        return len(content) <= MAX_RESOURCE_BYTES and hashlib.sha256(content).hexdigest() == TOKENIZER_SHA256
    except OSError:
        return False


def require_tokenizer_cache(directory: Path) -> None:
    """Fail without network I/O when the installation resource is absent or invalid."""
    path = cache_path(directory)
    if not _valid_resource(path):
        raise RuntimeError("Tokenizer cache is missing or invalid; run make tokenizer before starting")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(path.parent)


def prepare_tokenizer_cache(directory: Path) -> Path:
    path = cache_path(directory)
    if _valid_resource(path):
        return path
    with urlopen(TOKENIZER_URL, timeout=30) as response:
        content = response.read(MAX_RESOURCE_BYTES + 1)
    if len(content) > MAX_RESOURCE_BYTES or hashlib.sha256(content).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("Tokenizer download failed size or SHA-256 verification")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tokenizer-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


def main() -> None:
    from knowgrain.config import Settings

    settings = Settings()
    for attempt in range(3):
        try:
            path = prepare_tokenizer_cache(settings.tokenizer_cache_dir)
        except Exception as exc:
            if attempt == 2:
                raise SystemExit(f"Tokenizer preparation failed ({type(exc).__name__}); retry make tokenizer") from None
        else:
            print(f"Tokenizer cache verified: {path}")
            return
