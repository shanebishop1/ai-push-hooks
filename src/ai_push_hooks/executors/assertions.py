from __future__ import annotations

import json
import pathlib
from typing import Any

from ..types import RuntimeContext, StepConfig


def docs_apply_requires_manual_commit(
    _context: RuntimeContext,
    _step: StepConfig,
    inputs: list[pathlib.Path],
) -> dict[str, Any]:
    payload = json.loads(inputs[0].read_text(encoding="utf-8"))
    changed_files = payload.get("changed_files", [])
    if changed_files:
        return {
            "ok": False,
            "message": "Documentation updates were applied; review and commit them before pushing again.",
            "changed_files": changed_files,
        }
    return {"ok": True, "message": "", "changed_files": changed_files}


def beads_alignment_clean(
    _context: RuntimeContext,
    _step: StepConfig,
    inputs: list[pathlib.Path],
) -> dict[str, Any]:
    payload = json.loads(inputs[0].read_text(encoding="utf-8"))
    unresolved = bool(payload.get("unresolved", False))
    if unresolved:
        return {
            "ok": False,
            "message": "Beads alignment requires manual action before push.",
        }
    return {"ok": True, "message": ""}


ASSERTION_HANDLERS = {
    "docs_apply_requires_manual_commit": docs_apply_requires_manual_commit,
    "beads_alignment_clean": beads_alignment_clean,
}
