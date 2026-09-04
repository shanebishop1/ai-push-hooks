from __future__ import annotations

import pathlib

import pytest

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.types import HookError, ModuleConfig, ModuleRuntimeState, StepConfig


def test_cross_module_looking_artifact_reference_fails_clearly(tmp_path: pathlib.Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    state = ModuleRuntimeState(
        module=ModuleConfig(
            id="pr",
            enabled=True,
            steps=(StepConfig(id="compose", type="llm"),),
        )
    )

    local_artifact = tmp_path / "run" / "pr" / "00-collect" / "pr-context.txt"
    state.artifacts["collect/pr-context.txt"] = local_artifact

    with pytest.raises(HookError, match="Artifact references are module-local"):
        store.resolve_input(state, "docs:collect/push.diff")


@pytest.mark.parametrize(
    ("module_id", "step_id", "artifact_name"),
    [
        ("../escape", "collect", "result.json"),
        ("docs", "../escape", "result.json"),
        ("docs", "collect", "../result.json"),
        ("docs", "collect", "/tmp/result.json"),
    ],
)
def test_artifact_names_cannot_escape_run_directory(
    tmp_path: pathlib.Path,
    module_id: str,
    step_id: str,
    artifact_name: str,
) -> None:
    store = ArtifactStore(tmp_path / "run")
    store.prepare()
    state = ModuleRuntimeState(
        module=ModuleConfig(module_id, True, (StepConfig(id="collect", type="collect"),))
    )

    with pytest.raises(HookError):
        store.write_text(state, 0, step_id, artifact_name, "unsafe")

    assert not (tmp_path / "result.json").exists()


def test_artifact_write_rejects_symlink_escape(tmp_path: pathlib.Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    run_dir.mkdir()
    outside.mkdir()
    (run_dir / "docs").symlink_to(outside, target_is_directory=True)
    store = ArtifactStore(run_dir)
    state = ModuleRuntimeState(
        module=ModuleConfig("docs", True, (StepConfig(id="collect", type="collect"),))
    )

    with pytest.raises(HookError, match="symlink"):
        store.write_text(state, 0, "collect", "result.json", "unsafe")

    assert not (outside / "00-collect" / "result.json").exists()
