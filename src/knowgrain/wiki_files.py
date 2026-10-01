"""Safe filesystem projection for Obsidian-compatible Knowgrain Wiki pages."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any
from uuid import UUID, uuid4

import yaml
from yaml.events import AliasEvent

from knowgrain.vault import VaultPathError, VaultStore


MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_FRONTMATTER_BYTES = 32 * 1024
MAX_SCAN_CANDIDATES = 10_000
MAX_SCAN_ENTRIES = 30_000
MAX_SCAN_BYTES = 128 * 1024 * 1024
MAX_PAGE_LINKS = 10_000
MAX_SCAN_LINKS = 100_000
MAX_YAML_NODES = 2_048
MAX_YAML_DEPTH = 32
MAX_DIFF_BYTES = 64 * 1024
_MANAGED_ROOTS = ("Wiki/Drafts", "Wiki/Pages")
_WIKI_LINK_START = "[["


class _ScanReadLimit(Exception):
    """A single page cannot fit within the remaining scan byte budget."""


@dataclass(frozen=True)
class WikiLink:
    target: str
    anchor: str | None
    label: str | None
    embed: bool
    line: int


@dataclass(frozen=True)
class WikiFile:
    page_id: UUID
    vault_path: str
    title: str
    status: str
    content_sha256: str
    markdown: str
    links: tuple[WikiLink, ...]


@dataclass(frozen=True)
class WikiIssue:
    vault_path: str
    code: str
    detail: str


@dataclass(frozen=True)
class WikiScan:
    pages: tuple[WikiFile, ...]
    issues: tuple[WikiIssue, ...]
    complete: bool = True


class WikiValidationError(ValueError):
    """The supplied or stored Markdown is not a valid managed Wiki page."""


class WikiNotFoundError(LookupError):
    """No uniquely valid managed Wiki page has the requested identity."""


class WikiConflictError(RuntimeError):
    """A page changed, moved ambiguously, or could not be safely replaced."""

    def __init__(
        self,
        message: str,
        *,
        current: WikiFile | None,
        code: str,
        diff: str,
    ) -> None:
        super().__init__(message)
        self.current = current
        self.code = code
        self.diff = diff


class _BoundedSafeLoader(yaml.SafeLoader):
    """SafeLoader with strict resource limits and aliases disabled."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._kg_node_count = 0
        self._kg_compose_depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise WikiValidationError("YAML aliases are not allowed")
        self._kg_node_count += 1
        if self._kg_node_count > MAX_YAML_NODES:
            raise WikiValidationError("YAML frontmatter has too many nodes")
        self._kg_compose_depth += 1
        try:
            if self._kg_compose_depth > MAX_YAML_DEPTH:
                raise WikiValidationError("YAML frontmatter is nested too deeply")
            return super().compose_node(parent, index)
        finally:
            self._kg_compose_depth -= 1

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise WikiValidationError("YAML mapping expected")
        self.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise WikiValidationError("YAML mapping keys must be scalar values") from exc
            if duplicate:
                raise WikiValidationError(f"duplicate YAML mapping key: {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    """Return safe frontmatter data and body, enforcing the serialized bound."""
    if not isinstance(markdown, str):
        raise WikiValidationError("Markdown must be text")
    try:
        encoded = markdown.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WikiValidationError("Markdown contains invalid Unicode") from exc
    if len(encoded) > MAX_PAGE_BYTES:
        raise WikiValidationError("Wiki page exceeds the 2 MiB limit")

    lines = markdown.splitlines(keepends=True)
    if not lines or _without_newline(lines[0]) != "---":
        raise WikiValidationError("Wiki page must start with YAML frontmatter")

    opening_bytes = len(lines[0].encode("utf-8"))
    yaml_lines: list[str] = []
    frontmatter_size = opening_bytes
    for index, line in enumerate(lines[1:], start=1):
        line_bytes = len(line.encode("utf-8"))
        frontmatter_size += line_bytes
        if frontmatter_size > MAX_FRONTMATTER_BYTES:
            raise WikiValidationError("YAML frontmatter exceeds the 32 KiB limit")
        marker = _without_newline(line)
        if marker in {"---", "..."}:
            yaml_text = "".join(yaml_lines)
            try:
                data = yaml.load(yaml_text, Loader=_BoundedSafeLoader)
            except WikiValidationError:
                raise
            except yaml.YAMLError as exc:
                raise WikiValidationError("YAML frontmatter is malformed") from exc
            if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
                raise WikiValidationError("YAML frontmatter must be a string-keyed mapping")
            body = "".join(lines[index + 1 :])
            return data, body
        yaml_lines.append(line)

    raise WikiValidationError("YAML frontmatter is missing its closing delimiter")


def _without_newline(line: str) -> str:
    if line.endswith("\n"):
        line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
    return line


def _yaml_data(markdown: str) -> tuple[dict[str, Any], str]:
    data, body = _frontmatter(markdown)
    raw_id = data.get("kg_id")
    if not isinstance(raw_id, str):
        raise WikiValidationError("frontmatter kg_id must be a UUID string")
    try:
        page_id = UUID(raw_id)
    except (ValueError, AttributeError) as exc:
        raise WikiValidationError("frontmatter kg_id must be a UUID string") from exc
    if data.get("kg_kind") != "wiki":
        raise WikiValidationError("frontmatter kg_kind must be 'wiki'")
    status = data.get("kg_status")
    if not isinstance(status, str) or status not in {"draft", "reviewed"}:
        raise WikiValidationError("frontmatter kg_status must be 'draft' or 'reviewed'")
    # Keep validated identity/status in a canonical representation for callers.
    data["kg_id"] = page_id
    return data, body


def _body_without_frontmatter(markdown: str) -> str:
    """Skip a syntactically delimited frontmatter block without parsing YAML."""
    lines = markdown.splitlines(keepends=True)
    if not lines or _without_newline(lines[0]) != "---":
        return markdown
    for index, line in enumerate(lines[1:], start=1):
        if _without_newline(line) in {"---", "..."}:
            return "".join(lines[index + 1 :])
    # An unclosed frontmatter block has no trustworthy Markdown body.
    return ""


def _mask_range(chars: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if chars[index] not in "\r\n":
            chars[index] = " "


def _line_end(text: str, start: int) -> int:
    newline = text.find("\n", start)
    return len(text) if newline < 0 else newline + 1


def _mask_markdown_literals(text: str) -> list[str]:
    """Mask fenced code, inline code, and comments in source order."""
    chars = list(text)
    position = 0
    fence_char: str | None = None
    fence_length = 0
    while position < len(text):
        at_line_start = position == 0 or text[position - 1] == "\n"
        line_end = _line_end(text, position) if at_line_start else -1
        if fence_char is not None and at_line_start:
            line_body = _without_newline(text[position:line_end])
            _mask_range(chars, position, line_end)
            closer = re.match(r"^ {0,3}(`+|~+)[ \t]*$", line_body)
            if closer and closer.group(1)[0] == fence_char and len(closer.group(1)) >= fence_length:
                fence_char = None
                fence_length = 0
            position = line_end
            continue

        if fence_char is None and at_line_start:
            line_body = _without_newline(text[position:line_end])
            opener = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line_body)
            if opener:
                run, info = opener.groups()
                if run[0] != "`" or "`" not in info:
                    fence_char = run[0]
                    fence_length = len(run)
                    _mask_range(chars, position, line_end)
                    position = line_end
                    continue

        if text.startswith("<!--", position):
            comment_end = text.find("-->", position + 4)
            end = len(text) if comment_end < 0 else comment_end + 3
            _mask_range(chars, position, end)
            position = end
            continue

        if text[position] != "`":
            position += 1
            continue
        end_run = position + 1
        while end_run < len(text) and text[end_run] == "`":
            end_run += 1
        if _is_escaped(text, position):
            position = end_run
            continue
        run_length = end_run - position
        search = end_run
        closing: tuple[int, int] | None = None
        while search < len(text):
            candidate = text.find("`", search)
            if candidate < 0:
                break
            candidate_end = candidate + 1
            while candidate_end < len(text) and text[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - candidate == run_length:
                closing = (candidate, candidate_end)
                break
            search = candidate_end
        if closing is None:
            position = end_run
            continue
        _mask_range(chars, position, closing[1])
        position = closing[1]
    return chars


def _is_escaped(text: str, index: int) -> bool:
    slash_count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        slash_count += 1
        cursor -= 1
    return slash_count % 2 == 1


def _find_unescaped_close(text: str, start: int) -> int | None:
    cursor = start
    while cursor + 1 < len(text):
        close = text.find("]]", cursor)
        if close < 0:
            return None
        if not _is_escaped(text, close) and not _is_escaped(text, close + 1):
            return close
        cursor = close + 1
    return None


def _split_unescaped(value: str, delimiter: str) -> tuple[str, str | None]:
    for index, char in enumerate(value):
        if char == delimiter and not _is_escaped(value, index):
            return value[:index], value[index + 1 :]
    return value, None


def _parse_link_value(value: str, embed: bool, line: int) -> WikiLink | None:
    raw_target, label = _split_unescaped(value, "|")
    raw_target = raw_target.strip()
    label = label.strip() if label is not None else None
    target, anchor = _split_unescaped(raw_target, "#")
    target = target.strip()
    anchor = anchor.strip() if anchor is not None else None
    if not target and not anchor:
        return None
    return WikiLink(target=target, anchor=anchor, label=label, embed=embed, line=line)


def parse_wikilinks(markdown: str) -> tuple[WikiLink, ...]:
    """Extract Obsidian links while excluding frontmatter and literal regions."""
    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")
    body = _body_without_frontmatter(markdown)
    skipped_lines = markdown[: len(markdown) - len(body)].count("\n") if body else 0
    chars = _mask_markdown_literals(body)
    visible = "".join(chars)
    links: list[WikiLink] = []
    position = 0
    line_number = skipped_lines + 1
    while position < len(visible):
        start = visible.find(_WIKI_LINK_START, position)
        if start < 0:
            break
        line_number += visible.count("\n", position, start)
        if _is_escaped(visible, start):
            position = start + 2
            continue
        close = _find_unescaped_close(visible, start + 2)
        if close is None:
            break
        embed = start > 0 and visible[start - 1] == "!" and not _is_escaped(visible, start - 1)
        value = visible[start + 2 : close]
        link = _parse_link_value(value, embed, line_number)
        if link is not None:
            if len(links) >= MAX_PAGE_LINKS:
                raise WikiValidationError("Wiki page exceeds the 10,000 wikilink limit")
            links.append(link)
        line_number += visible.count("\n", start, close + 2)
        position = close + 2
    return tuple(links)


def _first_heading(body: str) -> str | None:
    visible = _mask_markdown_literals(body)
    for line in "".join(visible).splitlines():
        match = re.match(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", line)
        if match:
            title = match.group(1).strip()
            if title:
                return title
    return None


def _validate_managed_path(vault_path: str) -> str:
    if not isinstance(vault_path, str) or "\\" in vault_path:
        raise WikiValidationError("Wiki path must be Vault-relative POSIX text")
    path = PurePosixPath(vault_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WikiValidationError("Wiki path must be a safe relative path")
    if path.suffix.casefold() != ".md":
        raise WikiValidationError("Wiki pages must use the .md extension")
    if not any(path.parts[:2] == tuple(root.split("/")) for root in _MANAGED_ROOTS):
        raise WikiValidationError("Wiki page must be beneath Wiki/Drafts or Wiki/Pages")
    return path.as_posix()


def parse_wiki(markdown: str, vault_path: str) -> WikiFile:
    """Validate one Markdown page and return its rebuildable projection."""
    safe_path = _validate_managed_path(vault_path)
    try:
        encoded = markdown.encode("utf-8", errors="strict")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise WikiValidationError("Markdown must be valid UTF-8 text") from exc
    if b"\0" in encoded:
        raise WikiValidationError("Markdown cannot contain NUL characters")
    if len(encoded) > MAX_PAGE_BYTES:
        raise WikiValidationError("Wiki page exceeds the 2 MiB limit")
    data, body = _yaml_data(markdown)
    page_id: UUID = data["kg_id"]
    title_value = data.get("title")
    if isinstance(title_value, str) and "\0" in title_value:
        raise WikiValidationError("frontmatter title cannot contain NUL characters")
    if isinstance(title_value, str) and title_value.strip():
        title = title_value
    else:
        title = _first_heading(body) or PurePosixPath(safe_path).stem
    links = parse_wikilinks(markdown)
    if any(
        "\0" in value
        for link in links
        for value in (link.target, link.anchor, link.label)
        if value is not None
    ):
        raise WikiValidationError("wikilink fields cannot contain NUL characters")
    return WikiFile(
        page_id=page_id,
        vault_path=safe_path,
        title=title,
        status=data["kg_status"],
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        markdown=markdown,
        links=links,
    )


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _bounded_diff(current: str, submitted: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            submitted.splitlines(keepends=True),
            fromfile="current",
            tofile="submitted",
        )
    )
    encoded = diff.encode("utf-8")
    if len(encoded) <= MAX_DIFF_BYTES:
        return diff
    suffix = b"\n[diff truncated]\n"
    truncated = encoded[: MAX_DIFF_BYTES - len(suffix)].decode("utf-8", errors="ignore")
    return truncated + suffix.decode("ascii")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class WikiFileStore:
    """Synchronous, bounded file operations for managed Wiki pages."""

    def __init__(self, vault: VaultStore) -> None:
        self.vault = vault

    def scan(self) -> WikiScan:
        pages: list[WikiFile] = []
        issues: list[WikiIssue] = []
        candidates = 0
        scanned_entries = 0
        scanned_bytes = 0
        scanned_links = 0
        incomplete = False
        limit_reached = False
        pending: list[tuple[Path, str]] = []

        for root in _MANAGED_ROOTS:
            try:
                directory = self.vault.resolve(root)
                metadata = os.lstat(directory)
            except FileNotFoundError:
                continue
            except (OSError, VaultPathError) as exc:
                issues.append(WikiIssue(root, "unsafe_path", _safe_error(exc)))
                incomplete = True
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                issues.append(WikiIssue(root, "unsafe_path", "Wiki root is not a directory"))
                incomplete = True
                continue
            pending.append((directory, root))

        while pending and not limit_reached:
            directory, relative_directory = pending.pop()
            records: list[tuple[str, os.stat_result | None, OSError | None]] = []
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        scanned_entries += 1
                        if scanned_entries > MAX_SCAN_ENTRIES:
                            issues.append(
                                WikiIssue(
                                    f"{relative_directory}/{entry.name}",
                                    "scan_limit",
                                    "scan stopped after 30,000 Vault entries",
                                )
                            )
                            incomplete = True
                            limit_reached = True
                            break
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            records.append((entry.name, None, exc))
                        else:
                            records.append((entry.name, metadata, None))
            except OSError as exc:
                issues.append(WikiIssue(relative_directory, "scan_error", _safe_error(exc)))
                incomplete = True
                continue
            if limit_reached:
                break
            records.sort(key=lambda record: record[0].casefold())
            subdirectories: list[tuple[Path, str]] = []
            for name, metadata, stat_error in records:
                relative_path = f"{relative_directory}/{name}"
                if stat_error is not None:
                    issues.append(WikiIssue(relative_path, "scan_error", _safe_error(stat_error)))
                    incomplete = True
                    if name.casefold().endswith(".md"):
                        candidates += 1
                    if candidates > MAX_SCAN_CANDIDATES:
                        issues.append(
                            WikiIssue(
                                relative_path,
                                "scan_limit",
                                "scan stopped after 10,000 Markdown candidates",
                            )
                        )
                        incomplete = True
                        limit_reached = True
                        break
                    continue
                assert metadata is not None
                is_symlink = stat.S_ISLNK(metadata.st_mode)
                is_directory = stat.S_ISDIR(metadata.st_mode)
                is_markdown_name = name.casefold().endswith(".md")
                if is_markdown_name and (is_symlink or not is_directory):
                    candidates += 1
                    if candidates > MAX_SCAN_CANDIDATES:
                        issues.append(
                            WikiIssue(
                                relative_path,
                                "scan_limit",
                                "scan stopped after 10,000 Markdown candidates",
                            )
                        )
                        incomplete = True
                        limit_reached = True
                        break
                if is_symlink:
                    issues.append(
                        WikiIssue(relative_path, "unsafe_path", "symlink entries are not managed")
                    )
                    if not is_markdown_name:
                        incomplete = True
                    continue
                if is_directory:
                    try:
                        safe_directory = self.vault.resolve(relative_path)
                    except VaultPathError as exc:
                        issues.append(WikiIssue(relative_path, "unsafe_path", _safe_error(exc)))
                        incomplete = True
                    else:
                        subdirectories.append((safe_directory, relative_path))
                    continue
                if not is_markdown_name:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    issues.append(
                        WikiIssue(relative_path, "unsafe_path", "Wiki page is not a regular file")
                    )
                    continue
                remaining_bytes = MAX_SCAN_BYTES - scanned_bytes
                if metadata.st_size > MAX_PAGE_BYTES:
                    issues.append(
                        WikiIssue(relative_path, "oversized", "Wiki page exceeds the 2 MiB limit")
                    )
                    continue
                if metadata.st_size > remaining_bytes:
                    issues.append(
                        WikiIssue(
                            relative_path,
                            "scan_limit",
                            "scan stopped after 128 MiB of Markdown file reads",
                        )
                    )
                    incomplete = True
                    limit_reached = True
                    break
                try:
                    safe_path = _validate_managed_path(relative_path)
                    self.vault.resolve(safe_path)
                    content = self._read_regular(
                        relative_path,
                        max_bytes=remaining_bytes,
                    )
                except _ScanReadLimit:
                    issues.append(
                        WikiIssue(
                            relative_path,
                            "scan_limit",
                            "scan stopped after 128 MiB of Markdown file reads",
                        )
                    )
                    incomplete = True
                    limit_reached = True
                    break
                except VaultPathError as exc:
                    issues.append(WikiIssue(relative_path, "unsafe_path", _safe_error(exc)))
                    incomplete = True
                    continue
                except WikiValidationError as exc:
                    if "path" in str(exc).casefold():
                        issues.append(WikiIssue(relative_path, "unsafe_path", str(exc)))
                    else:
                        issues.append(WikiIssue(relative_path, _issue_code(exc), str(exc)))
                    continue
                except OSError as exc:
                    issues.append(WikiIssue(relative_path, "scan_error", _safe_error(exc)))
                    incomplete = True
                    continue
                scanned_bytes += len(content)
                try:
                    markdown = content.decode("utf-8", errors="strict")
                    page = parse_wiki(markdown, relative_path)
                except WikiValidationError as exc:
                    issues.append(WikiIssue(relative_path, _issue_code(exc), str(exc)))
                except UnicodeDecodeError:
                    issues.append(
                        WikiIssue(relative_path, "invalid_utf8", "Wiki page is not valid UTF-8")
                    )
                else:
                    scanned_links += len(page.links)
                    if scanned_links > MAX_SCAN_LINKS:
                        issues.append(
                            WikiIssue(
                                relative_path,
                                "scan_limit",
                                "scan stopped after 100,000 Wiki links",
                            )
                        )
                        incomplete = True
                        limit_reached = True
                        break
                    pages.append(page)
            if not limit_reached:
                pending.extend(reversed(subdirectories))

        duplicate_ids: dict[UUID, list[WikiFile]] = {}
        for page in pages:
            duplicate_ids.setdefault(page.page_id, []).append(page)
        duplicate_paths: set[str] = set()
        for page_id, matches in duplicate_ids.items():
            if len(matches) > 1:
                paths = ", ".join(sorted(page.vault_path for page in matches))
                for page in matches:
                    duplicate_paths.add(page.vault_path)
                    issues.append(
                        WikiIssue(
                            page.vault_path,
                            "duplicate_id",
                            f"page id {page_id} is also used by: {paths}",
                        )
                    )
        valid_pages = [page for page in pages if page.vault_path not in duplicate_paths]
        if incomplete:
            # A partial traversal cannot prove that a page ID is unique, so do
            # not expose a partial projection as an authoritative scan result.
            valid_pages = []
        valid_pages.sort(
            key=lambda page: (page.vault_path.casefold(), page.vault_path, str(page.page_id))
        )
        issues.sort(
            key=lambda issue: (
                issue.vault_path.casefold(), issue.vault_path, issue.code, issue.detail
            )
        )
        return WikiScan(tuple(valid_pages), tuple(issues), complete=not incomplete)

    def create(self, title: str, body: str) -> WikiFile:
        if not isinstance(title, str) or not isinstance(body, str):
            raise WikiValidationError("title and body must be text")
        if not title.strip():
            raise WikiValidationError("title must not be empty")
        if "\0" in title:
            raise WikiValidationError("title cannot contain NUL characters")
        page_id = uuid4()
        markdown = (
            "---\n"
            f"kg_id: {page_id}\n"
            "kg_kind: wiki\n"
            "kg_status: draft\n"
            f"title: {json.dumps(title, ensure_ascii=False)}\n"
            "---\n\n"
            f"{body}"
        )
        page = parse_wiki(markdown, f"Wiki/Drafts/{page_id}.md")
        relative_path = page.vault_path
        destination = self._safe_path(relative_path)
        self._ensure_parent(relative_path)
        self._publish_exclusive(destination, markdown.encode("utf-8"))
        return page

    def save(self, page_id: UUID, markdown: str, expected_sha256: str) -> WikiFile:
        try:
            page_id = UUID(str(page_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WikiNotFoundError("Wiki page not found") from exc

        scan = self.scan()
        if not scan.complete:
            has_limit_issue = any(issue.code == "scan_limit" for issue in scan.issues)
            code = "scan_limit" if has_limit_issue else "scan_incomplete"
            raise WikiConflictError(
                "Wiki scan is incomplete; page identities cannot be resolved safely",
                current=None,
                code=code,
                diff="",
            )
        page = next((candidate for candidate in scan.pages if candidate.page_id == page_id), None)
        if page is None:
            if any(
                issue.code == "duplicate_id" and f"page id {page_id} " in issue.detail
                for issue in scan.issues
            ):
                raise WikiConflictError(
                    "Wiki page identity is duplicated",
                    current=None,
                    code="duplicate_id",
                    diff="",
                )
            raise WikiNotFoundError(f"Wiki page not found: {page_id}")

        submitted = parse_wiki(markdown, page.vault_path)
        if submitted.page_id != page.page_id:
            raise WikiValidationError("page identity kg_id cannot be changed")
        if not isinstance(expected_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ):
            raise WikiValidationError("expected_sha256 must be a lowercase SHA-256 hash")
        if expected_sha256 != page.content_sha256:
            raise WikiConflictError(
                "Wiki page content changed since it was read",
                current=page,
                code="stale_content",
                diff=_bounded_diff(page.markdown, markdown),
            )
        if submitted.status != page.status:
            raise WikiValidationError("page status kg_status cannot be changed by save")

        destination = self._safe_path(page.vault_path)
        try:
            temporary_path = self._write_sibling(destination, markdown.encode("utf-8"))
        except (OSError, VaultPathError) as exc:
            raise WikiConflictError(
                "Wiki page could not be prepared for replacement",
                current=page,
                code="write_failed",
                diff=_bounded_diff(page.markdown, markdown),
            ) from exc

        try:
            current_bytes = self._read_regular(page.vault_path)
        except (FileNotFoundError, OSError, VaultPathError, WikiValidationError) as exc:
            self._unlink_temporary(temporary_path)
            raise WikiConflictError(
                "Wiki page changed or became unavailable before replacement",
                current=None,
                code="page_changed",
                diff="",
            ) from exc

        current_hash = _hash_bytes(current_bytes)
        try:
            current_markdown = current_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            self._unlink_temporary(temporary_path)
            raise WikiConflictError(
                "Wiki page changed to invalid UTF-8 before replacement",
                current=None,
                code="stale_content",
                diff=_bounded_diff(current_bytes.decode("utf-8", errors="replace"), markdown),
            ) from exc
        current_page: WikiFile | None
        try:
            current_page = parse_wiki(current_markdown, page.vault_path)
        except WikiValidationError:
            current_page = None
        if current_hash != expected_sha256:
            self._unlink_temporary(temporary_path)
            raise WikiConflictError(
                "Wiki page content changed since it was read",
                current=current_page,
                code="stale_content",
                diff=_bounded_diff(current_markdown, markdown),
            )
        if (
            current_page is None
            or current_page.page_id != page_id
            or current_page.status != page.status
        ):
            self._unlink_temporary(temporary_path)
            raise WikiConflictError(
                "Wiki page identity or status changed since it was read",
                current=current_page,
                code="identity_changed",
                diff=_bounded_diff(current_markdown, markdown),
            )

        try:
            self._publish_recovery(page_id, current_hash, current_bytes)
            # A second check closes the time spent publishing the snapshot. An
            # editor outside the app can still race this final check and rename.
            final_bytes = self._read_regular(page.vault_path)
            if _hash_bytes(final_bytes) != expected_sha256:
                final_markdown = final_bytes.decode("utf-8", errors="replace")
                final_page = None
                try:
                    final_page = parse_wiki(final_markdown, page.vault_path)
                except WikiValidationError:
                    pass
                raise WikiConflictError(
                    "Wiki page changed during replacement preparation",
                    current=final_page,
                    code="stale_content",
                    diff=_bounded_diff(final_markdown, markdown),
                )
            self._safe_path(page.vault_path)
            os.replace(temporary_path, destination)
            _fsync_directory(destination.parent)
        except WikiConflictError:
            self._unlink_temporary(temporary_path)
            raise
        except (OSError, VaultPathError) as exc:
            self._unlink_temporary(temporary_path)
            raise WikiConflictError(
                "Wiki page could not be safely replaced",
                current=current_page,
                code="write_failed",
                diff=_bounded_diff(current_markdown, markdown),
            ) from exc
        except WikiValidationError as exc:
            self._unlink_temporary(temporary_path)
            raise WikiConflictError(
                "Wiki page changed beyond supported bounds during replacement preparation",
                current=None,
                code="page_changed",
                diff="",
            ) from exc

        return parse_wiki(markdown, page.vault_path)

    def _safe_path(self, relative_path: str) -> Path:
        checked = _validate_managed_path(relative_path)
        return self.vault.resolve(checked)

    def _ensure_parent(self, relative_path: str) -> None:
        parent = PurePosixPath(relative_path).parent
        parts: list[str] = []
        for part in parent.parts:
            parts.append(part)
            relative = "/".join(parts)
            path = self.vault.resolve(relative)
            try:
                path.mkdir(exist_ok=True)
            except FileNotFoundError:
                path.mkdir(parents=True, exist_ok=True)
            self.vault.resolve(relative)
            metadata = os.lstat(path)
            if not stat.S_ISDIR(metadata.st_mode):
                raise VaultPathError("Wiki parent path is not a directory")

    def _read_regular(
        self,
        relative_path: str,
        *,
        max_bytes: int = MAX_PAGE_BYTES,
    ) -> bytes:
        path = self._safe_path(relative_path)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise VaultPathError("Wiki page must be a regular file")
            if metadata.st_size > MAX_PAGE_BYTES:
                raise WikiValidationError("Wiki page exceeds the 2 MiB limit")
            if metadata.st_size > max_bytes:
                raise _ScanReadLimit
            read_limit = min(MAX_PAGE_BYTES, max_bytes)
            with os.fdopen(descriptor, "rb", closefd=False) as file:
                content = file.read(read_limit)
            latest_size = os.fstat(descriptor).st_size
            if latest_size > MAX_PAGE_BYTES:
                raise WikiValidationError("Wiki page exceeds the 2 MiB limit")
            if latest_size > max_bytes:
                raise _ScanReadLimit
            return content
        finally:
            os.close(descriptor)

    def _write_sibling(self, destination: Path, content: bytes) -> Path:
        self._ensure_parent(destination.relative_to(self.vault.root).as_posix())
        self._safe_path(destination.relative_to(self.vault.root).as_posix())
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".knowgrain-wiki-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            return temporary_path
        except Exception:
            if temporary_path is not None:
                self._unlink_temporary(temporary_path)
            raise

    def _publish_exclusive(self, destination: Path, content: bytes) -> None:
        temporary_path = self._write_sibling(destination, content)
        try:
            try:
                os.link(temporary_path, destination)
            except FileExistsError as exc:
                raise WikiConflictError(
                    "A Wiki page already exists at the generated path",
                    current=None,
                    code="already_exists",
                    diff="",
                ) from exc
            _fsync_directory(destination.parent)
        finally:
            self._unlink_temporary(temporary_path)

    def _publish_recovery(self, page_id: UUID, content_hash: str, content: bytes) -> None:
        relative = f".knowgrain/wiki-recovery/{page_id}/{content_hash}.md"
        destination = self.vault.resolve(relative)
        self._ensure_parent(relative)
        try:
            existing = self._read_any_regular(destination)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != content:
                raise WikiConflictError(
                    "Wiki recovery snapshot path already contains different bytes",
                    current=None,
                    code="recovery_conflict",
                    diff="",
                )
            return
        self._publish_exclusive_bytes(destination, content)

    def _publish_exclusive_bytes(self, destination: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".knowgrain-recovery-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                existing = self._read_any_regular(destination)
                if existing != content:
                    raise WikiConflictError(
                        "Wiki recovery snapshot path already contains different bytes",
                        current=None,
                        code="recovery_conflict",
                        diff="",
                    )
            _fsync_directory(destination.parent)
        finally:
            if temporary_path is not None:
                self._unlink_temporary(temporary_path)

    def _read_any_regular(self, path: Path) -> bytes:
        relative = path.relative_to(self.vault.root).as_posix()
        checked = self.vault.resolve(relative)
        descriptor = os.open(
            checked,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise VaultPathError("Wiki recovery snapshot must be a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as file:
                return file.read(MAX_PAGE_BYTES + 1)
        finally:
            os.close(descriptor)

    @staticmethod
    def _unlink_temporary(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, VaultPathError):
        return str(exc)
    return "filesystem entry could not be safely accessed"


def _issue_code(exc: WikiValidationError) -> str:
    message = str(exc).casefold()
    if "wikilink limit" in message:
        return "oversized_links"
    if "2 mib" in message:
        return "oversized"
    if "32 kib" in message:
        return "oversized_frontmatter"
    if "utf-8" in message or "unicode" in message:
        return "invalid_utf8"
    return "invalid_frontmatter"


__all__ = [
    "WikiConflictError",
    "WikiFile",
    "WikiFileStore",
    "WikiIssue",
    "WikiLink",
    "WikiNotFoundError",
    "WikiScan",
    "WikiValidationError",
    "parse_wiki",
    "parse_wikilinks",
]
