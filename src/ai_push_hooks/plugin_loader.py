"""Internal loader and dispatcher for repository-local Python workflow hooks.

This module intentionally does not provide discovery, registration, or a plugin
SDK.  A :class:`PluginLoader` belongs to one workflow run.  It reads each
referenced source file once and keeps the resulting module snapshot in a
canonical, per-run cache.  :class:`PluginDispatcher` is the small adapter the
workflow engine can use after its normal gates and artifact resolution have
completed.
"""

from __future__ import annotations  # noqa: I001

import errno
import hashlib
import inspect
import os
import pathlib
import re
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from types import ModuleType
from typing import Any

from .paths import (
    path_has_symlink,
    relative_path_parts,
    resolve_contained_path,
)
from .plugins import PluginContext, PushContext
from .types import HookError, ModuleRuntimeState, RuntimeContext, StepConfig


_CALLABLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SOURCE_ENCODING = "utf-8"
PLUGIN_SOURCE_MAX_BYTES = 1 * 1024 * 1024
_SOURCE_READ_CHUNK_BYTES = 64 * 1024
_OS_OPEN = os.open
_DESCRIPTOR_RELATIVE_SUPPORTED = bool(
    getattr(os, "O_DIRECTORY", 0)
    and getattr(os, "O_NOFOLLOW", 0)
    and _OS_OPEN in getattr(os, "supports_dir_fd", ())
)


def _descriptor_relative_supported() -> bool:
    return _DESCRIPTOR_RELATIVE_SUPPORTED


def _read_source_limited(descriptor: int) -> bytes:
    """Read plugin source from a checked descriptor within the source budget."""

    oversize_message = (
        "Python plugin source exceeds maximum size of "
        f"{PLUGIN_SOURCE_MAX_BYTES} bytes"
    )
    if os.fstat(descriptor).st_size > PLUGIN_SOURCE_MAX_BYTES:
        raise HookError(oversize_message)
    content = bytearray()
    while True:
        read_limit = min(
            _SOURCE_READ_CHUNK_BYTES,
            PLUGIN_SOURCE_MAX_BYTES - len(content) + 1,
        )
        try:
            chunk = os.read(descriptor, max(1, read_limit))
        except OSError:
            raise HookError("Python plugin source could not be read") from None
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > PLUGIN_SOURCE_MAX_BYTES:
            raise HookError(oversize_message)


def _reference_parts(reference: str) -> tuple[str, str]:
    if not isinstance(reference, str) or reference.count(":") != 1:
        raise HookError(
            "Python plugin reference must be a repository-relative .py path followed by :callable"
        )
    path_value, callable_name = reference.split(":", 1)
    if not _CALLABLE_PATTERN.fullmatch(callable_name):
        raise HookError("Python plugin callable must be one top-level identifier")
    parts = relative_path_parts(path_value, "Python plugin path")
    if not parts[-1].endswith(".py"):
        raise HookError("Python plugin path must name a .py file")
    return "/".join(parts), callable_name


def _canonical_root(repo_root: pathlib.Path) -> pathlib.Path:
    try:
        root = pathlib.Path(repo_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HookError("Python plugin repository root is not accessible") from None
    if not root.is_dir():
        raise HookError("Python plugin repository root must be a directory")
    return root


def _open_source_descriptor_relative(
    root: pathlib.Path, parts: tuple[str, ...]
) -> tuple[pathlib.Path, bytes]:
    """Walk every component relative to an O_NOFOLLOW directory descriptor."""

    directory_fd = -1
    file_fd = -1
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        directory_fd = _OS_OPEN(root, directory_flags)
        for part in parts[:-1]:
            next_fd = _OS_OPEN(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = _OS_OPEN(parts[-1], file_flags, dir_fd=directory_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise HookError("Python plugin path must reference an ordinary regular file")
        return root.joinpath(*parts), _read_source_limited(file_fd)
    except HookError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise HookError("Python plugin path must not traverse a symlink or reparse point") from None
        if exc.errno == errno.ENOENT:
            raise HookError("Python plugin path must reference an existing regular file") from None
        raise HookError("Python plugin path could not be opened safely") from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _open_source_absolute(root: pathlib.Path, relative_path: str) -> tuple[pathlib.Path, bytes]:
    """Fallback for platforms without descriptor-relative open support."""

    lexical_path = root.joinpath(*relative_path.split("/"))
    if path_has_symlink(root, lexical_path):
        raise HookError("Python plugin path must not traverse a symlink or reparse point")
    callback_path = resolve_contained_path(root, relative_path, "Python plugin path")

    try:
        metadata = callback_path.lstat()
    except (FileNotFoundError, OSError):
        raise HookError("Python plugin path must reference an existing regular file") from None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    ):
        raise HookError("Python plugin path must not be a symlink or reparse point")
    if not stat.S_ISREG(metadata.st_mode):
        raise HookError("Python plugin path must reference an ordinary regular file")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(callback_path, flags)
    except OSError:
        raise HookError("Python plugin path could not be opened safely") from None
    try:
        descriptor_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_metadata.st_mode):
            raise HookError("Python plugin path must reference an ordinary regular file")
        return callback_path, _read_source_limited(descriptor)
    except HookError:
        raise
    except OSError:
        raise HookError("Python plugin source could not be read") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_source(root: pathlib.Path, relative_path: str) -> tuple[pathlib.Path, bytes]:
    """Open and read a contained ordinary file without following parents."""

    parts = tuple(relative_path.split("/"))
    if _descriptor_relative_supported():
        return _open_source_descriptor_relative(root, parts)
    return _open_source_absolute(root, relative_path)


def _failure(stage: str, relative_path: str, callable_name: str, detail: str) -> HookError:
    return HookError(
        f"Python {stage} plugin {relative_path}:{callable_name} {detail}"
    )


class PluginLoader:
    """Load explicit repository-local callbacks once for one workflow run."""

    def __init__(self) -> None:
        self._cache: dict[tuple[pathlib.Path, pathlib.Path], ModuleType] = {}
        self._lock = threading.RLock()
        self._source_locks: dict[tuple[pathlib.Path, pathlib.Path], threading.Lock] = {}
        self._module_number = 0

    def _source_lock(
        self, key: tuple[pathlib.Path, pathlib.Path]
    ) -> threading.Lock:
        with self._lock:
            lock = self._source_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._source_locks[key] = lock
            return lock

    def _load_module(
        self,
        root: pathlib.Path,
        relative_path: str,
        callable_name: str,
    ) -> ModuleType:
        try:
            callback_path, source = _open_source(root, relative_path)
        except HookError as exc:
            raise HookError(
                f"Python plugin {relative_path}:{callable_name} could not be loaded safely: "
                f"{str(exc).removeprefix('Python plugin ')}"
            ) from None
        # ``callback_path`` is canonical after containment checks and is equal
        # to the lexical cache key for a safe repository file.
        try:
            source_text = source.decode(_SOURCE_ENCODING)
            code = compile(source_text, str(callback_path), "exec")
        except UnicodeDecodeError:
            raise HookError(
                f"Python plugin {relative_path}:{callable_name} source is not valid UTF-8"
            ) from None
        except (SyntaxError, ValueError, TypeError):
            raise HookError(
                f"Python plugin {relative_path}:{callable_name} could not be compiled"
            ) from None

        with self._lock:
            self._module_number += 1
            module_number = self._module_number
        digest = hashlib.sha256(f"{root}\0{callback_path}".encode()).hexdigest()[:16]
        module_name = f"ai_push_hooks_plugin_{digest}_{module_number}"
        module = ModuleType(module_name)
        module.__file__ = str(callback_path)
        module.__package__ = ""
        try:
            exec(code, module.__dict__)  # noqa: S102
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise HookError(
                f"Python plugin {relative_path}:{callable_name} exited during import"
            ) from None
        except ModuleNotFoundError as exc:
            if exc.name:
                detail = f"is missing dependency {exc.name!r}"
            else:
                detail = "could not import a dependency"
            raise HookError(
                f"Python plugin {relative_path}:{callable_name} {detail}"
            ) from None
        except ImportError:
            raise HookError(
                f"Python plugin {relative_path}:{callable_name} could not import a dependency"
            ) from None
        except Exception:  # noqa: BLE001
            raise HookError(
                f"Python plugin {relative_path}:{callable_name} failed during import"
            ) from None
        return module

    def load(self, repo_root: pathlib.Path, reference: str) -> Callable[..., Any]:
        """Return the named top-level callable from a safe source snapshot.

        A per-source lock includes descriptor open, source read, compilation,
        and module execution.  Thus concurrent collectors cannot execute the
        same source twice, while different sources can import concurrently.
        The cache key includes both canonical repository root and canonical
        file path, so identical relative names in two repositories remain
        independent.
        """

        relative_path, callable_name = _reference_parts(reference)
        root = _canonical_root(repo_root)
        lexical_callback_path = root.joinpath(*relative_path.split("/"))
        key = (root, lexical_callback_path)
        with self._lock:
            module = self._cache.get(key)
        if module is None:
            with self._source_lock(key):
                with self._lock:
                    module = self._cache.get(key)
                if module is None:
                    module = self._load_module(root, relative_path, callable_name)
                    with self._lock:
                        self._cache[key] = module

        try:
            callback = getattr(module, callable_name)
        except AttributeError:
            raise _failure(
                "callback", relative_path, callable_name, "was not defined"
            ) from None
        if not callable(callback):
            raise _failure(
                "callback", relative_path, callable_name, "is not callable"
            ) from None
        if inspect.iscoroutinefunction(callback):
            raise _failure(
                "callback", relative_path, callable_name, "must be synchronous"
            ) from None
        return callback

    def invoke(
        self,
        repo_root: pathlib.Path,
        reference: str,
        context: PluginContext,
        *,
        stage: str = "workflow",
    ) -> Any:
        """Load lazily and invoke one callback with exactly one context argument."""

        relative_path, callable_name = _reference_parts(reference)
        try:
            callback = self.load(repo_root, reference)
        except HookError as exc:
            message = str(exc)
            if message.startswith("Python plugin "):
                message = message.replace(
                    "Python plugin ", f"Python {stage} plugin ", 1
                )
            elif message.startswith("Python callback plugin "):
                message = message.replace(
                    "Python callback plugin ", f"Python {stage} plugin ", 1
                )
            raise HookError(message) from None

        try:
            value = callback(context)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise _failure(stage, relative_path, callable_name, "exited") from None
        except Exception:  # noqa: BLE001
            raise _failure(stage, relative_path, callable_name, "raised an exception") from None

        if inspect.isawaitable(value):
            close = getattr(value, "close", None)
            if callable(close):
                close()
            raise _failure(stage, relative_path, callable_name, "must return synchronously")
        return value

    # Explicit aliases make the intended internal seam easy to integrate while
    # retaining one implementation and one cache.
    load_callback = load
    invoke_callback = invoke


def _ordered_inputs(
    step: StepConfig, input_paths: Mapping[str, pathlib.Path] | Sequence[pathlib.Path]
) -> dict[str, pathlib.Path]:
    if isinstance(input_paths, Mapping):
        declared = set(step.inputs)
        extra = [reference for reference in input_paths if reference not in declared]
        if extra:
            raise HookError(f"Undeclared Python plugin input: {extra[0]}")
        missing = [reference for reference in step.inputs if reference not in input_paths]
        if missing:
            raise HookError(f"Missing resolved Python plugin input: {missing[0]}")
        return {reference: input_paths[reference] for reference in step.inputs}
    if len(input_paths) != len(step.inputs):
        raise HookError("Resolved Python plugin inputs do not match declared inputs")
    return dict(zip(step.inputs, input_paths))


def build_plugin_context(
    runtime: RuntimeContext,
    state: ModuleRuntimeState,
    step: StepConfig,
    input_paths: Mapping[str, pathlib.Path] | Sequence[pathlib.Path],
) -> PluginContext:
    """Create the immutable callback snapshot from runtime state at invocation."""

    cache = runtime.cache
    branch_name = str(cache.get("branch_name", ""))
    checked_out_branch = str(cache.get("checked_out_branch", branch_name))
    ranges = cache.get("ranges", cache.get("branch_ranges", ()))
    changed_files = cache.get("changed_files", cache.get("branch_changed_files", ()))
    diff_text = str(cache.get("diff_text", cache.get("branch_diff_text", "")))
    push_updates = cache.get("push_updates", cache.get("pushed_branch_updates", ()))
    push = PushContext(
        branch_name=branch_name,
        checked_out_branch=checked_out_branch,
        base_branch=runtime.config.general.base_branch,
        ranges=tuple(ranges),
        changed_files=tuple(changed_files),
        diff_text=diff_text,
        push_updates=tuple(push_updates),
    )
    return PluginContext(
        repo_root=_canonical_root(runtime.repo_root),
        module_id=state.module.id,
        step_id=step.id,
        inputs=_ordered_inputs(step, input_paths),
        options=step.options,
        prior_module_metadata=state.metadata,
        push=push,
        logger=runtime.logger,
    )


class PluginDispatcher:
    """Dispatch configured Python steps without changing engine scheduling."""

    def __init__(self, loader: PluginLoader | None = None) -> None:
        self.loader = loader or PluginLoader()

    def dispatch(
        self,
        runtime: RuntimeContext,
        state: ModuleRuntimeState,
        step: StepConfig,
        input_paths: Mapping[str, pathlib.Path] | Sequence[pathlib.Path],
    ) -> Any:
        if not step.python:
            raise HookError(f"Python {step.type} step `{step.id}` has no callback reference")
        context = build_plugin_context(runtime, state, step, input_paths)
        return self.loader.invoke(
            runtime.repo_root,
            step.python,
            context,
            stage=step.type,
        )

    invoke = dispatch


__all__ = [
    "PluginDispatcher",
    "PluginLoader",
    "build_plugin_context",
]
