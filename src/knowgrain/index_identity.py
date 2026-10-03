"""Validated identity values for a LightRAG index generation."""

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowgrain.config import Settings

_VECTOR_MODEL_TOKEN = re.compile(r"kg_[0-9a-f]{24}\Z")


@dataclass(frozen=True)
class CoreIndexIdentity:
    """Immutable LightRAG workspace and optional vector model table identity."""

    workspace: str
    working_dir: Path
    vector_model_name: str | None = None

    def __post_init__(self) -> None:
        workspace = self.workspace
        if not isinstance(workspace, str) or not workspace or not workspace.strip():
            raise ValueError("LightRAG workspace must be a non-empty string")
        if len(workspace) > 128:
            raise ValueError("LightRAG workspace must be at most 128 characters")
        if "/" in workspace or "\\" in workspace or workspace in {".", ".."}:
            raise ValueError("LightRAG workspace must be a single safe path component")
        if any(unicodedata.category(character) == "Cc" for character in workspace):
            raise ValueError("LightRAG workspace must not contain control characters")
        try:
            workspace.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("LightRAG workspace must contain valid Unicode") from exc

        if not isinstance(self.working_dir, Path):
            raise TypeError("LightRAG working_dir must be a pathlib.Path")

        if self.vector_model_name is not None and (
            not isinstance(self.vector_model_name, str)
            or not _VECTOR_MODEL_TOKEN.fullmatch(self.vector_model_name)
        ):
            raise ValueError(
                "vector_model_name must be None or a kg_ token with 24 lowercase hex digits"
            )

    @classmethod
    def from_settings(cls, settings: "Settings") -> "CoreIndexIdentity":
        """Build the legacy Core identity without changing existing vector tables."""
        return cls(
            workspace=settings.lightrag_workspace,
            working_dir=settings.lightrag_working_dir,
            vector_model_name=None,
        )
