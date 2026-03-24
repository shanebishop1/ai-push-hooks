from __future__ import annotations

import pathlib
import re

import pytest

from ai_push_hooks.artifacts import generate_run_id
from ai_push_hooks.config import load_config
from ai_push_hooks.types import HookError


def test_load_config_uses_builtin_defaults_when_file_missing(tmp_path: pathlib.Path) -> None:
    config, path = load_config(tmp_path)
    assert path is None
    assert config.workflow.modules == ("docs",)
    assert config.modules["docs"].steps[0].collector == "docs_context"


def test_load_config_rejects_legacy_shape(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".ai-push-hooks.toml").write_text(
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
    (tmp_path / ".ai-push-hooks.toml").write_text(
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


def test_load_config_prefers_non_dot_filename_when_both_exist(tmp_path: pathlib.Path) -> None:
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
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config, path = load_config(tmp_path)

    assert path == tmp_path / "ai-push-hooks.toml"
    assert config.modules["docs"].enabled is True


def test_generate_run_id_is_unique_and_high_resolution() -> None:
    first = generate_run_id()
    second = generate_run_id()

    assert first != second
    assert re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{8}", first)
