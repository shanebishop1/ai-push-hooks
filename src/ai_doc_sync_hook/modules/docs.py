from __future__ import annotations

import pathlib
import re
import shutil
from pathlib import PurePosixPath
from typing import Any

from ..types import CollectorResult, RuntimeContext
from ..executors.exec import collect_commit_messages_for_ranges, git, run_command

DOC_INCLUDE_PATTERNS = ("README.md", "docs/**/*.md")
DOC_IGNORE_PATTERNS = ("docs/archive/**",)


def _path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    pure = PurePosixPath(path)
    return any(pure.match(pattern) for pattern in patterns)


def _expand_doc_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for candidate in repo_root.rglob("*.md"):
        relative = candidate.relative_to(repo_root).as_posix()
        if not _path_matches(relative, DOC_INCLUDE_PATTERNS):
            continue
        if _path_matches(relative, DOC_IGNORE_PATTERNS):
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


def _parse_rg_line(line: str) -> tuple[str, int, str] | None:
    match = re.match(r"^(.*?):(\d+):(.*)$", line)
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)


def _search_docs_context(repo_root: pathlib.Path, doc_files: list[pathlib.Path], queries: list[str]) -> str:
    if not doc_files:
        return ""
    if shutil.which("rg") is None or not queries:
        snippets: list[str] = []
        budget = 32000
        for path in doc_files[:8]:
            relative = path.relative_to(repo_root).as_posix()
            content = path.read_text(encoding="utf-8")[:4000]
            block = f"--- {relative} ---\n{content}"
            if len("\n\n".join(snippets)) + len(block) > budget:
                break
            snippets.append(block)
        return "\n\n".join(snippets)

    files = [str(path) for path in doc_files]
    chunks: list[str] = []
    seen: set[tuple[str, int]] = set()
    for query in queries:
        completed = run_command(
            ["rg", "--line-number", "--no-heading", "--color=never", "-C", "2", "--", query, *files],
            cwd=repo_root,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            continue
        for line in completed.stdout.splitlines():
            parsed = _parse_rg_line(line)
            if not parsed:
                continue
            file_name, line_number, content = parsed
            key = (file_name, line_number)
            if key in seen:
                continue
            seen.add(key)
            chunks.append(f"{file_name}:{line_number}: {content}")
            if sum(len(chunk) for chunk in chunks) > 32000:
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
