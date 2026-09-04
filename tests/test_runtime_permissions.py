from __future__ import annotations

import pathlib
import stat
import subprocess
from dataclasses import replace
from types import SimpleNamespace

from ai_push_hooks import paths as path_utils
from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.config import load_config
from ai_push_hooks.executors.llm import finalize_opencode_session
from ai_push_hooks.hook import _build_logger, _write_summary
from ai_push_hooks.types import ModuleRuntimeState

from .conftest import build_context, init_repo


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_runtime_directories_and_files_are_private_by_default(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/runtime-modes")
    config, _ = load_config(repo)
    config = replace(config, llm=replace(config.llm, delete_session_after_run=False))
    context = build_context(repo, config)

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

    monkeypatch.setattr(
        "ai_push_hooks.executors.llm.run_command",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout='{"session":"ok"}\n', stderr=""
        ),
    )
    finalize_opencode_session(context, "docs.query", "session-1")

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


def test_path_traversal_check_rejects_detected_reparse_component(monkeypatch, tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        path_utils,
        "path_is_link_or_reparse",
        lambda path: path.name == "junction",
    )

    assert path_utils.path_has_symlink(root, root / "junction" / "file.txt") is True
