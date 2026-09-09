from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
from types import ModuleType

import pytest

from ai_push_hooks.executors import apply as apply_executor
from ai_push_hooks.executors.runners.contracts import RunnerResult


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_script(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def live_probe() -> ModuleType:
    return _load_script("runner_live_probe_test", "runner-live-probe.py")


@pytest.fixture
def conformance() -> ModuleType:
    return _load_script("runner_conformance_test", "runner-conformance.py")


def test_live_probe_gate_prevents_child_invocation_without_opt_in(
    live_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def child(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("the live gate must run before disposable-project setup")

    monkeypatch.delenv(live_probe.LIVE_OPT_IN, raising=False)
    monkeypatch.setattr(live_probe.subprocess, "run", child)

    assert live_probe.main(["--profile", "codex", "--model", "gpt-5.6-codex"]) == 2
    assert calls == []


def test_live_probe_rejects_model_environment_override_before_setup(
    live_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def child(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("model override rejection must precede project setup")

    monkeypatch.setenv(live_probe.LIVE_OPT_IN, "1")
    monkeypatch.setenv(live_probe.MODEL_OVERRIDE_ENV, "different/model")
    monkeypatch.setattr(live_probe.subprocess, "run", child)

    assert live_probe.main(["--profile", "codex", "--model", "gpt-5.6-codex"]) == 2
    assert calls == []


def test_live_selection_is_profile_and_model_only_not_a_command_or_credential(
    live_probe: ModuleType,
) -> None:
    profile = live_probe.build_profile("pi", "provider/exact-model-id")

    assert profile.type == "command"
    assert profile.command[0:3] == ("pi", "--print", "--no-session")
    assert "--api-key" not in profile.command
    assert "{model}" in profile.command

    with pytest.raises(live_probe.LiveProbeError):
        live_probe.build_profile("sh -c 'pi'", "provider/exact-model-id")
    with pytest.raises(live_probe.LiveProbeError):
        live_probe.build_profile("pi", "--api-key=secret")
    with pytest.raises(live_probe.LiveProbeError):
        live_probe.build_profile("pi", "provider/model; touch compromised")


def test_fake_adapters_verify_nonce_and_production_apply_allowlist(
    live_probe: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test deliberately provides a fake adapter at the production apply
    # boundary.  It exercises the real disposable Git setup, staging inventory,
    # allowlist, CAS, and propagation policy without reading credentials or
    # starting Codex/Pi/Claude/OpenCode.
    monkeypatch.setenv(live_probe.LIVE_OPT_IN, "1")
    monkeypatch.setenv(live_probe.LIVE_APPLY_OPT_IN, "1")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read")

    def fake_llm(context: object, step: object, prompt: str, inputs: list[pathlib.Path], stage: str) -> str:
        assert live_probe.NONCE_FILENAME in prompt
        project = context.repo_root  # type: ignore[attr-defined]
        return (project / live_probe.NONCE_FILENAME).read_text(encoding="utf-8").strip()

    def fake_runner(
        context: object,
        step: object,
        prompt: str,
        inputs: list[pathlib.Path],
        stage: str,
        *,
        working_directory: pathlib.Path,
    ) -> RunnerResult:
        assert working_directory != context.repo_root  # type: ignore[attr-defined]
        assert step.allow_paths == (live_probe.README_FILENAME,)  # type: ignore[attr-defined]
        readme = working_directory / live_probe.README_FILENAME
        readme.write_text(
            readme.read_text(encoding="utf-8") + live_probe.APPLY_MARKER + "\n",
            encoding="utf-8",
        )
        return RunnerResult(final_text="", returncode=0, stdout="", stderr="")

    monkeypatch.setattr(live_probe, "run_llm_step", fake_llm)
    monkeypatch.setattr(apply_executor, "run_runner_once", fake_runner)

    result = live_probe.run_live_probe(
        "pi",
        "provider/exact-model-id",
        apply=True,
        apply_budget_seconds=10,
        timeout_seconds=20,
        env={
            live_probe.LIVE_OPT_IN: "1",
            live_probe.LIVE_APPLY_OPT_IN: "1",
        },
    )

    assert result.nonce_verified is True
    assert result.changed_files == (live_probe.README_FILENAME,)


def test_read_probe_rejects_git_visible_mutation_before_apply(
    live_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(live_probe.LIVE_OPT_IN, "1")

    def mutating_llm(
        context: object, step: object, prompt: str, inputs: list[pathlib.Path], stage: str
    ) -> str:
        project = context.repo_root  # type: ignore[attr-defined]
        (project / live_probe.OUTSIDE_FILENAME).write_text("unexpected\n", encoding="utf-8")
        return (project / live_probe.NONCE_FILENAME).read_text(encoding="utf-8").strip()

    monkeypatch.setattr(live_probe, "run_llm_step", mutating_llm)

    with pytest.raises(live_probe.LiveProbeError, match="Git-visible"):
        live_probe.run_live_probe(
            "pi",
            "provider/exact-model-id",
            env={live_probe.LIVE_OPT_IN: "1"},
        )


def test_conformance_checks_only_contract_text_and_never_auth_commands(
    conformance: ModuleType,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "codex-cli 0.148.0\n", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            "--json --color never --sandbox --ephemeral --cd --skip-git-repo-check --model -\n",
            "",
        )

    result = conformance._check_cli(
        "codex",
        "/fake/codex",
        required_help=conformance.CODEX_REQUIRED_HELP,
        run=fake_run,
    )

    assert result.status == "PASS"
    assert calls == [
        ("/fake/codex", "--version"),
        ("/fake/codex", "exec", "--help"),
    ]
    assert not any(argument in {"auth", "login", "status"} for call in calls for argument in call)
