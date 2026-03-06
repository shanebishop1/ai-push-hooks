from __future__ import annotations

import json
import pathlib
import sys
from typing import Sequence

from .artifacts import ArtifactStore, generate_run_id
from .config import load_config
from .engine import WorkflowEngine
from .executors.exec import (
    collect_changed_files,
    collect_diff,
    collect_ranges_from_stdin,
    current_branch,
    ensure_dir,
    git,
    resolve_git_dir,
    resolve_repo_root,
    resolve_storage_path,
    should_skip_for_sync_branch,
)
from .executors.llm import resolve_opencode_executable
from .types import HookConfig, HookError, HookLogger, RuntimeContext


def _build_logger(repo_root: pathlib.Path, git_dir: pathlib.Path, config: HookConfig) -> HookLogger:
    jsonl_path = None
    if config.logging.jsonl:
        log_dir = ensure_dir(resolve_storage_path(repo_root, git_dir, config.logging.dir))
        if log_dir is not None:
            jsonl_path = log_dir / "hook.jsonl"
    return HookLogger(jsonl_path=jsonl_path, console_level=config.logging.level)


def _write_summary(context: RuntimeContext, result: dict[str, object]) -> None:
    summary_dir = ensure_dir(
        resolve_storage_path(context.repo_root, context.git_dir, context.config.logging.summary_dir)
    )
    if summary_dir is None:
        return
    summary_path = summary_dir / f"{context.run_id}.json"
    summary_path.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _assert_clean_worktree(repo_root: pathlib.Path) -> None:
    status = git(repo_root, ["status", "--short"], check=False).strip()
    if status:
        raise HookError("Hook requires a clean worktree but local changes are present")


def run_hook(
    remote_name: str = "",
    remote_url: str = "",
    stdin_lines: Sequence[str] | None = None,
    cwd: pathlib.Path | None = None,
) -> int:
    current_dir = cwd or pathlib.Path.cwd()
    repo_root = resolve_repo_root(current_dir)
    git_dir = resolve_git_dir(repo_root)
    config, _config_path = load_config(repo_root)
    logger = _build_logger(repo_root, git_dir, config)

    if not config.general.enabled:
        logger.status("hook.disabled", "AI doc sync hook disabled")
        return 0
    if config.general.require_clean_worktree:
        _assert_clean_worktree(repo_root)
    if config.general.skip_on_sync_branch:
        skip_sync, reason = should_skip_for_sync_branch(repo_root)
        if skip_sync:
            logger.status("hook.skip_sync_branch", f"Skipping AI doc sync hook: {reason}")
            return 0

    actual_stdin = list(stdin_lines) if stdin_lines is not None else [line.rstrip("\n") for line in sys.stdin]
    ranges = collect_ranges_from_stdin(repo_root, remote_name or "origin", actual_stdin)
    changed_files = collect_changed_files(repo_root, ranges) if ranges else []
    diff_text = collect_diff(repo_root, ranges, config.llm.max_diff_bytes) if ranges else ""
    run_id = generate_run_id()
    run_dir = resolve_storage_path(repo_root, git_dir, f".git/ai-doc-sync/runs/{run_id}")

    opencode_executable = None
    if any(
        step.type in {"llm", "apply"}
        for module in config.modules.values()
        for step in module.steps
        if module.enabled
    ):
        opencode_executable = resolve_opencode_executable()

    context = RuntimeContext(
        repo_root=repo_root,
        git_dir=git_dir,
        config=config,
        logger=logger,
        remote_name=remote_name or "origin",
        remote_url=remote_url,
        stdin_lines=actual_stdin,
        run_id=run_id,
        run_dir=run_dir,
        opencode_executable=opencode_executable,
        cache={
            "ranges": ranges,
            "changed_files": changed_files,
            "diff_text": diff_text,
            "branch_name": current_branch(repo_root),
            "sync_branch": "beads-sync",
        },
    )
    logger.status(
        "hook.start",
        "Starting AI docs sync workflow",
        branch=context.cache["branch_name"],
        changed_files=len(changed_files),
        ranges=ranges,
    )
    engine = WorkflowEngine(context=context, artifacts=ArtifactStore(run_dir))
    try:
        workflow_result = engine.run()
        logger.llm_summary()
        _write_summary(context, {"run_dir": str(workflow_result.run_dir), "modules": workflow_result.modules})
        logger.status("hook.complete", "AI docs sync workflow completed", run_dir=str(workflow_result.run_dir))
        return 0
    except Exception as exc:  # noqa: BLE001
        message = str(exc).strip() or exc.__class__.__name__
        logger.error("hook.failed", "AI docs sync workflow failed", error=message)
        if config.general.allow_push_on_error:
            logger.warn("hook.fail_open", "Allowing push because allow_push_on_error=true", error=message)
            return 0
        raise
