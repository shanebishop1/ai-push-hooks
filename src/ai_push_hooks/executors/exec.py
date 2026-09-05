from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from ..paths import (
    ensure_private_directory,
    is_path_within,
    normalized_component,
    path_has_symlink,
    path_is_link_or_reparse,
    relative_path_parts,
    resolve_contained_path,
    write_text_no_follow,
)
from ..types import (
    FEATURE_BRANCH_PREFIXES,
    HookError,
    ModuleRuntimeState,
    PushRefUpdate,
    PushRevisionRange,
    RuntimeContext,
    StepConfig,
    ZERO_OID_LENGTHS,
)

ZERO_OID = "0" * 40
BEADS_ALIGNMENT_TIMEOUT_SECONDS = 30
BEADS_ALIGNMENT_TOTAL_TIMEOUT_SECONDS = 120
BEADS_ALIGNMENT_MAX_COMMANDS = 20
BEADS_ISSUE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
BEADS_UPDATE_STATUSES = frozenset({"open", "in_progress", "blocked"})
BEADS_ENV_NAMES = frozenset(
    {
        "ALL_PROXY",
        "APPDATA",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "PROGRAMDATA",
        "SSH_AUTH_SOCK",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
BEADS_ENV_PREFIXES = ("AWS_", "BD_", "BEADS_", "DOLT_")
GITHUB_REPOSITORY_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
GIT_DIFF_CHUNK_BYTES = 64 * 1024
GIT_ERROR_BYTES = 64 * 1024
DIFF_TRUNCATION_MARKER = "\n[diff truncated]\n"


def env_bool(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return None


def run_command(
    args: list[str],
    cwd: pathlib.Path,
    input_text: str | None = None,
    timeout: float | None = None,
    check: bool = False,
    env: dict[str, str | None] | None = None,
    inherit_env: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy() if inherit_env else {}
    if env is not None:
        for key, value in env.items():
            if value is None:
                merged_env.pop(key, None)
            else:
                merged_env[key] = value
    completed = subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        text=True,
        errors="surrogateescape",
        capture_output=True,
        timeout=timeout,
        env=merged_env,
    )
    if check and completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        details = stderr or stdout or f"exit code {completed.returncode}"
        raise HookError(f"Command failed: {' '.join(args)} :: {details}")
    return completed


def git(cwd: pathlib.Path, args: list[str], check: bool = True) -> str:
    completed = run_command(["git", *args], cwd=cwd, check=check)
    return completed.stdout.strip()


def resolve_repo_root(cwd: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(git(cwd, ["rev-parse", "--show-toplevel"])).resolve()


def resolve_git_dir(repo_root: pathlib.Path) -> pathlib.Path:
    raw = git(repo_root, ["rev-parse", "--git-dir"])
    path = pathlib.Path(raw)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def resolve_git_common_dir(repo_root: pathlib.Path) -> pathlib.Path:
    raw = git(repo_root, ["rev-parse", "--git-common-dir"])
    path = pathlib.Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def resolve_storage_path(repo_root: pathlib.Path, git_dir: pathlib.Path, raw: str) -> pathlib.Path:
    parts = relative_path_parts(raw, "Configured storage path")
    posix_raw = raw.replace("\\", "/")
    if parts[0] == ".git":
        if len(parts) == 1:
            return pathlib.Path(git_dir).resolve(strict=False)
        lexical_path = pathlib.Path(git_dir).joinpath(*parts[1:])
        if path_has_symlink(pathlib.Path(git_dir), lexical_path):
            raise HookError(f"Configured Git storage path must not traverse a symlink: {raw}")
        return resolve_contained_path(
            git_dir,
            "/".join(parts[1:]),
            "Configured Git storage path",
        )
    lexical_path = repo_root.joinpath(*parts)
    if path_has_symlink(repo_root, lexical_path):
        raise HookError(f"Configured repository storage path must not traverse a symlink: {raw}")
    return resolve_contained_path(repo_root, posix_raw, "Configured repository storage path")


def ensure_dir(path: pathlib.Path) -> pathlib.Path | None:
    try:
        return ensure_private_directory(path)
    except Exception:  # noqa: BLE001
        return None


def current_branch(repo_root: pathlib.Path) -> str:
    return git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()


def is_feature_branch(branch_name: str) -> bool:
    return bool(branch_name) and branch_name.startswith(FEATURE_BRANCH_PREFIXES)


def should_skip_for_sync_branch(
    repo_root: pathlib.Path,
    pushed_branches: list[str] | None = None,
    push_updates: list[PushRefUpdate] | None = None,
) -> tuple[bool, str]:
    sync_branch = os.getenv("BEADS_SYNC_BRANCH", "beads-sync")
    if pushed_branches is None:
        pushed_branches = [current_branch(repo_root)]
    if push_updates is not None:
        only_sync_branch_updates = bool(push_updates) and all(
            update.ref_kind == "branch"
            and update.operation != "delete"
            and update.branch_name == sync_branch
            for update in push_updates
        )
        if push_updates and not only_sync_branch_updates:
            return False, ""
    else:
        only_sync_branch_updates = bool(pushed_branches) and all(
            branch_name == sync_branch for branch_name in pushed_branches
        )
    if "/.beads-sync-worktrees/" in repo_root.as_posix():
        return True, "worktree is inside .beads-sync-worktrees"
    if only_sync_branch_updates:
        return True, f"all pushed branches are {sync_branch}"
    return False, ""


def path_matches(path: str, pattern: str) -> bool:
    path_parts = tuple(path.split("/"))
    if (
        not path_parts
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path_parts)
    ):
        return False
    try:
        pattern_parts = relative_path_parts(pattern, "Glob pattern")
    except HookError:
        return False

    memo: dict[tuple[int, int], bool] = {}

    def matches(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and matches(path_index + 1, pattern_index)
            )
        else:
            result = path_index < len(path_parts) and fnmatch.fnmatchcase(
                path_parts[path_index], pattern_parts[pattern_index]
            ) and matches(path_index + 1, pattern_index + 1)
        memo[key] = result
        return result

    return matches(0, 0)


def list_repo_changes(repo_root: pathlib.Path) -> set[str]:
    changes: set[str] = set()
    output = run_command(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo_root,
    ).stdout
    records = output.split("\x00")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise HookError("Malformed output from `git status --porcelain=v1 -z`")
        status = record[:2]
        changes.add(record[3:])
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise HookError("Malformed rename output from `git status --porcelain=v1 -z`")
            changes.add(records[index])
            index += 1
    return changes


def parse_push_updates(stdin_lines: list[str]) -> list[PushRefUpdate]:
    updates: list[PushRefUpdate] = []
    oid_pattern = re.compile(r"[0-9a-fA-F]+\Z")
    for line_number, line in enumerate(stdin_lines, start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise HookError(
                f"Malformed pre-push input on line {line_number}: expected four fields"
            )
        local_ref, local_sha, remote_ref, remote_sha = parts
        if (
            len(local_sha) not in ZERO_OID_LENGTHS
            or len(remote_sha) != len(local_sha)
            or oid_pattern.fullmatch(local_sha) is None
            or oid_pattern.fullmatch(remote_sha) is None
        ):
            raise HookError(
                f"Malformed pre-push input on line {line_number}: expected full SHA-1 or SHA-256 object IDs"
            )
        updates.append(
            PushRefUpdate(
                local_ref=local_ref,
                local_sha=local_sha.lower(),
                remote_ref=remote_ref,
                remote_sha=remote_sha.lower(),
            )
        )
    return updates


def _resolve_commit(repo_root: pathlib.Path, oid: str) -> str:
    return git(repo_root, ["rev-parse", "--verify", "--quiet", f"{oid}^{{commit}}"], check=False)


def _configured_base_commit(
    repo_root: pathlib.Path, remote_name: str, base_branch: str
) -> str:
    base_branch = base_branch.strip() or "main"
    candidates: list[str] = []
    if base_branch.startswith("refs/"):
        candidates.append(base_branch)
    else:
        configured_remotes = set(git(repo_root, ["remote"], check=False).splitlines())
        if remote_name in configured_remotes:
            candidates.append(f"refs/remotes/{remote_name}/{base_branch}")
        candidates.append(f"refs/heads/{base_branch}")
    for candidate in candidates:
        commit = _resolve_commit(repo_root, candidate)
        if commit:
            return commit
    return ""


def _empty_tree_oid(repo_root: pathlib.Path) -> str:
    completed = run_command(
        ["git", "hash-object", "-t", "tree", "--stdin"],
        cwd=repo_root,
        input_text="",
        check=True,
    )
    return (completed.stdout or "").strip()


def _fallback_range(
    repo_root: pathlib.Path,
    remote_name: str,
    base_branch: str,
    local_commit: str,
    *,
    reason: str,
) -> tuple[str, str]:
    base_commit = _configured_base_commit(repo_root, remote_name, base_branch)
    if base_commit:
        merge_base = git(repo_root, ["merge-base", local_commit, base_commit], check=False)
        if merge_base:
            return f"{merge_base}..{local_commit}", f"{reason}:configured-base"
    return f"{_empty_tree_oid(repo_root)}..{local_commit}", f"{reason}:empty-tree"


def collect_revision_ranges(
    repo_root: pathlib.Path,
    remote_name: str,
    updates: list[PushRefUpdate],
    base_branch: str = "main",
) -> list[PushRevisionRange]:
    ranges: list[PushRevisionRange] = []
    for update in updates:
        if update.operation == "delete":
            continue
        local_commit = _resolve_commit(repo_root, update.local_sha)
        if not local_commit:
            # Tags may legally point to non-commit objects. They still remain in
            # push_updates, but there is no commit/tree diff to collect for them.
            continue
        if update.operation == "update":
            remote_commit = _resolve_commit(repo_root, update.remote_sha)
            if remote_commit:
                expression = f"{remote_commit}..{local_commit}"
                strategy = "remote-object"
            else:
                raise HookError(
                    "Advertised remote commit is unavailable locally; refusing to "
                    f"approximate push range for {update.remote_ref}: {update.remote_sha}"
                )
        else:
            expression, strategy = _fallback_range(
                repo_root,
                remote_name,
                base_branch,
                local_commit,
                reason="new-ref",
            )
        ranges.append(
            PushRevisionRange(update=update, expression=expression, strategy=strategy)
        )
    return ranges


def unique_range_expressions(ranges: list[PushRevisionRange]) -> list[str]:
    return list(dict.fromkeys(item.expression for item in ranges))


def collect_ranges_from_stdin(
    repo_root: pathlib.Path,
    remote_name: str,
    stdin_lines: list[str],
    base_branch: str = "main",
) -> list[str]:
    updates = parse_push_updates(stdin_lines)
    return unique_range_expressions(
        collect_revision_ranges(repo_root, remote_name, updates, base_branch)
    )


def collect_changed_files(repo_root: pathlib.Path, ranges: list[str]) -> list[str]:
    files: set[str] = set()
    for range_expr in ranges:
        output = run_command(
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=ACMRD",
                "-z",
                range_expr,
            ],
            cwd=repo_root,
            check=True,
        ).stdout
        for path in output.split("\x00"):
            if path:
                files.add(path)
    return sorted(files)


def _read_bounded_stderr(stream: Any, captured: bytearray) -> None:
    try:
        while True:
            chunk = stream.read(GIT_DIFF_CHUNK_BYTES)
            if not chunk:
                return
            remaining = GIT_ERROR_BYTES - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
    except (OSError, ValueError):
        return


def _terminate_and_wait(process: subprocess.Popen[bytes]) -> int:
    if process.poll() is None:
        process.terminate()
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise HookError("Git diff process did not terminate safely") from error


def _collect_bounded_git_diff(
    repo_root: pathlib.Path, args: list[str], max_bytes: int
) -> tuple[bytes, bool]:
    process = subprocess.Popen(
        args,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        raise HookError("Could not capture Git diff output")

    stderr = bytearray()
    stderr_thread = threading.Thread(
        target=_read_bounded_stderr,
        args=(process.stderr, stderr),
        daemon=True,
    )
    stderr_thread.start()
    output = bytearray()
    limit = max(0, max_bytes)
    truncated = False
    returncode: int | None = None
    try:
        while True:
            remaining = limit - len(output)
            chunk = process.stdout.read(min(GIT_DIFF_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            if len(chunk) > remaining:
                if remaining > 0:
                    output.extend(chunk[:remaining])
                truncated = True
                returncode = _terminate_and_wait(process)
                break
            output.extend(chunk)
        if returncode is None:
            returncode = process.wait()
    finally:
        if process.poll() is None:
            _terminate_and_wait(process)
        stderr_thread.join(timeout=5)
        if stderr_thread.is_alive():
            process.stderr.close()
            stderr_thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()

    if returncode != 0 and not truncated:
        details = bytes(stderr).decode("utf-8", errors="surrogateescape").strip()
        details = details or f"exit code {returncode}"
        raise HookError(f"Command failed: {' '.join(args)} :: {details}")
    return bytes(output), truncated


def _decode_diff_output(output: bytes, max_bytes: int, truncated: bool) -> str:
    if not truncated:
        return output.decode("utf-8", errors="surrogateescape")
    limit = max(0, max_bytes)
    if limit == 0:
        return ""
    marker = DIFF_TRUNCATION_MARKER.encode("utf-8")
    if len(marker) >= limit:
        return marker[:limit].decode("utf-8", errors="surrogateescape")
    return (output[: limit - len(marker)] + marker).decode(
        "utf-8", errors="surrogateescape"
    )


def collect_diff(repo_root: pathlib.Path, ranges: list[str], max_bytes: int) -> str:
    output = bytearray()
    limit = max(0, max_bytes)
    truncated = False
    for index, range_expr in enumerate(ranges):
        prefix = ("\n" if index else "") + f"### RANGE {range_expr}\n"
        prefix_bytes = prefix.encode("utf-8", errors="surrogateescape")
        remaining = limit - len(output)
        if len(prefix_bytes) > remaining:
            output.extend(prefix_bytes[:remaining])
            truncated = True
            break
        output.extend(prefix_bytes)

        body, body_truncated = _collect_bounded_git_diff(
            repo_root,
            ["git", "diff", "--unified=3", range_expr],
            limit - len(output),
        )
        if not body_truncated:
            # `git()` historically stripped the captured diff before adding the
            # section's trailing newline. Keep that output shape when the body
            # fits, without ever collecting more than the remaining budget.
            body = body.rstrip()
        output.extend(body)
        if body_truncated:
            truncated = True
            break

        if len(output) >= limit:
            truncated = True
            break
        output.extend(b"\n")
    return _decode_diff_output(bytes(output), limit, truncated)


def collect_commit_messages_for_ranges(
    repo_root: pathlib.Path, ranges: list[str]
) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    seen_hashes: set[str] = set()
    for range_expr in ranges:
        completed = run_command(
            ["git", "log", "--format=%H%x1f%s%x1f%b%x1e", range_expr],
            cwd=repo_root,
            check=True,
        )
        raw = completed.stdout or ""
        for record in raw.split("\x1e"):
            payload = record.rstrip("\r\n")
            if not payload:
                continue
            parts = payload.split("\x1f", 2)
            if len(parts) == 2:
                commit_hash, subject = parts
                body = ""
            elif len(parts) == 3:
                commit_hash, subject, body = parts
            else:
                continue
            clean_hash = commit_hash.strip()
            if not clean_hash or clean_hash in seen_hashes:
                continue
            seen_hashes.add(clean_hash)
            commits.append(
                {
                    "hash": clean_hash,
                    "subject": subject.strip(),
                    "body": body.strip(),
                }
            )
    return commits


def write_text_file(
    path: pathlib.Path,
    content: str,
    *,
    root: pathlib.Path | None = None,
) -> bool:
    try:
        if root is None:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            root = root.resolve(strict=True)
            lexical_path = pathlib.Path(os.path.abspath(path))
            relative_parent = lexical_path.parent.relative_to(root)
            current = root
            for part in relative_parent.parts:
                current = current / part
                if path_is_link_or_reparse(current):
                    raise HookError(
                        f"Output path traverses a symlink or reparse point: {path}"
                    )
                if not current.exists():
                    current.mkdir()
                if not current.is_dir():
                    raise HookError(f"Output path has a non-directory parent: {path}")
            if path_has_symlink(root, lexical_path):
                raise HookError(f"Output path traverses a symlink: {path}")
            if lexical_path.exists() and not stat.S_ISREG(lexical_path.lstat().st_mode):
                raise HookError(f"Output path is not a regular file: {path}")
        write_text_no_follow(path, content)
        return True
    except Exception:  # noqa: BLE001
        return False


def parse_key_value_text(text: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key.strip()] = value.strip()
    return payload


def _github_repository_from_url(remote_url: str) -> str:
    value = remote_url.strip()
    if not value or "\x00" in value or any(ord(character) < 32 for character in value):
        return ""
    scp_match = re.fullmatch(r"(?:[^@/:\s]+@)?github\.com:([^/\s]+)/([^/\s]+)", value, re.IGNORECASE)
    if scp_match:
        owner, repository = scp_match.groups()
    else:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if (
            parsed.scheme.lower() not in {"git", "http", "https", "ssh"}
            or (parsed.hostname or "").casefold() != "github.com"
            or parsed.query
            or parsed.fragment
            or "%" in parsed.path
        ):
            return ""
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            return ""
        owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if (
        not owner
        or not repository
        or owner in {".", ".."}
        or repository in {".", ".."}
        or GITHUB_REPOSITORY_COMPONENT.fullmatch(owner) is None
        or GITHUB_REPOSITORY_COMPONENT.fullmatch(repository) is None
    ):
        return ""
    return f"{owner}/{repository}"


def resolve_github_repository(
    repo_root: pathlib.Path, remote_name: str, remote_url: str
) -> str:
    repository = _github_repository_from_url(remote_url)
    if repository:
        return repository
    if remote_url.strip():
        raise HookError(f"Cannot safely determine GitHub repository from push remote URL: {remote_url!r}")
    repository = _github_repository_from_url(remote_name)
    if repository:
        return repository
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", remote_name):
        raise HookError(f"Cannot safely resolve push remote name: {remote_name!r}")
    configured_url = git(repo_root, ["remote", "get-url", "--push", remote_name], check=False)
    repository = _github_repository_from_url(configured_url)
    if not repository:
        raise HookError(
            f"Cannot safely determine GitHub repository for push remote {remote_name!r}"
        )
    return repository


def lookup_open_pr_url(
    repo_root: pathlib.Path,
    branch_name: str,
    base_branch: str = "",
    repository: str = "",
) -> str:
    if not repository:
        raise HookError("GitHub repository scope is required for PR lookup")
    args = [
        "gh",
        "pr",
        "list",
        "--repo",
        repository,
        "--head",
        branch_name,
        "--state",
        "open",
        "--limit",
        "1",
        "--json",
        "url",
    ]
    if base_branch:
        args.extend(["--base", base_branch])
    completed = run_command(
        args,
        cwd=repo_root,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        raise HookError(details or "`gh pr list` failed")
    try:
        payload = json.loads((completed.stdout or "").strip() or "[]")
    except json.JSONDecodeError as exc:
        raise HookError("Failed to parse `gh pr list` JSON output") from exc
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return str(payload[0].get("url", "")).strip()
    return ""


def extract_pr_url(text: str) -> str:
    match = re.search(r"https://github\.com/[^\s]+/pull/\d+", text)
    return match.group(0).strip() if match else ""


def sanitize_pr_title(raw_title: str, branch_name: str) -> str:
    title = re.sub(r"\s+", " ", raw_title).strip() or branch_name
    return title[:240]


def initial_pr_defer_reason(branch_name: str, base_branch: str) -> str:
    return (
        f"PR creation deferred because `{branch_name}` does not exist on the remote before "
        "this initial push. Complete the push, then create the PR with "
        f"`gh pr create --head {shlex.quote(branch_name)} --base "
        f"{shlex.quote(base_branch)}`, or push another commit with PR creation enabled."
    )


def build_fallback_pr_body(
    branch_name: str,
    ranges: list[str],
    changed_files: list[str],
    commits: list[dict[str, str]],
) -> str:
    lines = [
        "## Summary",
        f"- Auto-created by `ai-push-hooks` for branch `{branch_name}`.",
    ]
    if ranges:
        lines.append(f"- Push range: `{', '.join(ranges)}`.")
    if commits:
        lines.append("")
        lines.append("## Commits")
        for commit in commits[:8]:
            subject = str(commit.get("subject", "")).strip()
            if subject:
                lines.append(f"- {subject}")
    if changed_files:
        lines.append("")
        lines.append("## Changed Files")
        for path in changed_files[:15]:
            lines.append(f"- `{path}`")
        if len(changed_files) > 15:
            lines.append(f"- and {len(changed_files) - 15} more")
    return "\n".join(lines).strip() + "\n"


def attempt_pr_creation_fallback(
    repo_root: pathlib.Path,
    branch_name: str,
    base_branch: str,
    ranges: list[str],
    changed_files: list[str],
    commits: list[dict[str, str]],
    repository: str,
) -> str:
    title = sanitize_pr_title(
        git(repo_root, ["log", "-1", "--pretty=%s"], check=False), branch_name
    )
    body = build_fallback_pr_body(branch_name, ranges, changed_files, commits)
    created = run_command(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repository,
            "--head",
            branch_name,
            "--base",
            base_branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=repo_root,
        check=False,
    )
    combined_output = "\n".join([(created.stdout or "").strip(), (created.stderr or "").strip()])
    if created.returncode == 0:
        pr_url = extract_pr_url(combined_output)
        if pr_url:
            return pr_url
    existing_pr = lookup_open_pr_url(repo_root, branch_name, base_branch, repository)
    if existing_pr:
        return existing_pr
    raise HookError(
        combined_output.strip() or f"gh pr create failed with exit code {created.returncode}"
    )


def remote_branch_exists(repo_root: pathlib.Path, remote_name: str, branch_name: str) -> bool:
    completed = run_command(
        ["git", "ls-remote", "--heads", remote_name, branch_name], cwd=repo_root, check=False
    )
    return completed.returncode == 0 and bool((completed.stdout or "").strip())


def _report_file_path(context: RuntimeContext, state: ModuleRuntimeState) -> pathlib.Path:
    branch_context = state.artifacts.get("collect/branch-context.txt")
    if branch_context and branch_context.exists():
        payload = parse_key_value_text(branch_context.read_text(encoding="utf-8"))
        report_file = payload.get("report_file", "BEADS_STATUS_ACTION_REQUIRED.md")
    else:
        report_file = "BEADS_STATUS_ACTION_REQUIRED.md"

    parts = relative_path_parts(report_file, "Beads alignment report path")
    if any(normalized_component(part) == ".git" for part in parts):
        raise HookError("Beads alignment report path must not reference Git metadata")
    lexical_path = context.repo_root.joinpath(*parts)
    if path_has_symlink(context.repo_root, lexical_path):
        raise HookError("Beads alignment report path must not traverse a symlink")
    report_path = resolve_contained_path(
        context.repo_root,
        report_file,
        "Beads alignment report path",
    )
    if report_path.exists() and not stat.S_ISREG(report_path.lstat().st_mode):
        raise HookError("Beads alignment report path must be a regular file")
    return report_path


def _validate_beads_issue_ids(values: list[str]) -> None:
    if not values or len(values) > 20:
        raise HookError("Beads alignment commands require between 1 and 20 issue ids")
    for issue_id in values:
        if not BEADS_ISSUE_ID_PATTERN.fullmatch(issue_id):
            raise HookError(f"Invalid Beads issue id in alignment command: {issue_id!r}")


def validate_beads_alignment_command(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise HookError("Beads alignment commands must be non-empty strings")
    if len(command) > 4096 or "\x00" in command or any(ord(char) < 32 for char in command):
        raise HookError("Beads alignment command contains invalid or excessive input")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise HookError(f"Malformed Beads alignment command: {exc}") from exc

    if len(argv) < 3 or argv[0] != "bd":
        raise HookError("Beads alignment commands must use the literal `bd` executable")

    subcommand = argv[1]
    if subcommand == "update":
        if len(argv) < 5 or argv[-2] != "--status" or argv[-1] not in BEADS_UPDATE_STATUSES:
            raise HookError(
                "Allowed Beads update form is: bd update <issue-id> [<issue-id> ...] "
                "--status <open|in_progress|blocked>"
            )
        _validate_beads_issue_ids(argv[2:-2])
        return argv

    if subcommand == "close":
        issue_ids = argv[2:]
        if "--reason" in issue_ids:
            if issue_ids.count("--reason") != 1 or issue_ids[-2] != "--reason":
                raise HookError(
                    "Allowed Beads close form is: bd close <issue-id> [<issue-id> ...] "
                    "[--reason <text>]"
                )
            reason = issue_ids[-1]
            if not reason or reason.startswith("-") or len(reason) > 500:
                raise HookError("Invalid Beads close reason")
            issue_ids = issue_ids[:-2]
        _validate_beads_issue_ids(issue_ids)
        return argv

    raise HookError(
        f"Beads alignment subcommand `{subcommand}` is not allowed; only `update` and `close` are permitted"
    )


def resolve_beads_executable(repo_root: pathlib.Path) -> str:
    candidate = shutil.which("bd")
    if not candidate:
        raise HookError("`bd` is required for Beads alignment but is not installed")
    lexical_candidate = pathlib.Path(os.path.abspath(candidate))
    resolved_repo_root = repo_root.resolve(strict=True)
    if is_path_within(lexical_candidate, resolved_repo_root):
        raise HookError(f"Refusing repository-contained `bd` executable: {lexical_candidate}")
    try:
        executable = lexical_candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HookError("Unable to safely resolve the `bd` executable") from exc
    if is_path_within(executable, resolved_repo_root):
        raise HookError(f"Refusing repository-contained `bd` executable: {executable}")
    if path_is_link_or_reparse(executable) or not stat.S_ISREG(executable.stat().st_mode):
        raise HookError(f"Resolved `bd` executable is not a regular file: {executable}")
    if not os.access(executable, os.X_OK):
        raise HookError(f"Resolved `bd` executable is not executable: {executable}")
    return str(executable)


def beads_alignment_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name in BEADS_ENV_NAMES or name.startswith(BEADS_ENV_PREFIXES)
    }


def beads_alignment_executor(
    context: RuntimeContext,
    state: ModuleRuntimeState,
    step: StepConfig,
    inputs: list[pathlib.Path],
) -> dict[str, Any]:
    if state.metadata.get("skip_module"):
        return {"skipped": True, "commands_run": [], "report_written": False, "unresolved": False}
    payload = json.loads(inputs[0].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HookError("beads_alignment payload must be an object")
    commands = payload.get("commands", [])
    if not isinstance(commands, list):
        raise HookError("beads_alignment commands must be an array")
    if len(commands) > BEADS_ALIGNMENT_MAX_COMMANDS:
        raise HookError(
            f"beads_alignment accepts at most {BEADS_ALIGNMENT_MAX_COMMANDS} commands"
        )
    validated_commands = [validate_beads_alignment_command(command) for command in commands]
    beads_executable = resolve_beads_executable(context.repo_root) if commands else ""
    command_env = beads_alignment_env()
    report_path = _report_file_path(context, state)
    commands_run: list[str] = []
    started_at = time.monotonic()
    for command, argv in zip(commands, validated_commands):
        remaining = BEADS_ALIGNMENT_TOTAL_TIMEOUT_SECONDS - (time.monotonic() - started_at)
        if remaining <= 0:
            raise HookError(
                f"Beads alignment exceeded its {BEADS_ALIGNMENT_TOTAL_TIMEOUT_SECONDS}-second total budget"
            )
        run_command(
            [beads_executable, *argv[1:]],
            cwd=context.repo_root,
            timeout=min(BEADS_ALIGNMENT_TIMEOUT_SECONDS, remaining),
            check=True,
            env=command_env,
            inherit_env=False,
        )
        commands_run.append(command)

    report_markdown = str(payload.get("report_markdown", "")).strip()
    unresolved = bool(payload.get("unresolved", False))
    report_written = False
    if report_markdown:
        if not report_markdown.endswith("\n"):
            report_markdown += "\n"
        if not write_text_file(report_path, report_markdown, root=context.repo_root):
            raise HookError(f"Failed to write Beads alignment report: {report_path}")
        report_written = True
    elif report_path.exists() and not unresolved:
        if path_has_symlink(context.repo_root, report_path) or not stat.S_ISREG(
            report_path.lstat().st_mode
        ):
            raise HookError("Refusing to remove unsafe Beads alignment report path")
        report_path.unlink()

    return {
        "skipped": False,
        "commands_run": commands_run,
        "report_written": report_written,
        "unresolved": unresolved,
        "report_file": report_path.relative_to(context.repo_root).as_posix(),
    }


def gh_pr_create_executor(
    context: RuntimeContext,
    state: ModuleRuntimeState,
    step: StepConfig,
    inputs: list[pathlib.Path],
) -> dict[str, Any]:
    if state.metadata.get("skip_module"):
        return {"skipped": True, "pr_url": state.metadata.get("existing_pr_url", "")}
    branch_name = str(context.cache.get("branch_name", "")).strip()
    if not branch_name:
        reason = str(
            context.cache.get("branch_selection_reason", "no single pushed branch is available")
        )
        raise HookError(f"PR creation requires one pushed branch: {reason}")
    default_base_branch = context.config.general.base_branch.strip() or "main"
    if bool(context.cache.get("branch_is_new", False)):
        reason = initial_pr_defer_reason(branch_name, default_base_branch)
        context.logger.warn("pr.create_deferred", reason, branch=branch_name)
        return {
            "skipped": True,
            "pr_url": "",
            "deferred_until_remote": True,
            "reason": reason,
        }
    if shutil.which("gh") is None:
        raise HookError("`gh` is required for PR creation but is not installed")
    repository = resolve_github_repository(
        context.repo_root, context.remote_name, context.remote_url
    )
    payload = json.loads(inputs[0].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HookError("PR creation payload must be an object")
    existing_pr = lookup_open_pr_url(
        context.repo_root, branch_name, default_base_branch, repository
    )
    if existing_pr:
        return {"skipped": False, "pr_url": existing_pr, "already_exists": True}

    base_branch = default_base_branch
    head_branch = branch_name
    title = sanitize_pr_title(str(payload.get("title", "")).strip(), branch_name)
    body = str(payload.get("body", "")).strip()
    if not body:
        commits = collect_commit_messages_for_ranges(
            context.repo_root,
            context.cache.get("branch_ranges", context.cache.get("ranges", [])),
        )
        body = build_fallback_pr_body(
            branch_name,
            context.cache.get("branch_ranges", context.cache.get("ranges", [])),
            context.cache.get(
                "branch_changed_files", context.cache.get("changed_files", [])
            ),
            commits,
        )
    args = [
        "gh",
        "pr",
        "create",
        "--repo",
        repository,
        "--head",
        head_branch,
        "--base",
        base_branch,
        "--title",
        title,
        "--body",
        body,
    ]
    if bool(payload.get("draft", False)):
        args.append("--draft")
    created = run_command(args, cwd=context.repo_root, check=False)
    combined_output = "\n".join([(created.stdout or "").strip(), (created.stderr or "").strip()])
    pr_url = extract_pr_url(combined_output)
    if created.returncode != 0 and not pr_url:
        pr_url = lookup_open_pr_url(
            context.repo_root, branch_name, default_base_branch, repository
        )
    if not pr_url:
        raise HookError(
            combined_output.strip() or f"gh pr create failed with exit code {created.returncode}"
        )
    return {"skipped": False, "pr_url": pr_url, "already_exists": False}


EXEC_HANDLERS = {
    "beads_alignment": beads_alignment_executor,
    "gh_pr_create": gh_pr_create_executor,
}
