from __future__ import annotations

import json
import pathlib
import sys

import pytest

from ai_push_hooks.artifacts import ArtifactStore, PLUGIN_ARTIFACT_MAX_BYTES
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.types import CollectorResult, HookError, ModuleConfig, StepConfig

from .conftest import build_context, make_config


def _write(repo: pathlib.Path, source: str, name: str = "hooks.py") -> str:
    path = repo / name
    path.write_text(source, encoding="utf-8")
    return f"{name}:hook"


def _engine(
    repo: pathlib.Path,
    module: ModuleConfig,
    *,
    artifacts: ArtifactStore | None = None,
    collectors=None,
) -> WorkflowEngine:
    config = make_config([module])
    context = build_context(repo, config)
    return WorkflowEngine(
        context,
        artifacts or ArtifactStore(context.run_dir),
        collectors=collectors,
    )


def test_python_collect_receives_inputs_and_preserves_skip_metadata(
    repo: pathlib.Path,
) -> None:
    reference = _write(
        repo,
        "from ai_push_hooks.types import CollectorResult\n"
        "def hook(context):\n"
        "    assert context.inputs['seed/input.txt'].read_text() == 'input'\n"
        "    return CollectorResult(artifacts={'answer.txt': 'answer'}, "
        "metadata={'source': 'plugin'}, skip_module=True, skip_reason='done')\n",
    )
    module = ModuleConfig(
        id="quality",
        enabled=True,
        steps=(
            StepConfig(id="seed", type="collect", collector="seed"),
            StepConfig(
                id="collect",
                type="collect",
                python=reference,
                inputs=("seed/input.txt",),
            ),
            StepConfig(id="unreached", type="exec", executor="missing"),
        ),
    )

    def seed(context, state):
        return CollectorResult(artifacts={"input.txt": "input"})

    engine = _engine(repo, module, collectors={"seed": seed})
    result = engine.run()
    assert result.modules == {"quality": "completed"}
    step_dir = repo / ".git" / "ai-push-hooks-tests" / "quality" / "01-collect"
    assert (step_dir / "answer.txt").read_text(encoding="utf-8") == "answer"
    assert not (
        repo / ".git" / "ai-push-hooks-tests" / "quality" / "02-unreached"
    ).exists()


@pytest.mark.parametrize(
    ("step_type", "source"),
    [
        ("collect", "def hook(context): return object()\n"),
        ("exec", "def hook(context): return {'value': object()}\n"),
        ("assert", "def hook(context): return {'ok': 1}\n"),
        (
            "collect",
            "from ai_push_hooks.types import CollectorResult\n"
            "def hook(context): return CollectorResult(artifacts={'bad.txt': object()})\n",
        ),
        (
            "exec",
            "def hook(context):\n"
            "    value = {}\n"
            "    value['self'] = value\n"
            "    return {'value': value}\n",
        ),
        (
            "assert",
            "def hook(context):\n"
            "    value = []\n"
            "    current = value\n"
            "    for _ in range(2000):\n"
            "        nested = []\n"
            "        current.append(nested)\n"
            "        current = nested\n"
            "    return {'ok': True, 'value': value}\n",
        ),
    ],
)
def test_python_results_are_validated_before_writes(
    repo: pathlib.Path, step_type: str, source: str
) -> None:
    reference = _write(repo, source)
    implementation = {"collect": {}, "exec": {}, "assert": {}}[step_type]
    step = StepConfig(id="step", type=step_type, python=reference, **implementation)
    module = ModuleConfig(id="quality", enabled=True, steps=(step,))

    engine = _engine(repo, module)
    with pytest.raises(HookError):
        engine.run()
    assert not (repo / ".git" / "ai-push-hooks-tests" / "quality" / "00-step").exists()


def test_python_assert_persists_report_before_false_verdict(repo: pathlib.Path) -> None:
    reference = _write(
        repo,
        "def hook(context): return {'ok': False, 'message': 'policy failed', 'severity': 'high'}\n",
    )
    module = ModuleConfig(
        id="quality",
        enabled=True,
        steps=(StepConfig(id="policy", type="assert", python=reference),),
    )

    engine = _engine(repo, module)
    with pytest.raises(HookError, match="policy failed"):
        engine.run()
    report = (
        repo / ".git" / "ai-push-hooks-tests" / "quality" / "00-policy" / "result.json"
    )
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is False


def test_python_exec_does_not_merge_result_into_module_metadata(
    repo: pathlib.Path,
) -> None:
    _write(
        repo,
        "from ai_push_hooks.types import CollectorResult\n"
        "def exec_hook(context): return {'metadata_value': 'must not merge'}\n"
        "def collect_hook(context):\n"
        "    present = 'metadata_value' in context.prior_module_metadata\n"
        "    return CollectorResult(artifacts={'metadata.txt': str(present)})\n",
    )
    module = ModuleConfig(
        id="quality",
        enabled=True,
        steps=(
            StepConfig(id="exec", type="exec", python="hooks.py:exec_hook"),
            StepConfig(id="collect", type="collect", python="hooks.py:collect_hook"),
        ),
    )
    engine = _engine(repo, module)
    engine.run()
    metadata = (
        repo
        / ".git"
        / "ai-push-hooks-tests"
        / "quality"
        / "01-collect"
        / "metadata.txt"
    )
    assert metadata.read_text(encoding="utf-8") == "False"


def test_imports_happen_only_after_when_env_and_input_gates(
    repo: pathlib.Path, monkeypatch
) -> None:
    marker = repo / "imported.txt"
    reference = _write(
        repo,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        "def hook(context): return {'ok': True}\n",
    )
    monkeypatch.delenv("PLUGIN_GATE", raising=False)
    gated = ModuleConfig(
        id="gated",
        enabled=True,
        steps=(
            StepConfig(id="env", type="exec", python=reference, when_env="PLUGIN_GATE"),
        ),
    )
    _engine(repo, gated).run()
    assert not marker.exists()

    disabled = ModuleConfig(
        id="disabled",
        enabled=False,
        steps=(StepConfig(id="disabled", type="exec", python=reference),),
    )
    _engine(repo, disabled).run()
    assert not marker.exists()

    missing = ModuleConfig(
        id="missing",
        enabled=True,
        steps=(
            StepConfig(
                id="input", type="collect", python=reference, inputs=("missing.txt",)
            ),
        ),
    )
    with pytest.raises(HookError, match="Unknown artifact reference"):
        _engine(repo, missing).run()
    assert not marker.exists()


def test_plugin_artifact_budget_is_checked_before_any_write(repo: pathlib.Path) -> None:
    reference = _write(
        repo,
        "from ai_push_hooks.types import CollectorResult\n"
        "def hook(context): return CollectorResult(artifacts={'large.txt': '12345'})\n",
    )

    class SmallBudgetStore(ArtifactStore):
        def serialize_plugin_artifacts(self, artifacts):
            return super().serialize_plugin_artifacts(
                artifacts, max_artifact_bytes=4, max_total_bytes=8
            )

    module = ModuleConfig(
        id="quality",
        enabled=True,
        steps=(StepConfig(id="collect", type="collect", python=reference),),
    )
    store = SmallBudgetStore(repo / ".git" / "ai-push-hooks-tests")
    with pytest.raises(HookError, match="per-artifact"):
        _engine(repo, module, artifacts=store).run()
    assert not (store.run_dir / "quality" / "00-collect").exists()


def test_plugin_artifact_aggregate_budget_is_checked_before_any_write(
    repo: pathlib.Path,
) -> None:
    reference = _write(
        repo,
        "from ai_push_hooks.types import CollectorResult\n"
        "def hook(context): return CollectorResult(artifacts={'one.txt': '1234', 'two.txt': '5678'})\n",
    )

    class SmallBudgetStore(ArtifactStore):
        def serialize_plugin_artifacts(self, artifacts):
            return super().serialize_plugin_artifacts(
                artifacts, max_artifact_bytes=4, max_total_bytes=6
            )

    module = ModuleConfig(
        id="quality",
        enabled=True,
        steps=(StepConfig(id="collect", type="collect", python=reference),),
    )
    store = SmallBudgetStore(repo / ".git" / "ai-push-hooks-tests")
    with pytest.raises(HookError, match="aggregate"):
        _engine(repo, module, artifacts=store).run()
    assert not (store.run_dir / "quality" / "00-collect").exists()


@pytest.mark.parametrize(
    ("step_type", "source"),
    [
        (
            "exec",
            f"def hook(context): return {{'value': 'x' * {PLUGIN_ARTIFACT_MAX_BYTES}}}\n",
        ),
        (
            "assert",
            f"def hook(context): return {{'ok': True, 'message': 'x' * {PLUGIN_ARTIFACT_MAX_BYTES}}}\n",
        ),
    ],
)
def test_python_exec_and_assert_result_artifacts_are_bounded(
    repo: pathlib.Path, step_type: str, source: str
) -> None:
    reference = _write(repo, source)

    module = ModuleConfig(
        id="quality",
        enabled=True,
        steps=(StepConfig(id="result", type=step_type, python=reference),),
    )
    with pytest.raises(HookError, match="per-artifact"):
        _engine(repo, module).run()
    assert not (
        repo / ".git" / "ai-push-hooks-tests" / "quality" / "00-result"
    ).exists()


def test_command_steps_persist_streams_without_model_accounting(
    repo: pathlib.Path,
) -> None:
    step = StepConfig(
        id="command",
        type="exec",
        command=(
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ),
    )
    module = ModuleConfig(id="quality", enabled=True, steps=(step,))
    engine = _engine(repo, module)
    engine.run()

    step_dir = repo / ".git" / "ai-push-hooks-tests" / "quality" / "00-command"
    assert (step_dir / "stdout.txt").read_text(encoding="utf-8") == "out\n"
    assert (step_dir / "stderr.txt").read_text(encoding="utf-8") == "err\n"
    assert (
        json.loads((step_dir / "result.json").read_text(encoding="utf-8"))["returncode"]
        == 0
    )
    assert engine.context.logger.llm_calls == []


def test_command_assert_report_is_saved_before_failure(repo: pathlib.Path) -> None:
    step = StepConfig(
        id="policy",
        type="assert",
        command=(
            sys.executable,
            "-c",
            "import sys; print('failed', file=sys.stderr); raise SystemExit(3)",
        ),
    )
    module = ModuleConfig(id="quality", enabled=True, steps=(step,))
    engine = _engine(repo, module)

    with pytest.raises(HookError):
        engine.run()
    report = (
        repo / ".git" / "ai-push-hooks-tests" / "quality" / "00-policy" / "result.json"
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert (report.parent / "stdout.txt").exists()
    assert (report.parent / "stderr.txt").read_text(encoding="utf-8") == "failed\n"
