from __future__ import annotations

from typing import Any

from ..executors.exec import collect_commit_messages_for_ranges, is_feature_branch
from ..types import CollectorResult, RuntimeContext


def collect_beads_status_context(context: RuntimeContext, state: Any) -> CollectorResult:
    branch_name = str(context.cache.get("branch_name", ""))
    branch_selection_reason = str(
        context.cache.get("branch_selection_reason", "no pushed branch updates")
    )
    if not branch_name:
        return CollectorResult(
            artifacts={
                "branch-context.txt": (
                    f"branch=\nbranch_selection_reason={branch_selection_reason}\n"
                )
            },
            skip_module=True,
            skip_reason=branch_selection_reason,
        )
    sync_branch = context.cache.get("sync_branch", "beads-sync")
    if branch_name in {"HEAD", "main", sync_branch} or not is_feature_branch(branch_name):
        return CollectorResult(
            artifacts={"branch-context.txt": f"branch={branch_name}\n"},
            skip_module=True,
            skip_reason="branch does not require beads alignment",
        )

    ranges = context.cache.get("branch_ranges", context.cache.get("ranges", []))
    changed_files = context.cache.get(
        "branch_changed_files", context.cache.get("changed_files", [])
    )
    diff_text = context.cache.get("branch_diff_text", context.cache.get("diff_text", ""))
    commits = collect_commit_messages_for_ranges(context.repo_root, ranges) if ranges else []
    report_file = "BEADS_STATUS_ACTION_REQUIRED.md"
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
            "branch-context.txt": "\n".join(
                [
                    f"branch={branch_name}",
                    f"ranges={','.join(ranges)}",
                    f"report_file={report_file}",
                ]
            )
            + "\n",
            "changed-files.txt": "\n".join(changed_files) + ("\n" if changed_files else ""),
            "push.diff": diff_text + ("\n" if diff_text and not diff_text.endswith("\n") else ""),
            "commits.txt": "\n".join(commit_lines).strip() + ("\n" if commit_lines else ""),
        }
    )
