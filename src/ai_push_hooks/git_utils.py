from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
from urllib.parse import urlsplit

from .executors.runners.contracts import (
    RunnerError,
    RunnerExecutableNotFoundError,
    RunnerSignalError,
    RunnerTimeoutError,
    bounded_redacted_diagnostics,
)
from .executors.runners.process import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ProcessResult,
    run_process,
)
from .paths import (
    ensure_private_directory,
    path_has_symlink,
    path_is_link_or_reparse,
    relative_path_parts,
    resolve_contained_path,
    write_text_no_follow,
)
from .types import (
    FEATURE_BRANCH_PREFIXES,
    ZERO_OID_LENGTHS,
    HookError,
    PushRefUpdate,
    PushRevisionRange,
)

GIT_ERROR_BYTES = 64 * 1024
DIFF_TRUNCATION_MARKER = "\n[diff truncated]\n"
COMMAND_DEFAULT_TIMEOUT_SECONDS = 120
GITHUB_REPOSITORY_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")


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
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run an argv command with bounded capture and fail-closed cleanup.

    ``timeout=None`` is retained for compatibility with existing internal
    callers, but now means the finite command budget rather than no timeout.
    Output remains text decoded with surrogateescape and non-zero results are
    returned unless ``check`` is true. Capture overflow always fails closed.
    """

    merged_env = os.environ.copy() if inherit_env else {}
    if env is not None:
        for key, value in env.items():
            if value is None:
                merged_env.pop(key, None)
            else:
                merged_env[key] = value

    effective_timeout = (
        COMMAND_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    )
    result = _run_bounded_text_command(
        args,
        cwd,
        input_text=input_text,
        timeout_seconds=effective_timeout,
        check=check,
        env=merged_env,
        max_bytes=max_output_bytes,
    )
    return subprocess.CompletedProcess(
        list(args), result.returncode, result.stdout, result.stderr
    )


def _run_bounded_text_command(
    args: list[str],
    cwd: pathlib.Path,
    max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    *,
    input_text: str | None = None,
    timeout_seconds: float = COMMAND_DEFAULT_TIMEOUT_SECONDS,
    check: bool = False,
    env: dict[str, str] | None = None,
    max_stderr_bytes: int | None = None,
) -> ProcessResult:
    """Run a bounded text command and normalize process failures.

    Most Git commands must not silently consume a partial result. Diff
    collection has separate handling because it deliberately preserves a
    partial result and adds its own marker.
    """

    try:
        process_kwargs: dict[str, object] = {
            "cwd": cwd,
            "input_text": input_text,
            "timeout_seconds": timeout_seconds,
            "env": env,
            "max_output_bytes": max_bytes,
        }
        if max_stderr_bytes is not None:
            process_kwargs["max_stderr_bytes"] = max_stderr_bytes
        result = run_process(args, **process_kwargs)
    except RunnerExecutableNotFoundError as exc:
        raise HookError("Command executable was not found") from exc
    except RunnerSignalError as exc:
        process_result = _process_result_from_error(exc)
        if not check and process_result is not None:
            return process_result
        details = _command_diagnostics(
            args,
            process_result.stdout if process_result else "",
            process_result.stderr if process_result else "",
            input_text=input_text,
            env=env,
        )
        suffix = f": {details}" if details else ""
        raise HookError(f"Command terminated by signal{suffix}") from exc
    except RunnerTimeoutError as exc:
        process_result = _process_result_from_error(exc)
        details = _command_diagnostics(
            args,
            process_result.stdout if process_result else "",
            process_result.stderr if process_result else "",
            input_text=input_text,
            env=env,
        )
        suffix = f": {details}" if details else ""
        raise HookError(f"Command timed out{suffix}") from exc
    except RunnerError as exc:
        raise HookError(str(exc)) from exc

    if result.stdout_truncated or result.stderr_truncated:
        details = _command_diagnostics(
            args,
            result.stdout,
            result.stderr,
            input_text=input_text,
            env=env,
        )
        details = details or "capture limit exceeded"
        raise HookError(f"Command output exceeded capture limit: {details}")
    if check and result.returncode != 0:
        details = _command_diagnostics(
            args,
            result.stdout,
            result.stderr,
            input_text=input_text,
            env=env,
        )
        details = details or f"exit code {result.returncode}"
        raise HookError(f"Command failed: {details}")
    return result


def _process_result_from_error(error: BaseException) -> ProcessResult | None:
    result = getattr(error, "_process_result", None)
    return result if isinstance(result, ProcessResult) else None


def _command_diagnostics(
    args: list[str],
    stdout: str,
    stderr: str,
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Build a short diagnostic without echoing argv, prompts, or secrets."""

    secret_values = [*args]
    if input_text:
        secret_values.append(input_text)
    if env is not None:
        secret_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
        secret_values.extend(
            value
            for name, value in env.items()
            if any(marker in name.upper() for marker in secret_markers)
        )
    return bounded_redacted_diagnostics(stdout, stderr, secrets=secret_values)


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


def _collect_bounded_git_diff(
    repo_root: pathlib.Path, args: list[str], max_bytes: int
) -> tuple[bytes, bool]:
    limit = max(0, max_bytes)
    # Keep the diff's historical partial-capture behavior: a diff that reaches
    # its caller-provided budget is returned with a marker rather than treated
    # as a failed command.  The generic process engine still owns timeout,
    # bounded stream draining, and process-group cleanup.  Keep stderr at its
    # historical diagnostic bound even when the diff budget is much larger.
    try:
        result = run_process(
            args,
            cwd=repo_root,
            timeout_seconds=COMMAND_DEFAULT_TIMEOUT_SECONDS,
            max_output_bytes=limit,
            max_stderr_bytes=GIT_ERROR_BYTES,
        )
    except (RunnerTimeoutError, RunnerSignalError) as exc:
        process_result = _process_result_from_error(exc)
        details = _command_diagnostics(
            args,
            process_result.stdout if process_result else "",
            process_result.stderr if process_result else "",
        )
        reason = "timed out" if isinstance(exc, RunnerTimeoutError) else "terminated by signal"
        suffix = f": {details}" if details else ""
        raise HookError(f"Git diff command {reason}{suffix}") from exc
    except RunnerError as exc:
        raise HookError(str(exc)) from exc

    output = result.stdout_bytes[:limit]
    truncated = result.stdout_truncated
    if result.returncode != 0 and not truncated:
        details = _command_diagnostics(args, result.stdout, result.stderr)
        details = details or f"exit code {result.returncode}"
        raise HookError(f"Command failed: {details}")
    return output, truncated


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
    if repository.endswith(".git"):  # noqa: FURB188
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
        raise HookError("Cannot safely determine GitHub repository from push remote URL")
    repository = _github_repository_from_url(remote_name)
    if repository:
        return repository
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", remote_name):
        raise HookError("Cannot safely resolve push remote name")
    configured_url = git(repo_root, ["remote", "get-url", "--push", remote_name], check=False)
    repository = _github_repository_from_url(configured_url)
    if not repository:
        raise HookError(
            "Cannot safely determine GitHub repository for the configured push remote"
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
        details = _command_diagnostics(args, completed.stdout or "", completed.stderr or "")
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
