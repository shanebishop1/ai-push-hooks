from __future__ import annotations

import pathlib
import stat
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_push_hooks import paths as path_utils
from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.config import load_config
from ai_push_hooks.executors.runners import (
    ProcessResult,
    RunnerRequest,
    RunnerResult,
    SessionMetadata,
)
from ai_push_hooks.executors.runners.opencode import OpenCodeRunner
from ai_push_hooks.hook import _build_logger, _write_summary
from ai_push_hooks.types import HookError, ModuleRuntimeState

from .conftest import build_context, init_repo


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _lifecycle_request(context) -> RunnerRequest:
    return RunnerRequest(
        profile_id="opencode",
        runner_type="opencode",
        stage="docs.query",
        purpose="ask:query",
        mode="ask",
        instruction="prompt",
        cwd=context.repo_root,
        timeout_seconds=3,
        integration_context=context,
    )


def test_runtime_directories_and_files_are_private_by_default(
    tmp_path, monkeypatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/runtime-modes")
    config, _ = load_config(repo)
    config = replace(config, llm=replace(config.llm, delete_session_after_run=False))
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"

    store = ArtifactStore(context.run_dir)
    state = ModuleRuntimeState(module=config.modules["docs"])
    artifact = store.write_text(state, 0, "collect", "private.txt", "private\n")

    runtime_root = context.git_dir / "ai-push-hooks"
    log_path = runtime_root / "logs" / "hook.jsonl"
    log_path.parent.mkdir(parents=True)
    runtime_root.chmod(0o777)
    log_path.parent.chmod(0o777)
    log_path.write_text("existing\n", encoding="utf-8")
    log_path.chmod(0o666)
    context.logger = _build_logger(repo, context.git_dir, config)
    context.logger.status("test.private", "private runtime output")
    _write_summary(context, {"ok": True})

    def fake_run_process(args, **kwargs):
        assert not pathlib.Path(kwargs["cwd"]).is_relative_to(repo.resolve())
        assert list(kwargs["cwd"].iterdir()) == []
        return ProcessResult(0, '{"session":"ok"}\n', "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    OpenCodeRunner().finalize(
        _lifecycle_request(context),
        RunnerResult("[]", 0, "", "", SessionMetadata("session-1", "persisted", True)),
    )

    transcript_dir = runtime_root / "transcripts"
    transcript = next(transcript_dir.iterdir())
    summary = runtime_root / "summaries" / f"{context.run_id}.json"

    for directory in (
        context.run_dir,
        artifact.parent,
        runtime_root,
        log_path.parent,
        summary.parent,
        transcript_dir,
        context.run_dir / "opencode-isolation",
    ):
        assert _mode(directory) == 0o700
    for directory in (context.run_dir / "opencode-isolation").rglob("*"):
        if directory.is_dir():
            assert _mode(directory) == 0o700
    for file_path in (artifact, log_path, summary, transcript):
        assert _mode(file_path) == 0o600


@pytest.mark.parametrize("replacement", ["symlink", "file"])
@pytest.mark.parametrize("use_private_root", [False, True])
def test_private_directory_rejects_preexisting_unsafe_target_without_chmod(
    tmp_path: pathlib.Path, replacement: str, use_private_root: bool
) -> None:
    private_root = tmp_path / "private-root"
    if use_private_root:
        private_root.mkdir()
    target_root = private_root if use_private_root else tmp_path
    target = target_root / "private"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)

    if replacement == "symlink":
        target.symlink_to(outside, target_is_directory=True)
        expected_mode_path = outside
    else:
        target.write_text("not a directory\n", encoding="utf-8")
        target.chmod(0o644)
        expected_mode_path = target
    expected_mode = _mode(expected_mode_path)

    with pytest.raises(HookError):
        path_utils.ensure_private_directory(
            target, private_root=private_root if use_private_root else None
        )

    assert _mode(expected_mode_path) == expected_mode


@pytest.mark.parametrize("replacement", ["symlink", "file"])
@pytest.mark.parametrize("use_private_root", [False, True])
def test_private_directory_rejects_unsafe_concurrent_replacement_without_chmod(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    use_private_root: bool,
) -> None:
    private_root = tmp_path / "private-root"
    if use_private_root:
        private_root.mkdir()
    target_root = private_root if use_private_root else tmp_path
    target = target_root / "private"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    replacement_started = threading.Barrier(2)
    replacement_done = threading.Event()
    replacement_errors: list[BaseException] = []

    def replace_target() -> None:
        try:
            replacement_started.wait(timeout=5)
            if replacement == "symlink":
                target.symlink_to(outside, target_is_directory=True)
            else:
                target.write_text("not a directory\n", encoding="utf-8")
                target.chmod(0o644)
            replacement_done.set()
        except BaseException as error:  # pragma: no cover - diagnostic path
            replacement_errors.append(error)
            replacement_done.set()

    attacker = threading.Thread(target=replace_target)
    attacker.start()
    original_mkdir = pathlib.Path.mkdir

    def mkdir_with_replacement(self, *args, **kwargs):
        if self == target:
            replacement_started.wait(timeout=5)
            assert replacement_done.wait(timeout=5)
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", mkdir_with_replacement)
    with pytest.raises(HookError):
        path_utils.ensure_private_directory(
            target, private_root=private_root if use_private_root else None
        )
    attacker.join(timeout=5)

    assert not attacker.is_alive()
    assert replacement_errors == []
    if replacement == "symlink":
        assert target.is_symlink()
        assert _mode(outside) == 0o755
    else:
        assert target.is_file()
        assert _mode(target) == 0o644


def test_windows_reparse_attribute_is_treated_as_unsafe(monkeypatch, tmp_path) -> None:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    monkeypatch.setattr(
        pathlib.Path,
        "lstat",
        lambda _path: SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=reparse_flag,
        ),
    )

    assert path_utils.path_is_link_or_reparse(tmp_path / "junction") is True


def test_path_traversal_check_rejects_detected_reparse_component(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        path_utils,
        "path_is_link_or_reparse",
        lambda path: path.name == "junction",
    )

    assert path_utils.path_has_symlink(root, root / "junction" / "file.txt") is True
