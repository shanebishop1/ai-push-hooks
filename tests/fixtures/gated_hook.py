from __future__ import annotations

import os
import pathlib

from ai_push_hooks.plugins import CollectorResult


marker = os.environ.get("AI_PUSH_HOOKS_PLUGIN_IMPORT_MARKER")
if marker:
    pathlib.Path(marker).write_text("gated-imported\n", encoding="utf-8")


def should_not_run(context) -> CollectorResult:
    raise AssertionError("a gated callback was imported or invoked")
