from __future__ import annotations

import pathlib

import pytest

from ai_doc_sync_hook.config import load_config
from ai_doc_sync_hook.types import HookError


def test_load_config_uses_builtin_defaults_when_file_missing(tmp_path: pathlib.Path) -> None:
    config, path = load_config(tmp_path)
    assert path is None
    assert config.workflow.modules == ("docs",)
    assert config.modules["docs"].steps[0].collector == "docs_context"


def test_load_config_rejects_legacy_shape(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".ai-doc-sync.toml").write_text(
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
    (tmp_path / ".ai-doc-sync.toml").write_text(
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
