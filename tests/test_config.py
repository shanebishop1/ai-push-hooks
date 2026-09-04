from __future__ import annotations

import pathlib
import re

import pytest

from ai_push_hooks.artifacts import generate_run_id
from ai_push_hooks.config import load_config
from ai_push_hooks.executors import exec as exec_module
from ai_push_hooks.types import HookError

from .conftest import init_repo


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
    local_oid = "a" * 40
    base_oid = "b" * 40

    def fake_git(cwd, args, check=True):
        calls.append(args)
        if args == ["remote"]:
            return "origin"
        if args == [
            "rev-parse",
            "--verify",
            "--quiet",
            f"{local_oid}^{{commit}}",
        ]:
            return local_oid
        if args == [
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/remotes/origin/develop^{commit}",
        ]:
            return base_oid
        if args == ["merge-base", local_oid, base_oid]:
            return base_oid
        return ""

    monkeypatch.setattr(exec_module, "git", fake_git)

    ranges = exec_module.collect_ranges_from_stdin(
        pathlib.Path("/repo"),
        "origin",
        [f"refs/heads/feature/x {local_oid} refs/heads/feature/x {'0' * 40}"],
        "develop",
    )

    assert ranges == [f"{base_oid}..{local_oid}"]
    assert ["merge-base", local_oid, base_oid] in calls


@pytest.mark.parametrize("storage_path", ["/tmp/logs", "../logs", "C:\\temp\\logs"])
def test_load_config_rejects_storage_paths_outside_repository(
    tmp_path: pathlib.Path, storage_path: str
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[logging]
dir = {storage_path!r}

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

    with pytest.raises(HookError, match="logging.dir"):
        load_config(tmp_path)


def test_resolve_storage_path_rejects_symlink_escape(tmp_path: pathlib.Path) -> None:
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    outside = tmp_path / "outside"
    repo_root.mkdir()
    git_dir.mkdir()
    outside.mkdir()
    (repo_root / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HookError, match="symlink"):
        exec_module.resolve_storage_path(repo_root, git_dir, "logs/output")


def test_resolve_storage_path_rejects_git_namespace_symlink(tmp_path: pathlib.Path) -> None:
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    objects = git_dir / "objects"
    repo_root.mkdir()
    objects.mkdir(parents=True)
    (git_dir / "ai-push-hooks").symlink_to(objects, target_is_directory=True)

    with pytest.raises(HookError, match="symlink"):
        exec_module.resolve_storage_path(
            repo_root, git_dir, ".git/ai-push-hooks/logs"
        )


@pytest.mark.parametrize("key", ["dir", "transcript_dir", "summary_dir"])
@pytest.mark.parametrize("storage_path", ["logs", ".git/objects", ".git/hooks/output"])
def test_load_config_constrains_all_runtime_storage_to_owned_git_namespace(
    tmp_path: pathlib.Path, key: str, storage_path: str
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[logging]
{key} = {storage_path!r}

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

    with pytest.raises(HookError, match="must be inside .git/ai-push-hooks"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "protected_allow_path", [".GiT/**", ".ＧＩＴ/**", "aGeNtS.Md", "ＡＧＥＮＴＳ.md"]
)
def test_load_config_rejects_case_variant_protected_apply_paths(
    tmp_path: pathlib.Path, protected_allow_path: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/config")
    config_path = repo / "ai-push-hooks.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'allow_paths = ["README.md", "docs/**/*.md"]',
            f'allow_paths = ["{protected_allow_path}"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="Git metadata|AGENTS.md"):
        load_config(repo)
