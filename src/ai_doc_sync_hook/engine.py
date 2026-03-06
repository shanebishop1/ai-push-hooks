from __future__ import annotations

import pathlib
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Callable

from .artifacts import ArtifactStore
from .config import resolve_prompt_text
from .executors.apply import run_apply_step
from .executors.assertions import ASSERTION_HANDLERS
from .executors.exec import EXEC_HANDLERS, env_bool
from .executors.llm import run_llm_step
from .modules import COLLECTORS
from .types import CollectorResult, HookError, ModuleRuntimeState, RuntimeContext, StepConfig, StepResult, WorkflowRunResult

CollectorHandler = Callable[[RuntimeContext, ModuleRuntimeState], CollectorResult]
ExecHandler = Callable[[RuntimeContext, ModuleRuntimeState, StepConfig, list[pathlib.Path]], dict[str, Any]]
AssertionHandler = Callable[[RuntimeContext, StepConfig, list[pathlib.Path]], dict[str, Any]]


class WorkflowEngine:
    def __init__(
        self,
        context: RuntimeContext,
        artifacts: ArtifactStore,
        collectors: dict[str, CollectorHandler] | None = None,
        exec_handlers: dict[str, ExecHandler] | None = None,
        assertion_handlers: dict[str, AssertionHandler] | None = None,
        llm_executor: Callable[[RuntimeContext, StepConfig, str, list[pathlib.Path], str], Any] = run_llm_step,
        apply_executor: Callable[[RuntimeContext, ModuleRuntimeState, StepConfig, str, list[pathlib.Path], str], dict[str, object]] = run_apply_step,
    ) -> None:
        self.context = context
        self.artifacts = artifacts
        self.collectors = collectors or COLLECTORS
        self.exec_handlers = exec_handlers or EXEC_HANDLERS
        self.assertion_handlers = assertion_handlers or ASSERTION_HANDLERS
        self.llm_executor = llm_executor
        self.apply_executor = apply_executor

    def run(self) -> WorkflowRunResult:
        self.artifacts.prepare()
        states = [
            ModuleRuntimeState(module=self.context.config.modules[module_id])
            for module_id in self.context.config.workflow.modules
            if self.context.config.modules[module_id].enabled
        ]
        statuses: dict[str, str] = {state.module.id: "pending" for state in states}
        futures: dict[Future[StepResult], tuple[ModuleRuntimeState, StepConfig]] = {}

        with ThreadPoolExecutor(max_workers=max(1, self.context.config.llm.max_parallel)) as pool:
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
                    if any(not running_step.is_read_only for _future, (_state, running_step) in futures.items()):
                        continue
                    if not step.is_read_only and futures:
                        continue
                    if step.is_read_only and len(futures) >= max(1, self.context.config.llm.max_parallel):
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
                    pending = [state.module.id for state in states if state.status not in {"completed", "failed"}]
                    raise HookError("Scheduler deadlock while running modules: " + ", ".join(pending))

                done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    state, step = futures.pop(future)
                    state.active_step_id = None
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
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
            path = self.artifacts.write_json(state, state.step_index, step.id, "result.json", payload)
            return StepResult(status="skipped", artifacts={"result.json": path}, metadata={})

        if step.type == "collect":
            return self._run_collect(state, step)

        input_paths = [self.artifacts.resolve_input(state, reference) for reference in step.inputs]
        stage_name = f"{state.module.id}.{step.id}"

        if step.type == "llm":
            prompt = resolve_prompt_text(self.context.repo_root, step)
            payload = self.llm_executor(self.context, step, prompt, input_paths, stage_name)
            artifact_name = step.output or "result.json"
            if isinstance(payload, (dict, list)) or artifact_name.endswith(".json"):
                path = self.artifacts.write_json(state, state.step_index, step.id, artifact_name, payload)
            else:
                path = self.artifacts.write_text(state, state.step_index, step.id, artifact_name, str(payload))
            return StepResult(artifacts={artifact_name: path})

        if step.type == "apply":
            prompt = resolve_prompt_text(self.context.repo_root, step)
            payload = self.apply_executor(self.context, state, step, prompt, input_paths, stage_name)
            path = self.artifacts.write_json(state, state.step_index, step.id, "result.json", payload)
            return StepResult(artifacts={"result.json": path})

        if step.type == "exec":
            handler = self.exec_handlers.get(step.executor or "")
            if handler is None:
                raise HookError(f"Unknown exec handler: {step.executor}")
            payload = handler(self.context, state, step, input_paths)
            path = self.artifacts.write_json(state, state.step_index, step.id, "result.json", payload)
            return StepResult(artifacts={"result.json": path})

        if step.type == "assert":
            handler = self.assertion_handlers.get(step.assertion or "")
            if handler is None:
                raise HookError(f"Unknown assertion handler: {step.assertion}")
            payload = handler(self.context, step, input_paths)
            path = self.artifacts.write_json(state, state.step_index, step.id, "result.json", payload)
            if not bool(payload.get("ok", False)):
                raise HookError(str(payload.get("message", "assertion failed")))
            return StepResult(artifacts={"result.json": path})

        raise HookError(f"Unsupported step type: {step.type}")

    def _run_collect(self, state: ModuleRuntimeState, step: StepConfig) -> StepResult:
        handler = self.collectors.get(step.collector or "")
        if handler is None:
            raise HookError(f"Unknown collector: {step.collector}")
        result = handler(self.context, state)
        artifacts: dict[str, pathlib.Path] = {}
        for artifact_name, payload in result.artifacts.items():
            if isinstance(payload, (dict, list)) or artifact_name.endswith(".json"):
                path = self.artifacts.write_json(state, state.step_index, step.id, artifact_name, payload)
            else:
                path = self.artifacts.write_text(state, state.step_index, step.id, artifact_name, str(payload))
            artifacts[artifact_name] = path
        metadata = dict(result.metadata)
        if result.skip_module:
            metadata["skip_module"] = True
            metadata["skip_reason"] = result.skip_reason
        return StepResult(artifacts=artifacts, metadata=metadata)
