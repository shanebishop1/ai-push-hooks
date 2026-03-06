from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
from pathlib import PurePosixPath
from typing import Any

from ..types import FEATURE_BRANCH_PREFIXES, HookError, ModuleRuntimeState, RuntimeContext, StepConfig

ZERO_OID = "0000000000000000000000000000000000000000"


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
    timeout: int | None = None,
    check: bool = False,
    env: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = None
    if env is not None:
        merged_env = os.environ.copy()
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


def resolve_storage_path(repo_root: pathlib.Path, git_dir: pathlib.Path, raw: str) -> pathlib.Path:
    path = pathlib.Path(raw)
    if path.is_absolute():
        return path
    posix_raw = raw.replace("\\", "/")
    if posix_raw == ".git":
        return git_dir
    if posix_raw.startswith(".git/"):
        return git_dir / posix_raw[len(".git/") :]
    return repo_root / path


def ensure_dir(path: pathlib.Path) -> pathlib.Path | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:  # noqa: BLE001
        return None


def current_branch(repo_root: pathlib.Path) -> str:
    return git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()


def is_feature_branch(branch_name: str) -> bool:
    return bool(branch_name) and branch_name.startswith(FEATURE_BRANCH_PREFIXES)


def should_skip_for_sync_branch(repo_root: pathlib.Path) -> tuple[bool, str]:
    sync_branch = os.getenv("BEADS_SYNC_BRANCH", "beads-sync")
    if "/.beads-sync-worktrees/" in repo_root.as_posix():
        return True, "worktree is inside .beads-sync-worktrees"
    branch_name = current_branch(repo_root)
    if branch_name == sync_branch:
        return True, f"current branch is {sync_branch}"
    return False, ""


def path_matches(path: str, pattern: str) -> bool:
    pure = PurePosixPath(path)
    return pure.match(pattern) or fnmatch.fnmatch(path, pattern)


def list_repo_changes(repo_root: pathlib.Path) -> set[str]:
    changes: set[str] = set()
    output = git(repo_root, ["status", "--short"], check=False)
    for line in output.splitlines():
        payload = line[3:].strip()
        if payload:
            changes.add(payload)
    return changes


def collect_ranges_from_stdin(
    repo_root: pathlib.Path,
    remote_name: str,
    stdin_lines: list[str],
) -> list[str]:
    ranges: set[str] = set()
    for line in stdin_lines:
        parts = line.strip().split()
        if len(parts) < 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts[:4]
        if local_sha == ZERO_OID:
            continue
        if remote_sha and remote_sha != ZERO_OID:
            if run_command(["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd=repo_root).returncode == 0:
                ranges.add(f"{remote_sha}..{local_sha}")
        else:
            merge_base = git(repo_root, ["merge-base", local_sha, f"{remote_name}/main"], check=False)
            if merge_base:
                ranges.add(f"{merge_base}..{local_sha}")
            else:
                ranges.add(f"{local_sha}~1..{local_sha}")
    if ranges:
        return sorted(ranges)

    upstream = git(repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], check=False)
    if upstream:
        merge_base = git(repo_root, ["merge-base", "HEAD", upstream], check=False)
        if merge_base:
            return [f"{merge_base}..HEAD"]
    previous = git(repo_root, ["rev-parse", "HEAD~1"], check=False)
    if previous:
        return [f"{previous}..HEAD"]
    return []


def collect_changed_files(repo_root: pathlib.Path, ranges: list[str]) -> list[str]:
    files: set[str] = set()
    for range_expr in ranges:
        output = git(repo_root, ["diff", "--name-only", "--diff-filter=ACMR", range_expr], check=True)
        for line in output.splitlines():
            clean = line.strip()
            if clean:
                files.add(clean)
    return sorted(files)


def collect_diff(repo_root: pathlib.Path, ranges: list[str], max_bytes: int) -> str:
    chunks: list[str] = []
    for range_expr in ranges:
        body = git(repo_root, ["diff", "--unified=3", range_expr], check=True)
        chunks.append(f"### RANGE {range_expr}\n{body}\n")
    return "\n".join(chunks)[:max_bytes]


def collect_commit_messages_for_ranges(repo_root: pathlib.Path, ranges: list[str]) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for range_expr in ranges:
        raw = git(repo_root, ["log", "--format=%H%x1f%s%x1f%b%x1e", range_expr], check=True)
        for record in raw.split("\x1e"):
            payload = record.strip()
            if not payload:
                continue
            parts = payload.split("\x1f", 2)
            if len(parts) != 3:
                continue
            commit_hash, subject, body = parts
            commits.append(
                {
                    "hash": commit_hash.strip(),
                    "subject": subject.strip(),
                    "body": body.strip(),
                }
            )
    return commits


def write_text_file(path: pathlib.Path, content: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
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


def lookup_open_pr_url(repo_root: pathlib.Path, branch_name: str) -> str:
    completed = run_command(
        ["gh", "pr", "list", "--head", branch_name, "--state", "open", "--limit", "1", "--json", "url"],
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


def build_fallback_pr_body(
    branch_name: str,
    ranges: list[str],
    changed_files: list[str],
    commits: list[dict[str, str]],
) -> str:
    lines = [
        "## Summary",
        f"- Auto-created by `ai-doc-sync-hook` for branch `{branch_name}`.",
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
) -> str:
    title = sanitize_pr_title(git(repo_root, ["log", "-1", "--pretty=%s"], check=False), branch_name)
    body = build_fallback_pr_body(branch_name, ranges, changed_files, commits)
    created = run_command(
        ["gh", "pr", "create", "--head", branch_name, "--base", base_branch, "--title", title, "--body", body],
        cwd=repo_root,
        check=False,
    )
    combined_output = "\n".join([(created.stdout or "").strip(), (created.stderr or "").strip()])
    if created.returncode == 0:
        pr_url = extract_pr_url(combined_output)
        if pr_url:
            return pr_url
    existing_pr = lookup_open_pr_url(repo_root, branch_name)
    if existing_pr:
        return existing_pr
    raise HookError(combined_output.strip() or f"gh pr create failed with exit code {created.returncode}")


def remote_branch_exists(repo_root: pathlib.Path, remote_name: str, branch_name: str) -> bool:
    completed = run_command(["git", "ls-remote", "--heads", remote_name, branch_name], cwd=repo_root, check=False)
    return completed.returncode == 0 and bool((completed.stdout or "").strip())


def _report_file_path(context: RuntimeContext, state: ModuleRuntimeState) -> pathlib.Path:
    branch_context = state.artifacts.get("collect/branch-context.txt")
    if branch_context and branch_context.exists():
        payload = parse_key_value_text(branch_context.read_text(encoding="utf-8"))
        report_file = payload.get("report_file", "BEADS_STATUS_ACTION_REQUIRED.md")
        return (context.repo_root / report_file).resolve()
    return (context.repo_root / "BEADS_STATUS_ACTION_REQUIRED.md").resolve()


def beads_alignment_executor(
    context: RuntimeContext,
    state: ModuleRuntimeState,
    step: StepConfig,
    inputs: list[pathlib.Path],
) -> dict[str, Any]:
    if state.metadata.get("skip_module"):
        return {"skipped": True, "commands_run": [], "report_written": False, "unresolved": False}
    payload = json.loads(inputs[0].read_text(encoding="utf-8"))
    commands = payload.get("commands", [])
    if not isinstance(commands, list):
        raise HookError("beads_alignment commands must be an array")
    report_path = _report_file_path(context, state)
    commands_run: list[str] = []
    for command in commands:
        if not isinstance(command, str) or not command.strip():
            continue
        run_command(shlex.split(command), cwd=context.repo_root, check=True)
        commands_run.append(command)

    report_markdown = str(payload.get("report_markdown", "")).strip()
    unresolved = bool(payload.get("unresolved", False))
    report_written = False
    if report_markdown:
        if not report_markdown.endswith("\n"):
            report_markdown += "\n"
        write_text_file(report_path, report_markdown)
        report_written = True
    elif report_path.exists() and not unresolved:
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
    if shutil.which("gh") is None:
        raise HookError("`gh` is required for PR creation but is not installed")
    payload = json.loads(inputs[0].read_text(encoding="utf-8"))
    branch_name = current_branch(context.repo_root)
    existing_pr = lookup_open_pr_url(context.repo_root, branch_name)
    if existing_pr:
        return {"skipped": False, "pr_url": existing_pr, "already_exists": True}

    base_branch = str(payload.get("base_branch", "main")).strip() or "main"
    head_branch = str(payload.get("head_branch", branch_name)).strip() or branch_name
    title = sanitize_pr_title(str(payload.get("title", "")).strip(), branch_name)
    body = str(payload.get("body", "")).strip()
    if not body:
        commits = collect_commit_messages_for_ranges(context.repo_root, context.cache.get("ranges", []))
        body = build_fallback_pr_body(
            branch_name,
            context.cache.get("ranges", []),
            context.cache.get("changed_files", []),
            commits,
        )
    args = ["gh", "pr", "create", "--head", head_branch, "--base", base_branch, "--title", title, "--body", body]
    if bool(payload.get("draft", False)):
        args.append("--draft")
    created = run_command(args, cwd=context.repo_root, check=False)
    combined_output = "\n".join([(created.stdout or "").strip(), (created.stderr or "").strip()])
    pr_url = extract_pr_url(combined_output)
    if created.returncode != 0 and not pr_url:
        pr_url = lookup_open_pr_url(context.repo_root, branch_name)
    if not pr_url:
        if remote_branch_exists(context.repo_root, context.remote_name or "origin", branch_name):
            raise HookError(combined_output.strip() or f"gh pr create failed with exit code {created.returncode}")
        return {"skipped": False, "pr_url": "", "deferred_until_remote": True}
    return {"skipped": False, "pr_url": pr_url, "already_exists": False}


EXEC_HANDLERS = {
    "beads_alignment": beads_alignment_executor,
    "gh_pr_create": gh_pr_create_executor,
}
