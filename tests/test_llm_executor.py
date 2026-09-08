from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import replace

import pytest

from ai_push_hooks.config import load_config
from ai_push_hooks.executors.llm import (
    OPENCODE_APPLY_AGENT,
    OPENCODE_READ_ONLY_AGENT,
    OpenCodeRunResult,
    call_opencode,
    finalize_opencode_session,
    run_llm_step,
)
from ai_push_hooks.types import HookError

from .conftest import build_context, init_repo


def test_run_llm_step_accepts_array_for_docs_issue_schema(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    analyze_step = next(step for step in config.modules["docs"].steps if step.id == "analyze")

    def fake_call_opencode(*args, **kwargs):
        return OpenCodeRunResult(
            output_text="[]",
            session_id=None,
            stdout="",
            stderr="",
            return_code=0,
        )

    monkeypatch.setattr("ai_push_hooks.executors.llm.call_opencode", fake_call_opencode)
    monkeypatch.setattr("ai_push_hooks.executors.llm.finalize_opencode_session", lambda *args, **kwargs: None)

    payload = run_llm_step(context, analyze_step, "prompt", [], "docs.analyze")

    assert payload == []


def test_call_opencode_constructs_command_with_explicit_agent(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    original_data_home = tmp_path / "opencode-data"
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
    monkeypatch.setenv("XDG_DATA_HOME", str(original_data_home))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "malicious.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"share":"auto"}')
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://secret@proxy.invalid")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    captured: dict[str, object] = {}

    def fake_run_command(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        captured["inherit_env"] = kwargs["inherit_env"]
        captured["cwd"] = kwargs["cwd"]
        captured["cwd_entries"] = list(kwargs["cwd"].iterdir())
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.llm.run_command", fake_run_command)

    call_opencode(context, "docs.query", "llm:query", "prompt", [], agent="read-only")

    assert captured["args"][:7] == [
        "/usr/local/bin/opencode",
        "run",
        "--agent",
        OPENCODE_READ_ONLY_AGENT,
        "--pure",
        "--format",
        "json",
    ]
    env = captured["env"]
    assert env["OPENCODE_PURE"] == "true"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "true"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "true"
    assert "OPENCODE_DISABLE_DEFAULT_PLUGINS" not in env
    assert env["OPENCODE_DISABLE_LSP_DOWNLOAD"] == "true"
    assert env["OPENCODE_DISABLE_SHARE"] == "true"
    assert "OPENCODE_CONFIG" not in env
    assert pathlib.Path(env["HOME"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["XDG_CONFIG_HOME"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["OPENCODE_CONFIG_DIR"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["XDG_DATA_HOME"]) == original_data_home.resolve()
    assert env["OPENAI_API_KEY"] == "provider-key"
    assert "HTTPS_PROXY" not in env
    assert "UNRELATED_SECRET" not in env
    assert captured["inherit_env"] is False
    assert not pathlib.Path(captured["cwd"]).is_relative_to(repo.resolve())
    assert captured["cwd_entries"] == []
    security_config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert security_config["plugin"] == []
    assert security_config["mcp"] == {}
    assert security_config["share"] == "disabled"
    assert security_config["instructions"] == []
    assert security_config["formatter"] is False
    assert security_config["lsp"] is False
    assert security_config["command"] == {}
    permissions = security_config["agent"][OPENCODE_READ_ONLY_AGENT]["permission"]
    assert permissions["*"] == "deny"
    assert permissions["read"] == "deny"
    assert permissions["glob"] == "deny"
    assert permissions["grep"] == "deny"
    assert permissions["list"] == "deny"
    for denied in (
        "edit",
        "bash",
        "task",
        "external_directory",
        "webfetch",
        "websearch",
        "lsp",
        "skill",
        "todowrite",
        "question",
    ):
        assert permissions[denied] == "deny"


def test_call_opencode_apply_config_uses_edit_permission_for_all_mutating_tools(
    tmp_path: pathlib.Path,
    monkeypatch,
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

    def fake_run_command(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.llm.run_command", fake_run_command)

    call_opencode(
        context,
        "docs.apply",
        "apply:apply",
        "prompt",
        [],
        agent="apply",
        allow_paths=("README.md", "docs/**/*.md"),
        working_directory=staging_link,
    )

    assert captured["args"][2:6] == ["--agent", OPENCODE_APPLY_AGENT, "--pure", "--format"]
    security_config = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
    permissions = security_config["agent"][OPENCODE_APPLY_AGENT]["permission"]
    assert permissions["*"] == "deny"
    assert permissions["read"] == "allow"
    assert permissions["glob"] == "deny"
    assert permissions["grep"] == "deny"
    assert permissions["list"] == "deny"
    prefix = staging.resolve().relative_to(pathlib.Path(staging.anchor)).as_posix()
    assert permissions["edit"] == {
        "*": "deny",
        f"{prefix}/README.md": "allow",
        f"{prefix}/docs/*.md": "allow",
        f"{prefix}/docs/**/*.md": "allow",
        f"{prefix}/.git": "deny",
        f"{prefix}/.git/**": "deny",
    }
    # OpenCode 1.18.29's write and edit tools both request the `edit`
    # permission; a separate `write` grant would be ineffective and broader
    # than the documented permission contract.
    assert "write" not in permissions
    for denied in ("bash", "task", "external_directory", "webfetch", "websearch"):
        assert permissions[denied] == "deny"


def test_call_opencode_apply_requires_isolated_staging_directory(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)

    with pytest.raises(HookError, match="requires an isolated staging directory"):
        call_opencode(
            context,
            "docs.apply",
            "apply:apply",
            "prompt",
            [],
            agent="apply",
            allow_paths=("README.md",),
        )


@pytest.mark.parametrize("directory_kind", ["missing", "file"])
def test_call_opencode_apply_rejects_missing_or_non_directory_working_directory(
    tmp_path: pathlib.Path, directory_kind: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    working_directory = tmp_path / "not-a-directory"
    if directory_kind == "file":
        working_directory.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(HookError, match="existing directory"):
        call_opencode(
            context,
            "docs.apply",
            "apply:apply",
            "prompt",
            [],
            agent="apply",
            allow_paths=("README.md",),
            working_directory=working_directory,
        )


def test_finalize_session_exports_from_private_scratch_and_deletes_same_session(
    tmp_path: pathlib.Path, monkeypatch
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
    calls: list[tuple[list[str], pathlib.Path]] = []
    status_before = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    def fake_run_command(args, **kwargs):
        cwd = pathlib.Path(kwargs["cwd"])
        calls.append((args, cwd))
        assert not cwd.is_relative_to(repo.resolve())
        assert list(cwd.iterdir()) == []
        assert kwargs["inherit_env"] is False
        assert kwargs["env"]["OPENCODE_PURE"] == "true"
        assert pathlib.Path(kwargs["env"]["HOME"]).is_relative_to(context.run_dir)
        assert "OPENCODE_CONFIG" not in kwargs["env"]
        if args[1] == "export":
            return subprocess.CompletedProcess(
                args, 0, stdout='{"session":"session-1"}\n', stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.llm.run_command", fake_run_command)

    finalize_opencode_session(context, "docs.apply", "session-1")

    assert [args[1:3] for args, _ in calls] == [
        ["export", "session-1"],
        ["session", "delete"],
    ]
    transcript = next((context.git_dir / "ai-push-hooks" / "transcripts").iterdir())
    assert transcript.read_text(encoding="utf-8") == '{"session":"session-1"}\n'
    assert not (repo / ".git" / "opencode").exists()
    status_after = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status_after == status_before


@pytest.mark.parametrize(
    ("return_code", "stdout"),
    [(1, "export failed\n"), (0, "")],
)
def test_finalize_session_warns_and_deletes_when_export_returns_false(
    tmp_path: pathlib.Path,
    monkeypatch,
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
    commands: list[list[str]] = []

    def fake_run_command(args, **kwargs):
        commands.append(args)
        if args[1] == "export":
            return subprocess.CompletedProcess(args, return_code, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.llm.run_command", fake_run_command)

    finalize_opencode_session(context, "docs.query", "session-failed")

    assert [args[1:3] for args in commands] == [
        ["export", "session-failed"],
        ["session", "delete"],
    ]
    assert list((context.git_dir / "ai-push-hooks" / "transcripts").iterdir()) == []
    assert "Could not capture the OpenCode transcript" in capsys.readouterr().err


def test_finalize_session_warns_and_deletes_when_export_raises(
    tmp_path: pathlib.Path, monkeypatch, capsys
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
    commands: list[list[str]] = []

    def fake_run_command(args, **kwargs):
        commands.append(args)
        if args[1] == "export":
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.llm.run_command", fake_run_command)

    finalize_opencode_session(context, "docs.query", "session-timeout")

    assert [args[1:3] for args in commands] == [
        ["export", "session-timeout"],
        ["session", "delete"],
    ]
    assert "Could not capture the OpenCode transcript" in capsys.readouterr().err


def test_finalize_session_warns_and_deletes_when_transcript_write_raises(
    tmp_path: pathlib.Path, monkeypatch, capsys
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
    commands: list[list[str]] = []

    def fake_run_command(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout='{"session":"session-write"}\n', stderr=""
        )

    monkeypatch.setattr("ai_push_hooks.executors.llm.run_command", fake_run_command)
    monkeypatch.setattr(
        "ai_push_hooks.executors.llm.write_text_no_follow",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated transcript failure")),
    )

    finalize_opencode_session(context, "docs.query", "session-write")

    assert [args[1:3] for args in commands] == [
        ["export", "session-write"],
        ["session", "delete"],
    ]
    assert "Could not capture the OpenCode transcript" in capsys.readouterr().err


def test_finalize_session_delete_runs_outside_repository(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, capture_llm_transcript=False),
        llm=replace(config.llm, delete_session_after_run=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    captured: dict[str, object] = {}

    def fake_run_command(args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        captured["cwd_entries"] = list(kwargs["cwd"].iterdir())
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.llm.run_command", fake_run_command)

    finalize_opencode_session(context, "docs.apply", "session-1")

    assert captured["args"][1:3] == ["session", "delete"]
    assert not pathlib.Path(captured["cwd"]).is_relative_to(repo.resolve())
    assert captured["cwd_entries"] == []


def test_run_llm_step_always_selects_read_only_agent_policy(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    query_step = next(step for step in config.modules["docs"].steps if step.id == "query")
    agents: list[str] = []

    def fake_call_opencode(*args, **kwargs):
        agents.append(kwargs["agent"])
        return OpenCodeRunResult("[]", None, "", "", 0)

    monkeypatch.setattr("ai_push_hooks.executors.llm.call_opencode", fake_call_opencode)
    monkeypatch.setattr(
        "ai_push_hooks.executors.llm.finalize_opencode_session", lambda *args, **kwargs: None
    )

    assert run_llm_step(context, query_step, "prompt", [], "docs.query") == []
    assert agents == ["read-only"]


def test_call_opencode_rejects_external_and_symlinked_attachments(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    external = repo / "input.txt"
    external.write_text("external", encoding="utf-8")

    with pytest.raises(HookError, match="not a hook-owned artifact"):
        call_opencode(
            context,
            "docs.query",
            "llm:query",
            "prompt",
            [external],
            agent="read-only",
        )

    artifact = context.run_dir / "input.txt"
    artifact.write_text("artifact", encoding="utf-8")
    symlink = context.run_dir / "linked-input.txt"
    symlink.symlink_to(artifact)
    with pytest.raises(HookError, match="symlink"):
        call_opencode(
            context,
            "docs.query",
            "llm:query",
            "prompt",
            [symlink],
            agent="read-only",
        )


def test_json_retry_new_session_finalizes_each_attempt(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    query_step = next(step for step in config.modules["docs"].steps if step.id == "query")
    results = iter(
        [
            OpenCodeRunResult("not json", "session-1", "", "", 0),
            OpenCodeRunResult("[]", "session-2", "", "", 0),
        ]
    )
    reused: list[str | None] = []
    finalized: list[str | None] = []

    def fake_call(*args, **kwargs):
        reused.append(kwargs["existing_session_id"])
        return next(results)

    monkeypatch.setattr("ai_push_hooks.executors.llm.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.llm.finalize_opencode_session",
        lambda _context, _stage, session_id: finalized.append(session_id),
    )

    assert run_llm_step(context, query_step, "prompt", [], "docs.query") == []
    assert reused == [None, None]
    assert finalized == ["session-1", "session-2"]


def test_json_retry_reused_session_finalizes_only_after_last_attempt(
    tmp_path, monkeypatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, json_retry_new_session=False),
    )
    context = build_context(repo, config)
    query_step = next(step for step in config.modules["docs"].steps if step.id == "query")
    results = iter(
        [
            OpenCodeRunResult("not json", "session-1", "", "", 0),
            OpenCodeRunResult("[]", "session-1", "", "", 0),
        ]
    )
    reused: list[str | None] = []
    finalized: list[str | None] = []

    def fake_call(*args, **kwargs):
        reused.append(kwargs["existing_session_id"])
        return next(results)

    monkeypatch.setattr("ai_push_hooks.executors.llm.call_opencode", fake_call)
    monkeypatch.setattr(
        "ai_push_hooks.executors.llm.finalize_opencode_session",
        lambda _context, _stage, session_id: finalized.append(session_id),
    )

    assert run_llm_step(context, query_step, "prompt", [], "docs.query") == []
    assert reused == [None, "session-1"]
    assert finalized == ["session-1"]
