from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

from ai_push_hooks.config import load_config, resolve_runner_profile
from ai_push_hooks.prompts_builtin import MINIMAL_DOCS_TEMPLATE
from ai_push_hooks.types import LlmConfig


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
    readme = (pathlib.Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
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
    context = SimpleNamespace(repo_root=tmp_path, push=SimpleNamespace(diff_text="test diff"))
    artifacts = namespace["collect_rules"](context).artifacts
    assert artifacts == {"push.diff": "test diff", "rules.txt": "Use our typed API client."}

    issues = tmp_path / "issues.json"
    context.inputs = {"review/issues.json": issues}
    issues.write_text("[]", encoding="utf-8")
    assert namespace["assert_rules"](context)["ok"] is True
    issues.write_text('[{"file":"src/app.ts","description":"Direct request"}]', encoding="utf-8")
    assert namespace["assert_rules"](context)["ok"] is False
