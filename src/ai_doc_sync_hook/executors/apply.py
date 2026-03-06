from __future__ import annotations

import json
import pathlib

from ..types import HookError, ModuleRuntimeState, RuntimeContext, StepConfig
from .exec import list_repo_changes, path_matches
from .llm import call_opencode, finalize_opencode_session


def run_apply_step(
    context: RuntimeContext,
    state: ModuleRuntimeState,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
) -> dict[str, object]:
    for input_path in input_paths:
        if input_path.name.endswith("issues.json"):
            issues = json.loads(input_path.read_text(encoding="utf-8"))
            if isinstance(issues, list) and not issues:
                return {"changed": False, "changed_files": [], "skipped": True}

    baseline = list_repo_changes(context.repo_root)
    files = list(input_paths)
    agents = context.repo_root / "AGENTS.md"
    if agents.exists():
        files.append(agents)

    result = call_opencode(
        context,
        stage_name=stage_name,
        purpose=f"{step.type}:{step.id}",
        prompt=prompt,
        files=files,
    )
    finalize_opencode_session(context, stage_name, result.session_id)
    if result.return_code != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"exit code {result.return_code}"
        raise HookError(f"Apply step failed: {details}")

    after = list_repo_changes(context.repo_root)
    changed_files = sorted(after - baseline)
    unexpected = [
        path for path in changed_files if not any(path_matches(path, pattern) for pattern in step.allow_paths)
    ]
    if unexpected:
        raise HookError("Apply step modified files outside allowlist: " + ", ".join(unexpected))
    return {
        "changed": bool(changed_files),
        "changed_files": changed_files,
        "allowed_paths": list(step.allow_paths),
        "skipped": False,
    }
