from __future__ import annotations

from typing import Any

from ..executors.exec import (
    collect_commit_messages_for_ranges,
    current_branch,
    env_bool,
    is_feature_branch,
    lookup_open_pr_url,
)
from ..types import CollectorResult, RuntimeContext


def collect_pr_context(context: RuntimeContext, state: Any) -> CollectorResult:
    branch_name = current_branch(context.repo_root)
    flag_env = ""
    for step in state.module.steps:
        if step.when_env:
            flag_env = step.when_env
            break
    if flag_env and env_bool(flag_env) is not True:
        return CollectorResult(
            artifacts={"pr-context.txt": f"branch={branch_name}\nflag_env={flag_env}\n"},
            skip_module=True,
            skip_reason="PR create env flag is not enabled",
        )
    if not branch_name or branch_name in {"HEAD", "main"} or not is_feature_branch(branch_name):
        return CollectorResult(
            artifacts={"pr-context.txt": f"branch={branch_name}\n"},
            skip_module=True,
            skip_reason="branch does not require PR creation",
        )
    existing_pr_url = ""
    try:
        existing_pr_url = lookup_open_pr_url(context.repo_root, branch_name)
    except Exception:  # noqa: BLE001
        existing_pr_url = ""
    if existing_pr_url:
        return CollectorResult(
            artifacts={"pr-context.txt": f"branch={branch_name}\nexisting_pr_url={existing_pr_url}\n"},
            skip_module=True,
            skip_reason="open PR already exists",
            metadata={"existing_pr_url": existing_pr_url},
        )

    ranges = context.cache.get("ranges", [])
    changed_files = context.cache.get("changed_files", [])
    diff_text = context.cache.get("diff_text", "")
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
            "pr-context.txt": "\n".join(
                [
                    f"branch={branch_name}",
                    f"base_branch=main",
                    f"remote_name={context.remote_name or 'origin'}",
                ]
            )
            + "\n",
            "changed-files.txt": "\n".join(changed_files) + ("\n" if changed_files else ""),
            "push.diff": diff_text + ("\n" if diff_text and not diff_text.endswith("\n") else ""),
            "commits.txt": "\n".join(commit_lines).strip() + ("\n" if commit_lines else ""),
        }
    )
