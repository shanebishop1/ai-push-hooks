"""Repository-local callbacks for the demo workflow.

Loaded by ai-push-hooks as trusted in-process code. `collect_rules` hands the
outgoing diff and the house rules to the runner explicitly -- the workflow never
relies on a CLI picking up AGENTS.md on its own. `assert_rules` turns the
runner's findings into a push verdict.
"""

from __future__ import annotations

import json

from ai_push_hooks.plugins import CollectorResult, PluginContext


def collect_rules(context: PluginContext) -> CollectorResult:
    rules = (context.repo_root / "AGENTS.md").read_text(encoding="utf-8")
    changed = "\n".join(context.push.changed_files)
    return CollectorResult(
        artifacts={
            "push.diff": context.push.diff_text,
            "rules.txt": rules,
            "changed-files.txt": f"{changed}\n",
        },
        metadata={"changed_file_count": len(context.push.changed_files)},
    )


def assert_rules(context: PluginContext) -> dict:
    payload = context.inputs["review/issues.json"].read_text(encoding="utf-8")
    issues = json.loads(payload)
    if not issues:
        return {"ok": True, "message": "No AGENTS.md violations in the outgoing diff."}

    lines = [
        f"  {issue.get('file', '?')}: {issue.get('description', '')}" for issue in issues
    ]
    return {
        "ok": False,
        "message": "AGENTS.md violations in the outgoing diff:\n" + "\n".join(lines),
    }
