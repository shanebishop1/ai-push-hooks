from __future__ import annotations

import json
import pathlib
import time
from typing import Any

from .types import HookError, ModuleRuntimeState


def generate_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


class ArtifactStore:
    def __init__(self, run_dir: pathlib.Path) -> None:
        self.run_dir = run_dir

    def prepare(self) -> pathlib.Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return self.run_dir

    def step_dir(self, module_id: str, step_index: int, step_id: str) -> pathlib.Path:
        path = self.run_dir / module_id / f"{step_index:02d}-{step_id}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def register(
        self,
        state: ModuleRuntimeState,
        step_id: str,
        artifact_name: str,
        path: pathlib.Path,
    ) -> pathlib.Path:
        state.artifacts[f"{step_id}/{artifact_name}"] = path
        return path

    def write_text(
        self,
        state: ModuleRuntimeState,
        step_index: int,
        step_id: str,
        artifact_name: str,
        content: str,
    ) -> pathlib.Path:
        path = self.step_dir(state.module.id, step_index, step_id) / artifact_name
        path.write_text(content, encoding="utf-8")
        return self.register(state, step_id, artifact_name, path)

    def write_json(
        self,
        state: ModuleRuntimeState,
        step_index: int,
        step_id: str,
        artifact_name: str,
        payload: Any,
    ) -> pathlib.Path:
        path = self.step_dir(state.module.id, step_index, step_id) / artifact_name
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        return self.register(state, step_id, artifact_name, path)

    def resolve_input(self, state: ModuleRuntimeState, reference: str) -> pathlib.Path:
        if ":" in reference:
            module_and_step, artifact_name = reference.split("/", 1)
            module_id, step_id = module_and_step.split(":", 1)
            key = f"{module_id}:{step_id}/{artifact_name}"
        else:
            key = reference
        path = state.artifacts.get(key)
        if path is None:
            path = state.artifacts.get(reference)
        if path is None:
            raise HookError(f"Unknown artifact reference: {reference}")
        return path

    def register_external(
        self,
        state: ModuleRuntimeState,
        module_id: str,
        step_id: str,
        artifact_name: str,
        path: pathlib.Path,
    ) -> None:
        state.artifacts[f"{module_id}:{step_id}/{artifact_name}"] = path
