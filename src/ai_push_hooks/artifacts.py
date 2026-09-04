from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .paths import (
    ensure_private_directory,
    is_path_within,
    path_has_symlink,
    path_is_link_or_reparse,
    resolve_contained_path,
    validate_path_component,
    write_text_no_follow,
)
from .types import HookError, ModuleRuntimeState


def generate_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


class ArtifactStore:
    def __init__(self, run_dir: pathlib.Path) -> None:
        self.run_dir = run_dir

    def prepare(self) -> pathlib.Path:
        if path_is_link_or_reparse(self.run_dir):
            raise HookError(
                f"Artifact run directory must not be a symlink or reparse point: {self.run_dir}"
            )
        return ensure_private_directory(self.run_dir)

    def step_dir(self, module_id: str, step_index: int, step_id: str) -> pathlib.Path:
        module_name = validate_path_component(module_id, "Artifact module id")
        step_name = validate_path_component(step_id, "Artifact step id")
        lexical_module_path = self.run_dir / module_name
        if path_has_symlink(self.run_dir, lexical_module_path):
            raise HookError(f"Artifact module path must not traverse a symlink: {module_id}")
        module_path = resolve_contained_path(self.run_dir, module_name, "Artifact module path")
        lexical_step_path = module_path / f"{step_index:02d}-{step_name}"
        if path_has_symlink(self.run_dir, lexical_step_path):
            raise HookError(f"Artifact step path must not traverse a symlink: {step_id}")
        path = resolve_contained_path(
            module_path,
            f"{step_index:02d}-{step_name}",
            "Artifact step path",
        )
        return ensure_private_directory(path)

    def _artifact_path(
        self,
        module_id: str,
        step_index: int,
        step_id: str,
        artifact_name: str,
    ) -> pathlib.Path:
        name = validate_path_component(artifact_name, "Artifact name")
        return resolve_contained_path(
            self.step_dir(module_id, step_index, step_id),
            name,
            "Artifact output path",
        )

    def register(
        self,
        state: ModuleRuntimeState,
        step_id: str,
        artifact_name: str,
        path: pathlib.Path,
    ) -> pathlib.Path:
        validate_path_component(step_id, "Artifact step id")
        validate_path_component(artifact_name, "Artifact name")
        resolved_run_dir = self.run_dir.resolve(strict=False)
        if path_has_symlink(self.run_dir, path):
            raise HookError(f"Artifact path must not traverse a symlink: {path}")
        if not is_path_within(path.resolve(strict=False), resolved_run_dir):
            raise HookError(f"Artifact path escapes run directory: {path}")
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
        path = self._artifact_path(state.module.id, step_index, step_id, artifact_name)
        write_text_no_follow(path, content)
        return self.register(state, step_id, artifact_name, path)

    def write_json(
        self,
        state: ModuleRuntimeState,
        step_index: int,
        step_id: str,
        artifact_name: str,
        payload: Any,
    ) -> pathlib.Path:
        path = self._artifact_path(state.module.id, step_index, step_id, artifact_name)
        write_text_no_follow(path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
        return self.register(state, step_id, artifact_name, path)

    def resolve_input(self, state: ModuleRuntimeState, reference: str) -> pathlib.Path:
        if ":" in reference:
            try:
                module_and_step, artifact_name = reference.split("/", 1)
                module_id, step_id = module_and_step.split(":", 1)
            except ValueError as exc:
                raise HookError(f"Invalid artifact reference: {reference}") from exc
            key = f"{module_id}:{step_id}/{artifact_name}"
        else:
            key = reference
        path = state.artifacts.get(key)
        if path is None:
            path = state.artifacts.get(reference)
        if path is None:
            if ":" in reference:
                raise HookError(
                    f"Unknown artifact reference: {reference}. Artifact references are module-local; "
                    "use '<step>/<artifact>' from an earlier step in the same module."
                )
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
        validate_path_component(module_id, "Artifact module id")
        validate_path_component(step_id, "Artifact step id")
        validate_path_component(artifact_name, "Artifact name")
        state.artifacts[f"{module_id}:{step_id}/{artifact_name}"] = path
