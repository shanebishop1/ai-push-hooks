from __future__ import annotations

import pathlib
import time

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.types import CollectorResult, ModuleConfig, StepConfig

from .conftest import build_context, init_repo, make_config


def test_read_only_steps_can_overlap(tmp_path: pathlib.Path) -> None:
    events: list[tuple[str, str, float]] = []

    def collect(context, state):
        events.append((state.module.id, "start", time.perf_counter()))
        time.sleep(0.15)
        events.append((state.module.id, "end", time.perf_counter()))
        return CollectorResult(artifacts={"out.txt": "ok"})

    config = make_config(
        [
            ModuleConfig(id="a", enabled=True, steps=(StepConfig(id="collect", type="collect", collector="stub"),)),
            ModuleConfig(id="b", enabled=True, steps=(StepConfig(id="collect", type="collect", collector="stub"),)),
        ]
    )
    repo = init_repo(tmp_path / "one", branch="feature/a")
    context = build_context(repo, config)
    engine = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        collectors={"stub": collect},
    )
    engine.run()
    starts = {module: stamp for module, label, stamp in events if label == "start"}
    assert abs(starts["a"] - starts["b"]) < 0.1


def test_side_effect_steps_run_serially(tmp_path: pathlib.Path) -> None:
    events: list[tuple[str, str, float]] = []

    def exec_handler(context, state, step, inputs):
        events.append((state.module.id, "start", time.perf_counter()))
        time.sleep(0.1)
        events.append((state.module.id, "end", time.perf_counter()))
        return {"ok": True}

    config = make_config(
        [
            ModuleConfig(id="a", enabled=True, steps=(StepConfig(id="apply", type="exec", executor="stub"),)),
            ModuleConfig(id="b", enabled=True, steps=(StepConfig(id="apply", type="exec", executor="stub"),)),
        ]
    )
    repo = init_repo(tmp_path / "two", branch="feature/a")
    context = build_context(repo, config)
    engine = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        collectors={},
        exec_handlers={"stub": exec_handler},
    )
    engine.run()
    a_start = next(stamp for module, label, stamp in events if module == "a" and label == "start")
    a_end = next(stamp for module, label, stamp in events if module == "a" and label == "end")
    b_start = next(stamp for module, label, stamp in events if module == "b" and label == "start")
    assert b_start >= a_end


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
