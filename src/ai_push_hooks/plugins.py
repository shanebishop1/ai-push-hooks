"""Public, deliberately small contracts for repository-local workflow callbacks.

This module is a contract surface, not a plugin SDK.  Callback code should only
depend on the three records exported here; loading and dispatch remain host
responsibilities.
"""

from __future__ import annotations

import copy
import json
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .paths import validate_path_component
from .types import CollectorResult, HookError, HookLogger, PushRefUpdate

__all__ = ["CollectorResult", "PluginContext", "PushContext"]


def _freeze(value: Any) -> Any:
    """Copy a supported snapshot value into immutable public containers."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class PushContext:
    """The bounded push facts made available to a configured callback."""

    branch_name: str
    checked_out_branch: str
    base_branch: str
    ranges: tuple[str, ...]
    changed_files: tuple[str, ...]
    diff_text: str
    push_updates: tuple[PushRefUpdate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ranges", tuple(self.ranges))
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(self, "push_updates", tuple(self.push_updates))


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Frozen, flat input passed as the sole argument to a callback."""

    repo_root: pathlib.Path
    module_id: str
    step_id: str
    inputs: Mapping[str, pathlib.Path]
    options: Mapping[str, Any]
    prior_module_metadata: Mapping[str, Any]
    push: PushContext
    logger: HookLogger

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        object.__setattr__(self, "options", _freeze(self.options))
        object.__setattr__(self, "prior_module_metadata", _freeze(self.prior_module_metadata))


def _require_json(value: Any, label: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HookError(f"{label} must be JSON-serializable") from exc


def validate_collector_result(value: Any) -> CollectorResult:
    """Validate and return a callback's collector result without mutating it."""

    if not isinstance(value, CollectorResult):
        raise HookError("Python collect callback must return CollectorResult")
    if not isinstance(value.artifacts, dict):
        raise HookError("CollectorResult.artifacts must be a mapping")
    if type(value.skip_module) is not bool:
        raise HookError("CollectorResult.skip_module must be a boolean")
    if not isinstance(value.skip_reason, str):
        raise HookError("CollectorResult.skip_reason must be a string")
    if not isinstance(value.metadata, dict):
        raise HookError("CollectorResult.metadata must be a mapping")
    _require_json(value.metadata, "CollectorResult.metadata")
    for name, payload in value.artifacts.items():
        if not isinstance(name, str):
            raise HookError("CollectorResult artifact names must be strings")
        validate_path_component(name, "CollectorResult artifact name")
        if not isinstance(payload, (str, dict, list)):
            raise HookError(
                f"CollectorResult.artifacts[{name!r}] must be a string, object, or array"
            )
        _require_json(payload, f"CollectorResult artifact {name!r}")
    return value


def validate_exec_result(value: Any) -> dict[str, Any]:
    """Validate the JSON object returned by a Python exec callback."""

    if not isinstance(value, dict):
        raise HookError("Python exec callback must return a dict")
    _require_json(value, "Python exec callback result")
    return value


def validate_assert_result(value: Any) -> dict[str, Any]:
    """Validate the JSON object and strict boolean verdict from an assert callback."""

    if not isinstance(value, dict):
        raise HookError("Python assert callback must return a dict")
    if "ok" not in value or type(value["ok"]) is not bool:
        raise HookError("Python assert callback result.ok must be a boolean")
    if "message" in value and not isinstance(value["message"], str):
        raise HookError("Python assert callback result.message must be a string")
    _require_json(value, "Python assert callback result")
    return value
