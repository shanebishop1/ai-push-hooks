from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import time
from typing import Any

from .. import git_utils
from ..paths import (
    is_path_within,
    normalized_component,
    path_has_symlink,
    path_is_link_or_reparse,
    relative_path_parts,
    resolve_contained_path,
)
from ..types import HookError, ModuleRuntimeState, RuntimeContext, StepConfig
from .ask import validate_schema

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
BEADS_MIGRATION_OVERRIDE_ENV_NAMES = frozenset(
    {
        "BD_ALLOW_REMOTE_MIGRATE",
        "BD_IGNORE_SCHEMA_SKEW",
        "BD_SMART_GATE",
    }
)


def _report_file_path(
    context: RuntimeContext, state: ModuleRuntimeState
) -> pathlib.Path:
    branch_context = state.artifacts.get("collect/branch-context.txt")
    if branch_context and branch_context.exists():
        payload = git_utils.parse_key_value_text(
            branch_context.read_text(encoding="utf-8")
        )
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
            raise HookError(
                f"Invalid Beads issue id in alignment command: {issue_id!r}"
            )


def validate_beads_alignment_command(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise HookError("Beads alignment commands must be non-empty strings")
    if (
        len(command) > 4096
        or "\x00" in command
        or any(ord(char) < 32 for char in command)
    ):
        raise HookError("Beads alignment command contains invalid or excessive input")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise HookError(f"Malformed Beads alignment command: {exc}") from exc

    if len(argv) < 3 or argv[0] != "bd":
        raise HookError("Beads alignment commands must use the literal `bd` executable")

    subcommand = argv[1]
    if subcommand == "update":
        if (
            len(argv) < 5
            or argv[-2] != "--status"
            or argv[-1] not in BEADS_UPDATE_STATUSES
        ):
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
        raise HookError(
            f"Refusing repository-contained `bd` executable: {lexical_candidate}"
        )
    try:
        executable = lexical_candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HookError("Unable to safely resolve the `bd` executable") from exc
    if is_path_within(executable, resolved_repo_root):
        raise HookError(f"Refusing repository-contained `bd` executable: {executable}")
    if path_is_link_or_reparse(executable) or not stat.S_ISREG(
        executable.stat().st_mode
    ):
        raise HookError(f"Resolved `bd` executable is not a regular file: {executable}")
    if not os.access(executable, os.X_OK):
        raise HookError(f"Resolved `bd` executable is not executable: {executable}")
    return str(executable)


def beads_alignment_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name not in BEADS_MIGRATION_OVERRIDE_ENV_NAMES
        and (name in BEADS_ENV_NAMES or name.startswith(BEADS_ENV_PREFIXES))
    }


def beads_alignment_executor(
    context: RuntimeContext,
    state: ModuleRuntimeState,
    step: StepConfig,
    inputs: list[pathlib.Path],
) -> dict[str, Any]:
    if state.metadata.get("skip_module"):
        return {
            "skipped": True,
            "commands_run": [],
            "report_written": False,
            "unresolved": False,
        }
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
    validated_commands = [
        validate_beads_alignment_command(command) for command in commands
    ]
    beads_executable = resolve_beads_executable(context.repo_root) if commands else ""
    command_env = beads_alignment_env()
    report_path = _report_file_path(context, state)
    commands_run: list[str] = []
    started_at = time.monotonic()
    for command, argv in zip(commands, validated_commands):
        remaining = BEADS_ALIGNMENT_TOTAL_TIMEOUT_SECONDS - (
            time.monotonic() - started_at
        )
        if remaining <= 0:
            raise HookError(
                f"Beads alignment exceeded its {BEADS_ALIGNMENT_TOTAL_TIMEOUT_SECONDS}-second total budget"
            )
        git_utils.run_command(
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
        if not git_utils.write_text_file(
            report_path, report_markdown, root=context.repo_root
        ):
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
            context.cache.get(
                "branch_selection_reason", "no single pushed branch is available"
            )
        )
        raise HookError(f"PR creation requires one pushed branch: {reason}")
    default_base_branch = context.config.general.base_branch.strip() or "main"
    if bool(context.cache.get("branch_is_new", False)):
        reason = git_utils.initial_pr_defer_reason(branch_name, default_base_branch)
        context.logger.warn("pr.create_deferred", reason, branch=branch_name)  # noqa: G010, PLE1205
        return {
            "skipped": True,
            "pr_url": "",
            "deferred_until_remote": True,
            "reason": reason,
        }
    payload = validate_schema(
        "pr_create_payload", json.loads(inputs[0].read_text(encoding="utf-8"))
    )
    if shutil.which("gh") is None:
        raise HookError("`gh` is required for PR creation but is not installed")
    repository = git_utils.resolve_github_repository(
        context.repo_root, context.remote_name, context.remote_url
    )
    existing_pr = git_utils.lookup_open_pr_url(
        context.repo_root, branch_name, default_base_branch, repository
    )
    if existing_pr:
        return {"skipped": False, "pr_url": existing_pr, "already_exists": True}

    base_branch = default_base_branch
    head_branch = branch_name
    title = git_utils.sanitize_pr_title(payload.get("title", "").strip(), branch_name)
    body = payload.get("body", "").strip()
    if not body:
        commits = git_utils.collect_commit_messages_for_ranges(
            context.repo_root,
            context.cache.get("branch_ranges", context.cache.get("ranges", [])),
        )
        body = git_utils.build_fallback_pr_body(
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
    if payload.get("draft", False):
        args.append("--draft")
    created = git_utils.run_command(args, cwd=context.repo_root, check=False)
    combined_output = "\n".join(
        [(created.stdout or "").strip(), (created.stderr or "").strip()]
    )
    if created.returncode != 0:
        # A URL in failed-command output is not proof that the create operation
        # succeeded. Reconcile against GitHub before accepting the result.
        pr_url = git_utils.lookup_open_pr_url(
            context.repo_root, branch_name, default_base_branch, repository
        )
    else:
        pr_url = git_utils.extract_pr_url(combined_output)
    if not pr_url:
        details = git_utils._command_diagnostics(
            args, created.stdout or "", created.stderr or ""
        )
        raise HookError(
            details or f"gh pr create failed with exit code {created.returncode}"
        )
    return {"skipped": False, "pr_url": pr_url, "already_exists": False}


EXEC_HANDLERS = {
    "beads_alignment": beads_alignment_executor,
    "gh_pr_create": gh_pr_create_executor,
}
