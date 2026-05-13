from __future__ import annotations

import pathlib
import re

import pytest

from ai_push_hooks.artifacts import generate_run_id
from ai_push_hooks.config import load_config
from ai_push_hooks.executors import exec as exec_module
from ai_push_hooks.types import HookError


def test_load_config_requires_config_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(HookError, match="Missing required config file `ai-push-hooks.toml`"):
        load_config(tmp_path)


def test_load_config_rejects_legacy_shape(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[prompts]
query_file = "query.txt"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(HookError, match="Legacy or unsupported config keys"):
        load_config(tmp_path)


def test_load_config_rejects_unknown_step_type(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "bad"
type = "mystery"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(HookError, match="Unknown step type"):
        load_config(tmp_path)


def test_load_config_supports_standard_toml_inline_tables(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules]
docs = { enabled = true, steps = [{ id = "collect", type = "collect", collector = "docs_context" }] }
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.workflow.modules == ("docs",)
    assert config.modules["docs"].steps[0].id == "collect"


def test_load_config_ignores_legacy_dot_filename(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = false

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(HookError, match="Missing required config file `ai-push-hooks.toml`"):
        load_config(tmp_path)


def test_generate_run_id_is_unique_and_high_resolution() -> None:
    first = generate_run_id()
    second = generate_run_id()

    assert first != second
    assert re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{8}", first)


def test_base_branch_can_be_overridden_by_env(tmp_path: pathlib.Path, monkeypatch) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[general]
base_branch = "develop"

[workflow]
modules = ["docs"]

[modules.docs]
enabled = false

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PUSH_HOOKS_BASE_BRANCH", "release")

    config, _ = load_config(tmp_path)

    assert config.general.base_branch == "release"


def test_collect_ranges_uses_configured_base_branch_for_new_remote_branch(monkeypatch) -> None:
    calls = []

    def fake_git(cwd, args, check=True):
        calls.append(args)
        if args == ["merge-base", "abc123", "origin/develop"]:
            return "base123"
        return ""

    monkeypatch.setattr(exec_module, "git", fake_git)

    ranges = exec_module.collect_ranges_from_stdin(
        pathlib.Path("/repo"),
        "origin",
        ["refs/heads/feature/x abc123 refs/heads/feature/x 0000000000000000000000000000000000000000"],
        "develop",
    )

    assert ranges == ["base123..abc123"]
    assert ["merge-base", "abc123", "origin/develop"] in calls
