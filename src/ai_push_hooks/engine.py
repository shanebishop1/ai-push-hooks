from __future__ import annotations

import pathlib
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

from .artifacts import ArtifactStore
from .config import resolve_prompt_text
from .executors.apply import run_apply_step
from .executors.ask import run_ask_step
from .executors.assertions import ASSERTION_HANDLERS
from .executors.exec import EXEC_HANDLERS
from .executors.step_commands import execute_step_command
from .git_utils import env_bool
from .modules import COLLECTORS
from .plugin_loader import PluginDispatcher
from .plugins import (
    validate_assert_result,
    validate_collector_result,
    validate_exec_result,
)
from .types import (
    CollectorResult,
    HookError,
    ModuleRuntimeState,
    RuntimeContext,
    StepConfig,
    StepResult,
    WorkflowRunResult,
)

CollectorHandler = Callable[[RuntimeContext, ModuleRuntimeState], CollectorResult]
ExecHandler = Callable[
    [RuntimeContext, ModuleRuntimeState, StepConfig, list[pathlib.Path]], dict[str, Any]
]
AssertionHandler = Callable[
    [RuntimeContext, StepConfig, list[pathlib.Path]], dict[str, Any]
]


class WorkflowEngine:
    def __init__(
        self,
        context: RuntimeContext,
        artifacts: ArtifactStore,
        collectors: dict[str, CollectorHandler] | None = None,
        exec_handlers: dict[str, ExecHandler] | None = None,
        assertion_handlers: dict[str, AssertionHandler] | None = None,
        ask_executor: Callable[
            [RuntimeContext, StepConfig, str, list[pathlib.Path], str], Any
        ] = run_ask_step,
        apply_executor: Callable[
            [
                RuntimeContext,
                ModuleRuntimeState,
                StepConfig,
                str,
                list[pathlib.Path],
                str,
            ],
            dict[str, object],
        ] = run_apply_step,
    ) -> None:
        self.context = context
        self.artifacts = artifacts
        self.collectors = collectors or COLLECTORS
        self.exec_handlers = exec_handlers or EXEC_HANDLERS
        self.assertion_handlers = assertion_handlers or ASSERTION_HANDLERS
        self.ask_executor = ask_executor
        self.apply_executor = apply_executor
        self._plugin_dispatcher: PluginDispatcher | None = None

    def run(self) -> WorkflowRunResult:
        self.artifacts.prepare()
        # A loader/cache is deliberately scoped to one workflow run.  In
        # particular, a second run must not reuse a module snapshot from the
        # first run.
        self._plugin_dispatcher = PluginDispatcher()
        states = [
            ModuleRuntimeState(module=self.context.config.modules[module_id])
            for module_id in self.context.config.workflow.modules
            if self.context.config.modules[module_id].enabled
        ]
        statuses: dict[str, str] = {state.module.id: "pending" for state in states}
        futures: dict[Future[StepResult], tuple[ModuleRuntimeState, StepConfig]] = {}

        with ThreadPoolExecutor(
            max_workers=max(1, self.context.config.llm.max_parallel)
        ) as pool:
            while True:
                for state in states:
                    if state.status in {"completed", "failed"}:
                        statuses[state.module.id] = state.status
                        continue
                    if state.active_step_id is not None:
                        continue
                    step = state.next_step
                    if step is None:
                        state.status = "completed"
                        statuses[state.module.id] = "completed"
                        continue
                    if futures and not step.is_read_only:
                        continue
                    if any(
                        not running_step.is_read_only
                        for _future, (_state, running_step) in futures.items()
                    ):
                        continue
                    if step.is_read_only and len(futures) >= max(
                        1, self.context.config.llm.max_parallel
                    ):
                        continue
                    future = pool.submit(self._execute_step, state, step)
                    futures[future] = (state, step)
                    state.active_step_id = step.id
                    state.status = "running"
                    if not step.is_read_only:
                        break

                if not futures:
                    if all(state.status == "completed" for state in states):
                        break
                    pending = [
                        state.module.id
                        for state in states
                        if state.status not in {"completed", "failed"}
                    ]
                    raise HookError(
                        "Scheduler deadlock while running modules: "
                        + ", ".join(pending)
                    )

                done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    state, step = futures.pop(future)
                    state.active_step_id = None
                    try:
                        result = future.result()
                    except Exception as exc:
                        state.status = "failed"
                        state.error = str(exc)
                        raise

                    state.metadata.update(result.metadata)
                    state.step_index += 1
                    if result.metadata.get("skip_module"):
                        state.step_index = len(state.module.steps)
                        state.status = "completed"
                    elif state.next_step is None:
                        state.status = "completed"
                    else:
                        state.status = "pending"
                    statuses[state.module.id] = state.status

        return WorkflowRunResult(run_dir=self.artifacts.run_dir, modules=statuses)

    def _execute_step(self, state: ModuleRuntimeState, step: StepConfig) -> StepResult:
        if step.when_env and env_bool(step.when_env) is not True:
            payload = {"skipped": True, "reason": f"{step.when_env} not enabled"}
            self.artifacts.write_json(
                state, state.step_index, step.id, "result.json", payload
            )
            return StepResult()

        if step.type == "collect":
            if step.python:
                input_paths = self._resolve_plugin_inputs(state, step)
                result = validate_collector_result(
                    self._dispatch_plugin(state, step, input_paths)
                )
                return self._persist_plugin_collect(state, step, result)
            return self._run_collect(state, step)

        input_paths = [
            self.artifacts.resolve_input(state, reference) for reference in step.inputs
        ]
        stage_name = f"{state.module.id}.{step.id}"

        if step.type == "ask":
            prompt = resolve_prompt_text(self.context.repo_root, step)
            payload = self.ask_executor(
                self.context, step, prompt, input_paths, stage_name
            )
            artifact_name = step.output or "result.json"
            if isinstance(payload, (dict, list)) or artifact_name.endswith(".json"):
                self.artifacts.write_json(
                    state, state.step_index, step.id, artifact_name, payload
                )
            else:
                self.artifacts.write_text(
                    state, state.step_index, step.id, artifact_name, str(payload)
                )
            return StepResult()

        if step.type == "apply":
            prompt = resolve_prompt_text(self.context.repo_root, step)
            payload = self.apply_executor(
                self.context, state, step, prompt, input_paths, stage_name
            )
            self.artifacts.write_json(
                state, state.step_index, step.id, "result.json", payload
            )
            return StepResult()

        if step.type == "exec":
            if step.python:
                plugin_inputs = dict(zip(step.inputs, input_paths))
                payload = validate_exec_result(
                    self._dispatch_plugin(state, step, plugin_inputs)
                )
                self._persist_plugin_result(state, step, payload)
                return StepResult()
            if step.command:
                execute_step_command(
                    self.context,
                    state,
                    step,
                    dict(zip(step.inputs, input_paths)),
                    artifacts=self.artifacts,
                )
                return StepResult()
            handler = self.exec_handlers.get(step.executor or "")
            if handler is None:
                raise HookError(f"Unknown exec handler: {step.executor}")
            payload = handler(self.context, state, step, input_paths)
            self.artifacts.write_json(
                state, state.step_index, step.id, "result.json", payload
            )
            return StepResult()

        if step.type == "assert":
            if step.python:
                plugin_inputs = dict(zip(step.inputs, input_paths))
                payload = validate_assert_result(
                    self._dispatch_plugin(state, step, plugin_inputs)
                )
                self._persist_plugin_result(state, step, payload)
                if not payload["ok"]:
                    raise HookError(payload.get("message", "assertion failed"))
                return StepResult()
            if step.command:
                execute_step_command(
                    self.context,
                    state,
                    step,
                    dict(zip(step.inputs, input_paths)),
                    artifacts=self.artifacts,
                )
                return StepResult()
            handler = self.assertion_handlers.get(step.assertion or "")
            if handler is None:
                raise HookError(f"Unknown assertion handler: {step.assertion}")
            payload = handler(self.context, step, input_paths)
            self.artifacts.write_json(
                state, state.step_index, step.id, "result.json", payload
            )
            if not bool(payload.get("ok", False)):
                raise HookError(str(payload.get("message", "assertion failed")))
            return StepResult()

        raise HookError(f"Unsupported step type: {step.type}")

    def _resolve_plugin_inputs(
        self, state: ModuleRuntimeState, step: StepConfig
    ) -> dict[str, pathlib.Path]:
        """Resolve declared inputs in declaration order for a callback."""

        return {
            reference: self.artifacts.resolve_input(state, reference)
            for reference in step.inputs
        }

    def _dispatch_plugin(
        self,
        state: ModuleRuntimeState,
        step: StepConfig,
        input_paths: dict[str, pathlib.Path],
    ) -> Any:
        dispatcher = self._plugin_dispatcher
        if dispatcher is None:  # pragma: no cover - only direct private calls
            dispatcher = PluginDispatcher()
        return dispatcher.dispatch(self.context, state, step, input_paths)

    def _persist_plugin_collect(
        self, state: ModuleRuntimeState, step: StepConfig, result: CollectorResult
    ) -> StepResult:
        # Serialize and enforce both limits before the first write/register.
        serialized = self.artifacts.serialize_plugin_artifacts(result.artifacts)
        for artifact_name, content in serialized.items():
            self.artifacts.write_bytes(
                state, state.step_index, step.id, artifact_name, content
            )
        metadata = dict(result.metadata)
        if result.skip_module:
            metadata["skip_module"] = True
            metadata["skip_reason"] = result.skip_reason
        return StepResult(metadata=metadata)

    def _persist_plugin_result(
        self, state: ModuleRuntimeState, step: StepConfig, payload: dict[str, Any]
    ) -> pathlib.Path:
        # Use the same bounded serializer as collector artifacts.  Validation
        # happens first, and serialization happens before the result is written
        # or registered.
        serialized = self.artifacts.serialize_plugin_artifacts({"result.json": payload})
        return self.artifacts.write_bytes(
            state, state.step_index, step.id, "result.json", serialized["result.json"]
        )

    def _run_collect(self, state: ModuleRuntimeState, step: StepConfig) -> StepResult:
        handler = self.collectors.get(step.collector or "")
        if handler is None:
            raise HookError(f"Unknown collector: {step.collector}")
        result = handler(self.context, state)
        for artifact_name, payload in result.artifacts.items():
            if isinstance(payload, (dict, list)) or artifact_name.endswith(".json"):
                self.artifacts.write_json(
                    state, state.step_index, step.id, artifact_name, payload
                )
            else:
                self.artifacts.write_text(
                    state, state.step_index, step.id, artifact_name, str(payload)
                )
        metadata = dict(result.metadata)
        if result.skip_module:
            metadata["skip_module"] = True
            metadata["skip_reason"] = result.skip_reason
        return StepResult(metadata=metadata)
