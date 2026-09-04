from __future__ import annotations

import pathlib
import stat
import subprocess
from dataclasses import replace

import pytest

import ai_push_hooks.executors.apply as apply_executor
from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.config import load_config
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.executors.apply import run_apply_step
from ai_push_hooks.hook import _build_logger
from ai_push_hooks.types import HookError, ModuleRuntimeState

from .conftest import build_context, init_repo


class ApplyResult:
    return_code = 0
    stderr = ""
    stdout = ""
    session_id = None


def _issues_artifact(context, payload: str | None = None) -> pathlib.Path:
    path = context.run_dir / "issues.json"
    path.write_text(
        payload or '[{"file":"README.md","description":"stale"}]\n',
        encoding="utf-8",
    )
    return path


def _run_apply(context, step, input_path):
    return run_apply_step(
        context,
        ModuleRuntimeState(module=context.config.modules["docs"]),
        step,
        "apply prompt",
        [input_path],
        "docs.apply",
    )


def test_docs_drift_detection_produces_issue_artifact(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    config, _ = load_config(repo)
    context = build_context(
        repo,
        config,
        ranges=[],
        changed_files=["src/app.py"],
        diff_text="+print('changed')\n",
    )

    def fake_llm(context, step, prompt, input_paths, stage_name):
        if step.id == "query":
            return ["README"]
        if step.id == "analyze":
            return [
                {
                    "file": "README.md",
                    "line": 1,
                    "description": "README is stale",
                    "doc_excerpt": "# Example",
                    "suggested_fix": "# Updated",
                }
            ]
        raise AssertionError(step.id)

    def fake_apply(context, state, step, prompt, input_paths, stage_name):
        return {"changed": False, "changed_files": [], "skipped": True}

    result = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        llm_executor=fake_llm,
        apply_executor=fake_apply,
    ).run()

    issues_path = result.run_dir / "docs" / "02-analyze" / "issues.json"
    assert issues_path.exists()
    assert "README is stale" in issues_path.read_text(encoding="utf-8")


def test_apply_runs_in_minimal_staging_and_propagates_allowed_changes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / ".gitignore").write_text("secret.env\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "ignore secret"], cwd=repo, check=True, capture_output=True
    )
    (repo / "secret.env").write_text("TOKEN=secret\n", encoding="utf-8")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        staging = kwargs["working_directory"]
        assert staging != repo
        assert (staging / "README.md").read_text(encoding="utf-8") == "# Example\n"
        assert (staging / "docs" / "INDEX.md").exists()
        assert not (staging / "src" / "app.py").exists()
        assert not (staging / "secret.env").exists()
        assert not (staging / ".git").exists()
        assert kwargs["files"] == [input_path.resolve()]
        (staging / "README.md").write_text("# Updated\n", encoding="utf-8")
        (staging / "docs" / "NEW.md").write_text("# New\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None
    )

    result = _run_apply(context, step, input_path)

    assert result["changed_files"] == ["README.md", "docs/NEW.md"]
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Updated\n"
    assert (repo / "docs" / "NEW.md").read_text(encoding="utf-8") == "# New\n"
    assert (repo / "secret.env").read_text(encoding="utf-8") == "TOKEN=secret\n"


def test_apply_propagates_allowed_deletion(tmp_path: pathlib.Path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        (kwargs["working_directory"] / "docs" / "INDEX.md").unlink()
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None
    )

    result = _run_apply(context, step, input_path)

    assert result["changed_files"] == ["docs/INDEX.md"]
    assert not (repo / "docs" / "INDEX.md").exists()


def test_apply_preserves_dirty_allowed_content_as_staging_baseline(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / "README.md").write_text("# Dirty user content\n", encoding="utf-8")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        staged_readme = kwargs["working_directory"] / "README.md"
        assert staged_readme.read_text(encoding="utf-8") == "# Dirty user content\n"
        staged_readme.write_text("# Dirty user content\n\nAgent addition.\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None
    )

    result = _run_apply(context, step, input_path)

    assert result["changed_files"] == ["README.md"]
    assert "Dirty user content" in (repo / "README.md").read_text(encoding="utf-8")


def test_apply_rejects_non_allowlisted_staging_output_before_copy(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        (kwargs["working_directory"] / "escape.txt").write_text("bad\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="outside allowlist"):
        _run_apply(context, step, input_path)
    assert not (repo / "escape.txt").exists()


def test_apply_rejects_staging_symlink_escape_before_copy(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        staged = kwargs["working_directory"] / "docs" / "ESCAPE.md"
        staged.symlink_to(outside)
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="contains symlink"):
        _run_apply(context, step, input_path)
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_apply_never_propagates_staged_git_metadata_with_broad_allowlist(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], allow_paths=("*",))
    input_path = _issues_artifact(context)
    original_git_config = (repo / ".git" / "config").read_bytes()

    def fake_call(*args, **kwargs):
        staged_git = kwargs["working_directory"] / ".git"
        staged_git.mkdir()
        (staged_git / "config").write_text("malicious\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="outside allowlist|Git metadata"):
        _run_apply(context, step, input_path)

    assert (repo / ".git" / "config").read_bytes() == original_git_config


def test_apply_rejects_outputs_ignored_by_staged_gitignore(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], allow_paths=("*",))
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        staging = kwargs["working_directory"]
        (staging / ".gitignore").write_text("generated.txt\n", encoding="utf-8")
        (staging / "generated.txt").write_text("generated\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="ignored paths"):
        _run_apply(context, step, input_path)

    assert not (repo / "generated.txt").exists()
    assert not (repo / ".gitignore").exists()


def test_apply_verifies_real_checkout_matches_validated_staging(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        (kwargs["working_directory"] / "README.md").write_text(
            "# Validated staging output\n", encoding="utf-8"
        )
        return ApplyResult()

    real_propagate = apply_executor._propagate_staging_changes

    def corrupt_after_propagation(*args, **kwargs):
        result = real_propagate(*args, **kwargs)
        (repo / "README.md").write_text("# Different real output\n", encoding="utf-8")
        return result

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply._propagate_staging_changes", corrupt_after_propagation
    )

    with pytest.raises(HookError, match="does not match validated staging output"):
        _run_apply(context, step, input_path)


def test_apply_does_not_attach_symlinked_agents_file(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    outside = tmp_path / "instructions.md"
    outside.write_text("malicious instructions\n", encoding="utf-8")
    (repo / "AGENTS.md").symlink_to(outside)
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], allow_paths=("*",))
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        assert kwargs["files"] == [input_path.resolve()]
        assert all(path.name != "AGENTS.md" for path in kwargs["files"])
        assert not (kwargs["working_directory"] / "AGENTS.md").exists()
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None
    )

    assert _run_apply(context, step, input_path)["changed"] is False


def test_apply_rejects_external_or_symlinked_input_artifacts(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    external = repo / "issues.json"
    external.write_text("[]\n", encoding="utf-8")

    with pytest.raises(HookError, match="not a hook-owned artifact"):
        _run_apply(context, step, external)

    target = context.run_dir / "real-issues.json"
    target.write_text("[]\n", encoding="utf-8")
    symlink = context.run_dir / "issues.json"
    symlink.symlink_to(target)
    with pytest.raises(HookError, match="symlink"):
        _run_apply(context, step, symlink)


def test_apply_default_logging_succeeds_in_linked_worktree(tmp_path, monkeypatch) -> None:
    primary = init_repo(tmp_path, branch="main")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/linked", str(linked)],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    config, _ = load_config(linked)
    context = build_context(linked, config)
    context.logger = _build_logger(linked, context.git_dir, config)
    log_path = context.logger.jsonl_path
    assert log_path == context.git_dir / "ai-push-hooks" / "logs" / "hook.jsonl"
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        context.logger.llm_call("docs.apply", "apply:apply", context.config.llm.model)
        (kwargs["working_directory"] / "README.md").write_text("# Updated\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None
    )

    result = _run_apply(context, step, input_path)

    assert result["changed_files"] == ["README.md"]
    assert log_path.exists()


def test_apply_detects_linked_worktree_common_control_metadata_change(tmp_path, monkeypatch) -> None:
    primary = init_repo(tmp_path, branch="main")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/linked", str(linked)],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    config, _ = load_config(linked)
    context = build_context(linked, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        with (primary / ".git" / "config").open("a", encoding="utf-8") as handle:
            handle.write("\n# unsafe change\n")
        (kwargs["working_directory"] / "README.md").write_text(
            "must not propagate\n", encoding="utf-8"
        )
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None
    )

    with pytest.raises(HookError, match="shared:config"):
        _run_apply(context, step, input_path)
    assert (linked / "README.md").read_text(encoding="utf-8") == "# Example\n"


@pytest.mark.parametrize("linked_worktree", [False, True])
def test_apply_fails_closed_on_symlinked_shared_git_config_before_opencode(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_worktree: bool,
) -> None:
    primary = init_repo(tmp_path, branch="main")
    repo = primary
    if linked_worktree:
        repo = tmp_path / "linked"
        subprocess.run(
            ["git", "worktree", "add", "-b", "feature/linked-config", str(repo)],
            cwd=primary,
            check=True,
            capture_output=True,
        )
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    shared_config = primary / ".git" / "config"
    config_target = tmp_path / "shared-config-target"
    config_target.write_bytes(shared_config.read_bytes())
    shared_config.unlink()
    shared_config.symlink_to(config_target)
    calls: list[str] = []
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.call_opencode",
        lambda *args, **kwargs: calls.append("called"),
    )

    with pytest.raises(HookError, match="symlinked monitored Git metadata.*shared:config"):
        _run_apply(context, step, input_path)

    assert calls == []


def test_apply_detects_index_flag_change(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        subprocess.run(
            ["git", "update-index", "--skip-worktree", "README.md"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        (kwargs["working_directory"] / "README.md").write_text(
            "must not propagate\n", encoding="utf-8"
        )
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None
    )

    with pytest.raises(HookError, match="index"):
        _run_apply(context, step, input_path)
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Example\n"


@pytest.mark.parametrize(
    "concurrent_change",
    ["modify", "create", "delete", "mode"],
)
def test_apply_cas_never_overwrites_concurrent_checkout_changes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_change: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    target = repo / ("docs/NEW.md" if concurrent_change == "create" else "README.md")

    def fake_call(*args, **kwargs):
        staged_target = kwargs["working_directory"] / target.relative_to(repo)
        staged_target.parent.mkdir(parents=True, exist_ok=True)
        staged_target.write_text("agent output\n", encoding="utf-8")
        return ApplyResult()

    real_verify = apply_executor._verify_pre_propagation_security_state

    def verify_then_change(*args, **kwargs):
        real_verify(*args, **kwargs)
        if concurrent_change in {"modify", "create"}:
            target.write_text("concurrent user content\n", encoding="utf-8")
        elif concurrent_change == "delete":
            target.unlink()
        else:
            target.chmod(0o600)

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply._verify_pre_propagation_security_state",
        verify_then_change,
    )

    with pytest.raises(HookError, match="changed concurrently"):
        _run_apply(context, step, input_path)

    if concurrent_change in {"modify", "create"}:
        assert target.read_text(encoding="utf-8") == "concurrent user content\n"
    elif concurrent_change == "delete":
        assert not target.exists()
    else:
        assert target.read_text(encoding="utf-8") == "# Example\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("existing_mode", "expected_mode"),
    [
        (0o640, 0o640),
        (0o666, 0o600),
        (0o4755, 0o755),
        (0o2755, 0o755),
        (0o1755, 0o755),
    ],
)
def test_apply_uses_safe_propagation_modes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_mode: int,
    expected_mode: int,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / "README.md").chmod(existing_mode)
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        staging = kwargs["working_directory"]
        (staging / "README.md").write_text("updated\n", encoding="utf-8")
        (staging / "docs" / "NEW.md").write_text("new\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    _run_apply(context, step, input_path)

    assert stat.S_IMODE((repo / "README.md").stat().st_mode) == expected_mode
    assert stat.S_IMODE((repo / "docs" / "NEW.md").stat().st_mode) == 0o600


@pytest.mark.parametrize("special_mode", [stat.S_ISUID, stat.S_ISGID, stat.S_ISVTX])
def test_apply_rejects_staged_special_modes_before_any_propagation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    special_mode: int,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        staging = kwargs["working_directory"]
        (staging / "README.md").write_text("ordinary staged change\n", encoding="utf-8")
        special = staging / "docs" / "INDEX.md"
        special.write_text("special staged change\n", encoding="utf-8")
        special.chmod(0o644 | special_mode)
        assert special.stat().st_mode & special_mode
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="setuid, setgid, or sticky"):
        _run_apply(context, step, input_path)

    assert (repo / "README.md").read_text(encoding="utf-8") == "# Example\n"
    assert (repo / "docs" / "INDEX.md").read_text(encoding="utf-8") == "# Docs Index\n"


@pytest.mark.parametrize(
    "protected_path",
    [".GiT/config", ".ＧＩＴ/config", "aGeNtS.Md", "ＡＧＥＮＴＳ.md"],
)
def test_apply_rejects_case_variant_protected_staging_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_path: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], allow_paths=("*", "**/*"))
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        target = kwargs["working_directory"].joinpath(*pathlib.PurePosixPath(protected_path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("malicious\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="outside allowlist"):
        _run_apply(context, step, input_path)


def test_apply_rejects_destination_inside_actual_nonstandard_git_dir(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / ".git").rename(repo / "repo-metadata")
    (repo / ".git").write_text("gitdir: repo-metadata\n", encoding="utf-8")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], allow_paths=("**/*",))
    input_path = _issues_artifact(context)
    original_config = (repo / "repo-metadata" / "config").read_bytes()

    def fake_call(*args, **kwargs):
        target = kwargs["working_directory"] / "repo-metadata" / "config"
        target.parent.mkdir(parents=True)
        target.write_text("malicious\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="resolves inside Git metadata"):
        _run_apply(context, step, input_path)
    assert (repo / "repo-metadata" / "config").read_bytes() == original_config


def test_session_finalization_tamper_is_detected_before_propagation(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    finalized: list[tuple[str, str | None]] = []

    class SessionResult(ApplyResult):
        session_id = "session-1"

    def fake_call(*args, **kwargs):
        (kwargs["working_directory"] / "README.md").write_text(
            "staged output\n", encoding="utf-8"
        )
        return SessionResult()

    def fake_finalize(_context, finalized_stage, session_id):
        finalized.append((finalized_stage, session_id))
        with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
            handle.write("\n# tampered during finalization\n")

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr("ai_push_hooks.executors.apply.finalize_opencode_session", fake_finalize)

    with pytest.raises(HookError, match="before propagation"):
        _run_apply(context, step, input_path)

    assert finalized == [("docs.apply", "session-1")]
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Example\n"


def test_unrelated_linked_worktree_metadata_does_not_block_propagation(tmp_path, monkeypatch) -> None:
    primary = init_repo(tmp_path, branch="main")
    current = tmp_path / "current"
    unrelated = tmp_path / "unrelated"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/current", str(current)],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/unrelated", str(unrelated)],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    config, _ = load_config(current)
    context = build_context(current, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    unrelated_git_dir = pathlib.Path(
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=unrelated,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    if not unrelated_git_dir.is_absolute():
        unrelated_git_dir = (unrelated / unrelated_git_dir).resolve()

    def fake_call(*args, **kwargs):
        (unrelated_git_dir / "unrelated-state").write_text("active\n", encoding="utf-8")
        (kwargs["working_directory"] / "README.md").write_text(
            "updated\n", encoding="utf-8"
        )
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    result = _run_apply(context, step, input_path)

    assert result["changed_files"] == ["README.md"]
    assert (current / "README.md").read_text(encoding="utf-8") == "updated\n"


def test_apply_failure_does_not_copy_staging_changes(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    class FailedResult(ApplyResult):
        return_code = 1
        stderr = "failed"

    def fake_call(*args, **kwargs):
        (kwargs["working_directory"] / "README.md").write_text("# Must not copy\n", encoding="utf-8")
        return FailedResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None
    )

    with pytest.raises(HookError, match="failed in isolated staging"):
        _run_apply(context, step, input_path)
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Example\n"


def test_docs_apply_blocks_push_until_manual_commit(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(
        repo,
        config,
        ranges=[],
        changed_files=["src/app.py"],
        diff_text="+print('changed')\n",
    )

    def fake_llm(context, step, prompt, input_paths, stage_name):
        if step.id == "query":
            return ["README"]
        if step.id == "analyze":
            return [{"file": "README.md", "line": 1, "description": "README stale"}]
        raise AssertionError(step.id)

    def fake_apply(context, state, step, prompt, input_paths, stage_name):
        return {"changed": True, "changed_files": ["README.md"], "skipped": False}

    engine = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        llm_executor=fake_llm,
        apply_executor=fake_apply,
    )
    with pytest.raises(HookError, match="review and commit"):
        engine.run()


def test_apply_requires_single_pushed_branch_at_checked_out_head(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    first_update = context.cache["pushed_branch_updates"][0]
    context.cache["pushed_branch_updates"] = [
        first_update,
        replace(first_update, remote_ref="refs/heads/feature/other"),
    ]
    calls: list[str] = []
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.call_opencode",
        lambda *args, **kwargs: calls.append("called"),
    )

    with pytest.raises(HookError, match="exactly one"):
        _run_apply(context, step, input_path)

    assert calls == []


def test_apply_skips_empty_issues_without_a_pushed_branch(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.cache["pushed_branch_updates"] = []
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context, "[]")
    calls: list[str] = []
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.call_opencode",
        lambda *args, **kwargs: calls.append("called"),
    )

    assert _run_apply(context, step, input_path) == {
        "changed": False,
        "changed_files": [],
        "skipped": True,
    }
    assert calls == []


def test_apply_rejects_push_commit_that_is_not_checked_out_head(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    subprocess.run(["git", "add", "later.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "later"], cwd=repo, check=True, capture_output=True
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.call_opencode",
        lambda *args, **kwargs: calls.append("called"),
    )

    with pytest.raises(HookError, match="checked-out HEAD"):
        _run_apply(context, step, input_path)

    assert calls == []


def test_apply_rejects_oversized_checkout_file_before_opencode(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    calls: list[str] = []
    monkeypatch.setattr(apply_executor, "STAGING_MAX_BYTES", 4)
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.call_opencode",
        lambda *args, **kwargs: calls.append("called"),
    )

    with pytest.raises(HookError, match="before reading"):
        _run_apply(context, step, input_path)

    assert calls == []


def test_apply_rejects_oversized_staging_output_before_hashing(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], allow_paths=("docs/NEW.md",))
    input_path = _issues_artifact(context)
    hashed_paths: list[pathlib.Path] = []
    real_hash = apply_executor._hash_file

    def tracking_hash(path, *args, **kwargs):
        hashed_paths.append(path)
        return real_hash(path, *args, **kwargs)

    def fake_call(*args, **kwargs):
        (kwargs["working_directory"] / "docs").mkdir()
        (kwargs["working_directory"] / "docs" / "NEW.md").write_text(
            "oversized\n", encoding="utf-8"
        )
        return ApplyResult()

    monkeypatch.setattr(apply_executor, "STAGING_MAX_BYTES", 4)
    monkeypatch.setattr(apply_executor, "_hash_file", tracking_hash)
    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="bounded inventory budget"):
        _run_apply(context, step, input_path)

    assert all(path.name != "NEW.md" for path in hashed_paths)


def test_apply_source_must_resolve_inside_repository(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], allow_paths=("README.md",))
    input_path = _issues_artifact(context)
    outside = tmp_path / "outside" / "README.md"
    original_resolve = pathlib.Path.resolve

    def redirected_resolve(path, *args, **kwargs):
        if path == repo / "README.md":
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", redirected_resolve)

    with pytest.raises(HookError, match="source escapes repository"):
        _run_apply(context, step, input_path)


def test_apply_propagation_error_reports_already_applied_paths(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    real_atomic_write = apply_executor.atomic_write_bytes

    def fake_call(*args, **kwargs):
        staging = kwargs["working_directory"]
        (staging / "README.md").write_text("updated readme\n", encoding="utf-8")
        (staging / "docs" / "INDEX.md").write_text("updated index\n", encoding="utf-8")
        return ApplyResult()

    def fail_second_checkout_write(path, content, **kwargs):
        if path == repo / "docs" / "INDEX.md":
            raise OSError("simulated write failure")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)
    monkeypatch.setattr(apply_executor, "atomic_write_bytes", fail_second_checkout_write)

    with pytest.raises(HookError, match="already-applied paths: README.md"):
        _run_apply(context, step, input_path)

    assert (repo / "README.md").read_text(encoding="utf-8") == "updated readme\n"
    assert (repo / "docs" / "INDEX.md").read_text(encoding="utf-8") == "# Docs Index\n"


def test_apply_monitors_configured_external_hooks_path(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo", branch="feature/docs")
    hooks_path = tmp_path / "shared-hooks"
    hooks_path.mkdir()
    hook_path = hooks_path / "pre-push"
    hook_path.write_text("original\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks_path)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_call(*args, **kwargs):
        hook_path.write_text("modified\n", encoding="utf-8")
        return ApplyResult()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call)

    with pytest.raises(HookError, match="Git control metadata before propagation"):
        _run_apply(context, step, input_path)


def test_apply_rejects_hooks_path_inside_runtime_metadata(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    subprocess.run(
        ["git", "config", "core.hooksPath", ".git/ai-push-hooks/custom-hooks"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)
    calls: list[str] = []
    monkeypatch.setattr(
        "ai_push_hooks.executors.apply.call_opencode",
        lambda *args, **kwargs: calls.append("called"),
    )

    with pytest.raises(HookError, match="must not overlap"):
        _run_apply(context, step, input_path)

    assert calls == []
