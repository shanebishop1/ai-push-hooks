from __future__ import annotations

import pathlib

import pytest

from ai_doc_sync_hook.cli import init_config
from ai_doc_sync_hook.types import HookError


def test_init_creates_config(tmp_path: pathlib.Path) -> None:
    assert init_config("minimal-docs", False, cwd=tmp_path) == 0
    assert (tmp_path / ".ai-doc-sync.toml").exists()


def test_init_rejects_unsupported_template(tmp_path: pathlib.Path) -> None:
    with pytest.raises(HookError, match="Only `minimal-docs`"):
        init_config("ezeke-compatible", False, cwd=tmp_path)


def test_init_refuses_overwrite_without_force(tmp_path: pathlib.Path) -> None:
    init_config("minimal-docs", False, cwd=tmp_path)
    with pytest.raises(HookError, match="Refusing to overwrite"):
        init_config("minimal-docs", False, cwd=tmp_path)
