from __future__ import annotations

import pathlib
import sys
import time
import threading

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.config import load_config
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.executors.apply import run_apply_step
from ai_push_hooks.types import CollectorResult, ModuleConfig, StepConfig

from .conftest import build_context, init_repo, make_config


def _write_barrier_llm(tmp_path: pathlib.Path) -> pathlib.Path:
    script = tmp_path / "barrier-llm.py"
    script.write_text(
        """
import os
import pathlib
import sys
import time

barrier = pathlib.Path(os.environ["AI_PUSH_HOOKS_TEST_BARRIER"])
barrier.mkdir(parents=True, exist_ok=True)
stage = sys.argv[1]
(barrier / (stage + ".ready")).touch()
expected = [item for item in os.environ["AI_PUSH_HOOKS_TEST_STAGES"].split(",") if item]
while not all((barrier / (item + ".ready")).exists() for item in expected):
    time.sleep(0.005)
sys.stdin.read()
print(stage)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return script


def _write_apply_runner(tmp_path: pathlib.Path) -> pathlib.Path:
    script = tmp_path / "serialized-apply.py"
    script.write_text(
        """
import pathlib
import sys

stage = sys.argv[1]
pathlib.Path("README.md" if stage == "a.apply" else "docs/INDEX.md").write_text(
    stage + "\\n", encoding="utf-8"
)
sys.stdin.read()
print("applied")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return script


def test_trusted_custom_llm_processes_really_overlap_at_a_barrier(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path / "overlap", branch="feature/scheduler")
    script = _write_barrier_llm(tmp_path)
    barrier = tmp_path / "barrier"
    monkeypatch.setenv("AI_PUSH_HOOKS_TEST_BARRIER", str(barrier))
    monkeypatch.setenv("AI_PUSH_HOOKS_TEST_STAGES", "a.query,b.query")
    repo.joinpath("ai-push-hooks.toml").write_text(
        f"""
[llm]
runner = "trusted-command"
model = "opaque/scheduler-model"
max_parallel = 2

[runners.trusted-command]
type = "command"
model = "opaque/profile-model"
project_access = "project"
prompt_transport = "stdin"
command = [{sys.executable!r}, {str(script)!r}, "{{stage}}"]

[workflow]
modules = ["a", "b"]

[modules.a]
enabled = true
[[modules.a.steps]]
id = "query"
type = "llm"
prompt = "a"
output = "answer.txt"

[modules.b]
enabled = true
[[modules.b.steps]]
id = "query"
type = "llm"
prompt = "b"
output = "answer.txt"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config, _ = load_config(repo)
    context = build_context(repo, config)

    result = WorkflowEngine(context, ArtifactStore(context.run_dir)).run()

    assert result.modules == {"a": "completed", "b": "completed"}
    assert (context.run_dir / "a" / "00-query" / "answer.txt").read_text(encoding="utf-8") == "a.query\n"
    assert (context.run_dir / "b" / "00-query" / "answer.txt").read_text(encoding="utf-8") == "b.query\n"


def test_custom_apply_is_serialized_against_all_other_side_effect_work(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path / "serial", branch="feature/scheduler")
    script = _write_apply_runner(tmp_path)
    repo.joinpath("ai-push-hooks.toml").write_text(
        f"""
[llm]
runner = "trusted-apply"
model = "opaque/apply-model"
max_parallel = 2

[runners.trusted-apply]
type = "command"
model = "opaque/apply-profile"
project_access = "project"
prompt_transport = "stdin"
command = [{sys.executable!r}, {str(script)!r}, "{{stage}}"]

[workflow]
modules = ["a", "b"]

[modules.a]
enabled = true
[[modules.a.steps]]
id = "apply"
type = "apply"
prompt = "a"
allow_paths = ["README.md"]

[modules.b]
enabled = true
[[modules.b.steps]]
id = "apply"
type = "apply"
prompt = "b"
allow_paths = ["docs/INDEX.md"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config, _ = load_config(repo)
    context = build_context(repo, config)
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def gated_apply(context, state, step, prompt, input_paths, stage_name):
        if state.module.id == "a":
            first_started.set()
            assert not second_started.is_set()
            assert release_first.wait(timeout=5)
        else:
            second_started.set()
        return run_apply_step(context, state, step, prompt, input_paths, stage_name)

    errors: list[BaseException] = []

    def run_engine() -> None:
        try:
            WorkflowEngine(
                context,
                ArtifactStore(context.run_dir),
                apply_executor=gated_apply,
            ).run()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run_engine)
    worker.start()
    assert first_started.wait(timeout=5)
    assert not second_started.is_set()
    release_first.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    assert second_started.is_set()
    assert (repo / "README.md").read_text(encoding="utf-8") == "a.apply\n"
    assert (repo / "docs" / "INDEX.md").read_text(encoding="utf-8") == "b.apply\n"


def test_module_local_sequencing_is_preserved(tmp_path: pathlib.Path) -> None:
    events: list[tuple[str, str, float]] = []

    def collect(context, state):
        events.append((state.module.id, "collect-start", time.perf_counter()))
        time.sleep(0.05 if state.module.id == "a" else 0.12)
        events.append((state.module.id, "collect-end", time.perf_counter()))
        return CollectorResult(artifacts={"out.txt": "ok"})

    def exec_handler(context, state, step, inputs):
        events.append((state.module.id, "exec-start", time.perf_counter()))
        return {"ok": True}

    config = make_config(
        [
            ModuleConfig(
                id="a",
                enabled=True,
                steps=(
                    StepConfig(id="collect", type="collect", collector="stub"),
                    StepConfig(id="exec", type="exec", executor="stub"),
                ),
            ),
            ModuleConfig(id="b", enabled=True, steps=(StepConfig(id="collect", type="collect", collector="stub"),)),
        ]
    )
    repo = init_repo(tmp_path / "three", branch="feature/a")
    context = build_context(repo, config)
    engine = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        collectors={"stub": collect},
        exec_handlers={"stub": exec_handler},
    )
    engine.run()
    a_collect_end = next(stamp for module, label, stamp in events if module == "a" and label == "collect-end")
    a_exec_start = next(stamp for module, label, stamp in events if module == "a" and label == "exec-start")
    assert a_exec_start >= a_collect_end
