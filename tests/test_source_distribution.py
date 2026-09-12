from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tarfile


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT_SECONDS = 120
REQUIRED_ROOT_FILES = {
    "MANIFEST.in",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "pyproject.toml",
    "constraints-ci.txt",
    "ai-push-hooks.toml",
    "release-channel.toml",
    "package.json",
    "bin/ai-push-hooks.js",
    "docs/configuration.md",
    "vendor/README.md",
    "vendor/requirements.txt",
    "vendor/tomli-2.4.0-py3-none-any.whl",
}
FORBIDDEN_PATHS = {
    "docs/architecture.html",
}
FORBIDDEN_PREFIXES = (
    "docs/reports/",
    "prds/",
    ".dist/",
    "build/",
    "dist/",
    ".pytest_cache/",
    ".ruff_cache/",
)
SOURCE_DIRECTORIES = ("src", "tests", "scripts")


def _ignore_local_artifacts(_directory: str, names: list[str]) -> set[str]:
    ignored = {
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".dist",
        ".beads",
        "build",
        "dist",
    }
    return {
        name
        for name in names
        if name in ignored
        or name.endswith(".egg-info")
        or name == "__pycache__"
        or pathlib.Path(_directory, name).is_symlink()
    }


def _copy_fresh_source(destination: pathlib.Path) -> None:
    destination.mkdir()
    for relative in REQUIRED_ROOT_FILES:
        source = PROJECT_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for relative in SOURCE_DIRECTORIES:
        shutil.copytree(
            PROJECT_ROOT / relative,
            destination / relative,
            symlinks=True,
            ignore=_ignore_local_artifacts,
        )


def _archive_files(archive: pathlib.Path) -> set[str]:
    with tarfile.open(archive, mode="r:gz") as handle:
        files = [member.name for member in handle.getmembers() if member.isfile()]
    roots = {pathlib.PurePosixPath(name).parts[0] for name in files}
    assert len(roots) == 1
    prefix = next(iter(roots)) + "/"
    return {name.removeprefix(prefix) for name in files}


def _source_files(source: pathlib.Path, directory: str, suffixes: set[str]) -> set[str]:
    root = source / directory
    return {
        path.relative_to(source).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix in suffixes
    }


def test_fresh_sdist_contains_shipped_sources_and_collects_from_archive(
    tmp_path: pathlib.Path,
) -> None:
    """Build with the minimum supported backend, then validate an unpacked sdist."""
    source = tmp_path / "fresh-source"
    _copy_fresh_source(source)
    output = source / "fresh-dist"
    output.mkdir()
    backend_constraints = tmp_path / "backend-constraints.txt"
    backend_constraints.write_text(
        "setuptools==77.0.1\nwheel==0.45.1\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PIP_CONSTRAINT"] = str(backend_constraints)
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(output)],
        cwd=source,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )

    archives = sorted(output.glob("*.tar.gz"))
    assert len(archives) == 1
    files = _archive_files(archives[0])
    required = REQUIRED_ROOT_FILES | _source_files(source, "tests", {".py", ".mjs"})
    required |= _source_files(source, "scripts", {".py", ".sh"})
    required |= _source_files(source, "src", {".py"})
    assert required <= files
    assert not any(
        path in FORBIDDEN_PATHS
        or path.startswith(FORBIDDEN_PREFIXES)
        or "__pycache__" in path
        or path.endswith((".pyc", ".pyo"))
        or path.endswith(".egg-info")
        for path in files
    )

    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(archives[0], mode="r:gz") as handle:
        handle.extractall(unpacked, filter="data")
    archive_root = next(unpacked.iterdir())
    wheel_output = tmp_path / "wheel-dist"
    wheel_output.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheel_output),
            str(archive_root),
        ],
        cwd=unpacked,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert len(list(wheel_output.glob("*.whl"))) == 1
    collect_environment = os.environ.copy()
    collect_environment.pop("PYTHONPATH", None)
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_runner_examples.py"],
        cwd=archive_root,
        env=collect_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests",
            "--ignore=tests/test_source_distribution.py",
        ],
        cwd=archive_root,
        env=collect_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
