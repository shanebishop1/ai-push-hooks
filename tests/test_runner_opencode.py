from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import replace

import pytest

from ai_push_hooks.executors.runners import (
    ProcessResult,
    RunnerRequest,
    RunnerArtifact,
    RunnerContractError,
    RunnerError,
    RunnerMissingOutputError,
    RunnerProtocolError,
    RunnerResult,
    RunnerTimeoutError,
    SessionMetadata,
)
from ai_push_hooks.executors.runners.opencode import OpenCodeRunner, create_runner
from ai_push_hooks.config import load_config
from ai_push_hooks.types import HookError, HookLogger

from .conftest import build_context, init_repo


def _request(
    context, *, project_access: str = "artifacts", **overrides: object
) -> RunnerRequest:
    artifact = context.run_dir / "input.txt"
    artifact.write_text("artifact body\n", encoding="utf-8")
    values: dict[str, object] = {
        "profile_id": "opencode",
        "runner_type": "opencode",
        "stage": "docs.query",
        "purpose": "ask:query",
        "mode": "ask",
        "instruction": "Return a JSON array.",
        "artifacts": (RunnerArtifact("input.txt", "artifact body\n", artifact),),
        "cwd": context.repo_root,
        "timeout_seconds": 3,
        "model": "profile/model",
        "variant": "profile-variant",
        "project_access": project_access,
        "integration_context": context,
    }
    values.update(overrides)
    return RunnerRequest(**values)


def test_create_runner_exposes_resume_finalize_and_transcript_capabilities() -> None:
    runner = create_runner()

    assert isinstance(runner, OpenCodeRunner)
    assert runner.capabilities.supports_resume
    assert runner.capabilities.supports_finalize
    assert runner.capabilities.supports_transcript


def test_project_request_uses_effective_model_variant_and_rooted_read_policy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return ProcessResult(
            returncode=0,
            stdout='{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            stderr="",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    result = OpenCodeRunner().run(_request(context, project_access="project"))

    assert result.final_text == "[]"
    assert context.logger.llm_calls == []
    argv = captured["argv"]
    assert argv[0:7] == [
        "/usr/local/bin/opencode",
        "run",
        "--agent",
        "ai-push-hooks-readonly",
        "--pure",
        "--format",
        "json",
    ]
    assert ["--model", "profile/model"] == argv[7:9]
    assert "--variant" in argv and "profile-variant" in argv
    assert captured["cwd"] == repo.resolve()
    import json

    security = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
    permissions = security["agent"]["ai-push-hooks-readonly"]["permission"]
    assert permissions["edit"] == "deny"
    for tool in ("read", "list", "glob", "grep"):
        assert permissions[tool] == "allow"
    for tool in (
        "bash",
        "task",
        "webfetch",
        "websearch",
        "skill",
        "todowrite",
        "question",
    ):
        assert permissions[tool] == "deny"
    assert security["plugin"] == []
    assert security["mcp"] == {}


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("not json\n", RunnerProtocolError),
        ('{"type":"step_start"}\n', RunnerMissingOutputError),
    ],
)
def test_malformed_or_incomplete_output_fails_closed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: type[Exception],
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(0, stdout, ""),
    )

    with pytest.raises(expected):
        OpenCodeRunner().run(_request(context))


@pytest.mark.parametrize(
    "stdout, expected",
    [
        (
            '{"type":"session.created","sessionID":"session-1"}\nnot json\n',
            RunnerProtocolError,
        ),
        (
            '{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"step_start"}\n',
            RunnerMissingOutputError,
        ),
        (
            '{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"error","error":"model-generated-secret"}\n',
            RunnerProtocolError,
        ),
    ],
)
def test_protocol_failures_preserve_announced_session_identity(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: type[Exception],
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(0, stdout, ""),
    )

    with pytest.raises(expected) as error:
        OpenCodeRunner().run(_request(context))

    assert getattr(error.value, "session_id", None) == "session-1"
    assert "model-generated-secret" not in str(error.value)


def test_resumed_protocol_failure_uses_the_exact_known_session_fallback(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(0, '{"type":"step_start"}\n', ""),
    )

    with pytest.raises(RunnerMissingOutputError) as error:
        OpenCodeRunner().run(
            _request(context, session_id="known-session", resume_session=True)
        )

    assert getattr(error.value, "session_id", None) == "known-session"


def test_truncated_process_output_fails_closed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
            stdout_truncated=True,
        ),
    )

    with pytest.raises(RunnerProtocolError, match="truncated") as error:
        OpenCodeRunner().run(_request(context))
    assert getattr(error.value, "session_id", None) is None


def test_opencode_materializes_exact_artifact_snapshots_and_cleans_them(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    source = context.run_dir / "input.txt"
    source.write_text("source-before-request\n", encoding="utf-8")
    request = _request(
        context,
        artifacts=(
            RunnerArtifact("logical/input.txt", "snapshot-one\n", source),
            RunnerArtifact("../second.txt", "snapshot-two\n"),
        ),
    )
    source.write_text("source-mutated-after-request\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = list(argv)
        attachment_paths = [
            pathlib.Path(argv[index + 1])
            for index, value in enumerate(argv)
            if value == "--file"
        ]
        captured["attachment_paths"] = attachment_paths
        captured["attachment_contents"] = [
            path.read_text(encoding="utf-8") for path in attachment_paths
        ]
        return ProcessResult(
            0,
            '{"type":"session.created","sessionID":"snapshot-session"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    result = OpenCodeRunner().run(request)

    paths = captured["attachment_paths"]
    assert captured["attachment_contents"] == ["snapshot-one\n", "snapshot-two\n"]
    assert all(path.is_relative_to(context.run_dir) for path in paths)
    assert all(not path.exists() for path in paths)
    assert captured["argv"][-1] == request.instruction
    assert "snapshot-one" not in captured["argv"][-1]
    assert result.final_text == "[]"


def test_opencode_still_rejects_symlinked_or_escaping_source_artifacts(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    external = tmp_path / "external.txt"
    external.write_text("outside\n", encoding="utf-8")
    linked = context.run_dir / "linked.txt"
    linked.symlink_to(external)

    with pytest.raises(HookError, match="symlink"):
        OpenCodeRunner().run(
            _request(
                context, artifacts=(RunnerArtifact("linked.txt", "snapshot", linked),)
            )
        )
    with pytest.raises(HookError, match="not a hook-owned artifact"):
        OpenCodeRunner().run(
            _request(
                context,
                artifacts=(RunnerArtifact("external.txt", "snapshot", external),),
            )
        )


def test_timeout_preserves_session_for_lifecycle_cleanup_and_attachment_cleanup(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []
    attachment_paths: list[pathlib.Path] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "run":
            attachment_paths.extend(
                pathlib.Path(argv[index + 1])
                for index, value in enumerate(argv)
                if value == "--file"
            )
            error = RunnerTimeoutError("runner process exceeded its timeout")
            error._process_result = ProcessResult(  # type: ignore[attr-defined]
                -9,
                '{"type":"session.created","sessionID":"timeout-session"}\n',
                "secret=do-not-leak",
            )
            raise error
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    request = _request(context)

    with pytest.raises(RunnerTimeoutError) as error:
        OpenCodeRunner().run(request)

    assert getattr(error.value, "session_id", None) == "timeout-session"
    assert "do-not-leak" not in str(error.value)
    assert all(not path.exists() for path in attachment_paths)

    failed = RunnerResult(
        "", 1, "", "", SessionMetadata("timeout-session", "persisted", True)
    )
    finalized = OpenCodeRunner().finalize(request, failed)
    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert [call[1:3] for call in calls] == [
        ["run", "--agent"],
        ["export", "timeout-session"],
        ["session", "delete"],
    ]


def test_successful_run_blocks_on_attachment_cleanup_failure_without_raw_details(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"type":"session.created","sessionID":"cleanup-session"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
        ),
    )

    import shutil

    original_rmtree = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if "opencode-attachments-" in str(path):
            raise OSError("cleanup-secret /private/temporary/path")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.shutil.rmtree", fail_cleanup
    )

    with pytest.raises(RunnerError) as error:
        OpenCodeRunner().run(_request(context))

    assert getattr(error.value, "session_id", None) == "cleanup-session"
    assert "cleanup-secret" not in str(error.value)
    assert "/private/temporary/path" not in str(error.value)
    assert "cleanup-secret" not in repr(error.value)


def test_invocation_error_survives_attachment_cleanup_failure_with_session_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    invocation_error = RunnerProtocolError("invocation failed")
    invocation_error.session_id = "invocation-session"  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(invocation_error),
    )

    import shutil

    original_rmtree = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if "opencode-attachments-" in str(path):
            raise OSError("cleanup-secret /private/temporary/path")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.shutil.rmtree", fail_cleanup
    )

    with pytest.raises(RunnerProtocolError) as error:
        OpenCodeRunner().run(_request(context))

    assert error.value is invocation_error
    assert getattr(error.value, "session_id", None) == "invocation-session"
    assert "cleanup-secret" not in str(error.value)
    assert "/private/temporary/path" not in str(error.value)


def test_finalize_reports_deleted_session_and_private_transcript(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, capture_llm_transcript=True),
        llm=replace(config.llm, delete_session_after_run=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            return ProcessResult(0, '{"session":"session-1"}\n', "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    result = RunnerResult(
        "[]", 0, "", "", SessionMetadata("session-1", "persisted", True)
    )

    finalized = OpenCodeRunner().finalize(_request(context), result)

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.resumable is False
    assert finalized.session.transcript is not None
    assert pathlib.Path(finalized.session.transcript).is_relative_to(context.git_dir)
    assert (
        pathlib.Path(finalized.session.transcript).read_text(encoding="utf-8")
        == '{"session":"session-1"}\n'
    )
    assert [call[1:3] for call in calls] == [
        ["export", "session-1"],
        ["session", "delete"],
    ]


def test_finalize_never_persists_truncated_export(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, capture_llm_transcript=True),
        llm=replace(config.llm, delete_session_after_run=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"

    def fake_run_process(argv, **kwargs):
        if argv[1] == "export":
            return ProcessResult(
                0,
                '{"session":"session-truncated"}\n',
                "",
                stdout_truncated=True,
            )
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    result = RunnerResult(
        "[]", 0, "", "", SessionMetadata("session-truncated", "persisted", True)
    )

    finalized = OpenCodeRunner().finalize(_request(context), result)

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.transcript is None
    assert list((context.git_dir / "ai-push-hooks" / "transcripts").iterdir()) == []


def test_readonly_run_uses_private_isolation_and_provider_auth_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    host_home = tmp_path / "host-home"
    host_config = tmp_path / "host-config"
    (host_home / ".opencode").mkdir(parents=True)
    (host_home / ".opencode" / "opencode.json").write_text(
        '{"share":"auto","mcp":{"unsafe":{"type":"local","command":["sh"]}}}',
        encoding="utf-8",
    )
    (host_config / "opencode").mkdir(parents=True)
    (host_config / "opencode" / "opencode.json").write_text(
        '{"share":"auto","plugin":["unsafe-plugin"]}', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(host_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "opencode-data"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "malicious.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"share":"auto"}')
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://secret@proxy.invalid")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        captured["cwd_entries"] = list(kwargs["cwd"].iterdir())
        return ProcessResult(
            0,
            '{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    OpenCodeRunner().run(_request(context, artifacts=()))

    assert captured["argv"][:7] == [
        "/usr/local/bin/opencode",
        "run",
        "--agent",
        "ai-push-hooks-readonly",
        "--pure",
        "--format",
        "json",
    ]
    env = captured["env"]
    assert env["OPENCODE_PURE"] == "true"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "true"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "true"
    assert env["OPENCODE_DISABLE_LSP_DOWNLOAD"] == "true"
    assert env["OPENCODE_DISABLE_SHARE"] == "true"
    assert "OPENCODE_CONFIG" not in env
    assert pathlib.Path(env["HOME"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["XDG_CONFIG_HOME"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["OPENCODE_CONFIG_DIR"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["XDG_DATA_HOME"]) == (tmp_path / "opencode-data").resolve()
    assert env["OPENAI_API_KEY"] == "provider-key"
    assert "HTTPS_PROXY" not in env
    assert "UNRELATED_SECRET" not in env
    assert captured["cwd_entries"] == []
    permissions = json.loads(env["OPENCODE_CONFIG_CONTENT"])["agent"][
        "ai-push-hooks-readonly"
    ]["permission"]
    assert permissions["*"] == "deny"
    for tool in (
        "read",
        "glob",
        "grep",
        "list",
        "edit",
        "bash",
        "task",
        "external_directory",
        "webfetch",
        "websearch",
        "skill",
        "todowrite",
        "question",
    ):
        assert permissions[tool] == "deny"


def test_apply_run_uses_allowlisted_edit_permissions_for_staging(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    staging = tmp_path / "Temp Root" / "OpenCode-Staging"
    staging.mkdir(parents=True)
    staging_link = tmp_path / "staging-link"
    staging_link.symlink_to(staging, target_is_directory=True)
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        pathlib.Path(kwargs["cwd"]).joinpath("README.md").write_text(
            "updated in isolated staging\n", encoding="utf-8"
        )
        return ProcessResult(
            0,
            '{"type":"session.created","sessionID":"apply-session"}\n'
            '{"type":"text","part":{"text":"done"}}\n',
            "",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    OpenCodeRunner().run(
        _request(
            context,
            mode="apply",
            artifacts=(),
            cwd=staging_link,
            allow_paths=("README.md", "docs/**/*.md"),
        )
    )

    assert captured["argv"][2:6] == [
        "--agent",
        "ai-push-hooks-apply",
        "--pure",
        "--format",
    ]
    assert (staging / "README.md").read_text(
        encoding="utf-8"
    ) == "updated in isolated staging\n"
    permissions = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])["agent"][
        "ai-push-hooks-apply"
    ]["permission"]
    prefix = staging.resolve().relative_to(pathlib.Path(staging.anchor)).as_posix()
    assert permissions["read"] == "allow"
    assert permissions["edit"] == {
        "*": "deny",
        f"{prefix}/README.md": "allow",
        f"{prefix}/docs/*.md": "allow",
        f"{prefix}/docs/**/*.md": "allow",
        f"{prefix}/.git": "deny",
        f"{prefix}/.git/**": "deny",
    }
    assert "write" not in permissions
    for tool in ("bash", "task", "external_directory", "webfetch", "websearch"):
        assert permissions[tool] == "deny"


@pytest.mark.parametrize("directory_kind", ["missing", "file"])
def test_apply_run_rejects_missing_or_non_directory_cwd(
    tmp_path: pathlib.Path, directory_kind: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    cwd = tmp_path / "not-a-directory"
    if directory_kind == "file":
        cwd.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(RunnerContractError, match="existing directory"):
        OpenCodeRunner().run(
            _request(
                context, mode="apply", artifacts=(), cwd=cwd, allow_paths=("README.md",)
            )
        )


def test_apply_run_rejects_repository_cwd_as_missing_isolated_staging(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)

    with pytest.raises(RunnerContractError, match="isolated staging directory"):
        OpenCodeRunner().run(
            _request(
                context,
                mode="apply",
                artifacts=(),
                cwd=context.repo_root,
                allow_paths=("README.md",),
            )
        )


@pytest.mark.parametrize(
    ("return_code", "stdout"),
    [(1, "export failed\n"), (0, "")],
)
def test_finalize_warns_and_deletes_when_export_has_no_transcript(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    return_code: int,
    stdout: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            return ProcessResult(return_code, stdout, "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    result = RunnerResult(
        "[]", 0, "", "", SessionMetadata("session-failed", "persisted", True)
    )

    finalized = OpenCodeRunner().finalize(_request(context), result)

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.transcript is None
    assert [argv[1:3] for argv in calls] == [
        ["export", "session-failed"],
        ["session", "delete"],
    ]
    assert (
        "Could not capture or delete the OpenCode session cleanly"
        in capsys.readouterr().err
    )


def test_finalize_export_process_exception_still_deletes_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    warning_log = tmp_path / "warning.jsonl"
    context.logger = HookLogger(warning_log)
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            raise subprocess.TimeoutExpired(
                ["opencode", "export", "session-exception", "token=paid-secret"], 1
            )
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    finalized = OpenCodeRunner().finalize(
        _request(context),
        RunnerResult(
            "[]", 0, "", "", SessionMetadata("session-exception", "persisted", True)
        ),
    )

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.transcript is None
    assert [argv[1:3] for argv in calls] == [
        ["export", "session-exception"],
        ["session", "delete"],
    ]
    warning = capsys.readouterr().err
    assert "Could not capture or delete the OpenCode session cleanly" in warning
    assert "paid-secret" not in warning
    record = json.loads(warning_log.read_text(encoding="utf-8").strip())
    assert record["reason"] == "TimeoutExpired"
    assert record["session_id"] == "session-exception"
    assert "paid-secret" not in json.dumps(record)


def test_finalize_deletes_when_transcript_write_fails(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            return ProcessResult(0, '{"session":"session-write"}\n', "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.write_text_no_follow",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("simulated transcript failure")
        ),
    )

    OpenCodeRunner().finalize(
        _request(context),
        RunnerResult(
            "[]", 0, "", "", SessionMetadata("session-write", "persisted", True)
        ),
    )

    assert [argv[1:3] for argv in calls] == [
        ["export", "session-write"],
        ["session", "delete"],
    ]
    assert (
        "Could not capture or delete the OpenCode session cleanly"
        in capsys.readouterr().err
    )
