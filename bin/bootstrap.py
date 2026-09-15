"""Isolated entry point for the npm launcher.

The npm package is not a Python installation.  This file adds only the Python
sources and the vendored Python 3.10 TOML dependency shipped beside it, while
leaving the selected interpreter's regular site-packages available for user
callbacks.
"""

from __future__ import annotations

import os
import pathlib
import runpy
import sys


_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SHIPPED_SOURCE = _PACKAGE_ROOT / "src"
_SHIPPED_TOMLI = _PACKAGE_ROOT / "vendor" / "tomli-2.4.0-py3-none-any.whl"
_CAPABILITY_ENVIRONMENT = "AI_PUSH_HOOKS_INTERNAL_CAPABILITY"


def _without_working_directory(entries: list[str]) -> list[str]:
    working_directory = os.path.realpath(os.getcwd())
    safe_entries: list[str] = []
    for entry in entries:
        if not entry or not os.path.isabs(entry):
            continue
        try:
            if os.path.realpath(entry) == working_directory:
                continue
        except OSError:
            continue
        safe_entries.append(entry)
    return safe_entries


def _configure_imports() -> None:
    """Retain interpreter site-packages and add only shipped import paths."""
    sys.path[:] = _without_working_directory(sys.path)
    sys.path.insert(0, str(_SHIPPED_TOMLI))
    sys.path.insert(0, str(_SHIPPED_SOURCE))


def _has_supported_toml() -> bool:
    if sys.version_info < (3, 10):
        return False
    try:
        if sys.version_info >= (3, 11):
            import tomllib  # noqa: F401
        else:
            import tomli  # noqa: F401
    except ImportError:
        return False
    return True


def main() -> int:
    _configure_imports()
    if not _has_supported_toml():
        return 1
    if os.environ.get(_CAPABILITY_ENVIRONMENT) == "1":
        return 0
    runpy.run_module("ai_push_hooks", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
