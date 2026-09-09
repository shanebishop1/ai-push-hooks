from __future__ import annotations

import pathlib

from ai_push_hooks.config import load_config, resolve_runner_profile
from ai_push_hooks.prompts_builtin import MINIMAL_DOCS_TEMPLATE


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
    assert profile.model == "openai/gpt-5.6-terra"
