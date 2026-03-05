#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import fnmatch
import glob
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for older system python
    tomllib = None  # type: ignore[assignment]

ZERO_OID = "0000000000000000000000000000000000000000"

DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "enabled": True,
        "allow_push_on_error": False,
        "mode": "apply-and-block",
        "execution_mode": "multi",
        "require_clean_worktree": False,
        "skip_on_sync_branch": True,
    },
    "llm": {
        "runner": "opencode",
        "model": "openai/gpt-5.3-codex-spark",
        "variant": "",
        "max_diff_bytes": 180000,
        "timeout_seconds": 800,
        "session_title_prefix": "ai-doc-sync",
        "delete_session_after_run": True,
        "json_max_retries": 2,
        "invalid_json_feedback_max_chars": 6000,
        "json_retry_new_session": True,
    },
    "prompts": {
        "sync_file": "tools/ai-doc-sync/prompts/sync.txt",
        "query_file": "tools/ai-doc-sync/prompts/query.txt",
        "analysis_file": "tools/ai-doc-sync/prompts/analysis.txt",
        "apply_file": "tools/ai-doc-sync/prompts/apply.txt",
        "beads_status_file": "tools/ai-doc-sync/prompts/beads-status.txt",
        "pr_create_file": "tools/ai-doc-sync/prompts/create-pr.txt",
    },
    "docs": {
        "paths": ["README.md", "docs/**/*.md"],
        "ignore": ["docs/archive/**"],
        "max_context_tokens": 8000,
    },
    "cache": {
        "enabled": True,
        "dir": ".git/ai-doc-sync-cache",
        "ttl_seconds": 3600,
    },
    "logging": {
        "level": "status",
        "jsonl": True,
        "dir": ".git/ai-doc-sync/logs",
        "capture_llm_transcript": True,
        "transcript_dir": ".git/ai-doc-sync/transcripts",
        "summary_dir": ".git/ai-doc-sync/summaries",
        "print_llm_output": False,
    },
    "beads": {
        "enabled": True,
        "report_file": "BEADS_STATUS_ACTION_REQUIRED.md",
    },
    "pr": {
        "enabled": True,
        "flag_env": "AI_DOC_SYNC_CREATE_PR",
        "base_branch": "main",
    },
}

FEATURE_BRANCH_PREFIXES = ("feat/", "feature/")

LLM_STAGE_PURPOSES = {
    "00-beads-status": "Align Beads task statuses to branch changes",
    "01-sync": "Run unified pre-push Beads/docs/PR workflow",
    "01-query": "Generate retrieval queries for documentation context",
    "02-analysis": "Analyze documentation drift against code changes",
    "03-apply": "Apply minimal documentation fixes",
    "99-create-pr": "Create or locate pull request for current branch",
}


def stage_purpose(stage_name: str) -> str:
    return LLM_STAGE_PURPOSES.get(stage_name, "Run stage-specific LLM workflow")


DEFAULT_ANALYSIS_PROMPT = """You are a strict documentation consistency reviewer. Your job is to find ONLY clear, obvious documentation errors caused by code changes.

ONLY report an issue if:
1. Documentation explicitly states something that is NOW FACTUALLY WRONG due to the code change
2. A code example in the docs would NOW FAIL or produce different results
3. A function signature, parameter, or return type documented is NOW DIFFERENT in the code

DO NOT report:
- Stylistic improvements or suggestions
- Documentation that is vague but not technically wrong
- Potential improvements or clarifications
- Anything where the docs are still technically accurate
- Anything speculative that cannot be grounded in the provided diff and doc excerpts

Be conservative. When in doubt, return no issue.
If there are no clear issues, return [].

Output only a JSON array with objects containing:
- "file": doc file path
- "line": approximate line number (0 if unknown)
- "description": what is factually wrong
- "doc_excerpt": exact doc text that is wrong
- "suggested_fix": minimal fix (optional)

Use attached files:
- push.diff
- docs-context.txt
- recent-commits.txt

Return JSON only."""

SEARCH_QUERIES_PROMPT = """Given the attached push diff and changed file list, output a JSON array of documentation search queries.

Requirements:
- Include exact tokens: function/class names, flags, config keys, endpoints.
- Include semantic expansion terms for related concepts (for example, port changes -> networking/runtime terms).
- Keep queries short and searchable with ripgrep.
- Return 8-30 unique strings.

Use attached files:
- push.diff
- changed-files.txt

Output only valid JSON array, no prose."""

APPLY_PROMPT = """You are applying documentation fixes in a git pre-push hook.

Use attached files:
- push diff
- changed file list
- docs inventory
- detected issues
- AGENTS.md

Rules:
1) Modify only Markdown docs in docs/ and README.md.
2) Do not modify code, scripts, lockfiles, configs, or non-doc assets.
3) Keep edits minimal and factual.
4) If a doc is added or moved, update relevant docs/**/INDEX.md references.
5) If no doc changes are needed, do not edit files.

Apply the minimum doc updates required to resolve the detected factual drift."""

BEADS_STATUS_PROMPT = """You are checking Beads task status alignment before a git push.

Use attached files:
- branch-context.txt
- changed-files.txt
- push.diff
- commits.txt
- AGENTS.md

Goal:
- Map current branch to feature/PRD scope.
- Check Beads tasks for that scope.
- Fix status with `br` commands when clearly needed.
- Keep partially completed parent tiers (`epic`, `feature`, `prd`) in `in_progress` while child work remains.
- Do not close `feature`/`epic` unless documented child roster in parent docs is fully represented in `br` `parent-child` edges.
- If unresolved changes remain, write a root markdown report to `report_file` from branch-context.txt.

Skip rules (do NOT write report when ALL apply):
- Matched scope is "none"
- Changed files are clearly one-off infrequent changes: toolchain version pins (mise.toml, package.json, Cargo.toml), README typos, trivial docs tweaks
- No production code changes involved

Only allowed writes:
- Beads updates through `br` commands.
- The report markdown file when unresolved actions remain.
"""

PR_CREATE_PROMPT = """You are running inside a git pre-push hook and must create a GitHub PR only when needed.

Use attached files:
- branch-context.txt
- changed-files.txt
- push.diff
- commits.txt
- AGENTS.md
- .codex/prompts/pr.md

Follow the workflow from `.codex/prompts/pr.md` and adapt for pre-push:
1) Use current branch from git; stop if `main`.
2) Check for existing open PR for current branch first. If one exists, output URL and stop.
3) If no PR exists yet, push current branch with upstream using:
   `AI_DOC_SYNC_SKIP=1 git push -u <remote_name> <current_branch>`
   - `<remote_name>` comes from branch-context.txt (fallback `origin`).
4) Detect PRD/feature/epic context from docs under docs/epics.
5) Build PR title/body using mandatory structure from `.codex/prompts/pr.md`.
6) Create PR with gh against base branch from branch-context.txt.
7) Print final PR URL and number.

Rules:
- Do not edit repository files.
- Use only non-interactive gh commands.
- Be concise.
"""

SYNC_PROMPT = """You are the single orchestrator agent for the AI docs sync pre-push hook.

This run MUST complete all required work in one pass. Do not stop early.

Tooling discipline:
- Use the TodoWrite tool immediately and keep it updated as you progress.
- Keep exactly one task in progress at a time.
- Continue until every required task is complete or you hit a hard blocker.

Attached context files:
- branch-context.txt
- changed-files.txt
- push.diff
- commits.txt
- docs-inventory.txt
- docs-context.txt
- recent-doc-commits.txt
- AGENTS.md
- .codex/prompts/pr.md (when present)

Execution order (strict):
1) Read branch-context.txt and initialize a todo list for all steps below.
2) Beads status alignment:
   - Detect PRD/feature scope from docs/epics and branch context.
   - Use br commands to inspect and align task statuses when safe.
   - Keep partially completed parent tiers (`epic`, `feature`, `prd`) at `in_progress` while child work remains.
   - Before closing parent tiers, verify documented child roster in parent docs (`INDEX.md` tables/lists) is represented in `br` `parent-child` edges.
   - If documented children are missing in `br`, do not close parent; emit report_file with exact `br create`/`br dep` recovery commands.
   - If unresolved status changes remain, write report_file from branch-context.txt as:
     # Beads Status Alignment Required
   - Include branch, matched scope, and exact br commands needed.
3) Documentation drift handling:
   - Evaluate push.diff and docs-context.txt for factual drift only.
   - Follow mode from branch-context.txt:
     - apply-and-block: apply minimal Markdown fixes in README.md and docs/** only.
     - check-only: do not edit docs; only report drift in final JSON.
   - Never edit code, scripts, lockfiles, config, or non-doc assets.
   - If adding/moving docs, update relevant docs/**/INDEX.md entries.
4) PR handling (only if create_pr_requested=true in branch-context.txt):
   - Check if open PR already exists for current branch.
   - If missing, create PR with gh against base_branch from branch-context.txt.
   - Follow .codex/prompts/pr.md when PRD context is available.
   - If PRD context is missing, still create best-effort PR.
   - Never run nested git push from this hook.
5) Mark all todo items complete and emit final machine-readable summary.

Safety rules:
- Allowed writes: docs markdown edits, Beads status updates via br, and report_file when unresolved.
- Disallowed writes: code/config/script/lockfile/non-doc asset changes.
- Be concise; avoid unrelated operations.

Return EXACTLY one JSON object and no other text:
{
  "beads_report_written": boolean,
  "docs_drift_detected": boolean,
  "docs_edits_applied": boolean,
  "pr_requested": boolean,
  "pr_url": "string-or-empty"
}

Do NOT include markdown sections, headings (for example `## Goal`), explanations, or code fences.
Emit only the raw JSON object as the complete response body.
"""


class HookError(RuntimeError):
    pass


def parse_toml_fallback(raw: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    current: dict[str, Any] = parsed
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            current = parsed.setdefault(section_name, {})
            if not isinstance(current, dict):
                raise HookError(f"Invalid TOML section: [{section_name}]")
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        current[key] = parse_toml_value(value)
    return parsed


def parse_toml_value(raw: str) -> Any:
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw in {"true", "false"}:
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HookError(f"Invalid TOML list value: {raw}") from exc
    return raw


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


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


def normalize_execution_mode(raw: str) -> str:
    normalized = raw.strip().lower()
    if normalized in {"multi", "single"}:
        return normalized
    raise HookError(f"Invalid execution mode value: {raw}. Expected one of: multi, single.")


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


def resolve_storage_path(repo_root: pathlib.Path, git_dir: pathlib.Path, raw: str) -> pathlib.Path:
    path = pathlib.Path(raw)
    if path.is_absolute():
        return path
    posix_raw = raw.replace("\\", "/")
    if posix_raw == ".git":
        return git_dir
    if posix_raw.startswith(".git/"):
        suffix = posix_raw[len(".git/") :]
        return git_dir / suffix
    return repo_root / path


def resolve_repo_path(repo_root: pathlib.Path, raw: str) -> pathlib.Path:
    path = pathlib.Path(raw)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def load_prompt_from_file(
    repo_root: pathlib.Path,
    raw_path: str,
    fallback: str,
    logger: HookLogger,
    prompt_name: str,
) -> str:
    if not raw_path.strip():
        return fallback
    path = resolve_repo_path(repo_root, raw_path)
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warn(
            "prompt.read_failed",
            f"Failed to read {prompt_name} prompt file; using fallback prompt",
            path=str(path),
            error=str(exc),
        )
        return fallback
    if not text.strip():
        logger.warn(
            "prompt.empty",
            f"{prompt_name} prompt file was empty; using fallback prompt",
            path=str(path),
        )
        return fallback
    return text


def ensure_dir(path: pathlib.Path) -> pathlib.Path | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:  # noqa: BLE001
        return None


def should_skip_for_beads_sync(repo_root: pathlib.Path, sync_branch: str) -> tuple[bool, str]:
    root_posix = repo_root.as_posix()
    if "/.beads-sync-worktrees/" in root_posix:
        return True, "worktree is inside .beads-sync-worktrees"

    current_branch = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if current_branch == sync_branch:
        return True, f"current branch is {sync_branch}"

    return False, ""


def write_text_file(path: pathlib.Path, content: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def collect_commit_messages_for_ranges(
    repo_root: pathlib.Path, ranges: list[str]
) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for range_expr in ranges:
        raw = git(repo_root, ["log", "--format=%H%x1f%s%x1f%b%x1e", range_expr], check=True)
        for record in raw.split("\x1e"):
            payload = record.strip()
            if not payload:
                continue
            parts = payload.split("\x1f", 2)
            if len(parts) < 3:
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


def lookup_open_pr_url(repo_root: pathlib.Path, branch_name: str) -> str:
    completed = run_command(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch_name,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "url",
        ],
        cwd=repo_root,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        raise HookError(details or "`gh pr list` failed")

    payload = (completed.stdout or "").strip()
    if not payload:
        return ""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HookError("Failed to parse `gh pr list` JSON output") from exc
    if not isinstance(parsed, list) or not parsed:
        return ""

    first = parsed[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("url", "")).strip()


def extract_pr_url(text: str) -> str:
    match = re.search(r"https://github\.com/[^\s]+/pull/\d+", text)
    if not match:
        return ""
    return match.group(0).strip()


def sanitize_pr_title(raw_title: str, branch_name: str) -> str:
    title = re.sub(r"\s+", " ", raw_title).strip()
    if not title:
        title = branch_name
    return title[:240]


def build_fallback_pr_body(
    branch_name: str,
    ranges: list[str],
    changed_files: list[str],
    commits: list[dict[str, str]],
) -> str:
    body: list[str] = [
        "## Summary",
        f"- Auto-created by `mise pr` fallback flow for branch `{branch_name}`.",
    ]
    if ranges:
        body.append(f"- Push range: `{', '.join(ranges)}`.")
    if commits:
        body.append("")
        body.append("## Commits")
        for commit in commits[:8]:
            subject = str(commit.get("subject", "")).strip()
            if not subject:
                continue
            body.append(f"- {subject}")
    if changed_files:
        body.append("")
        body.append("## Changed Files")
        for path in changed_files[:15]:
            body.append(f"- `{path}`")
        if len(changed_files) > 15:
            body.append(f"- and {len(changed_files) - 15} more")
    return "\n".join(body).strip() + "\n"


def attempt_pr_creation_fallback(
    repo_root: pathlib.Path,
    branch_name: str,
    base_branch: str,
    ranges: list[str],
    changed_files: list[str],
    commits: list[dict[str, str]],
) -> str:
    latest_subject = git(repo_root, ["log", "-1", "--pretty=%s"], check=False)
    title = sanitize_pr_title(latest_subject, branch_name)
    body = build_fallback_pr_body(branch_name, ranges, changed_files, commits)
    created = run_command(
        [
            "gh",
            "pr",
            "create",
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

    existing_pr = lookup_open_pr_url(repo_root, branch_name)
    if existing_pr:
        return existing_pr

    details = combined_output.strip() or f"exit code {created.returncode}"
    raise HookError(f"Fallback PR creation failed: {details}")


def remote_branch_exists(repo_root: pathlib.Path, remote_name: str, branch_name: str) -> bool:
    resolved_remote = remote_name.strip() or "origin"
    completed = run_command(
        ["git", "ls-remote", "--heads", resolved_remote, branch_name],
        cwd=repo_root,
        check=False,
    )
    if completed.returncode != 0:
        return False
    return bool((completed.stdout or "").strip())


def run_pr_creation_gate(
    repo_root: pathlib.Path,
    remote_name: str,
    remote_url: str,
    ranges: list[str],
    changed_files: list[str],
    diff_text: str,
    logger: HookLogger,
    config: dict[str, Any],
    git_dir: pathlib.Path,
    run_id: str,
    opencode_executable: str,
) -> tuple[bool, str]:
    pr_cfg = config.get("pr", {})
    if not bool(pr_cfg.get("enabled", True)):
        logger.debug("pr.disabled", "PR auto-create gate disabled")
        return True, ""

    flag_env_name = str(pr_cfg.get("flag_env", "AI_DOC_SYNC_CREATE_PR")).strip()
    if not flag_env_name:
        flag_env_name = "AI_DOC_SYNC_CREATE_PR"
    if env_bool(flag_env_name) is not True:
        logger.debug(
            "pr.flag_not_set",
            "PR auto-create gate skipped; flag not enabled",
            flag_env=flag_env_name,
        )
        return True, ""

    branch_name = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
    sync_branch = os.getenv("BEADS_SYNC_BRANCH", "beads-sync")
    if not branch_name or branch_name in {"HEAD", "main", sync_branch}:
        logger.debug(
            "pr.skip_branch",
            "PR auto-create gate skipped for branch",
            branch=branch_name or "<unknown>",
        )
        return True, ""

    if not branch_name.startswith(FEATURE_BRANCH_PREFIXES):
        logger.debug(
            "pr.skip_non_feature_branch",
            "PR auto-create gate skipped for non-feature branch",
            branch=branch_name,
        )
        return True, ""

    if shutil.which("gh") is None:
        return False, "`gh` is required for PR auto-create but is not installed"

    try:
        existing_pr_url = lookup_open_pr_url(repo_root, branch_name)
        if existing_pr_url:
            logger.status(
                "pr.already_exists",
                "Open PR already exists for current branch",
                branch=branch_name,
                url=existing_pr_url,
            )
            return True, ""

        baseline = set(
            line.strip()
            for line in git(repo_root, ["diff", "--name-only"], check=True).splitlines()
            if line.strip()
        )
        prompts_cfg = config.get("prompts", {})
        prompt = load_prompt_from_file(
            repo_root=repo_root,
            raw_path=str(prompts_cfg.get("pr_create_file", "")),
            fallback=PR_CREATE_PROMPT,
            logger=logger,
            prompt_name="create-pr",
        )

        commits = collect_commit_messages_for_ranges(repo_root, ranges)
        max_diff_bytes = int(config["llm"].get("max_diff_bytes", 180000))
        resolved_remote_name = (remote_name or "origin").strip() or "origin"
        base_branch = str(pr_cfg.get("base_branch", "main")).strip() or "main"

        stage_error: str | None = None
        with tempfile.TemporaryDirectory(prefix="ai-doc-sync-pr.") as tmp_dir_raw:
            tmp_dir = pathlib.Path(tmp_dir_raw)
            branch_context_file = tmp_dir / "branch-context.txt"
            changed_files_file = tmp_dir / "changed-files.txt"
            diff_file = tmp_dir / "push.diff"
            commits_file = tmp_dir / "commits.txt"

            branch_context_file.write_text(
                "\n".join(
                    [
                        f"branch={branch_name}",
                        f"remote_name={resolved_remote_name}",
                        f"remote_url={remote_url}",
                        f"base_branch={base_branch}",
                        f"push_flag_env={flag_env_name}",
                        f"ranges={','.join(ranges)}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            changed_files_file.write_text("\n".join(changed_files) + "\n", encoding="utf-8")
            diff_file.write_text(diff_text[:max_diff_bytes], encoding="utf-8")

            commit_lines: list[str] = []
            for commit in commits:
                commit_lines.append(f"--- {commit.get('hash', '')}")
                commit_lines.append(f"subject: {commit.get('subject', '')}")
                body = str(commit.get("body", "")).strip()
                if body:
                    commit_lines.append("body:")
                    commit_lines.append(body)
                commit_lines.append("")
            commits_file.write_text("\n".join(commit_lines).strip() + "\n", encoding="utf-8")

            transcript_dir: pathlib.Path | None = None
            if bool(config.get("logging", {}).get("capture_llm_transcript", False)):
                transcript_dir = ensure_dir(
                    resolve_storage_path(
                        repo_root,
                        git_dir,
                        str(
                            config.get("logging", {}).get(
                                "transcript_dir", ".git/ai-doc-sync/transcripts"
                            )
                        ),
                    )
                )

            files = [
                branch_context_file,
                changed_files_file,
                diff_file,
                commits_file,
                repo_root / "AGENTS.md",
            ]
            codex_pr_prompt = repo_root / ".codex/prompts/pr.md"
            if codex_pr_prompt.exists():
                files.append(codex_pr_prompt)

            try:
                result = call_opencode(
                    repo_root=repo_root,
                    opencode_executable=opencode_executable,
                    model=str(config["llm"].get("model", "openai/gpt-5.3-codex-spark")).strip(),
                    variant=str(config["llm"].get("variant", "")).strip(),
                    timeout_seconds=int(config["llm"].get("timeout_seconds", 800)),
                    prompt=prompt,
                    files=files,
                    logger=logger,
                    stage_name="99-create-pr",
                    run_id=run_id,
                    session_title_prefix=str(
                        config["llm"].get("session_title_prefix", "ai-doc-sync")
                    ),
                    call_purpose=stage_purpose("99-create-pr"),
                    print_output=(
                        bool(config.get("logging", {}).get("print_llm_output", False))
                        and str(config.get("logging", {}).get("level", "status")).strip().lower()
                        == "debug"
                    ),
                )
                finalize_opencode_session(
                    repo_root=repo_root,
                    logger=logger,
                    timeout_seconds=int(config["llm"].get("timeout_seconds", 800)),
                    run_id=run_id,
                    stage_name="99-create-pr",
                    session_id=result.session_id,
                    transcript_dir=transcript_dir,
                    delete_session_after_run=bool(
                        config["llm"].get("delete_session_after_run", True)
                    ),
                    opencode_executable=opencode_executable,
                )
                if result.return_code != 0:
                    stage_error = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"exit code {result.return_code}"
                    )
                    logger.warn(
                        "pr.stage_failed_fallback",
                        "PR creation stage failed; attempting deterministic fallback",
                        branch=branch_name,
                        error=stage_error,
                    )
            except Exception as stage_exc:  # noqa: BLE001
                stage_error = str(stage_exc).strip() or stage_exc.__class__.__name__
                logger.warn(
                    "pr.stage_failed_fallback",
                    "PR creation stage errored; attempting deterministic fallback",
                    branch=branch_name,
                    error=stage_error,
                )

        post_changes = set(
            line.strip()
            for line in git(repo_root, ["diff", "--name-only"], check=True).splitlines()
            if line.strip()
        )
        new_changes = sorted(post_changes - baseline)
        if new_changes:
            raise HookError("PR creation stage modified tracked files: " + ", ".join(new_changes))

        fallback_error: str | None = None
        pr_url = lookup_open_pr_url(repo_root, branch_name)
        if not pr_url:
            logger.warn(
                "pr.no_open_after_stage",
                "No open PR found after LLM stage; attempting deterministic fallback",
                branch=branch_name,
            )
            try:
                pr_url = attempt_pr_creation_fallback(
                    repo_root=repo_root,
                    branch_name=branch_name,
                    base_branch=base_branch,
                    ranges=ranges,
                    changed_files=changed_files,
                    commits=commits,
                )
            except Exception as fallback_exc:  # noqa: BLE001
                fallback_error = str(fallback_exc).strip() or fallback_exc.__class__.__name__
                logger.warn(
                    "pr.fallback_failed",
                    "Deterministic fallback failed",
                    branch=branch_name,
                    error=fallback_error,
                )
        if not pr_url:
            if not remote_branch_exists(repo_root, resolved_remote_name, branch_name):
                logger.status(
                    "pr.deferred_until_remote_branch",
                    "PR creation deferred until branch exists on remote; allowing push",
                    branch=branch_name,
                )
                return True, ""
            if stage_error and fallback_error:
                raise HookError(
                    "No open PR found for branch after PR creation stage and fallback failed "
                    f"(stage error: {stage_error}; fallback error: {fallback_error})"
                )
            if stage_error:
                raise HookError(
                    "No open PR found for branch after PR creation stage and fallback failed "
                    f"(stage error: {stage_error})"
                )
            if fallback_error:
                raise HookError(
                    "No open PR found for branch after PR creation stage and fallback failed "
                    f"(fallback error: {fallback_error})"
                )
            raise HookError("No open PR found for branch after PR creation stage")
        logger.status(
            "pr.created",
            "Created open PR for current branch",
            branch=branch_name,
            url=pr_url,
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        message = str(exc).strip() or exc.__class__.__name__
        logger.error(
            "pr.gate_failed",
            "PR auto-create gate failed",
            branch=branch_name,
            error=message,
        )
        return False, f"PR auto-create failed: {message}"


def ensure_pr_created_for_push(
    repo_root: pathlib.Path,
    remote_name: str,
    ranges: list[str],
    changed_files: list[str],
    logger: HookLogger,
    config: dict[str, Any],
) -> tuple[bool, str]:
    pr_cfg = config.get("pr", {})
    if not bool(pr_cfg.get("enabled", True)):
        return True, ""

    flag_env_name = str(pr_cfg.get("flag_env", "AI_DOC_SYNC_CREATE_PR")).strip()
    if not flag_env_name:
        flag_env_name = "AI_DOC_SYNC_CREATE_PR"
    if env_bool(flag_env_name) is not True:
        return True, ""

    branch_name = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
    sync_branch = os.getenv("BEADS_SYNC_BRANCH", "beads-sync")
    if not branch_name or branch_name in {"HEAD", "main", sync_branch}:
        return True, ""
    if not branch_name.startswith(FEATURE_BRANCH_PREFIXES):
        return True, ""

    if shutil.which("gh") is None:
        return False, "`gh` is required for PR auto-create but is not installed"

    resolved_remote_name = (remote_name or "origin").strip() or "origin"
    base_branch = str(pr_cfg.get("base_branch", "main")).strip() or "main"

    try:
        existing_pr_url = lookup_open_pr_url(repo_root, branch_name)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).strip() or exc.__class__.__name__
        logger.error(
            "pr.gate_failed",
            "PR auto-create fallback failed",
            branch=branch_name,
            error=message,
        )
        return False, f"PR auto-create failed: {message}"

    if existing_pr_url:
        logger.status(
            "pr.already_exists",
            "Open PR already exists for current branch",
            branch=branch_name,
            url=existing_pr_url,
        )
        return True, ""

    commits = collect_commit_messages_for_ranges(repo_root, ranges) if ranges else []
    try:
        pr_url = attempt_pr_creation_fallback(
            repo_root=repo_root,
            branch_name=branch_name,
            base_branch=base_branch,
            ranges=ranges,
            changed_files=changed_files,
            commits=commits,
        )
    except Exception as exc:  # noqa: BLE001
        if not remote_branch_exists(repo_root, resolved_remote_name, branch_name):
            logger.status(
                "pr.deferred_until_remote_branch",
                "PR creation deferred until branch exists on remote; allowing push",
                branch=branch_name,
            )
            return True, ""
        message = str(exc).strip() or exc.__class__.__name__
        logger.error(
            "pr.gate_failed",
            "PR auto-create fallback failed",
            branch=branch_name,
            error=message,
        )
        return False, f"PR auto-create failed: {message}"

    logger.status(
        "pr.created",
        "Created open PR for current branch",
        branch=branch_name,
        url=pr_url,
    )
    return True, ""


def run_unified_sync_stage(
    repo_root: pathlib.Path,
    ranges: list[str],
    changed_files: list[str],
    diff_text: str,
    docs_files: list[pathlib.Path],
    logger: HookLogger,
    config: dict[str, Any],
    git_dir: pathlib.Path,
    run_id: str,
    opencode_executable: str,
    remote_name: str,
    remote_url: str,
) -> dict[str, Any]:
    branch_name = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
    pr_cfg = config.get("pr", {})
    beads_cfg = config.get("beads", {})

    report_file_raw = str(beads_cfg.get("report_file", "BEADS_STATUS_ACTION_REQUIRED.md")).strip()
    report_file_path = repo_root / report_file_raw
    try:
        report_rel = report_file_path.relative_to(repo_root).as_posix()
    except Exception:  # noqa: BLE001
        report_rel = report_file_raw

    flag_env_name = str(pr_cfg.get("flag_env", "AI_DOC_SYNC_CREATE_PR")).strip()
    if not flag_env_name:
        flag_env_name = "AI_DOC_SYNC_CREATE_PR"
    base_branch = str(pr_cfg.get("base_branch", "main")).strip() or "main"
    create_pr_requested = env_bool(flag_env_name) is True and bool(pr_cfg.get("enabled", True))

    prompts_cfg = config.get("prompts", {})
    prompt_template = load_prompt_from_file(
        repo_root=repo_root,
        raw_path=str(prompts_cfg.get("sync_file", "")),
        fallback=SYNC_PROMPT,
        logger=logger,
        prompt_name="sync",
    )

    commits = collect_commit_messages_for_ranges(repo_root, ranges)
    max_diff_bytes = int(config["llm"].get("max_diff_bytes", 180000))
    mode = str(config["general"].get("mode", "apply-and-block")).strip() or "apply-and-block"

    with tempfile.TemporaryDirectory(prefix="ai-doc-sync-unified.") as tmp_dir_raw:
        tmp_dir = pathlib.Path(tmp_dir_raw)
        branch_context_file = tmp_dir / "branch-context.txt"
        changed_files_file = tmp_dir / "changed-files.txt"
        diff_file = tmp_dir / "push.diff"
        commits_file = tmp_dir / "commits.txt"
        docs_inventory_file = tmp_dir / "docs-inventory.txt"
        docs_context_file = tmp_dir / "docs-context.txt"
        recent_doc_commits_file = tmp_dir / "recent-doc-commits.txt"

        branch_context_lines = [
            f"branch={branch_name}",
            f"remote_name={(remote_name or 'origin').strip() or 'origin'}",
            f"remote_url={remote_url}",
            f"base_branch={base_branch}",
            f"ranges={','.join(ranges)}",
            f"mode={mode}",
            f"report_file={report_rel}",
            f"create_pr_requested={'true' if create_pr_requested else 'false'}",
            f"create_pr_flag_env={flag_env_name}",
        ]
        branch_context_file.write_text("\n".join(branch_context_lines) + "\n", encoding="utf-8")
        changed_files_file.write_text("\n".join(changed_files) + "\n", encoding="utf-8")
        diff_file.write_text(diff_text[:max_diff_bytes], encoding="utf-8")

        commit_lines: list[str] = []
        for commit in commits:
            commit_lines.append(f"--- {commit.get('hash', '')}")
            commit_lines.append(f"subject: {commit.get('subject', '')}")
            body = str(commit.get("body", "")).strip()
            if body:
                commit_lines.append("body:")
                commit_lines.append(body)
            commit_lines.append("")
        commits_file.write_text("\n".join(commit_lines).strip() + "\n", encoding="utf-8")

        docs_inventory_file.write_text(
            "\n".join(path.relative_to(repo_root).as_posix() for path in docs_files) + "\n",
            encoding="utf-8",
        )

        docs_context = ""
        if docs_files:
            queries = deterministic_seed_queries(diff_text, changed_files)[:40]
            if queries and shutil.which("rg") is not None:
                try:
                    chunks = search_docs_with_queries(repo_root, queries, docs_files, logger)
                    chunks = truncate_chunks_to_budget(
                        chunks, int(config["docs"].get("max_context_tokens", 8000))
                    )
                    docs_context = "\n\n".join(
                        (
                            f"--- {chunk['file']} "
                            f"(lines {chunk['start_line']}-{chunk['end_line']}) ---\n"
                            f"{chunk['content']}"
                        )
                        for chunk in chunks
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warn(
                        "sync.docs_context_failed",
                        "Failed to build docs context snapshot; continuing",
                        error=str(exc),
                    )
        docs_context_file.write_text(
            docs_context + ("\n" if docs_context else ""), encoding="utf-8"
        )

        recent_doc_commits = git(
            repo_root,
            ["log", "--oneline", "-n", "20", "--", "docs", "README.md"],
            check=False,
        )
        recent_doc_commits_file.write_text(recent_doc_commits + "\n", encoding="utf-8")

        transcript_dir: pathlib.Path | None = None
        if bool(config.get("logging", {}).get("capture_llm_transcript", False)):
            raw_transcript_dir = resolve_storage_path(
                repo_root,
                git_dir,
                str(
                    config.get("logging", {}).get("transcript_dir", ".git/ai-doc-sync/transcripts")
                ),
            )
            transcript_dir = ensure_dir(raw_transcript_dir)
            if transcript_dir is None:
                logger.warn(
                    "llm.transcript_dir_unavailable",
                    "Session export directory unavailable; continuing without session export capture",
                    path=str(raw_transcript_dir),
                )

        runtime_block = "\n".join(
            [
                "Runtime context:",
                f"- branch: {branch_name}",
                f"- mode: {mode}",
                f"- create_pr_requested: {'true' if create_pr_requested else 'false'}",
                f"- report_file: {report_rel}",
                f"- changed_files_count: {len(changed_files)}",
                f"- docs_inventory_count: {len(docs_files)}",
            ]
        )
        prompt = prompt_template.strip() + "\n\n" + runtime_block + "\n"

        files = [
            branch_context_file,
            changed_files_file,
            diff_file,
            commits_file,
            docs_inventory_file,
            docs_context_file,
            recent_doc_commits_file,
            repo_root / "AGENTS.md",
        ]
        codex_pr_prompt = repo_root / ".codex/prompts/pr.md"
        if codex_pr_prompt.exists():
            files.append(codex_pr_prompt)

        print_llm_output = (
            bool(config.get("logging", {}).get("print_llm_output", False))
            and str(config.get("logging", {}).get("level", "status")).strip().lower() == "debug"
        )
        try:
            summary = call_opencode_json_object_with_retries(
                repo_root=repo_root,
                opencode_executable=opencode_executable,
                model=str(config["llm"].get("model", "openai/gpt-5.3-codex-spark")).strip(),
                variant=str(config["llm"].get("variant", "")).strip(),
                timeout_seconds=int(config["llm"].get("timeout_seconds", 800)),
                base_prompt=prompt,
                files=files,
                logger=logger,
                stage_name="01-sync",
                max_retries=int(config["llm"].get("json_max_retries", 2)),
                invalid_json_feedback_max_chars=int(
                    config["llm"].get("invalid_json_feedback_max_chars", 6000)
                ),
                json_retry_new_session=bool(config["llm"].get("json_retry_new_session", True)),
                transcript_dir=transcript_dir,
                run_id=run_id,
                session_title_prefix=str(config["llm"].get("session_title_prefix", "ai-doc-sync")),
                delete_session_after_run=bool(config["llm"].get("delete_session_after_run", True)),
                print_output=print_llm_output,
            )
        except HookError as exc:
            logger.info(
                "sync.summary_parse_failed",
                "Unified sync stage returned non-JSON summary",
                error=str(exc),
            )
            pr_url = extract_pr_url(str(exc))
            if pr_url:
                logger.info(
                    "sync.summary_parse_failed_pr_url_hint",
                    "Detected PR URL in non-JSON unified summary",
                    url=pr_url,
                )
            raise HookError("Unified sync stage returned invalid or empty JSON summary") from exc

        if not summary:
            logger.warn(
                "sync.summary_parse_failed",
                "Unified sync stage returned non-JSON summary",
                error="Empty JSON object returned",
            )
            pr_url = extract_pr_url(json.dumps(summary, ensure_ascii=True))
            if pr_url:
                logger.info(
                    "sync.summary_parse_failed_pr_url_hint",
                    "Detected PR URL in non-JSON unified summary",
                    url=pr_url,
                )
            raise HookError("Unified sync stage returned invalid or empty JSON summary")
        return summary


def run_beads_status_alignment_gate(
    repo_root: pathlib.Path,
    ranges: list[str],
    changed_files: list[str],
    diff_text: str,
    logger: HookLogger,
    config: dict[str, Any],
    git_dir: pathlib.Path,
    run_id: str,
    opencode_executable: str,
) -> tuple[bool, str]:
    beads_cfg = config.get("beads", {})
    if not bool(beads_cfg.get("enabled", True)):
        logger.debug("beads.disabled", "Beads status alignment gate disabled")
        return True, ""

    branch_name = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
    sync_branch = os.getenv("BEADS_SYNC_BRANCH", "beads-sync")
    if not branch_name or branch_name in {"HEAD", "main", sync_branch}:
        logger.debug(
            "beads.skip_branch",
            "Skipping Beads status alignment for branch",
            branch=branch_name or "<unknown>",
        )
        return True, ""

    if not branch_name.startswith(FEATURE_BRANCH_PREFIXES):
        logger.debug(
            "beads.skip_non_feature_branch",
            "Skipping Beads status alignment for non-feature branch",
            branch=branch_name,
        )
        return True, ""

    report_file_raw = str(beads_cfg.get("report_file", "BEADS_STATUS_ACTION_REQUIRED.md")).strip()
    report_file_path = repo_root / report_file_raw

    try:
        baseline = set(
            line.strip()
            for line in git(repo_root, ["diff", "--name-only"], check=True).splitlines()
            if line.strip()
        )
        report_rel = report_file_path.relative_to(repo_root).as_posix()

        prompts_cfg = config.get("prompts", {})
        prompt = load_prompt_from_file(
            repo_root=repo_root,
            raw_path=str(prompts_cfg.get("beads_status_file", "")),
            fallback=BEADS_STATUS_PROMPT,
            logger=logger,
            prompt_name="beads-status",
        )

        commits = collect_commit_messages_for_ranges(repo_root, ranges)
        max_diff_bytes = int(config["llm"].get("max_diff_bytes", 180000))
        with tempfile.TemporaryDirectory(prefix="ai-doc-sync-beads.") as tmp_dir_raw:
            tmp_dir = pathlib.Path(tmp_dir_raw)
            branch_context_file = tmp_dir / "branch-context.txt"
            changed_files_file = tmp_dir / "changed-files.txt"
            diff_file = tmp_dir / "push.diff"
            commits_file = tmp_dir / "commits.txt"

            branch_context_file.write_text(
                "\n".join(
                    [
                        f"branch={branch_name}",
                        f"ranges={','.join(ranges)}",
                        f"report_file={report_rel}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            changed_files_file.write_text("\n".join(changed_files) + "\n", encoding="utf-8")
            diff_file.write_text(diff_text[:max_diff_bytes], encoding="utf-8")

            commit_lines: list[str] = []
            for commit in commits:
                commit_lines.append(f"--- {commit.get('hash', '')}")
                commit_lines.append(f"subject: {commit.get('subject', '')}")
                body = str(commit.get("body", "")).strip()
                if body:
                    commit_lines.append("body:")
                    commit_lines.append(body)
                commit_lines.append("")
            commits_file.write_text("\n".join(commit_lines).strip() + "\n", encoding="utf-8")

            transcript_dir: pathlib.Path | None = None
            if bool(config.get("logging", {}).get("capture_llm_transcript", False)):
                transcript_dir = ensure_dir(
                    resolve_storage_path(
                        repo_root,
                        git_dir,
                        str(
                            config.get("logging", {}).get(
                                "transcript_dir", ".git/ai-doc-sync/transcripts"
                            )
                        ),
                    )
                )

            result = call_opencode(
                repo_root=repo_root,
                opencode_executable=opencode_executable,
                model=str(config["llm"].get("model", "openai/gpt-5.3-codex-spark")).strip(),
                variant=str(config["llm"].get("variant", "")).strip(),
                timeout_seconds=int(config["llm"].get("timeout_seconds", 800)),
                prompt=prompt,
                files=[
                    branch_context_file,
                    changed_files_file,
                    diff_file,
                    commits_file,
                    repo_root / "AGENTS.md",
                ],
                logger=logger,
                stage_name="00-beads-status",
                run_id=run_id,
                session_title_prefix=str(config["llm"].get("session_title_prefix", "ai-doc-sync")),
                call_purpose=stage_purpose("00-beads-status"),
                print_output=(
                    bool(config.get("logging", {}).get("print_llm_output", False))
                    and str(config.get("logging", {}).get("level", "status")).strip().lower()
                    == "debug"
                ),
            )
            finalize_opencode_session(
                repo_root=repo_root,
                logger=logger,
                timeout_seconds=int(config["llm"].get("timeout_seconds", 800)),
                run_id=run_id,
                stage_name="00-beads-status",
                session_id=result.session_id,
                transcript_dir=transcript_dir,
                delete_session_after_run=bool(config["llm"].get("delete_session_after_run", True)),
                opencode_executable=opencode_executable,
            )
            if result.return_code != 0:
                details = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"exit code {result.return_code}"
                )
                raise HookError(f"Beads status stage failed: {details}")

        post_changes = set(
            line.strip()
            for line in git(repo_root, ["diff", "--name-only"], check=True).splitlines()
            if line.strip()
        )
        new_changes = sorted(post_changes - baseline)
        unexpected = [
            file_path
            for file_path in new_changes
            if file_path != report_rel and not file_path.startswith(".beads/")
        ]
        if unexpected:
            raise HookError(
                "Beads status stage modified unexpected files: " + ", ".join(unexpected)
            )

        if report_rel in new_changes:
            logger.error(
                "beads.alignment_report",
                "Beads status alignment requires manual action; blocking push",
                report=str(report_file_path),
            )
            return False, f"Beads status alignment requires manual action. See {report_file_path}"

        if report_file_path.exists():
            try:
                report_file_path.unlink()
                logger.info(
                    "beads.alignment_report_cleared",
                    "Removed stale Beads alignment report after successful alignment",
                    report=str(report_file_path),
                )
            except Exception as remove_exc:  # noqa: BLE001
                logger.warn(
                    "beads.alignment_report_clear_failed",
                    "Failed to remove stale Beads alignment report",
                    report=str(report_file_path),
                    error=str(remove_exc),
                )

        logger.debug(
            "beads.alignment_ok",
            "Beads status alignment check passed",
            branch=branch_name,
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        message = str(exc).strip() or exc.__class__.__name__
        report = "\n".join(
            [
                "# Beads Status Alignment Required",
                "",
                "## Context",
                f"- Branch: `{branch_name}`",
                f"- Push ranges: `{', '.join(ranges) if ranges else '<none>'}`",
                "",
                "## Blocking reason",
                f"- {message}",
                "",
                "## Next step",
                "- Re-run push after resolving the issue above.",
                "",
            ]
        )
        write_text_file(report_file_path, report)
        logger.error(
            "beads.alignment_exception",
            "Beads status alignment gate errored; blocking push",
            error=message,
            report=str(report_file_path),
        )
        return False, f"Beads status alignment error: {message}. See {report_file_path}"


@dataclass
class HookLogger:
    jsonl_path: pathlib.Path | None
    console_level: str = "status"
    jsonl_write_failed: bool = False
    llm_calls: list[dict[str, Any]] = field(default_factory=list)

    _verbosity_order = {"status": 0, "info": 1, "debug": 2}

    def _level_is_enabled(self, level: str) -> bool:
        if level in {"warn", "error"}:
            return True
        configured = self._verbosity_order.get(self.console_level, 0)
        required = self._verbosity_order.get(level, 0)
        return configured >= required

    def _emit(self, level: str, event: str, message: str, **fields: Any) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        if not self._level_is_enabled(level):
            return
        sys.stderr.write(f"[ai-doc-sync] {message}\n")
        if self.jsonl_path is None or self.jsonl_write_failed:
            return
        record = {"ts": stamp, "level": level, "event": event, "message": message, **fields}
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        except Exception as exc:  # noqa: BLE001
            self.jsonl_write_failed = True
            sys.stderr.write(f"[ai-doc-sync] JSONL logging disabled after write failure: {exc}\n")

    def info(self, event: str, message: str, **fields: Any) -> None:
        self._emit("info", event, message, **fields)

    def status(self, event: str, message: str, **fields: Any) -> None:
        self._emit("status", event, message, **fields)

    def debug(self, event: str, message: str, **fields: Any) -> None:
        self._emit("debug", event, message, **fields)

    def warn(self, event: str, message: str, **fields: Any) -> None:
        self._emit("warn", event, message, **fields)

    def error(self, event: str, message: str, **fields: Any) -> None:
        self._emit("error", event, message, **fields)

    def llm_call(
        self,
        stage_name: str,
        purpose: str,
        model: str,
        attempt: int | None = None,
        total_attempts: int | None = None,
    ) -> None:
        call_number = len(self.llm_calls) + 1
        record: dict[str, Any] = {
            "call_number": call_number,
            "stage_name": stage_name,
            "purpose": purpose,
            "model": model,
        }
        if attempt is not None:
            record["attempt"] = attempt
        if total_attempts is not None:
            record["total_attempts"] = total_attempts
        self.llm_calls.append(record)
        self.status(
            "llm.call",
            f"LLM call #{call_number}: {stage_name} - {purpose}",
            **record,
        )

    def llm_summary(self) -> None:
        stage_counts: dict[str, int] = {}
        for call in self.llm_calls:
            stage_name = str(call.get("stage_name", "")).strip() or "<unknown>"
            stage_counts[stage_name] = stage_counts.get(stage_name, 0) + 1
        total_calls = len(self.llm_calls)
        self.status(
            "llm.calls_total",
            f"Total LLM calls this run: {total_calls}",
            total_calls=total_calls,
            stage_counts=stage_counts,
        )


@dataclass
class OpenCodeRunResult:
    output_text: str
    session_id: str | None
    stdout: str
    stderr: str
    return_code: int


def load_config(repo_root: pathlib.Path) -> tuple[dict[str, Any], pathlib.Path | None]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_path = None
    for candidate in [repo_root / ".ai-doc-sync.toml", repo_root / "ai-doc-sync.toml"]:
        if candidate.exists():
            if tomllib is not None:
                with candidate.open("rb") as handle:
                    loaded = tomllib.load(handle)
            else:
                loaded = parse_toml_fallback(candidate.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise HookError(f"Invalid config format in {candidate}")
            config = deep_merge(config, loaded)
            config_path = candidate
            break

    skip = env_bool("AI_DOC_SYNC_SKIP")
    if skip is True:
        config["general"]["enabled"] = False

    allow_on_error = env_bool("AI_DOC_SYNC_ALLOW_PUSH_ON_ERROR")
    if allow_on_error is not None:
        config["general"]["allow_push_on_error"] = allow_on_error

    require_clean = env_bool("AI_DOC_SYNC_REQUIRE_CLEAN")
    if require_clean is not None:
        config["general"]["require_clean_worktree"] = require_clean

    allow_dirty = env_bool("AI_DOC_SYNC_ALLOW_DIRTY")
    if allow_dirty is True:
        config["general"]["require_clean_worktree"] = False

    execution_mode = os.getenv("AI_DOC_SYNC_EXECUTION_MODE")
    if execution_mode is not None:
        config["general"]["execution_mode"] = execution_mode

    config["general"]["execution_mode"] = normalize_execution_mode(
        str(config["general"].get("execution_mode", "multi"))
    )

    logging_level = os.getenv("AI_DOC_SYNC_LOG_LEVEL")
    if logging_level:
        normalized = logging_level.strip().lower()
        if normalized not in {"status", "info", "debug"}:
            raise HookError(
                f"Invalid AI_DOC_SYNC_LOG_LEVEL value: {logging_level}. "
                "Expected one of: status, info, debug."
            )
        config["logging"]["level"] = normalized

    print_llm_output = env_bool("AI_DOC_SYNC_PRINT_LLM_OUTPUT")
    if print_llm_output is not None:
        config["logging"]["print_llm_output"] = print_llm_output

    model = os.getenv("AI_DOC_SYNC_MODEL")
    if model:
        config["llm"]["model"] = model

    variant = os.getenv("AI_DOC_SYNC_VARIANT")
    if variant is not None:
        config["llm"]["variant"] = variant.strip()

    max_diff = os.getenv("AI_DOC_SYNC_MAX_DIFF_BYTES")
    if max_diff:
        try:
            config["llm"]["max_diff_bytes"] = int(max_diff)
        except ValueError:
            raise HookError(f"Invalid AI_DOC_SYNC_MAX_DIFF_BYTES value: {max_diff}")

    timeout_seconds = os.getenv("AI_DOC_SYNC_TIMEOUT_SECONDS")
    if timeout_seconds:
        try:
            parsed_timeout = int(timeout_seconds)
        except ValueError as err:
            raise HookError(
                f"Invalid AI_DOC_SYNC_TIMEOUT_SECONDS value: {timeout_seconds}"
            ) from err
        if parsed_timeout <= 0:
            raise HookError(
                f"Invalid AI_DOC_SYNC_TIMEOUT_SECONDS value: {timeout_seconds}. Must be > 0."
            )
        config["llm"]["timeout_seconds"] = parsed_timeout

    prompt_query_file = os.getenv("AI_DOC_SYNC_PROMPT_QUERY_FILE")
    if prompt_query_file:
        config["prompts"]["query_file"] = prompt_query_file
    prompt_sync_file = os.getenv("AI_DOC_SYNC_PROMPT_SYNC_FILE")
    if prompt_sync_file:
        config["prompts"]["sync_file"] = prompt_sync_file
    prompt_analysis_file = os.getenv("AI_DOC_SYNC_PROMPT_ANALYSIS_FILE")
    if prompt_analysis_file:
        config["prompts"]["analysis_file"] = prompt_analysis_file
    prompt_apply_file = os.getenv("AI_DOC_SYNC_PROMPT_APPLY_FILE")
    if prompt_apply_file:
        config["prompts"]["apply_file"] = prompt_apply_file
    prompt_pr_create_file = os.getenv("AI_DOC_SYNC_PROMPT_PR_CREATE_FILE")
    if prompt_pr_create_file:
        config["prompts"]["pr_create_file"] = prompt_pr_create_file

    return config, config_path


def path_matches(path: str, pattern: str) -> bool:
    pure = PurePosixPath(path)
    if pure.match(pattern):
        return True
    return fnmatch.fnmatch(path, pattern)


def is_doc_path(path: str, docs_cfg: dict[str, Any]) -> bool:
    include = docs_cfg.get("paths", [])
    ignore = docs_cfg.get("ignore", [])
    if not any(path_matches(path, pattern) for pattern in include):
        return False
    if any(path_matches(path, pattern) for pattern in ignore):
        return False
    return True


def collect_ranges_from_stdin(
    repo_root: pathlib.Path, remote_name: str, stdin_lines: list[str]
) -> list[str]:
    ranges: set[str] = set()

    for line in stdin_lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts[:4]
        if local_sha == ZERO_OID:
            continue

        if remote_sha and remote_sha != ZERO_OID:
            remote_sha_exists = (
                run_command(
                    ["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd=repo_root
                ).returncode
                == 0
            )
            if remote_sha_exists:
                ranges.add(f"{remote_sha}..{local_sha}")
                continue

        base = ""
        if remote_name:
            remote_head = f"refs/remotes/{remote_name}/HEAD"
            exists = (
                run_command(
                    ["git", "show-ref", "--verify", "--quiet", remote_head], cwd=repo_root
                ).returncode
                == 0
            )
            if exists:
                base = git(repo_root, ["merge-base", local_sha, remote_head], check=False)

        if not base:
            base = git(
                repo_root, ["rev-list", "--max-parents=0", local_sha], check=True
            ).splitlines()[0]
        if base and base != local_sha:
            ranges.add(f"{base}..{local_sha}")

    if ranges:
        return sorted(ranges)

    upstream = git(
        repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], check=False
    )
    if upstream:
        base = git(repo_root, ["merge-base", "HEAD", upstream], check=False)
        if base:
            return [f"{base}..HEAD"]

    return []


def collect_changed_files(repo_root: pathlib.Path, ranges: list[str]) -> list[str]:
    changed: set[str] = set()
    for range_expr in ranges:
        out = git(repo_root, ["diff", "--name-only", "--diff-filter=ACMR", range_expr], check=True)
        for line in out.splitlines():
            if line.strip():
                changed.add(line.strip())
    return sorted(changed)


def collect_diff(repo_root: pathlib.Path, ranges: list[str]) -> str:
    chunks: list[str] = []
    for range_expr in ranges:
        body = git(repo_root, ["diff", "--unified=3", range_expr], check=True)
        chunks.append(f"### RANGE {range_expr}\n{body}\n")
    return "\n".join(chunks).strip() + "\n"


def expand_doc_files(repo_root: pathlib.Path, docs_cfg: dict[str, Any]) -> list[pathlib.Path]:
    include = docs_cfg.get("paths", [])
    ignore = docs_cfg.get("ignore", [])
    files: set[pathlib.Path] = set()

    for pattern in include:
        full_pattern = pattern
        if not pathlib.Path(pattern).is_absolute():
            full_pattern = str(repo_root / pattern)
        for match in glob.glob(full_pattern, recursive=True):
            path = pathlib.Path(match)
            if not path.is_file():
                continue
            rel = path.relative_to(repo_root).as_posix()
            if any(path_matches(rel, ig) for ig in ignore):
                continue
            files.add(path.resolve())
    return sorted(files)


def deterministic_seed_queries(diff_text: str, changed_files: list[str]) -> list[str]:
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
        "update",
        "changes",
        "docs",
        "readme",
    }

    seeds: list[str] = []
    for changed in changed_files:
        pure = PurePosixPath(changed)
        stem = pure.stem
        if len(stem) >= 4:
            seeds.append(stem)
        for segment in pure.parts:
            if len(segment) >= 4 and segment not in {"docs", "src", "test", "tests"}:
                seeds.append(segment)

    for match in re.findall(r"--[a-zA-Z0-9][a-zA-Z0-9_-]*", diff_text):
        seeds.append(match)

    key_matches = re.findall(
        r"^[+-]\s*[\"']?([A-Za-z_][A-Za-z0-9_.-]{2,})[\"']?\s*[:=]", diff_text, re.M
    )
    seeds.extend(key_matches)

    words = re.findall(r"\b[A-Za-z][A-Za-z0-9_.-]{3,}\b", diff_text)
    frequency: dict[str, int] = {}
    for word in words:
        lower = word.lower()
        if lower in stopwords or lower.startswith("http"):
            continue
        frequency[word] = frequency.get(word, 0) + 1
    frequent_words = sorted(frequency.items(), key=lambda item: item[1], reverse=True)[:20]
    seeds.extend(word for word, _ in frequent_words)

    deduped: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        clean = seed.strip()
        if not clean or len(clean) < 3:
            continue
        if clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped[:40]


def extract_json_array(text: str) -> list[Any]:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end < start:
        raise HookError("Could not find JSON array in model output")
    payload = text[start : end + 1]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HookError(f"Failed to parse JSON array from model output: {exc}") from exc
    if not isinstance(parsed, list):
        raise HookError("Model output JSON is not an array")
    return parsed


def extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end < start:
        raise HookError("Could not find JSON object in model output")
    payload = text[start : end + 1]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HookError(f"Failed to parse JSON object from model output: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HookError("Model output JSON is not an object")
    return parsed


def object_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def maybe_load_cached_queries(cache_file: pathlib.Path, ttl_seconds: int) -> list[str] | None:
    if not cache_file.exists():
        return None
    try:
        with cache_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None

    created_at = int(payload.get("created_at", 0))
    if created_at <= 0:
        return None
    if int(time.time()) - created_at > ttl_seconds:
        return None
    queries = payload.get("queries")
    if not isinstance(queries, list):
        return None
    return [str(item) for item in queries if str(item).strip()]


def store_cached_queries(cache_file: pathlib.Path, queries: list[str]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"created_at": int(time.time()), "queries": queries}
    cache_file.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def sanitize_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-")
    return cleaned or "value"


def prefer_opencode_cli_candidate(candidate: str) -> str:
    path = pathlib.Path(candidate)
    if path.name != "opencode":
        return candidate
    sibling = path.with_name("opencode-cli")
    if sibling.exists() and os.access(sibling, os.X_OK):
        return str(sibling)
    return candidate


def resolve_opencode_executable() -> str:
    cli_path = shutil.which("opencode-cli")
    if cli_path:
        return cli_path

    opencode_path = shutil.which("opencode")
    if opencode_path:
        return prefer_opencode_cli_candidate(opencode_path)

    raise HookError(
        "opencode is required but not installed. Install with `pnpm install -g opencode-ai` "
        "or `brew install anomalyco/tap/opencode`."
    )


def parse_opencode_json_run_output(raw: str) -> tuple[str | None, str]:
    session_id: str | None = None
    text_chunks: list[str] = []
    for line in raw.splitlines():
        payload = line.strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        sid = event.get("sessionID")
        if session_id is None and isinstance(sid, str) and sid.strip():
            session_id = sid.strip()

        if event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            text_chunks.append(text)

    return session_id, "\n".join(text_chunks).strip()


def export_opencode_session_json(
    repo_root: pathlib.Path,
    session_id: str,
    export_path: pathlib.Path,
    logger: HookLogger,
    timeout_seconds: int,
    opencode_executable: str,
) -> bool:
    completed = run_command(
        [opencode_executable, "export", session_id],
        cwd=repo_root,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        logger.warn(
            "llm.session_export_failed",
            "Failed to export OpenCode session JSON",
            session_id=session_id,
            error=details or f"exit code {completed.returncode}",
        )
        return False

    export_payload = completed.stdout or ""
    first_brace = export_payload.find("{")
    if first_brace > 0:
        export_payload = export_payload[first_brace:]
    export_payload = export_payload.strip()
    if not export_payload:
        logger.warn(
            "llm.session_export_empty",
            "OpenCode session export produced empty payload",
            session_id=session_id,
            path=str(export_path),
        )
        return False

    if not write_text_file(export_path, export_payload + "\n"):
        logger.warn(
            "llm.session_export_write_failed",
            "Failed to persist exported OpenCode session JSON",
            session_id=session_id,
            path=str(export_path),
        )
        return False

    logger.status(
        "llm.session_exported",
        f"Saved OpenCode session export: {export_path}",
        session_id=session_id,
        path=str(export_path),
    )
    quoted_path = shlex.quote(str(export_path))
    open_cmd = (
        f"opencode import {quoted_path} && "
        f"SESSION_ID=\"$(jq -r '.id? // .sessionID? // .sessionId? // empty' {quoted_path})\" && "
        '{ [ -n "$SESSION_ID" ] && opencode --session "$SESSION_ID" || opencode --continue; }'
    )
    logger.status(
        "llm.session_open_hint",
        f"To import+open in OpenCode run: `{open_cmd}`",
        session_id=session_id,
        path=str(export_path),
    )
    return True


def delete_opencode_session(
    repo_root: pathlib.Path,
    session_id: str,
    logger: HookLogger,
    timeout_seconds: int,
    opencode_executable: str,
) -> None:
    completed = run_command(
        [opencode_executable, "session", "delete", session_id],
        cwd=repo_root,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        logger.warn(
            "llm.session_delete_failed",
            "Failed to delete OpenCode session",
            session_id=session_id,
            error=details or f"exit code {completed.returncode}",
        )
        return
    logger.debug("llm.session_deleted", "Deleted OpenCode session", session_id=session_id)


def finalize_opencode_session(
    repo_root: pathlib.Path,
    logger: HookLogger,
    timeout_seconds: int,
    run_id: str,
    stage_name: str,
    session_id: str | None,
    transcript_dir: pathlib.Path | None,
    delete_session_after_run: bool,
    opencode_executable: str,
) -> None:
    if not session_id:
        if transcript_dir is not None or delete_session_after_run:
            logger.warn(
                "llm.session_id_missing",
                "Could not determine OpenCode session ID from run output; skipping export/delete",
                stage_name=stage_name,
            )
        return

    if transcript_dir is not None:
        export_name = (
            f"{sanitize_filename_component(run_id)}-"
            f"{sanitize_filename_component(stage_name)}-"
            f"{sanitize_filename_component(session_id)}.json"
        )
        export_path = transcript_dir / export_name
        export_opencode_session_json(
            repo_root=repo_root,
            session_id=session_id,
            export_path=export_path,
            logger=logger,
            timeout_seconds=timeout_seconds,
            opencode_executable=opencode_executable,
        )

    if delete_session_after_run:
        delete_opencode_session(
            repo_root=repo_root,
            session_id=session_id,
            logger=logger,
            timeout_seconds=timeout_seconds,
            opencode_executable=opencode_executable,
        )


def call_opencode(
    repo_root: pathlib.Path,
    opencode_executable: str,
    model: str,
    variant: str,
    timeout_seconds: int,
    prompt: str,
    files: list[pathlib.Path],
    logger: HookLogger,
    stage_name: str,
    run_id: str,
    session_title_prefix: str,
    call_purpose: str | None = None,
    attempt: int | None = None,
    total_attempts: int | None = None,
    existing_session_id: str | None = None,
    print_output: bool = False,
) -> OpenCodeRunResult:
    session_title = f"{session_title_prefix} {run_id} {stage_name}"
    purpose = (
        call_purpose.strip()
        if isinstance(call_purpose, str) and call_purpose.strip()
        else stage_purpose(stage_name)
    )
    logger.llm_call(
        stage_name=stage_name,
        purpose=purpose,
        model=model,
        attempt=attempt,
        total_attempts=total_attempts,
    )
    cmd = [opencode_executable, "run", "--format", "json", "--model", model]
    if variant:
        cmd.extend(["--variant", variant])
    if existing_session_id:
        cmd.extend(["--session", existing_session_id])
    else:
        cmd.extend(["--title", session_title])
    for file_path in files:
        cmd.extend(["--file", str(file_path)])
    # `--file` is an array option in opencode CLI. Use `--` so the prompt is
    # always parsed as message text, never as another file argument.
    cmd.extend(["--", prompt])

    logger.debug("llm.command", "Running opencode command", args=" ".join(cmd[:8]) + " ...")
    logger.debug(
        "llm.prompt",
        "Submitting prompt to model",
        prompt=prompt,
        attached_files=[str(path) for path in files],
    )
    completed = run_command(
        cmd,
        cwd=repo_root,
        timeout=timeout_seconds,
        check=False,
        env={"OPENCODE_SERVER_PASSWORD": None},
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    session_id, text_output = parse_opencode_json_run_output(stdout)
    if session_id is None and existing_session_id:
        session_id = existing_session_id

    logger.debug(
        "llm.raw_response",
        "Received raw model response",
        exit_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        session_id=session_id,
    )

    if print_output and stdout.strip():
        sys.stderr.write("[ai-doc-sync] LLM output begin\n")
        sys.stderr.write(stdout.strip() + "\n")
        sys.stderr.write("[ai-doc-sync] LLM output end\n")
    if print_output and stderr.strip():
        sys.stderr.write("[ai-doc-sync] LLM stderr begin\n")
        sys.stderr.write(stderr.strip() + "\n")
        sys.stderr.write("[ai-doc-sync] LLM stderr end\n")

    return OpenCodeRunResult(
        output_text=text_output if text_output else stdout.strip(),
        session_id=session_id,
        stdout=stdout,
        stderr=stderr,
        return_code=completed.returncode,
    )


def call_opencode_json_array_with_retries(
    repo_root: pathlib.Path,
    opencode_executable: str,
    model: str,
    variant: str,
    timeout_seconds: int,
    base_prompt: str,
    files: list[pathlib.Path],
    logger: HookLogger,
    stage_name: str,
    max_retries: int,
    invalid_json_feedback_max_chars: int,
    json_retry_new_session: bool,
    transcript_dir: pathlib.Path | None,
    run_id: str,
    session_title_prefix: str,
    delete_session_after_run: bool,
    print_output: bool,
) -> list[Any]:
    prompt = base_prompt
    last_parse_error = ""
    last_output = ""
    session_id: str | None = None
    total_attempts = max_retries + 1

    for attempt in range(1, total_attempts + 1):
        logger.debug(
            "llm.json_attempt",
            f"JSON parse attempt for {stage_name}",
            attempt=attempt,
            total_attempts=total_attempts,
        )
        result = call_opencode(
            repo_root=repo_root,
            opencode_executable=opencode_executable,
            model=model,
            variant=variant,
            timeout_seconds=timeout_seconds,
            prompt=prompt,
            files=files,
            logger=logger,
            stage_name=stage_name,
            run_id=run_id,
            session_title_prefix=session_title_prefix,
            call_purpose=stage_purpose(stage_name),
            attempt=attempt,
            total_attempts=total_attempts,
            existing_session_id=session_id,
            print_output=print_output,
        )
        if result.session_id:
            session_id = result.session_id
        if result.return_code != 0:
            finalize_opencode_session(
                repo_root=repo_root,
                logger=logger,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                stage_name=stage_name,
                session_id=session_id,
                transcript_dir=transcript_dir,
                delete_session_after_run=delete_session_after_run,
                opencode_executable=opencode_executable,
            )
            details = (
                result.stderr.strip() or result.stdout.strip() or f"exit code {result.return_code}"
            )
            raise HookError(f"OpenCode command failed: {details}")

        output = result.output_text
        try:
            parsed = extract_json_array(output)
            finalize_opencode_session(
                repo_root=repo_root,
                logger=logger,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                stage_name=stage_name,
                session_id=session_id,
                transcript_dir=transcript_dir,
                delete_session_after_run=delete_session_after_run,
                opencode_executable=opencode_executable,
            )
            return parsed
        except HookError as exc:
            last_parse_error = str(exc)
            last_output = output
            logger.debug(
                "llm.invalid_json",
                f"Model output was invalid JSON for {stage_name}",
                parse_error=last_parse_error,
                invalid_output=last_output,
            )
            if attempt >= total_attempts:
                break

            feedback = output[:invalid_json_feedback_max_chars]
            logger.info(
                "llm.invalid_json_retry",
                f"Model returned invalid JSON for {stage_name}; retrying",
                attempt=attempt,
                total_attempts=total_attempts,
            )
            prompt = (
                base_prompt
                + "\n\nIMPORTANT: Your previous response was invalid JSON and could not be parsed.\n"
                + f"Parse error: {last_parse_error}\n"
                + "Return ONLY valid JSON array. No markdown. No prose. No headings.\n"
                + "Previous invalid output follows:\n"
                + "```text\n"
                + feedback
                + "\n```"
            )
            if json_retry_new_session:
                # Start a fresh run session but keep original prompt + invalid output feedback.
                session_id = None

    finalize_opencode_session(
        repo_root=repo_root,
        logger=logger,
        timeout_seconds=timeout_seconds,
        run_id=run_id,
        stage_name=stage_name,
        session_id=session_id,
        transcript_dir=transcript_dir,
        delete_session_after_run=delete_session_after_run,
        opencode_executable=opencode_executable,
    )
    snippet = last_output[:1000].replace("\n", "\\n")
    raise HookError(
        f"Model failed to return valid JSON for {stage_name} after {total_attempts} attempts. "
        f"Last parse error: {last_parse_error}. Last output snippet: {snippet}"
    )


def call_opencode_json_object_with_retries(
    repo_root: pathlib.Path,
    opencode_executable: str,
    model: str,
    variant: str,
    timeout_seconds: int,
    base_prompt: str,
    files: list[pathlib.Path],
    logger: HookLogger,
    stage_name: str,
    max_retries: int,
    invalid_json_feedback_max_chars: int,
    json_retry_new_session: bool,
    transcript_dir: pathlib.Path | None,
    run_id: str,
    session_title_prefix: str,
    delete_session_after_run: bool,
    print_output: bool,
) -> dict[str, Any]:
    prompt = base_prompt
    last_parse_error = ""
    last_output = ""
    session_id: str | None = None
    total_attempts = max_retries + 1

    for attempt in range(1, total_attempts + 1):
        logger.debug(
            "llm.json_attempt",
            f"JSON parse attempt for {stage_name}",
            attempt=attempt,
            total_attempts=total_attempts,
        )
        result = call_opencode(
            repo_root=repo_root,
            opencode_executable=opencode_executable,
            model=model,
            variant=variant,
            timeout_seconds=timeout_seconds,
            prompt=prompt,
            files=files,
            logger=logger,
            stage_name=stage_name,
            run_id=run_id,
            session_title_prefix=session_title_prefix,
            call_purpose=stage_purpose(stage_name),
            attempt=attempt,
            total_attempts=total_attempts,
            existing_session_id=session_id,
            print_output=print_output,
        )
        if result.session_id:
            session_id = result.session_id
        if result.return_code != 0:
            finalize_opencode_session(
                repo_root=repo_root,
                logger=logger,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                stage_name=stage_name,
                session_id=session_id,
                transcript_dir=transcript_dir,
                delete_session_after_run=delete_session_after_run,
                opencode_executable=opencode_executable,
            )
            details = (
                result.stderr.strip() or result.stdout.strip() or f"exit code {result.return_code}"
            )
            raise HookError(f"OpenCode command failed: {details}")

        output = result.output_text
        try:
            parsed = extract_json_object(output)
            finalize_opencode_session(
                repo_root=repo_root,
                logger=logger,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                stage_name=stage_name,
                session_id=session_id,
                transcript_dir=transcript_dir,
                delete_session_after_run=delete_session_after_run,
                opencode_executable=opencode_executable,
            )
            return parsed
        except HookError as exc:
            last_parse_error = str(exc)
            last_output = output
            logger.debug(
                "llm.invalid_json",
                f"Model output was invalid JSON for {stage_name}",
                parse_error=last_parse_error,
                invalid_output=last_output,
            )
            pr_url = extract_pr_url(output)
            if pr_url:
                logger.info(
                    "llm.invalid_json_pr_url_hint",
                    "Detected PR URL in invalid JSON output",
                    stage_name=stage_name,
                    url=pr_url,
                )
            if attempt >= total_attempts:
                break

            feedback = output[:invalid_json_feedback_max_chars]
            logger.info(
                "llm.invalid_json_retry",
                f"Model returned invalid JSON for {stage_name}; retrying",
                attempt=attempt,
                total_attempts=total_attempts,
            )
            prompt = (
                base_prompt
                + "\n\nIMPORTANT: Your previous response was invalid JSON and could not be parsed.\n"
                + f"Parse error: {last_parse_error}\n"
                + "Return ONLY valid JSON object. No markdown. No prose. No headings.\n"
                + "Previous invalid output follows:\n"
                + "```text\n"
                + feedback
                + "\n```"
            )
            if json_retry_new_session:
                # Start a fresh run session but keep original prompt + invalid output feedback.
                session_id = None

    finalize_opencode_session(
        repo_root=repo_root,
        logger=logger,
        timeout_seconds=timeout_seconds,
        run_id=run_id,
        stage_name=stage_name,
        session_id=session_id,
        transcript_dir=transcript_dir,
        delete_session_after_run=delete_session_after_run,
        opencode_executable=opencode_executable,
    )
    snippet = last_output[:1000].replace("\n", "\\n")
    raise HookError(
        f"Model failed to return valid JSON for {stage_name} after {total_attempts} attempts. "
        f"Last parse error: {last_parse_error}. Last output snippet: {snippet}"
    )


def parse_rg_line(line: str) -> tuple[str, int, str] | None:
    def split_at_line_number(raw: str, sep: str) -> tuple[str, str] | None:
        encoded = raw.encode("utf-8", errors="ignore")
        sep_byte = ord(sep)
        for idx in range(len(encoded) - 1):
            if encoded[idx] == sep_byte and chr(encoded[idx + 1]).isdigit():
                return raw[:idx], raw[idx + 1 :]
        return None

    split_match = split_at_line_number(line, ":")
    if split_match:
        file_name, rest = split_match
        if ":" in rest:
            line_part, content = rest.split(":", 1)
            if line_part.isdigit():
                return file_name, int(line_part), content

    split_context = split_at_line_number(line, "-")
    if split_context:
        file_name, rest = split_context
        if "-" in rest:
            line_part, content = rest.split("-", 1)
            if line_part.isdigit():
                return file_name, int(line_part), content
    return None


def create_chunk(file_name: str, lines: list[tuple[int, str]]) -> dict[str, Any]:
    return {
        "file": file_name,
        "start_line": lines[0][0],
        "end_line": lines[-1][0],
        "content": "\n".join(content for _, content in lines),
    }


def parse_ripgrep_output(output: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current_file: str | None = None
    current_lines: list[tuple[int, str]] = []

    for line in output.splitlines():
        if line == "--":
            if current_file and current_lines:
                chunks.append(create_chunk(current_file, current_lines))
                current_lines = []
            continue
        parsed = parse_rg_line(line)
        if not parsed:
            continue
        file_name, line_number, content = parsed
        if current_file != file_name:
            if current_file and current_lines:
                chunks.append(create_chunk(current_file, current_lines))
                current_lines = []
            current_file = file_name
        current_lines.append((line_number, content))

    if current_file and current_lines:
        chunks.append(create_chunk(current_file, current_lines))

    return chunks


def merge_adjacent_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not chunks:
        return chunks
    sorted_chunks = sorted(chunks, key=lambda item: (item["file"], item["start_line"]))
    merged: list[dict[str, Any]] = []

    for chunk in sorted_chunks:
        if merged:
            last = merged[-1]
            if last["file"] == chunk["file"] and chunk["start_line"] <= last["end_line"] + 5:
                last["end_line"] = chunk["end_line"]
                last["content"] = f"{last['content']}\n...\n{chunk['content']}"
                continue
        merged.append(chunk)
    return merged


def truncate_chunks_to_budget(
    chunks: list[dict[str, Any]], max_context_tokens: int
) -> list[dict[str, Any]]:
    chars_budget = max_context_tokens * 4
    total_chars = 0
    result: list[dict[str, Any]] = []

    for chunk in sorted(chunks, key=lambda item: len(item["content"])):
        chunk_chars = len(chunk["content"])
        if total_chars + chunk_chars > chars_budget:
            if not result:
                truncated = dict(chunk)
                truncated["content"] = chunk["content"][:chars_budget]
                result.append(truncated)
            break
        result.append(chunk)
        total_chars += chunk_chars
    return result


def search_docs_with_queries(
    repo_root: pathlib.Path, queries: list[str], files: list[pathlib.Path], logger: HookLogger
) -> list[dict[str, Any]]:
    if not queries or not files:
        return []
    if shutil.which("rg") is None:
        raise HookError("ripgrep (rg) is required for retrieval but was not found")

    all_chunks: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    file_args = [str(path) for path in files]
    workers = min(8, len(queries))

    def search_query(query: str) -> list[dict[str, Any]]:
        cmd = [
            "rg",
            "--line-number",
            "--no-heading",
            "--color=never",
            "-C",
            "3",
            "--",
            query,
            *file_args,
        ]
        completed = run_command(cmd, cwd=repo_root, check=False)
        if completed.returncode not in {0, 1}:
            raise HookError((completed.stderr or "").strip() or f"rg failed for query: {query}")
        return parse_ripgrep_output(completed.stdout)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(search_query, query): query for query in queries}
        for future in as_completed(futures):
            query = futures[future]
            try:
                chunks = future.result()
            except Exception as exc:
                logger.warn("retrieval.query_failed", f"Query failed: {query}", error=str(exc))
                continue
            for chunk in chunks:
                key = (chunk["file"], int(chunk["start_line"]))
                if key in seen:
                    continue
                seen.add(key)
                all_chunks.append(chunk)
    return merge_adjacent_chunks(all_chunks)


def format_issue(issue: dict[str, Any]) -> str:
    file_name = issue.get("file", "<unknown>")
    line = issue.get("line", 0)
    description = issue.get("description", "").strip()
    return f"{file_name}:{line} {description}"


def build_doc_change_summary(repo_root: pathlib.Path, changed_doc_files: list[str]) -> str:
    summary_lines = ["# AI Doc Sync Change Summary", ""]
    summary_lines.append("## Files")
    for path in changed_doc_files:
        summary_lines.append(f"- `{path}`")

    numstat = git(repo_root, ["diff", "--numstat", "--", "docs", "README.md"], check=False)
    if numstat:
        summary_lines.append("")
        summary_lines.append("## Numstat")
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                added, deleted, path = parts[0], parts[1], parts[2]
                summary_lines.append(f"- `{path}`: +{added} / -{deleted}")

    stat = git(repo_root, ["--no-pager", "diff", "--stat", "--", "docs", "README.md"], check=False)
    if stat:
        summary_lines.append("")
        summary_lines.append("## Diffstat")
        summary_lines.append("```")
        summary_lines.append(stat.rstrip())
        summary_lines.append("```")

    summary_lines.append("")
    summary_lines.append("## Commit Policy")
    summary_lines.append(
        "- No commit is created automatically by the hook. Review edits, then either create a new commit or amend your latest commit before pushing again."
    )
    return "\n".join(summary_lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="AI docs sync pre-push hook")
    parser.add_argument("remote_name", nargs="?", default="")
    parser.add_argument("remote_url", nargs="?", default="")
    args = parser.parse_args()

    stdin_lines = sys.stdin.read().splitlines()
    repo_root = pathlib.Path(git(pathlib.Path.cwd(), ["rev-parse", "--show-toplevel"], check=True))
    git_dir_raw = git(repo_root, ["rev-parse", "--git-dir"], check=True)
    git_dir = pathlib.Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = (repo_root / git_dir).resolve()

    config, config_path = load_config(repo_root)
    allow_push_on_error = bool(config["general"].get("allow_push_on_error", True))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    log_path = None
    if config["logging"].get("jsonl", True):
        log_dir = resolve_storage_path(repo_root, git_dir, str(config["logging"]["dir"]))
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_name = f"{run_id}.jsonl"
            log_path = log_dir / log_name
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"[ai-doc-sync] Logging path unavailable ({log_dir}): {exc}. Continuing without JSONL logs.\n"
            )
            log_path = None

    logger = HookLogger(log_path, str(config["logging"].get("level", "status")).strip().lower())
    logger.status(
        "hook.start",
        "Starting pre-push AI docs sync",
        remote_name=args.remote_name,
        remote_url=args.remote_url,
        config_path=str(config_path) if config_path else "<defaults>",
    )

    summary_emitted = False

    def exit_with(code: int) -> int:
        nonlocal summary_emitted
        if not summary_emitted:
            logger.llm_summary()
            summary_emitted = True
        return code

    if not config["general"].get("enabled", True):
        logger.status("hook.disabled", "Hook disabled by configuration; allowing push")
        return exit_with(0)

    sync_branch = os.getenv("BEADS_SYNC_BRANCH", "beads-sync")
    if bool(config["general"].get("skip_on_sync_branch", True)):
        skip_sync, reason = should_skip_for_beads_sync(repo_root, sync_branch)
        if skip_sync:
            logger.status(
                "hook.skip_sync_branch",
                "Skipping AI docs sync in beads sync worktree/branch",
                sync_branch=sync_branch,
                reason=reason,
            )
            return exit_with(0)

    try:

        def finalize_pr_only_push(
            ranges_for_gate: list[str], changed_files_for_gate: list[str], diff_for_gate: str
        ) -> int:
            pr_cfg = config.get("pr", {})
            flag_env_name = str(pr_cfg.get("flag_env", "AI_DOC_SYNC_CREATE_PR")).strip()
            if not flag_env_name:
                flag_env_name = "AI_DOC_SYNC_CREATE_PR"
            if env_bool(flag_env_name) is not True:
                return 0

            branch_name = git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
            if not branch_name or branch_name in {"HEAD", "main", sync_branch}:
                return 0
            if not branch_name.startswith(FEATURE_BRANCH_PREFIXES):
                return 0

            opencode_executable = resolve_opencode_executable()
            pr_ok, pr_message = run_pr_creation_gate(
                repo_root=repo_root,
                remote_name=args.remote_name,
                remote_url=args.remote_url,
                ranges=ranges_for_gate,
                changed_files=changed_files_for_gate,
                diff_text=diff_for_gate,
                logger=logger,
                config=config,
                git_dir=git_dir,
                run_id=run_id,
                opencode_executable=opencode_executable,
            )
            if not pr_ok:
                logger.error("pr.block", "Blocking push due to PR auto-create failure")
                sys.stderr.write(f"[ai-doc-sync] {pr_message}\n")
                return 1
            return 0

        if config["general"].get("require_clean_worktree", True):
            if run_command(["git", "diff", "--quiet"], cwd=repo_root).returncode != 0:
                raise HookError("Refusing to run with unstaged changes in working tree")
            if run_command(["git", "diff", "--cached", "--quiet"], cwd=repo_root).returncode != 0:
                raise HookError("Refusing to run with staged-but-uncommitted changes")

        baseline = set(
            line.strip()
            for line in git(repo_root, ["diff", "--name-only"], check=True).splitlines()
            if line.strip()
        )

        logger.debug("step.ranges", "Resolving push ranges")
        ranges = collect_ranges_from_stdin(repo_root, args.remote_name, stdin_lines)
        if not ranges:
            logger.status("step.ranges_empty", "No push ranges detected; allowing push")
            return exit_with(finalize_pr_only_push([], [], ""))

        logger.debug("step.changed_files", "Collecting changed files", range_count=len(ranges))
        changed_files = collect_changed_files(repo_root, ranges)
        if not changed_files:
            logger.status(
                "step.changed_files_empty", "No changed files in push range; allowing push"
            )
            return exit_with(finalize_pr_only_push(ranges, [], ""))

        opencode_executable = resolve_opencode_executable()
        logger.debug(
            "llm.executable",
            "Resolved OpenCode executable",
            executable=opencode_executable,
        )

        logger.debug("step.diff", "Collecting push diff")
        diff_text = collect_diff(repo_root, ranges)
        if not diff_text.strip():
            logger.status("step.diff_empty", "Diff was empty; allowing push")
            return exit_with(finalize_pr_only_push(ranges, changed_files, ""))

        raw_summary_dir = resolve_storage_path(
            repo_root,
            git_dir,
            str(config["logging"].get("summary_dir", ".git/ai-doc-sync/summaries")),
        )
        summary_dir = ensure_dir(raw_summary_dir)
        if summary_dir is None:
            logger.warn(
                "summary.dir_unavailable",
                "Summary directory unavailable; continuing without summary file output",
                path=str(raw_summary_dir),
            )

        def finalize_success_push_multi() -> int:
            pr_ok, pr_message = run_pr_creation_gate(
                repo_root=repo_root,
                remote_name=args.remote_name,
                remote_url=args.remote_url,
                ranges=ranges,
                changed_files=changed_files,
                diff_text=diff_text,
                logger=logger,
                config=config,
                git_dir=git_dir,
                run_id=run_id,
                opencode_executable=opencode_executable,
            )
            if not pr_ok:
                logger.error("pr.block", "Blocking push due to PR auto-create failure")
                sys.stderr.write(f"[ai-doc-sync] {pr_message}\n")
                return 1
            return 0

        def finalize_success_push_single() -> int:
            pr_ok, pr_message = ensure_pr_created_for_push(
                repo_root=repo_root,
                remote_name=args.remote_name,
                ranges=ranges,
                changed_files=changed_files,
                logger=logger,
                config=config,
            )
            if not pr_ok:
                logger.error("pr.block", "Blocking push due to PR auto-create failure")
                sys.stderr.write(f"[ai-doc-sync] {pr_message}\n")
                return 1
            return 0

        execution_mode = normalize_execution_mode(
            str(config["general"].get("execution_mode", "multi"))
        )
        logger.info(
            "hook.execution_mode", "Running ai-doc-sync execution mode", mode=execution_mode
        )

        docs_files = expand_doc_files(repo_root, config["docs"])

        if execution_mode == "single":
            beads_cfg = config.get("beads", {})
            report_file_raw = str(
                beads_cfg.get("report_file", "BEADS_STATUS_ACTION_REQUIRED.md")
            ).strip()
            report_file_path = repo_root / report_file_raw
            try:
                report_rel = report_file_path.relative_to(repo_root).as_posix()
            except Exception:  # noqa: BLE001
                report_rel = report_file_raw

            if report_file_path.exists():
                try:
                    report_file_path.unlink()
                    logger.info(
                        "beads.alignment_report_cleared",
                        "Removed stale Beads alignment report before unified run",
                        report=str(report_file_path),
                    )
                except Exception as remove_exc:  # noqa: BLE001
                    logger.warn(
                        "beads.alignment_report_clear_failed",
                        "Failed to remove stale Beads alignment report before unified run",
                        report=str(report_file_path),
                        error=str(remove_exc),
                    )

            summary = run_unified_sync_stage(
                repo_root=repo_root,
                ranges=ranges,
                changed_files=changed_files,
                diff_text=diff_text,
                docs_files=docs_files,
                logger=logger,
                config=config,
                git_dir=git_dir,
                run_id=run_id,
                opencode_executable=opencode_executable,
                remote_name=args.remote_name,
                remote_url=args.remote_url,
            )

            if report_file_path.exists() or object_bool(summary, "beads_report_written"):
                logger.error(
                    "beads.alignment_report",
                    "Beads status alignment requires manual action; blocking push",
                    report=str(report_file_path),
                )
                raise HookError(
                    f"Beads status alignment requires manual action. See {report_file_path}"
                )

            mode = str(config["general"].get("mode", "apply-and-block")).strip()
            if mode == "check-only" and object_bool(summary, "docs_drift_detected"):
                raise HookError("Documentation drift detected (check-only mode)")
            if (
                mode != "check-only"
                and object_bool(summary, "docs_drift_detected")
                and not object_bool(summary, "docs_edits_applied")
            ):
                raise HookError(
                    "Documentation issues were detected, but no doc updates were applied. "
                    "Run `opencode` manually to review/fix, then push again."
                )

            post_changes = set(
                line.strip()
                for line in git(repo_root, ["diff", "--name-only"], check=True).splitlines()
                if line.strip()
            )
            new_changes = sorted(post_changes - baseline)

            non_doc_changes = [
                path
                for path in new_changes
                if path != report_rel
                and not path.startswith(".beads/")
                and not is_doc_path(path, config["docs"])
            ]
            if non_doc_changes:
                logger.error(
                    "safety.non_doc_write",
                    "AI attempted to modify non-doc files; blocking push",
                    files=",".join(non_doc_changes),
                )
                raise HookError("AI attempted non-doc edits")

            doc_changes = [path for path in new_changes if is_doc_path(path, config["docs"])]
            if (
                mode != "check-only"
                and object_bool(summary, "docs_drift_detected")
                and object_bool(summary, "docs_edits_applied")
                and not doc_changes
            ):
                raise HookError(
                    "Documentation drift was reported as fixed, but no documentation updates were applied. "
                    "Run `opencode` manually to review/fix, then push again."
                )
            if doc_changes:
                summary_text = build_doc_change_summary(repo_root, doc_changes)
                summary_path = summary_dir / f"{run_id}.md" if summary_dir is not None else None
                if summary_path is not None and write_text_file(summary_path, summary_text):
                    logger.info(
                        "summary.written",
                        "Wrote doc change summary",
                        path=str(summary_path),
                    )
                else:
                    logger.warn(
                        "summary.write_failed",
                        "Failed to persist doc change summary to file",
                        path=str(summary_path) if summary_path is not None else "<disabled>",
                    )

                logger.warn(
                    "step.apply_block",
                    "Doc updates were applied; review+commit required before push",
                )
                sys.stderr.write("[ai-doc-sync] Doc change summary:\n")
                sys.stderr.write(summary_text + "\n")
                if summary_path is not None:
                    sys.stderr.write(f"[ai-doc-sync] Summary file: {summary_path}\n")
                sys.stderr.write(
                    "[ai-doc-sync] No commit was created automatically. "
                    "Create a new commit or amend your latest commit, then push again.\n"
                )
                return exit_with(1)

            return exit_with(finalize_success_push_single())

        beads_ok, beads_message = run_beads_status_alignment_gate(
            repo_root=repo_root,
            ranges=ranges,
            changed_files=changed_files,
            diff_text=diff_text,
            logger=logger,
            config=config,
            git_dir=git_dir,
            run_id=run_id,
            opencode_executable=opencode_executable,
        )
        if not beads_ok:
            raise HookError(beads_message)

        if all(is_doc_path(path, config["docs"]) for path in changed_files):
            logger.status("step.docs_only", "Only docs changed in push range; skipping AI sync")
            return exit_with(finalize_success_push_multi())

        if not docs_files:
            logger.status(
                "step.docs_inventory_empty",
                "No docs files found in configured scope; allowing push",
            )
            return exit_with(finalize_success_push_multi())

        print_llm_output = (
            bool(config["logging"].get("print_llm_output", False))
            and str(config["logging"].get("level", "status")).strip().lower() == "debug"
        )
        llm_model = str(config["llm"].get("model", "openai/gpt-5.3-codex-spark")).strip()
        llm_variant = str(config["llm"].get("variant", "")).strip()
        prompts_cfg = config.get("prompts", {})
        query_prompt_template = load_prompt_from_file(
            repo_root=repo_root,
            raw_path=str(prompts_cfg.get("query_file", "")),
            fallback=SEARCH_QUERIES_PROMPT,
            logger=logger,
            prompt_name="query",
        )
        analysis_prompt_template = load_prompt_from_file(
            repo_root=repo_root,
            raw_path=str(prompts_cfg.get("analysis_file", "")),
            fallback=DEFAULT_ANALYSIS_PROMPT,
            logger=logger,
            prompt_name="analysis",
        )
        apply_prompt_template = load_prompt_from_file(
            repo_root=repo_root,
            raw_path=str(prompts_cfg.get("apply_file", "")),
            fallback=APPLY_PROMPT,
            logger=logger,
            prompt_name="apply",
        )
        transcript_dir: pathlib.Path | None = None
        if bool(config["logging"].get("capture_llm_transcript", False)):
            raw_transcript_dir = resolve_storage_path(
                repo_root,
                git_dir,
                str(config["logging"].get("transcript_dir", ".git/ai-doc-sync/transcripts")),
            )
            transcript_dir = ensure_dir(raw_transcript_dir)
            if transcript_dir is None:
                logger.warn(
                    "llm.transcript_dir_unavailable",
                    "Session export directory unavailable; continuing without session export capture",
                    path=str(raw_transcript_dir),
                )

        with tempfile.TemporaryDirectory(prefix="ai-doc-sync.") as tmp_dir_raw:
            tmp_dir = pathlib.Path(tmp_dir_raw)
            diff_file = tmp_dir / "push.diff"
            changed_file = tmp_dir / "changed-files.txt"
            docs_inventory_file = tmp_dir / "docs-inventory.txt"
            docs_context_file = tmp_dir / "docs-context.txt"
            issues_file = tmp_dir / "issues.json"

            max_diff_bytes = int(config["llm"]["max_diff_bytes"])
            diff_payload = diff_text[:max_diff_bytes]
            diff_file.write_text(diff_payload, encoding="utf-8")
            changed_file.write_text("\n".join(changed_files) + "\n", encoding="utf-8")
            docs_inventory_file.write_text(
                "\n".join(path.relative_to(repo_root).as_posix() for path in docs_files) + "\n",
                encoding="utf-8",
            )

            logger.debug("step.discovery", "Generating hybrid retrieval queries")
            seed_queries = deterministic_seed_queries(diff_text, changed_files)
            diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
            cache_dir = resolve_storage_path(repo_root, git_dir, str(config["cache"]["dir"]))
            cache_file = cache_dir / "queries" / f"{diff_hash}.json"
            ai_queries: list[str] = []

            if config["cache"].get("enabled", True):
                cached = maybe_load_cached_queries(cache_file, int(config["cache"]["ttl_seconds"]))
                if cached:
                    ai_queries = cached
                    logger.info(
                        "step.discovery_cache_hit", "Using cached AI queries", count=len(ai_queries)
                    )

            if not ai_queries:
                raw_query_array = call_opencode_json_array_with_retries(
                    repo_root=repo_root,
                    opencode_executable=opencode_executable,
                    model=llm_model,
                    variant=llm_variant,
                    timeout_seconds=int(config["llm"]["timeout_seconds"]),
                    base_prompt=query_prompt_template,
                    files=[diff_file, changed_file],
                    logger=logger,
                    stage_name="01-query",
                    max_retries=int(config["llm"].get("json_max_retries", 2)),
                    invalid_json_feedback_max_chars=int(
                        config["llm"].get("invalid_json_feedback_max_chars", 6000)
                    ),
                    json_retry_new_session=bool(
                        config["llm"].get("json_retry_new_session", True)
                    ),
                    transcript_dir=transcript_dir,
                    run_id=run_id,
                    session_title_prefix=str(
                        config["llm"].get("session_title_prefix", "ai-doc-sync")
                    ),
                    delete_session_after_run=bool(
                        config["llm"].get("delete_session_after_run", True)
                    ),
                    print_output=print_llm_output,
                )
                ai_queries = [str(item).strip() for item in raw_query_array if str(item).strip()]
                if config["cache"].get("enabled", True):
                    try:
                        store_cached_queries(cache_file, ai_queries)
                        logger.debug(
                            "step.discovery_cache_store",
                            "Stored AI queries in cache",
                            count=len(ai_queries),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warn(
                            "step.discovery_cache_store_failed",
                            "Failed to write cache",
                            error=str(exc),
                        )

            all_queries = []
            seen_queries: set[str] = set()
            for query in [*seed_queries, *ai_queries]:
                if not query or query in seen_queries:
                    continue
                seen_queries.add(query)
                all_queries.append(query)
            all_queries = all_queries[:40]
            logger.debug(
                "step.discovery_done", "Prepared retrieval queries", total=len(all_queries)
            )

            logger.debug("step.retrieval", "Searching docs via rg")
            chunks = search_docs_with_queries(repo_root, all_queries, docs_files, logger)
            chunks = truncate_chunks_to_budget(chunks, int(config["docs"]["max_context_tokens"]))
            if not chunks:
                logger.status("step.retrieval_empty", "No relevant doc chunks found; allowing push")
                return exit_with(finalize_success_push_multi())

            docs_context = "\n\n".join(
                f"--- {chunk['file']} (lines {chunk['start_line']}-{chunk['end_line']}) ---\n{chunk['content']}"
                for chunk in chunks
            )
            docs_context_file.write_text(docs_context, encoding="utf-8")

            recent_commits = git(
                repo_root,
                ["log", "--oneline", "-n", "20", "--", "docs", "README.md"],
                check=False,
            )
            recent_commits_file = tmp_dir / "recent-commits.txt"
            recent_commits_file.write_text(recent_commits + "\n", encoding="utf-8")

            logger.debug("step.analysis", "Running consistency analysis")
            raw_issues = call_opencode_json_array_with_retries(
                repo_root=repo_root,
                opencode_executable=opencode_executable,
                model=llm_model,
                variant=llm_variant,
                timeout_seconds=int(config["llm"]["timeout_seconds"]),
                base_prompt=analysis_prompt_template,
                files=[diff_file, docs_context_file, recent_commits_file],
                logger=logger,
                stage_name="02-analysis",
                max_retries=int(config["llm"].get("json_max_retries", 2)),
                invalid_json_feedback_max_chars=int(
                    config["llm"].get("invalid_json_feedback_max_chars", 6000)
                ),
                json_retry_new_session=bool(config["llm"].get("json_retry_new_session", True)),
                transcript_dir=transcript_dir,
                run_id=run_id,
                session_title_prefix=str(config["llm"].get("session_title_prefix", "ai-doc-sync")),
                delete_session_after_run=bool(config["llm"].get("delete_session_after_run", True)),
                print_output=print_llm_output,
            )

            issues: list[dict[str, Any]] = []
            for item in raw_issues:
                if not isinstance(item, dict):
                    continue
                issues.append(
                    {
                        "file": str(item.get("file", "")).strip(),
                        "line": int(item.get("line", 0) or 0),
                        "description": str(item.get("description", "")).strip(),
                        "doc_excerpt": str(item.get("doc_excerpt", "")).strip(),
                        "suggested_fix": str(item.get("suggested_fix", "")).strip()
                        if item.get("suggested_fix") is not None
                        else "",
                    }
                )

            if not issues:
                logger.status(
                    "step.analysis_no_issues", "No documentation drift detected; allowing push"
                )
                return exit_with(finalize_success_push_multi())

            logger.warn(
                "step.analysis_issues", "Documentation drift detected", issue_count=len(issues)
            )
            for issue in issues[:8]:
                logger.warn("issue", format_issue(issue))

            mode = str(config["general"].get("mode", "apply-and-block"))
            if mode == "check-only":
                raise HookError("Documentation drift detected (check-only mode)")

            issues_file.write_text(
                json.dumps(issues, ensure_ascii=True, indent=2), encoding="utf-8"
            )
            logger.debug("step.apply", "Applying doc updates with OpenCode")
            apply_result = call_opencode(
                repo_root=repo_root,
                opencode_executable=opencode_executable,
                model=llm_model,
                variant=llm_variant,
                timeout_seconds=int(config["llm"]["timeout_seconds"]),
                prompt=apply_prompt_template,
                files=[
                    diff_file,
                    changed_file,
                    docs_inventory_file,
                    issues_file,
                    repo_root / "AGENTS.md",
                ],
                logger=logger,
                stage_name="03-apply",
                run_id=run_id,
                session_title_prefix=str(config["llm"].get("session_title_prefix", "ai-doc-sync")),
                call_purpose=stage_purpose("03-apply"),
                print_output=print_llm_output,
            )
            finalize_opencode_session(
                repo_root=repo_root,
                logger=logger,
                timeout_seconds=int(config["llm"]["timeout_seconds"]),
                run_id=run_id,
                stage_name="03-apply",
                session_id=apply_result.session_id,
                transcript_dir=transcript_dir,
                delete_session_after_run=bool(config["llm"].get("delete_session_after_run", True)),
                opencode_executable=opencode_executable,
            )
            if apply_result.return_code != 0:
                details = (
                    apply_result.stderr.strip()
                    or apply_result.stdout.strip()
                    or f"exit code {apply_result.return_code}"
                )
                raise HookError(f"OpenCode command failed: {details}")

            post_changes = set(
                line.strip()
                for line in git(repo_root, ["diff", "--name-only"], check=True).splitlines()
                if line.strip()
            )
            new_changes = sorted(post_changes - baseline)
            if not new_changes:
                raise HookError(
                    "Documentation issues were detected, but no doc updates were applied. "
                    "Run `opencode` manually to review/fix, then push again."
                )

            non_doc_changes = [
                path for path in new_changes if not is_doc_path(path, config["docs"])
            ]
            if non_doc_changes:
                logger.error(
                    "safety.non_doc_write",
                    "AI attempted to modify non-doc files; blocking push",
                    files=",".join(non_doc_changes),
                )
                raise HookError("AI attempted non-doc edits")

            summary_text = build_doc_change_summary(repo_root, new_changes)
            summary_path = summary_dir / f"{run_id}.md" if summary_dir is not None else None
            if summary_path is not None and write_text_file(summary_path, summary_text):
                logger.info(
                    "summary.written",
                    "Wrote doc change summary",
                    path=str(summary_path),
                )
            else:
                logger.warn(
                    "summary.write_failed",
                    "Failed to persist doc change summary to file",
                    path=str(summary_path) if summary_path is not None else "<disabled>",
                )

            logger.warn(
                "step.apply_block", "Doc updates were applied; review+commit required before push"
            )
            sys.stderr.write("[ai-doc-sync] Doc change summary:\n")
            sys.stderr.write(summary_text + "\n")
            if summary_path is not None:
                sys.stderr.write(f"[ai-doc-sync] Summary file: {summary_path}\n")
            sys.stderr.write(
                "[ai-doc-sync] No commit was created automatically. "
                "Create a new commit or amend your latest commit, then push again.\n"
            )
            return exit_with(1)

    except Exception as exc:  # noqa: BLE001
        message = str(exc) if str(exc) else exc.__class__.__name__
        if allow_push_on_error:
            logger.status(
                "hook.fail_open",
                f"Non-blocking AI docs sync issue: {message}. allow_push_on_error=true, allowing push.",
            )
            return exit_with(0)
        logger.error("hook.fail_closed", f"{message}. allow_push_on_error=false, blocking push.")
        return exit_with(1)


if __name__ == "__main__":
    sys.exit(main())
