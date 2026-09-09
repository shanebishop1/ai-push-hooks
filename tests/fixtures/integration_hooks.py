"""Repository-local callbacks used by the source and installed-hook tests."""

from __future__ import annotations

import json
import os
import pathlib
import time

try:
    import tomllib as tomli
except ModuleNotFoundError:  # Python 3.10's interpreter-provided package dependency.
    import tomli

import ai_push_hooks
from ai_push_hooks.plugins import CollectorResult


def _append_marker(value: str) -> None:
    marker = os.environ.get("AI_PUSH_HOOKS_PLUGIN_IMPORT_MARKER")
    if marker:
        with pathlib.Path(marker).open("a", encoding="utf-8") as handle:
            handle.write(value + "\n")


_append_marker("imported")


def collect_context(context) -> CollectorResult:
    barrier_name = os.environ.get("AI_PUSH_HOOKS_COLLECT_BARRIER")
    if barrier_name:
        barrier = pathlib.Path(barrier_name)
        barrier.mkdir(parents=True, exist_ok=True)
        (barrier / f"{context.module_id}.ready").touch()
        expected = tuple(
            item for item in os.environ.get("AI_PUSH_HOOKS_COLLECT_MODULES", "").split(",") if item
        )
        while expected and not all((barrier / f"{item}.ready").exists() for item in expected):
            time.sleep(0.005)

    parsed = tomli.loads("source = 'installed-interpreter'")
    return CollectorResult(
        artifacts={
            "context.json": {
                "module": context.module_id,
                "step": context.step_id,
                "dependency": parsed["source"],
                "package_origin": str(pathlib.Path(ai_push_hooks.__file__).resolve()),
            }
        },
        metadata={"callback": "collect_context"},
    )


def exec_context(context) -> dict[str, object]:
    payload = json.loads(context.inputs["context.json"].read_text(encoding="utf-8"))
    return {"callback": "exec_context", "module": context.module_id, "input": payload["module"]}


def assert_context(context) -> dict[str, object]:
    payload = json.loads(context.inputs["command/result.json"].read_text(encoding="utf-8"))
    return {
        "ok": payload.get("returncode") == 0,
        "message": "command did not succeed" if payload.get("returncode") != 0 else "",
    }


def reject(context) -> dict[str, object]:
    return {"ok": False, "message": "local policy rejected"}


def should_not_run(context) -> CollectorResult:
    raise AssertionError("a gated callback was imported or invoked")
