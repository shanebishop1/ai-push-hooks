from __future__ import annotations

import os
import pathlib

import pytest

from ai_push_hooks.cli import init_config
from ai_push_hooks.types import HookError


def test_init_creates_config(tmp_path: pathlib.Path) -> None:
    assert init_config("minimal-docs", False, cwd=tmp_path) == 0
    assert (tmp_path / "ai-push-hooks.toml").exists()


def test_init_rejects_unsupported_template(tmp_path: pathlib.Path) -> None:
    with pytest.raises(HookError, match="Only `minimal-docs`"):
        init_config("ezeke-compatible", False, cwd=tmp_path)


def test_init_refuses_overwrite_without_force(tmp_path: pathlib.Path) -> None:
    init_config("minimal-docs", False, cwd=tmp_path)
    with pytest.raises(HookError, match="Refusing to overwrite"):
        init_config("minimal-docs", False, cwd=tmp_path)


def test_init_force_replaces_existing_regular_file(tmp_path: pathlib.Path) -> None:
    config_path = tmp_path / "ai-push-hooks.toml"
    config_path.write_text("old config\n", encoding="utf-8")

    assert init_config("minimal-docs", True, cwd=tmp_path) == 0
    assert "old config" not in config_path.read_text(encoding="utf-8")


def test_init_without_force_refuses_dangling_symlink(tmp_path: pathlib.Path) -> None:
    link_path = tmp_path / "ai-push-hooks.toml"
    target_path = tmp_path / "missing-target.toml"
    link_path.symlink_to(target_path)

    with pytest.raises(HookError, match="symlink"):
        init_config("minimal-docs", False, cwd=tmp_path)

    assert link_path.is_symlink()
    assert not target_path.exists()


@pytest.mark.parametrize("target_exists", [False, True])
def test_init_force_refuses_external_symlink_without_touching_target(
    tmp_path: pathlib.Path, target_exists: bool
) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    target_path = outside_dir / "target.toml"
    if target_exists:
        target_path.write_text("keep me\n", encoding="utf-8")
    link_path = tmp_path / "ai-push-hooks.toml"
    link_path.symlink_to(target_path)

    with pytest.raises(HookError, match="symlink"):
        init_config("minimal-docs", True, cwd=tmp_path)

    assert link_path.is_symlink()
    if target_exists:
        assert target_path.read_text(encoding="utf-8") == "keep me\n"
    else:
        assert not target_path.exists()


def test_init_force_refuses_special_file(tmp_path: pathlib.Path) -> None:
    config_path = tmp_path / "ai-push-hooks.toml"
    os.mkfifo(config_path)

    with pytest.raises(HookError, match="non-regular"):
        init_config("minimal-docs", True, cwd=tmp_path)

    assert config_path.is_fifo()
