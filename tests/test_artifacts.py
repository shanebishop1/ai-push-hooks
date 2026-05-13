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
