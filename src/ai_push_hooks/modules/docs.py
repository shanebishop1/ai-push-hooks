from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
from pathlib import PurePosixPath
from typing import Any

from ..types import CollectorResult, RuntimeContext
from ..executors.exec import collect_commit_messages_for_ranges, git, path_matches, run_command

DOC_INCLUDE_PATTERNS = ("README.md", "docs/**/*.md")
DOC_IGNORE_PATTERNS = ("docs/archive/**",)
DOC_CONTEXT_LINES = 2
DOC_MAX_BYTES = 64 * 1024
DOC_CONTEXT_BUDGET = 32000
DOC_FALLBACK_FILE_LIMIT = 8


def _path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(path_matches(path, pattern) for pattern in patterns)


def _is_safe_doc_file(repo_root: pathlib.Path, candidate: pathlib.Path) -> bool:
    """Return whether candidate is a contained, non-link regular file."""
    try:
        relative = candidate.relative_to(repo_root)
        current = repo_root
        for part in relative.parts:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
        candidate_stat = candidate.lstat()
        if not stat.S_ISREG(candidate_stat.st_mode):
            return False
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repo_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _expand_doc_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    repo_root = repo_root.resolve(strict=True)
    files: list[pathlib.Path] = []
    for candidate in repo_root.rglob("*.md"):
        relative = candidate.relative_to(repo_root).as_posix()
        if not _path_matches(relative, DOC_INCLUDE_PATTERNS):
            continue
        if _path_matches(relative, DOC_IGNORE_PATTERNS):
            continue
        if not _is_safe_doc_file(repo_root, candidate):
            continue
        files.append(candidate)
    return sorted(files)


def _deterministic_seed_queries(diff_text: str, changed_files: list[str]) -> list[str]:
    stopwords = {
        "const",
        "return",
        "value",
        "false",
        "true",
        "string",
        "number",
        "object",
        "class",
        "function",
        "public",
        "private",
        "static",
        "async",
        "await",
        "import",
        "export",
        "from",
        "default",
        "docs",
        "readme",
    }
    seeds: list[str] = []
    for changed in changed_files:
        pure = PurePosixPath(changed)
        if len(pure.stem) >= 4:
            seeds.append(pure.stem)
        for segment in pure.parts:
            if len(segment) >= 4 and segment not in {"docs", "src", "tests"}:
                seeds.append(segment)
    seeds.extend(re.findall(r"\b[A-Za-z][A-Za-z0-9_.-]{3,}\b", diff_text))
    deduped: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        clean = seed.strip()
        if clean.lower() in stopwords or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped[:20]


def _read_bounded_text(path: pathlib.Path, max_bytes: int | None = None) -> str:
    if max_bytes is None:
        max_bytes = DOC_MAX_BYTES
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            return ""
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _append_context_chunk(chunks: list[str], chunk: str, budget: int) -> bool:
    current_size = sum(len(item) for item in chunks) + max(0, len(chunks) - 1)
    remaining = budget - current_size
    if remaining <= 0:
        return False
    truncated = len(chunk) > remaining
    if truncated:
        if chunks:
            return False
        chunk = chunk[:remaining]
    chunks.append(chunk)
    return not truncated


def _search_docs_context(repo_root: pathlib.Path, doc_files: list[pathlib.Path], queries: list[str]) -> str:
    if not doc_files:
        return ""
    if shutil.which("rg") is None or not queries:
        snippets: list[str] = []
        for path in doc_files[:DOC_FALLBACK_FILE_LIMIT]:
            relative = path.relative_to(repo_root).as_posix()
            content = _read_bounded_text(path)
            block = f"--- {relative} ---\n{content}"
            current_size = len("\n\n".join(snippets))
            remaining = DOC_CONTEXT_BUDGET - current_size
            if len(block) > remaining:
                if not snippets and remaining > 0:
                    snippets.append(block[:remaining])
                break
            snippets.append(block)
        return "\n\n".join(snippets)

    files = [path.relative_to(repo_root).as_posix() for path in doc_files]
    allowed_files = set(files)
    chunks: list[str] = []
    seen: set[tuple[str, int]] = set()
    for query in queries:
        completed = run_command(
            [
                "rg",
                "--json",
                "--fixed-strings",
                "--with-filename",
                "--color=never",
                "--context",
                str(DOC_CONTEXT_LINES),
                "--max-filesize",
                str(DOC_MAX_BYTES),
                "--",
                query,
                *files,
            ],
            cwd=repo_root,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            continue
        for line in completed.stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") not in {"match", "context"}:
                continue
            data = message.get("data")
            if not isinstance(data, dict):
                continue
            path_data = data.get("path")
            lines_data = data.get("lines")
            file_name = path_data.get("text") if isinstance(path_data, dict) else None
            line_number = data.get("line_number")
            content = lines_data.get("text") if isinstance(lines_data, dict) else None
            if (
                not isinstance(file_name, str)
                or file_name not in allowed_files
                or not isinstance(line_number, int)
                or not isinstance(content, str)
            ):
                continue
            key = (file_name, line_number)
            if key in seen:
                continue
            seen.add(key)
            clean_content = content.rstrip("\r\n")
            chunk = f"{file_name}:{line_number}: {clean_content}"
            if not _append_context_chunk(chunks, chunk, DOC_CONTEXT_BUDGET):
                return "\n".join(chunks)
    return "\n".join(chunks)


def collect_docs_context(context: RuntimeContext, _state: Any) -> CollectorResult:
    ranges = context.cache.get("ranges", [])
    changed_files = context.cache.get("changed_files", [])
    diff_text = context.cache.get("diff_text", "")
    doc_files = _expand_doc_files(context.repo_root)
    docs_context = _search_docs_context(
        context.repo_root,
        doc_files,
        _deterministic_seed_queries(diff_text, changed_files),
    )
    recent_commits = git(
        context.repo_root,
        ["log", "--oneline", "-n", "20", "--", "README.md", "docs"],
        check=False,
    )
    commits = collect_commit_messages_for_ranges(context.repo_root, ranges) if ranges else []
    commit_lines = []
    for commit in commits:
        commit_lines.append(f"--- {commit['hash']}")
        commit_lines.append(f"subject: {commit['subject']}")
        if commit["body"]:
            commit_lines.append("body:")
            commit_lines.append(commit["body"])
        commit_lines.append("")
    return CollectorResult(
        artifacts={
            "changed-files.txt": "\n".join(changed_files) + ("\n" if changed_files else ""),
            "push.diff": diff_text + ("\n" if diff_text and not diff_text.endswith("\n") else ""),
            "docs-inventory.txt": "\n".join(path.relative_to(context.repo_root).as_posix() for path in doc_files)
            + ("\n" if doc_files else ""),
            "docs-context.txt": docs_context + ("\n" if docs_context else ""),
            "recent-commits.txt": recent_commits + ("\n" if recent_commits and not recent_commits.endswith("\n") else ""),
            "commits.txt": "\n".join(commit_lines).strip() + ("\n" if commit_lines else ""),
        }
    )
