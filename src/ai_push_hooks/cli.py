from __future__ import annotations

import argparse
import os
import pathlib
import stat
import sys

from .hook import run_hook
from .paths import path_is_link_or_reparse, write_text_no_follow
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
    if force:
        try:
            metadata = config_path.lstat()
        except FileNotFoundError:
            metadata = None
        except OSError as exc:
            raise HookError(f"Could not inspect config path {config_path}: {exc}") from exc
        if metadata is not None:
            if path_is_link_or_reparse(config_path):
                raise HookError(f"Refusing to replace symlink or reparse point: {config_path}")
            if not stat.S_ISREG(metadata.st_mode):
                raise HookError(f"Refusing to replace non-regular config path: {config_path}")
        try:
            write_text_no_follow(config_path, MINIMAL_DOCS_TEMPLATE)
        except HookError:
            raise
        except OSError as exc:
            raise HookError(f"Could not write config file {config_path}: {exc}") from exc
    else:
        try:
            metadata = config_path.lstat()
        except FileNotFoundError:
            metadata = None
        except OSError as exc:
            raise HookError(f"Could not inspect config path {config_path}: {exc}") from exc
        if metadata is not None:
            if path_is_link_or_reparse(config_path):
                raise HookError(f"Refusing to overwrite symlink or reparse point: {config_path}")
            if not stat.S_ISREG(metadata.st_mode):
                raise HookError(f"Refusing to overwrite non-regular config path: {config_path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(config_path, flags, 0o600)
        except FileExistsError as exc:
            raise HookError(
                f"Refusing to overwrite existing config without --force: {config_path}"
            ) from exc
        except OSError as exc:
            raise HookError(f"Could not create config file {config_path}: {exc}") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(MINIMAL_DOCS_TEMPLATE)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
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
