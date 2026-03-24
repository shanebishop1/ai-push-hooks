from __future__ import annotations

import argparse
import pathlib
import sys

from .hook import run_hook
from .prompts_builtin import MINIMAL_DOCS_TEMPLATE
from .types import HookError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI push hooks workflow runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    hook_parser = subparsers.add_parser("hook", help="Run the hook workflow")
    hook_parser.add_argument("remote_name", nargs="?", default="")
    hook_parser.add_argument("remote_url", nargs="?", default="")

    init_parser = subparsers.add_parser("init", help="Write a starter config")
    init_parser.add_argument("--template", default="minimal-docs")
    init_parser.add_argument("--force", action="store_true")
    return parser


def init_config(template: str, force: bool, cwd: pathlib.Path | None = None) -> int:
    if template != "minimal-docs":
        raise HookError("Only `minimal-docs` is supported")
    target_dir = cwd or pathlib.Path.cwd()
    config_path = target_dir / "ai-push-hooks.toml"
    legacy_path = target_dir / ".ai-push-hooks.toml"
    existing_path = config_path if config_path.exists() else legacy_path if legacy_path.exists() else None
    if existing_path is not None and not force:
        raise HookError(f"Refusing to overwrite existing config without --force: {existing_path}")
    config_path.write_text(MINIMAL_DOCS_TEMPLATE, encoding="utf-8")
    sys.stdout.write(str(config_path) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "hook":
            return run_hook(args.remote_name, args.remote_url)
        if args.command == "init":
            return init_config(args.template, args.force)
        raise HookError(f"Unknown command: {args.command}")
    except HookError as exc:
        sys.stderr.write(f"[ai-push-hooks] {exc}\n")
        return 1
