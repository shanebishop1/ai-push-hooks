from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.config import load_config, resolve_runner_profile
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.prompts_builtin import MINIMAL_DOCS_TEMPLATE
from ai_push_hooks.types import HookError, LlmConfig, RunnerProfile

from .conftest import build_context, init_repo


def test_generated_minimal_docs_starter_loads_with_compatibility_runner(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    for name in (
        "AI_PUSH_HOOKS_MODEL",
        "AI_PUSH_HOOKS_VARIANT",
        "AI_PUSH_HOOKS_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "ai-push-hooks.toml"
    config_path.write_text(MINIMAL_DOCS_TEMPLATE, encoding="utf-8")

    config, loaded_path = load_config(tmp_path)

    assert loaded_path == config_path
    assert config.llm.runner == "opencode"
    assert not config.runners
    query_step = config.modules["docs"].steps[1]
    profile = resolve_runner_profile(config, query_step, {})
    assert (profile.name, profile.type, profile.project_access) == (
        "opencode",
        "opencode",
        "artifacts",
    )
    assert profile.model == LlmConfig().model == "openai/gpt-5.6-luna"


def test_readme_rules_example(tmp_path: pathlib.Path) -> None:
    readme = (pathlib.Path(__file__).parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    callback = re.findall(r"```python\n(.*?)```", readme, re.DOTALL)[0]
    configuration = re.findall(r"```toml\n(.*?)```", readme, re.DOTALL)[0]
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "hooks.py").write_text(callback, encoding="utf-8")
    (tmp_path / "ai-push-hooks.toml").write_text(configuration, encoding="utf-8")
    config, _ = load_config(tmp_path)
    assert config.workflow.modules == ("rules",)
    assert config.runners["opencode"].model == "openai/gpt-5.6-luna"

    namespace = {}
    exec(compile(callback, "README.md", "exec"), namespace)
    (tmp_path / "AGENTS.md").write_text("Use our typed API client.", encoding="utf-8")
    context = SimpleNamespace(
        repo_root=tmp_path, push=SimpleNamespace(diff_text="test diff")
    )
    artifacts = namespace["collect_rules"](context).artifacts
    assert artifacts == {
        "push.diff": "test diff",
        "rules.txt": "Use our typed API client.",
    }

    issues = tmp_path / "issues.json"
    context.inputs = {"review/issues.json": issues}
    issues.write_text("[]", encoding="utf-8")
    assert namespace["assert_rules"](context)["ok"] is True
    issues.write_text(
        '[{"file":"src/app.ts","description":"Direct request"}]', encoding="utf-8"
    )
    assert namespace["assert_rules"](context)["ok"] is False


def _postcondition_recipe() -> str:
    configuration = (
        pathlib.Path(__file__).parents[1] / "docs" / "configuration.md"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"## Deterministic postconditions after apply.*?```toml\n(.*?)```",
        configuration,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize(
    ("initial_status", "applied_status", "failure", "post_ok", "manual_ok"),
    [
        ("DRAFT", None, "command", False, None),
        ("DRAFT", "WRONG", "command", False, None),
        ("READY", None, None, True, True),
        ("DRAFT", "READY", "Documentation updates were applied", True, False),
    ],
)
def test_documented_postcondition_recipe_with_real_engine(
    tmp_path: pathlib.Path,
    initial_status: str,
    applied_status: str | None,
    failure: str | None,
    post_ok: bool,
    manual_ok: bool | None,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / "README.md").write_text(
        f"Release note: {initial_status}.\n", encoding="utf-8"
    )
    configuration_path = repo / "ai-push-hooks.toml"
    configuration_path.write_text(_postcondition_recipe(), encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md", "ai-push-hooks.toml"], cwd=repo, check=True
    )
    subprocess.run(["git", "commit", "-m", "set release note"], cwd=repo, check=True)
    config, loaded_path = load_config(repo)
    assert loaded_path == configuration_path
    assert config.general.require_clean_worktree is True
    assert config.workflow.modules == ("docs",)
    apply, postcondition, manual_commit = config.modules["docs"].steps
    assert [apply.id, postcondition.id, manual_commit.id] == [
        "apply",
        "postcondition",
        "manual-commit",
    ]
    assert apply.allow_paths == ("README.md",)
    assert postcondition.inputs == manual_commit.inputs == ("apply/result.json",)
    assert postcondition.command[0] == "{python}"
    assert "sys.exit(0 if" in postcondition.command[2]
    assert "assert " not in postcondition.command[2]
    applied_content = f"Release note: {applied_status}.\n" if applied_status else None
    apply_script = (
        "print('apply completed')"
        if applied_content is None
        else (
            "from pathlib import Path; "
            f"Path('README.md').write_text({applied_content!r}, encoding='utf-8'); "
            "print('apply completed')"
        )
    )
    profile = RunnerProfile(
        name="deterministic",
        type="command",
        project_access="artifacts",
        command=(sys.executable, "-c", apply_script),
    )
    docs_module = config.modules["docs"]
    docs_module = replace(
        docs_module,
        steps=tuple(
            replace(step, runner="deterministic") if step.id == "apply" else step
            for step in docs_module.steps
        ),
    )
    config = replace(
        config,
        modules={"docs": docs_module},
        runners={"deterministic": profile},
    )
    context = build_context(repo, config)
    engine = WorkflowEngine(context, ArtifactStore(context.run_dir))

    if failure is None:
        assert engine.run().modules == {"docs": "completed"}
    else:
        with pytest.raises(HookError, match=failure):
            engine.run()

    expected_status = applied_status or initial_status
    assert (repo / "README.md").read_text(encoding="utf-8") == (
        f"Release note: {expected_status}.\n"
    )
    run_dir = engine.context.run_dir
    apply_result = json.loads(
        (run_dir / "docs" / "00-apply" / "result.json").read_text(encoding="utf-8")
    )
    postcondition_result = json.loads(
        (run_dir / "docs" / "01-postcondition" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert apply_result["skipped"] is False
    assert apply_result["changed_files"] == (
        [] if applied_status is None else ["README.md"]
    )
    assert postcondition_result["ok"] is post_ok
    if manual_ok is None:
        assert not (run_dir / "docs" / "02-manual-commit" / "result.json").exists()
    else:
        manual_result = json.loads(
            (run_dir / "docs" / "02-manual-commit" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        assert manual_result["ok"] is manual_ok
