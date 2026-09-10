"""Static, lazy registry for the four built-in runner adapter types."""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from types import ModuleType
from typing import Callable, Mapping

from .contracts import (
    Runner,
    RunnerAdapterUnavailableError,
    RunnerContractError,
)


KNOWN_RUNNER_TYPES = ("opencode", "codex", "claude", "command")


@dataclass(frozen=True)
class LazyRunnerSpec:
    """Import and construct an adapter only when that type is selected."""

    module: str
    factory_name: str = "create_runner"

    def load(self) -> Runner:
        try:
            module: ModuleType = importlib.import_module(self.module)
        except ModuleNotFoundError as exc:
            if exc.name == self.module:
                raise RunnerAdapterUnavailableError(
                    "selected runner adapter is not installed in this build"
                ) from exc
            raise RunnerAdapterUnavailableError(
                "selected runner adapter could not load its dependency",
                details=type(exc).__name__,
            ) from exc
        try:
            factory: Callable[[], Runner] = getattr(module, self.factory_name)
        except AttributeError as exc:
            raise RunnerAdapterUnavailableError(
                "selected runner adapter has no factory"
            ) from exc
        try:
            runner = factory()
        except RunnerAdapterUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RunnerAdapterUnavailableError(
                "selected runner adapter could not be constructed",
                details=type(exc).__name__,
            ) from exc
        if not callable(getattr(runner, "run", None)):
            raise RunnerAdapterUnavailableError("selected runner does not implement run(request)")
        return runner


_DEFAULT_SPECS = {
    "opencode": LazyRunnerSpec("ai_push_hooks.executors.runners.opencode"),
    "codex": LazyRunnerSpec("ai_push_hooks.executors.runners.codex"),
    "claude": LazyRunnerSpec("ai_push_hooks.executors.runners.claude"),
    "command": LazyRunnerSpec("ai_push_hooks.executors.runners.command"),
}


class RunnerRegistry:
    """A fixed registry; it intentionally has no runtime plugin registration API."""

    def __init__(self, specs: Mapping[str, LazyRunnerSpec | Callable[[], Runner]] | None = None) -> None:
        selected = _DEFAULT_SPECS if specs is None else dict(specs)
        unknown = set(selected) - set(KNOWN_RUNNER_TYPES)
        if unknown:
            raise RunnerContractError(
                "runner registry contains unsupported adapter types: " + ", ".join(sorted(unknown))
            )
        if set(selected) != set(KNOWN_RUNNER_TYPES):
            raise RunnerContractError("runner registry must contain all four known adapter types")
        self._specs = dict(selected)
        self._loaded: dict[str, Runner] = {}
        self._load_locks = {
            runner_type: threading.Lock() for runner_type in KNOWN_RUNNER_TYPES
        }

    @property
    def known_types(self) -> tuple[str, ...]:
        return KNOWN_RUNNER_TYPES

    def get(self, runner_type: str) -> Runner:
        if runner_type not in self._specs:
            raise RunnerContractError(f"unknown runner type: {runner_type!r}")
        runner = self._loaded.get(runner_type)
        if runner is not None:
            return runner
        # Initialization is serialized only for the selected adapter.  Other
        # runner types may load concurrently, which matters for capability
        # probes such as Claude's --help check.
        with self._load_locks[runner_type]:
            runner = self._loaded.get(runner_type)
            if runner is None:
                spec = self._specs[runner_type]
                runner = spec.load() if isinstance(spec, LazyRunnerSpec) else spec()
                self._loaded[runner_type] = runner
            return runner

    def __contains__(self, runner_type: object) -> bool:
        return runner_type in self._specs


DEFAULT_RUNNER_REGISTRY = RunnerRegistry()


def get_runner(runner_type: str, *, registry: RunnerRegistry = DEFAULT_RUNNER_REGISTRY) -> Runner:
    """Resolve one of the four static adapter types lazily."""

    return registry.get(runner_type)
