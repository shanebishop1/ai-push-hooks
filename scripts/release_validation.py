#!/usr/bin/env python3
"""Small, side-effect-aware helpers for the protected release workflow.

The network client in this file deliberately retries reads only.  Publication,
release creation, deployment creation, and status updates are single attempts;
an unknown result is a recovery case, never a reason to publish again.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable


DEFAULT_TIMEOUT = 10.0
DEFAULT_ATTEMPTS = 3
POST_PUBLICATION_ATTEMPTS = 10
POST_PUBLICATION_DEADLINE = 120.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PROJECT_NAME = "ai-push-hooks"
PYPI_URL = "https://pypi.org/pypi"
NPM_URL = "https://registry.npmjs.org"
GITHUB_API_URL = "https://api.github.com"
MANIFEST_NAME = "checksum-manifest.json"
DEPLOYMENT_ENVIRONMENT = "npm"
DEPLOYMENT_DESCRIPTION = "npm publish"
DEPLOYMENT_TASK = "npm-publish"


class ReleaseValidationError(RuntimeError):
    """An actionable release validation or bookkeeping failure."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class VersionInfo:
    python_version: str
    npm_version: str
    tag: str
    channel: str
    npm_dist_tag: str
    github_prerelease: bool


@dataclass(frozen=True)
class ReleaseChannel:
    version: str
    channel: str
    npm_dist_tag: str
    github_prerelease: bool
    python_classifier: str
    pypi_channel: str


@dataclass(frozen=True)
class Artifact:
    name: str
    path: str
    kind: str
    size: int
    sha256: str
    sha512_integrity: str


def _url_origin(url: str) -> tuple[str, str, int | None]:
    """Return the origin used when deciding whether credentials may follow."""

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ReleaseValidationError("request URL is malformed") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ReleaseValidationError("request URL must not contain embedded credentials")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ReleaseValidationError("request URL must use HTTP or HTTPS")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReleaseValidationError("request URL has an invalid port") from exc
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _same_origin(left: str, right: str) -> bool:
    left_origin = _url_origin(left)
    right_origin = _url_origin(right)
    left_port = left_origin[2] or (443 if left_origin[0] == "https" else 80)
    right_port = right_origin[2] or (443 if right_origin[0] == "https" else 80)
    return left_origin[:2] == right_origin[:2] and left_port == right_port


def _strip_redirect_credentials(request: urllib.request.Request) -> None:
    for header in ("Authorization", "Proxy-Authorization", "Cookie", "Cookie2"):
        for collection in (request.headers, request.unredirected_hdrs):
            for key in list(collection):
                if key.lower() == header.lower():
                    collection.pop(key, None)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow read redirects without forwarding credentials to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _same_origin(req.full_url, newurl):
            old_scheme = _url_origin(req.full_url)[0]
            new_scheme = _url_origin(newurl)[0]
            if old_scheme == "https" and new_scheme != "https":
                raise ReleaseValidationError("refusing insecure redirect downgrade")
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is not None and not _same_origin(req.full_url, newurl):
            _strip_redirect_credentials(new_request)
        return new_request


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never replay a non-idempotent request at a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_read_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(_SafeRedirectHandler()).open(request, timeout=timeout)


def _default_write_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def _validate_github_api_url(url: str, *, repo: str | None = None) -> None:
    """Require an HTTPS GitHub API URL rooted at the requested repository."""

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ReleaseValidationError("GitHub API URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ReleaseValidationError("GitHub API URL must use https://api.github.com")
    try:
        if parsed.port not in (None, 443):
            raise ReleaseValidationError("GitHub API URL has an invalid port")
    except ValueError as exc:
        raise ReleaseValidationError("GitHub API URL has an invalid port") from exc
    parts = parsed.path.split("/")
    if len(parts) < 4 or parts[1] != "repos" or not parts[2] or not parts[3]:
        raise ReleaseValidationError("GitHub API URL is not a repository endpoint")
    if any(part in {".", ".."} for part in parts[1:]):
        raise ReleaseValidationError("GitHub API URL contains an unsafe path")
    if repo is not None and "/".join(parts[2:4]) != repo:
        raise ReleaseValidationError("GitHub API URL repository does not match the request")


def _validate_github_asset_url(
    url: str,
    *,
    repo: str | None = None,
    allow_actions_artifact: bool = False,
) -> None:
    """Validate an authenticated GitHub asset API URL before sending a token."""

    _validate_github_api_url(url, repo=repo)
    path = urllib.parse.urlsplit(url).path.split("/")
    is_release_asset = (
        len(path) == 7
        and path[4] == "releases"
        and path[5] == "assets"
        and path[6].isdecimal()
        and int(path[6]) > 0
    )
    is_actions_asset = (
        len(path) == 8
        and path[4] == "actions"
        and path[5] == "artifacts"
        and path[6].isdecimal()
        and int(path[6]) > 0
        and path[7] == "zip"
    )
    if not is_release_asset and not (allow_actions_artifact and is_actions_asset):
        raise ReleaseValidationError("GitHub asset URL is not an approved API asset endpoint")


def response_class(status: int) -> str:
    """Classify a response without treating outages as absence."""

    if status == 404:
        return "not_found"
    if status in (401, 403):
        return "auth"
    if status == 429 or 500 <= status <= 599:
        return "transient"
    if 200 <= status <= 299:
        return "ok"
    return "error"


def _bounded_read(response: Any, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ReleaseValidationError(
            f"response exceeded bounded read limit ({limit} bytes)"
        )
    return body


def _retry_delay(headers: dict[str, str], attempt: int) -> float:
    retry_after = headers.get("Retry-After", "")
    try:
        requested = float(retry_after)
    except ValueError:
        requested = 0.0
    return min(max(requested, 0.0), 2.0, 0.25 * (2**attempt))


def _request_read(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> HttpResult:
    """Perform a bounded, retryable GET-like request.

    HTTP 404 is returned to the caller as an explicit result.  Auth failures,
    malformed responses, and exhausted transient failures raise with their
    category so a caller cannot accidentally interpret them as absence.
    """

    if attempts < 1 or timeout <= 0:
        raise ValueError("attempts must be positive and timeout must be positive")
    _url_origin(url)
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    open_request = opener or _default_read_opener
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with open_request(request, timeout=timeout) as response:
                status = int(response.status)
                response_headers = {key: value for key, value in response.headers.items()}
                body = _bounded_read(response, max_bytes)
            category = response_class(status)
            if category == "ok" or category == "not_found":
                return HttpResult(status, response_headers, body)
            if category == "auth":
                raise ReleaseValidationError(
                    f"registry/API read rejected credentials (HTTP {status})"
                )
            if category != "transient":
                raise ReleaseValidationError(f"registry/API read failed (HTTP {status})")
            last_error = ReleaseValidationError(
                f"transient registry/API read failure (HTTP {status})"
            )
            if attempt + 1 < attempts:
                sleeper(_retry_delay(response_headers, attempt))
        except urllib.error.HTTPError as exc:
            category = response_class(exc.code)
            response_headers = {key: value for key, value in exc.headers.items()}
            if category == "not_found":
                return HttpResult(exc.code, response_headers, b"")
            if category == "auth":
                raise ReleaseValidationError(
                    f"registry/API read rejected credentials (HTTP {exc.code})"
                ) from exc
            if category != "transient":
                raise ReleaseValidationError(
                    f"registry/API read failed (HTTP {exc.code})"
                ) from exc
            last_error = ReleaseValidationError(
                f"transient registry/API read failure (HTTP {exc.code})"
            )
            if attempt + 1 < attempts:
                sleeper(_retry_delay(response_headers, attempt))
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            last_error = ReleaseValidationError("registry/API read timed out or failed")
            if attempt + 1 < attempts:
                sleeper(0.25 * (2**attempt))
            else:
                raise last_error from exc
    assert last_error is not None
    raise last_error


def _request_write(
    url: str,
    *,
    data: bytes,
    headers: dict[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable[..., Any] | None = None,
) -> HttpResult:
    """Perform exactly one bounded non-idempotent request."""

    _url_origin(url)
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    open_request = opener or _default_write_opener
    try:
        with open_request(request, timeout=timeout) as response:
            body = _bounded_read(response)
            result = HttpResult(
                int(response.status),
                {key: value for key, value in response.headers.items()},
                body,
            )
    except urllib.error.HTTPError as exc:
        category = response_class(exc.code)
        if category == "auth":
            raise ReleaseValidationError(
                f"non-idempotent write rejected credentials (HTTP {exc.code})"
            ) from exc
        raise ReleaseValidationError(
            f"non-idempotent write failed (HTTP {exc.code}); do not retry blindly"
        ) from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise ReleaseValidationError(
            "non-idempotent write timed out or failed; result is unknown, recover manually; "
            "do not retry blindly"
        ) from exc
    if not 200 <= result.status <= 299:
        raise ReleaseValidationError(
            f"non-idempotent write failed (HTTP {result.status}); do not retry blindly"
        )
    return result


def _json_result(result: HttpResult, context: str) -> dict[str, Any]:
    try:
        value = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"{context} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseValidationError(f"{context} returned a non-object JSON value")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"cannot read JSON file {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseValidationError(f"JSON file {path} must contain an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


_PEP_PRERELEASE = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?P<kind>a|b|rc)(?P<number>\d+)$")
_PEP_STABLE = re.compile(r"^\d+\.\d+\.\d+$")
_CHANNEL_DIST_TAGS = {"stable": "latest", "alpha": "alpha", "beta": "beta", "rc": "next"}
_CHANNEL_CLASSIFIERS = {
    "stable": "Development Status :: 5 - Production/Stable",
    "alpha": "Development Status :: 3 - Alpha",
    "beta": "Development Status :: 4 - Beta",
    "rc": "Development Status :: 4 - Beta",
}


def load_release_channel(path: Path) -> ReleaseChannel:
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib

        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        values = raw["release"]
        channel = ReleaseChannel(
            version=values["version"],
            channel=values["channel"],
            npm_dist_tag=values["npm_dist_tag"],
            github_prerelease=values["github_prerelease"],
            python_classifier=values["python_classifier"],
            pypi_channel=values["pypi_channel"],
        )
    except (KeyError, OSError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ReleaseValidationError(f"invalid release channel metadata: {path}") from exc
    if values.get("version") != channel.version or not isinstance(channel.version, str):
        raise ReleaseValidationError("release channel version must be a string")
    if channel.channel not in _CHANNEL_DIST_TAGS:
        raise ReleaseValidationError(f"unsupported release channel: {channel.channel}")
    if not isinstance(channel.npm_dist_tag, str) or not isinstance(channel.github_prerelease, bool):
        raise ReleaseValidationError("release channel registry settings have invalid types")
    if channel.npm_dist_tag != _CHANNEL_DIST_TAGS[channel.channel]:
        raise ReleaseValidationError("release channel npm dist-tag is inconsistent")
    if channel.github_prerelease != (channel.channel != "stable"):
        raise ReleaseValidationError("release channel GitHub prerelease setting is inconsistent")
    if channel.python_classifier != _CHANNEL_CLASSIFIERS[channel.channel]:
        raise ReleaseValidationError("release channel Python classifier is inconsistent")
    if channel.pypi_channel != "none":
        raise ReleaseValidationError("PyPI does not provide package channels; pypi_channel must be none")
    return channel


def map_versions(
    python_version: str,
    npm_version: str,
    tag: str,
    release_channel: ReleaseChannel | None = None,
) -> VersionInfo:
    """Apply the release policy shared by Python, npm, and Git tags.

    Stable versions are identical in all three surfaces.  Python ``aN``,
    ``bN``, and ``rcN`` map to npm ``-alpha.N``, ``-beta.N``, and ``-rc.N``;
    the canonical tag is ``v`` plus that npm spelling.  Prereleases use the
    matching npm dist-tag (``next`` for release candidates).  A checked-in
    channel metadata file may explicitly select a prerelease channel for a
    stable-looking version, but cannot rewrite an explicit PEP 440 prerelease.
    """

    if tag.startswith("v"):
        tag_version = tag[1:]
    else:
        raise ReleaseValidationError(f"tag must start with v: {tag}")
    prerelease = _PEP_PRERELEASE.fullmatch(python_version)
    if _PEP_STABLE.fullmatch(python_version):
        expected_npm = python_version
        derived_channel = "stable"
    elif prerelease:
        suffix = {"a": "alpha", "b": "beta", "rc": "rc"}[prerelease["kind"]]
        expected_npm = f"{prerelease['base']}-{suffix}.{prerelease['number']}"
        derived_channel = suffix
    else:
        raise ReleaseValidationError(
            "unsupported version policy; use X.Y.Z or X.Y.ZaN/bN/rcN"
        )
    if npm_version != expected_npm or tag_version != expected_npm:
        raise ReleaseValidationError(
            "version mismatch: expected Python/npm/tag mapping "
            f"{python_version}/{expected_npm}/v{expected_npm}, got "
            f"{python_version}/{npm_version}/{tag}"
        )
    channel = derived_channel
    if release_channel is not None:
        if release_channel.version != python_version:
            raise ReleaseValidationError(
                "release channel metadata version does not match package version"
            )
        if prerelease and release_channel.channel != derived_channel:
            raise ReleaseValidationError(
                "explicit PEP 440 prerelease cannot use a different release channel"
            )
        channel = release_channel.channel
        if release_channel.npm_dist_tag != _CHANNEL_DIST_TAGS[channel]:
            raise ReleaseValidationError("release channel npm dist-tag is inconsistent")
    dist_tag = _CHANNEL_DIST_TAGS[channel]
    github_prerelease = channel != "stable"
    if release_channel is not None and release_channel.github_prerelease != github_prerelease:
        raise ReleaseValidationError("release channel GitHub prerelease is inconsistent")
    return VersionInfo(python_version, npm_version, tag, channel, dist_tag, github_prerelease)


def _run_git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseValidationError(f"git validation failed: {' '.join(args)}") from exc


def validate_source(tag: str, commit: str) -> None:
    if not re.fullmatch(r"v[0-9A-Za-z][0-9A-Za-z.\-+]*", tag):
        raise ReleaseValidationError(f"invalid release tag: {tag}")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ReleaseValidationError(f"commit must be a full SHA-1: {commit}")
    tag_commit = _run_git("rev-parse", f"refs/tags/{tag}^{{commit}}")
    requested_commit = _run_git("rev-parse", f"{commit}^{{commit}}")
    if tag_commit != requested_commit:
        raise ReleaseValidationError(
            f"tag/commit binding mismatch: {tag} -> {tag_commit}, expected {requested_commit}"
        )


def _read_versions(pyproject: Path, package_json: Path) -> tuple[str, str, list[str]]:
    try:
        import tomllib

        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReleaseValidationError(f"cannot read {pyproject}") from exc
    try:
        python_version = project["project"]["version"]
        classifiers = project["project"].get("classifiers", [])
        package = json.loads(package_json.read_text(encoding="utf-8"))
        npm_version = package["version"]
    except (KeyError, OSError, UnicodeDecodeError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError("package version metadata is incomplete") from exc
    if not isinstance(python_version, str) or not isinstance(npm_version, str):
        raise ReleaseValidationError("package versions must be strings")
    if not isinstance(classifiers, list) or not all(isinstance(item, str) for item in classifiers):
        raise ReleaseValidationError("Python classifiers must be a list of strings")
    return python_version, npm_version, classifiers


def _artifact_kind(path: Path) -> str:
    if path.suffix == ".whl":
        return "wheel"
    if path.name.endswith(".tar.gz"):
        return "sdist"
    if path.suffix == ".tgz":
        return "npm"
    raise ReleaseValidationError(f"unexpected release artifact: {path.name}")


def _artifact(path: Path, root: Path, kind: str) -> Artifact:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReleaseValidationError(f"cannot read release artifact {path}") from exc
    sha256 = hashlib.sha256(data).hexdigest()
    sha512 = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    return Artifact(
        path.name,
        path.relative_to(root).as_posix(),
        kind,
        len(data),
        sha256,
        f"sha512-{sha512}",
    )


def create_manifest(root: Path, info: VersionInfo, commit: str) -> dict[str, Any]:
    if not root.is_dir():
        raise ReleaseValidationError(f"release root is not a directory: {root}")
    files = sorted(path for path in root.rglob("*") if path.is_file())
    artifacts = []
    for path in files:
        try:
            kind = _artifact_kind(path)
        except ReleaseValidationError:
            continue
        artifacts.append(_artifact(path, root, kind))
    by_kind = {artifact.kind: artifact for artifact in artifacts}
    if set(by_kind) != {"wheel", "sdist", "npm"} or len(by_kind) != len(artifacts):
        raise ReleaseValidationError(
            "release set must contain exactly one wheel, sdist, and npm tarball"
        )
    wheel = by_kind["wheel"]
    sdist = by_kind["sdist"]
    npm = by_kind["npm"]
    normalized = PROJECT_NAME.replace("-", "_")
    if not wheel.name.startswith(f"{normalized}-{info.python_version}-"):
        raise ReleaseValidationError(f"wheel does not identify version {info.python_version}")
    if sdist.name != f"{normalized}-{info.python_version}.tar.gz":
        raise ReleaseValidationError(f"sdist name is not for version {info.python_version}")
    if npm.name != f"{PROJECT_NAME}-{info.npm_version}.tgz":
        raise ReleaseValidationError(f"npm tarball name is not for version {info.npm_version}")
    manifest = {
        "schema": 1,
        "project": PROJECT_NAME,
        "version": info.python_version,
        "npm_version": info.npm_version,
        "tag": info.tag,
        "channel": info.channel,
        "commit": commit,
        "npm_dist_tag": info.npm_dist_tag,
        "github_prerelease": info.github_prerelease,
        "artifacts": [artifact.__dict__ for artifact in artifacts],
    }
    validate_manifest(manifest, info, commit, root=root)
    return manifest


def _safe_relative_path(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseValidationError(f"{context} must be a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseValidationError(f"{context} must be a safe relative path")
    return path.as_posix()


def _safe_artifact_path(root: Path, relative: object, context: str) -> Path:
    normalized = _safe_relative_path(relative, context)
    candidate = root / Path(*PurePosixPath(normalized).parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ReleaseValidationError(f"{context} escapes the release root") from exc
    return candidate


def _validate_artifact_descriptor(
    artifact: object,
    index: int,
) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise ReleaseValidationError(f"release manifest artifact {index} is not an object")
    expected_keys = {"name", "path", "kind", "size", "sha256", "sha512_integrity"}
    if set(artifact) != expected_keys:
        raise ReleaseValidationError(f"release manifest artifact {index} has invalid fields")
    name = artifact["name"]
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or "\\" in name
        or name in {".", ".."}
    ):
        raise ReleaseValidationError(f"release manifest artifact {index} has an unsafe name")
    path = _safe_relative_path(artifact["path"], f"release manifest artifact {index} path")
    if PurePosixPath(path).name != name:
        raise ReleaseValidationError(
            f"release manifest artifact {index} path does not end in its name"
        )
    kind = artifact["kind"]
    if not isinstance(kind, str) or kind not in {"wheel", "sdist", "npm"}:
        raise ReleaseValidationError(f"release manifest artifact {index} has an unknown kind")
    size = artifact["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ReleaseValidationError(f"release manifest artifact {index} has an invalid size")
    sha256 = artifact["sha256"]
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise ReleaseValidationError(f"release manifest artifact {index} has an invalid sha256")
    integrity = artifact["sha512_integrity"]
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise ReleaseValidationError(
            f"release manifest artifact {index} has an invalid sha512 integrity"
        )
    try:
        decoded = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReleaseValidationError(
            f"release manifest artifact {index} has an invalid sha512 integrity"
        ) from exc
    if len(decoded) != hashlib.sha512().digest_size:
        raise ReleaseValidationError(
            f"release manifest artifact {index} has an invalid sha512 integrity"
        )
    normalized = dict(artifact)
    normalized["path"] = path
    return normalized


def _artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
        raise ReleaseValidationError("checksum manifest has no valid artifacts")
    return artifacts


def validate_manifest(
    manifest: dict[str, Any],
    info: VersionInfo,
    commit: str,
    *,
    root: Path | None = None,
) -> None:
    """Validate a release manifest against source metadata and optional files.

    The manifest is used as a release input during recovery, so its identity
    must be derived from the checked-out source rather than trusted merely
    because its own hashes are internally consistent.
    """

    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ReleaseValidationError("manifest commit must be a full SHA-1")
    expected = {
        "schema",
        "project",
        "version",
        "npm_version",
        "tag",
        "channel",
        "commit",
        "npm_dist_tag",
        "github_prerelease",
        "artifacts",
    }
    if set(manifest) != expected:
        raise ReleaseValidationError("release manifest has invalid fields")
    if (
        manifest["schema"] != 1
        or manifest["project"] != PROJECT_NAME
        or manifest["version"] != info.python_version
        or manifest["npm_version"] != info.npm_version
        or manifest["tag"] != info.tag
        or manifest["channel"] != info.channel
        or manifest["commit"] != commit
        or manifest["npm_dist_tag"] != info.npm_dist_tag
        or manifest["github_prerelease"] is not info.github_prerelease
    ):
        raise ReleaseValidationError("release manifest does not match checked-out source metadata")

    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 3:
        raise ReleaseValidationError("release manifest must contain exactly three artifacts")
    normalized = [_validate_artifact_descriptor(item, index) for index, item in enumerate(artifacts)]
    if {item["kind"] for item in normalized} != {"wheel", "sdist", "npm"}:
        raise ReleaseValidationError("release manifest must contain wheel, sdist, and npm artifacts")
    if len({item["name"] for item in normalized}) != len(normalized):
        raise ReleaseValidationError("release manifest contains duplicate artifact names")
    if len({item["path"] for item in normalized}) != len(normalized):
        raise ReleaseValidationError("release manifest contains duplicate artifact paths")

    by_kind = {item["kind"]: item for item in normalized}
    normalized_name = PROJECT_NAME.replace("-", "_")
    if not (
        by_kind["wheel"]["name"].startswith(f"{normalized_name}-{info.python_version}-")
        and by_kind["wheel"]["name"].endswith(".whl")
    ):
        raise ReleaseValidationError(f"wheel does not identify version {info.python_version}")
    if by_kind["sdist"]["name"] != f"{normalized_name}-{info.python_version}.tar.gz":
        raise ReleaseValidationError(f"sdist name is not for version {info.python_version}")
    if by_kind["npm"]["name"] != f"{PROJECT_NAME}-{info.npm_version}.tgz":
        raise ReleaseValidationError(f"npm tarball name is not for version {info.npm_version}")

    if root is None:
        return
    if not root.is_dir():
        raise ReleaseValidationError(f"release root is not a directory: {root}")
    resolved_root = root.resolve()
    approved_paths = {artifact["path"] for artifact in normalized}
    for artifact in normalized:
        path = _safe_artifact_path(resolved_root, artifact["path"], "release artifact path")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ReleaseValidationError(f"manifest artifact is missing: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise ReleaseValidationError(f"manifest artifact is not a regular file: {path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ReleaseValidationError(f"cannot read release artifact {path}") from exc
        if len(data) != artifact["size"]:
            raise ReleaseValidationError(f"manifest artifact size mismatch: {artifact['name']}")
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ReleaseValidationError(f"manifest artifact hash mismatch: {artifact['name']}")
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
        if integrity != artifact["sha512_integrity"]:
            raise ReleaseValidationError(
                f"manifest artifact integrity mismatch: {artifact['name']}"
            )
    for path in resolved_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(resolved_root).as_posix()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz") or path.suffix == ".tgz":
            if relative not in approved_paths:
                raise ReleaseValidationError(f"unapproved release artifact is present: {relative}")


def _manifest_artifact(manifest: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [item for item in _artifacts(manifest) if item.get("kind") == kind]
    if len(matches) != 1:
        raise ReleaseValidationError(f"manifest must contain one {kind} artifact")
    return matches[0]


def _pypi_decision(manifest: dict[str, Any], payload: dict[str, Any] | None) -> tuple[str, list[str]]:
    expected = {item["name"]: item for item in _artifacts(manifest) if item.get("kind") in {"wheel", "sdist"}}
    if payload is None:
        return "absent", sorted(expected)
    info = payload.get("info", {})
    if not isinstance(info, dict) or info.get("name") != PROJECT_NAME:
        raise ReleaseValidationError("PyPI metadata package identity does not match manifest")
    if info.get("version") != manifest.get("version"):
        raise ReleaseValidationError("PyPI metadata version does not match manifest")
    observed: dict[str, dict[str, Any]] = {}
    for item in payload.get("urls", []):
        if isinstance(item, dict) and isinstance(item.get("filename"), str):
            observed[item["filename"]] = item
    missing: list[str] = []
    for name, artifact in expected.items():
        remote = observed.get(name)
        if remote is None:
            missing.append(name)
            continue
        digests = remote.get("digests")
        if not isinstance(digests, dict) or digests.get("sha256") != artifact["sha256"]:
            raise ReleaseValidationError(f"PyPI artifact hash mismatch for {name}")
    unexpected = sorted(set(observed) - set(expected))
    if unexpected:
        raise ReleaseValidationError(f"PyPI has unapproved artifacts: {unexpected}")
    return ("complete" if not missing else "partial"), sorted(missing)


def _npm_decision(manifest: dict[str, Any], payload: dict[str, Any] | None) -> tuple[str, list[str]]:
    artifact = _manifest_artifact(manifest, "npm")
    if payload is None:
        return "absent", [artifact["name"]]
    if payload.get("name") != PROJECT_NAME or payload.get("version") != manifest.get("npm_version"):
        raise ReleaseValidationError("npm metadata package identity does not match manifest")
    dist = payload.get("dist")
    if not isinstance(dist, dict):
        raise ReleaseValidationError("npm metadata has no dist identity")
    if dist.get("integrity") != artifact["sha512_integrity"]:
        raise ReleaseValidationError("npm tarball integrity does not match manifest")
    tarball = dist.get("tarball")
    if not isinstance(tarball, str) or Path(urllib.parse.urlparse(tarball).path).name != artifact["name"]:
        raise ReleaseValidationError("npm tarball name does not match manifest")
    return "complete", []


def registry_decision(
    registry: str,
    manifest: dict[str, Any],
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[str, list[str]]:
    if registry == "pypi":
        url = f"{PYPI_URL}/{PROJECT_NAME}/{urllib.parse.quote(str(manifest['version']), safe='')}/json"
    elif registry == "npm":
        url = f"{NPM_URL}/{PROJECT_NAME}/{urllib.parse.quote(str(manifest['npm_version']), safe='')}"
    else:
        raise ReleaseValidationError(f"unknown registry: {registry}")
    result = _request_read(url, opener=opener, sleeper=sleeper)
    if response_class(result.status) == "not_found":
        payload = None
    else:
        payload = _json_result(result, f"{registry} metadata")
    return _pypi_decision(manifest, payload) if registry == "pypi" else _npm_decision(manifest, payload)


def verify_registry_after_publish(
    registry: str,
    manifest: dict[str, Any],
    *,
    attempts: int = POST_PUBLICATION_ATTEMPTS,
    deadline: float = POST_PUBLICATION_DEADLINE,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> None:
    """Verify a successful publish while allowing bounded registry propagation.

    Pre-publication checks must continue using :func:`registry_decision`, where
    a 404 is an absence decision.  This post-publication-only path retries only
    the safe-to-re-read incomplete states; authentication, outage, malformed
    metadata, and hash mismatches still raise immediately.
    """

    if attempts < 1 or deadline <= 0:
        raise ValueError("attempts must be positive and deadline must be positive")
    deadline_at = clock() + deadline
    last_state = "unknown"
    last_missing: list[str] = []
    performed_attempts = 0
    for attempt in range(1, attempts + 1):
        if attempt > 1 and clock() >= deadline_at:
            break
        state, missing = registry_decision(
            registry,
            manifest,
            opener=opener,
            sleeper=sleeper,
        )
        performed_attempts = attempt
        last_state, last_missing = state, missing
        status = "HTTP 404" if state == "absent" else state
        log(
            f"{registry} post-publication verification attempt "
            f"{attempt}/{attempts}: state={status}, missing={missing}"
        )
        if state == "complete" and not missing:
            log(
                f"{registry} post-publication verification complete: "
                f"exact artifact hashes matched on attempt {attempt}/{attempts}"
            )
            return
        if state not in {"absent", "partial"}:
            raise ReleaseValidationError(
                f"{registry} post-publication verification returned unsupported state: {state}"
            )
        if attempt == attempts:
            break
        remaining = deadline_at - clock()
        if remaining <= 0:
            break
        delay = min(0.25 * (2 ** (attempt - 1)), remaining)
        log(
            f"{registry} post-publication verification is {state}; "
            f"retrying in {delay:.2f}s before deadline"
        )
        sleeper(delay)
    raise ReleaseValidationError(
        f"{registry} post-publication verification exhausted after "
        f"{performed_attempts} attempts (limit {attempts}, deadline {deadline:.1f}s): "
        f"last_state={last_state}, "
        f"missing={last_missing}"
    )


def stage_missing(manifest: dict[str, Any], root: Path, destination: Path, names: Iterable[str]) -> None:
    wanted = set(names)
    destination.mkdir(parents=True, exist_ok=True)
    for artifact in _artifacts(manifest):
        name = artifact.get("name")
        if not isinstance(name, str):
            continue
        if name not in wanted:
            continue
        if Path(name).name != name or "\\" in name:
            raise ReleaseValidationError(f"manifest artifact has an unsafe name: {name}")
        source = _safe_artifact_path(root, artifact.get("path"), "manifest artifact path")
        try:
            metadata = source.lstat()
        except OSError as exc:
            raise ReleaseValidationError(f"manifest artifact is missing: {source}") from exc
        if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
            raise ReleaseValidationError(f"manifest artifact is missing: {source}")
        shutil.copyfile(source, destination / artifact["name"])


def verify_staged(manifest: dict[str, Any], root: Path, destination: Path) -> None:
    expected = {item["name"]: item for item in _artifacts(manifest)}
    observed = []
    for path in destination.iterdir():
        if path.is_symlink():
            raise ReleaseValidationError(f"staged artifact is not a regular file: {path.name}")
        if path.is_file():
            observed.append(path.name)
    for name in observed:
        if name not in expected:
            raise ReleaseValidationError(f"staged artifact is not approved: {name}")
        data = (destination / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected[name]["sha256"]:
            raise ReleaseValidationError(f"staged artifact hash mismatch: {name}")


def _auth_headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _validate_manifest_binding(manifest: dict[str, Any], tag: str, commit: str) -> None:
    if not re.fullmatch(r"v[0-9A-Za-z][0-9A-Za-z.\-+]*", tag):
        raise ReleaseValidationError(f"invalid release tag: {tag}")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ReleaseValidationError(f"commit must be a full SHA-1: {commit}")
    if manifest.get("project") != PROJECT_NAME:
        raise ReleaseValidationError("release manifest has the wrong project")
    if manifest.get("tag") != tag or manifest.get("commit") != commit:
        raise ReleaseValidationError("release manifest does not bind to the expected tag and commit")


def _github_get(
    url: str,
    token: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any] | list[Any] | None:
    _validate_github_api_url(url)
    request_args: dict[str, Any] = {"headers": _auth_headers(token)}
    if opener is not None:
        request_args["opener"] = opener
    result = _request_read(url, **request_args)
    if response_class(result.status) == "not_found":
        return None
    try:
        value = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError("GitHub API returned invalid JSON") from exc
    if not isinstance(value, (dict, list)):
        raise ReleaseValidationError("GitHub API returned an invalid JSON value")
    return value


def _github_write(url: str, token: str, payload: dict[str, Any], *, content_type: str = "application/json") -> dict[str, Any]:
    headers = _auth_headers(token)
    headers["Content-Type"] = content_type
    result = _request_write(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    return _json_result(result, "GitHub API write")


def _asset_matches(
    asset: dict[str, Any],
    expected: dict[str, Any],
    token: str,
    *,
    repo: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> bool:
    if asset.get("name") != MANIFEST_NAME or asset.get("size") != expected["size"]:
        return False
    asset_url = asset.get("url")
    if not isinstance(asset_url, str):
        return False
    _validate_github_asset_url(asset_url, repo=repo)
    digest = asset.get("digest")
    if digest:
        return digest == f"sha256:{expected['sha256']}"
    request_args: dict[str, Any] = {
        "headers": _auth_headers(token, "application/octet-stream")
    }
    if opener is not None:
        request_args["opener"] = opener
    result = _request_read(
        asset_url,
        **request_args,
    )
    return hashlib.sha256(result.body).hexdigest() == expected["sha256"]


def _load_release_manifest(
    repo: str,
    tag: str,
    token: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    url = f"{GITHUB_API_URL}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    if opener is None:
        release = _github_get(url, token)
    else:
        release = _github_get(url, token, opener=opener)
    if not isinstance(release, dict):
        raise ReleaseValidationError("expected GitHub release was not found")
    if release.get("tag_name") != tag or release.get("draft") is True:
        raise ReleaseValidationError("GitHub release identity/state does not match recovery tag")
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise ReleaseValidationError("GitHub release assets are malformed")
    matches = [
        asset for asset in assets if isinstance(asset, dict) and asset.get("name") == MANIFEST_NAME
    ]
    if len(matches) != 1:
        raise ReleaseValidationError("GitHub release has no unique checksum manifest asset")
    asset = matches[0]
    asset_url = asset.get("url")
    if not isinstance(asset_url, str):
        raise ReleaseValidationError("checksum manifest asset has no API URL")
    _validate_github_asset_url(asset_url, repo=repo)
    request_args: dict[str, Any] = {
        "headers": _auth_headers(token, "application/octet-stream")
    }
    if opener is not None:
        request_args["opener"] = opener
    result = _request_read(asset_url, **request_args)
    if isinstance(asset.get("size"), int) and asset["size"] != len(result.body):
        raise ReleaseValidationError("checksum manifest asset size mismatch")
    digest = asset.get("digest")
    if digest and digest != f"sha256:{hashlib.sha256(result.body).hexdigest()}":
        raise ReleaseValidationError("checksum manifest asset digest mismatch")
    try:
        manifest = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError("checksum manifest asset is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise ReleaseValidationError("checksum manifest asset is not an object")
    if release.get("prerelease") != manifest.get("github_prerelease"):
        raise ReleaseValidationError("GitHub release prerelease state does not match manifest")
    return manifest


def _verify_release(release: dict[str, Any], manifest: dict[str, Any], token: str) -> dict[str, Any]:
    if release.get("tag_name") != manifest.get("tag"):
        raise ReleaseValidationError("existing GitHub release has the wrong tag")
    if release.get("draft") is True or release.get("prerelease") != manifest.get("github_prerelease"):
        raise ReleaseValidationError("existing GitHub release has the wrong release state")
    _artifacts(manifest)
    return release


def ensure_github_release(repo: str, manifest: dict[str, Any], manifest_path: Path, token: str) -> None:
    tag = str(manifest["tag"])
    validate_source(tag, str(manifest["commit"]))
    url = f"{GITHUB_API_URL}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    existing = _github_get(url, token)
    if existing is not None:
        if not isinstance(existing, dict):
            raise ReleaseValidationError("GitHub release lookup returned an invalid object")
        _verify_release(existing, manifest, token)
        release = existing
    else:
        payload = {
            "tag_name": tag,
            "name": tag,
            "generate_release_notes": True,
            "prerelease": bool(manifest["github_prerelease"]),
            "draft": False,
        }
        try:
            release = _github_write(f"{GITHUB_API_URL}/repos/{repo}/releases", token, payload)
        except ReleaseValidationError as exc:
            # A concurrent creator may have won.  Re-read and accept only an
            # exactly matching release; never repeat the POST.
            existing = _github_get(url, token)
            if existing is not None and isinstance(existing, dict):
                _verify_release(existing, manifest, token)
                release = existing
            else:
                raise exc
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise ReleaseValidationError("GitHub release assets are malformed")
    expected = {
        "name": MANIFEST_NAME,
        "size": manifest_path.stat().st_size,
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == MANIFEST_NAME]
    if matching:
        if len(matching) != 1 or not _asset_matches(matching[0], expected, token, repo=repo):
            raise ReleaseValidationError("existing checksum manifest asset does not match")
        return
    upload_url = release.get("upload_url")
    release_id = release.get("id")
    if not isinstance(upload_url, str) or not isinstance(release_id, int):
        raise ReleaseValidationError("GitHub release has no upload endpoint")
    upload_url = upload_url.split("{", 1)[0] + "?name=" + urllib.parse.quote(MANIFEST_NAME)
    data = manifest_path.read_bytes()
    headers = _auth_headers(token)
    headers["Content-Type"] = "application/json"
    try:
        uploaded = _request_write(upload_url, data=data, headers=headers)
        uploaded_asset = _json_result(uploaded, "GitHub asset upload")
        if not _asset_matches(uploaded_asset, expected, token, repo=repo):
            raise ReleaseValidationError("uploaded checksum manifest asset does not match")
    except ReleaseValidationError as exc:
        reread = _github_get(url, token)
        if isinstance(reread, dict):
            reread_assets = reread.get("assets", [])
            matches = [item for item in reread_assets if isinstance(item, dict) and item.get("name") == MANIFEST_NAME]
            if len(matches) == 1 and _asset_matches(matches[0], expected, token, repo=repo):
                return
        raise exc


def _deployment_url(repo: str) -> str:
    return f"{GITHUB_API_URL}/repos/{repo}/deployments"


def _validate_deployment(deployment: dict[str, Any], tag: str, commit: str) -> int:
    expected = {
        "environment": DEPLOYMENT_ENVIRONMENT,
        "ref": tag,
        "sha": commit,
        "description": DEPLOYMENT_DESCRIPTION,
        "task": DEPLOYMENT_TASK,
    }
    if any(deployment.get(key) != value for key, value in expected.items()):
        raise ReleaseValidationError("deployment identity does not match the release")
    deployment_id = deployment.get("id")
    if not isinstance(deployment_id, int):
        raise ReleaseValidationError("deployment has no numeric id")
    return deployment_id


def record_deployment(
    repo: str,
    tag: str,
    commit: str,
    run_url: str,
    token: str,
    manifest: dict[str, Any],
) -> int:
    _validate_manifest_binding(manifest, tag, commit)
    state, missing = registry_decision("npm", manifest)
    if state != "complete" or missing:
        raise ReleaseValidationError(
            f"npm artifact identity is not complete; refusing bookkeeping: {state}, {missing}"
        )
    query = urllib.parse.urlencode(
        {"environment": DEPLOYMENT_ENVIRONMENT, "ref": tag, "per_page": "100"}
    )
    listed = _github_get(f"{_deployment_url(repo)}?{query}", token)
    if not isinstance(listed, list):
        raise ReleaseValidationError("GitHub deployment lookup returned no list")
    candidates = [
        item
        for item in listed
        if isinstance(item, dict)
        and item.get("environment") == DEPLOYMENT_ENVIRONMENT
        and item.get("ref") == tag
        and item.get("description") == DEPLOYMENT_DESCRIPTION
        and item.get("task") == DEPLOYMENT_TASK
        and item.get("sha") == commit
    ]
    ambiguous = [
        item
        for item in listed
        if isinstance(item, dict)
        and item.get("environment") == DEPLOYMENT_ENVIRONMENT
        and item.get("ref") == tag
        and item not in candidates
    ]
    if ambiguous:
        raise ReleaseValidationError("existing npm deployment has mismatched identity")
    if len(candidates) > 1:
        raise ReleaseValidationError("multiple ambiguous npm deployments require manual recovery")
    if candidates:
        deployment = candidates[0]
        deployment_id = _validate_deployment(deployment, tag, commit)
    else:
        payload = {
            "ref": tag,
            "sha": commit,
            "environment": DEPLOYMENT_ENVIRONMENT,
            "description": DEPLOYMENT_DESCRIPTION,
            "task": DEPLOYMENT_TASK,
            "auto_merge": False,
            "transient_environment": False,
            "production_environment": False,
            "required_contexts": [],
        }
        deployment = _github_write(_deployment_url(repo), token, payload)
        deployment_id = _validate_deployment(deployment, tag, commit)
    statuses_url = f"{_deployment_url(repo)}/{deployment_id}/statuses?per_page=100"
    statuses = _github_get(statuses_url, token)
    if isinstance(statuses, list) and any(
        isinstance(status, dict) and status.get("state") == "success" for status in statuses
    ):
        return deployment_id
    _github_write(
        f"{_deployment_url(repo)}/{deployment_id}/statuses",
        token,
        {"state": "success", "log_url": run_url, "description": "npm publish verified"},
    )
    return deployment_id


def recover_deployment_status(
    repo: str,
    deployment_id: int,
    tag: str,
    commit: str,
    run_url: str,
    token: str,
) -> None:
    manifest = _load_release_manifest(repo, tag, token)
    _validate_manifest_binding(manifest, tag, commit)
    state, missing = registry_decision("npm", manifest)
    if state != "complete" or missing:
        raise ReleaseValidationError(
            f"npm artifact identity is not complete; refusing recovery: {state}, {missing}"
        )
    deployment_url = f"{_deployment_url(repo)}/{deployment_id}"
    deployment = _github_get(deployment_url, token)
    if not isinstance(deployment, dict):
        raise ReleaseValidationError("deployment to recover was not found")
    if _validate_deployment(deployment, tag, commit) != deployment_id:
        raise ReleaseValidationError("deployment id does not match the requested recovery")
    statuses = _github_get(f"{deployment_url}/statuses?per_page=100", token)
    if isinstance(statuses, list) and any(
        isinstance(status, dict) and status.get("state") == "success" for status in statuses
    ):
        return
    _github_write(
        f"{deployment_url}/statuses",
        token,
        {
            "state": "success",
            "log_url": run_url,
            "description": "npm publish verified (recovery)",
        },
    )


def _emit(values: dict[str, str], output: Path | None) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    print("\n".join(lines))
    if output is not None:
        with output.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--tag", required=True)
    validate.add_argument("--commit", required=True)
    validate.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    validate.add_argument("--package-json", type=Path, default=Path("package.json"))
    validate.add_argument("--release-channel", type=Path, default=Path("release-channel.toml"))
    validate.add_argument("--output", type=Path)
    manifest = sub.add_parser("manifest")
    manifest.add_argument("--root", type=Path, required=True)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--npm-version", required=True)
    manifest.add_argument("--tag", required=True)
    manifest.add_argument("--commit", required=True)
    manifest.add_argument("--release-channel", type=Path, default=Path("release-channel.toml"))
    manifest.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("registry-check")
    check.add_argument("--registry", choices=("pypi", "npm"), required=True)
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--decision", type=Path, required=True)
    check.add_argument("--output", type=Path)
    stage = sub.add_parser("stage-missing")
    stage.add_argument("--manifest", type=Path, required=True)
    stage.add_argument("--root", type=Path, required=True)
    stage.add_argument("--decision", type=Path, required=True)
    stage.add_argument("--destination", type=Path, required=True)
    verify = sub.add_parser("registry-verify")
    verify.add_argument("--registry", choices=("pypi", "npm"), required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    github = sub.add_parser("github-release")
    github.add_argument("--repo", required=True)
    github.add_argument("--manifest", type=Path, required=True)
    github.add_argument("--token-env", default="GITHUB_TOKEN")
    deployment = sub.add_parser("record-deployment")
    deployment.add_argument("--repo", required=True)
    deployment.add_argument("--tag", required=True)
    deployment.add_argument("--commit", required=True)
    deployment.add_argument("--run-url", required=True)
    deployment.add_argument("--manifest", type=Path, required=True)
    deployment.add_argument("--token-env", default="GITHUB_TOKEN")
    recovery = sub.add_parser("recover-deployment")
    recovery.add_argument("--repo", required=True)
    recovery.add_argument("--deployment-id", type=int, required=True)
    recovery.add_argument("--tag", required=True)
    recovery.add_argument("--commit", required=True)
    recovery.add_argument("--run-url", required=True)
    recovery.add_argument("--token-env", default="GITHUB_TOKEN")
    value = sub.add_parser("manifest-value")
    value.add_argument("--manifest", type=Path, required=True)
    value.add_argument("--key", required=True)
    artifact = sub.add_parser("manifest-artifact")
    artifact.add_argument("--manifest", type=Path, required=True)
    artifact.add_argument("--kind", choices=("wheel", "sdist", "npm"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            python_version, npm_version, classifiers = _read_versions(
                args.pyproject, args.package_json
            )
            policy = load_release_channel(args.release_channel)
            info = map_versions(python_version, npm_version, args.tag, policy)
            status_classifiers = [
                item for item in classifiers if item.startswith("Development Status :: ")
            ]
            if status_classifiers != [policy.python_classifier]:
                raise ReleaseValidationError(
                    "release channel Python classifier is inconsistent in pyproject.toml"
                )
            validate_source(args.tag, args.commit)
            _emit(
                {
                    "version": info.python_version,
                    "npm_version": info.npm_version,
                    "channel": info.channel,
                    "npm_dist_tag": info.npm_dist_tag,
                    "github_prerelease": str(info.github_prerelease).lower(),
                    "python_classifier": policy.python_classifier,
                    "pypi_channel": policy.pypi_channel,
                },
                args.output,
            )
        elif args.command == "manifest":
            policy = load_release_channel(args.release_channel)
            info = map_versions(args.version, args.npm_version, args.tag, policy)
            manifest = create_manifest(args.root, info, args.commit)
            _write_json(args.output, manifest)
            print(f"wrote {args.output}")
        elif args.command == "registry-check":
            manifest = _read_json(args.manifest)
            state, missing = registry_decision(args.registry, manifest)
            decision = {"registry": args.registry, "state": state, "missing": missing}
            _write_json(args.decision, decision)
            _emit(
                {"state": state, "publish_required": str(bool(missing)).lower()},
                args.output,
            )
        elif args.command == "stage-missing":
            manifest = _read_json(args.manifest)
            decision = _read_json(args.decision)
            stage_missing(manifest, args.root, args.destination, decision.get("missing", []))
            verify_staged(manifest, args.root, args.destination)
        elif args.command == "registry-verify":
            manifest = _read_json(args.manifest)
            verify_registry_after_publish(args.registry, manifest)
            print(f"{args.registry} artifact identity verified after publication")
        elif args.command == "github-release":
            manifest = _read_json(args.manifest)
            token = os.environ.get(args.token_env)
            if not token:
                raise ReleaseValidationError(f"missing GitHub token environment variable {args.token_env}")
            ensure_github_release(args.repo, manifest, args.manifest, token)
            print("GitHub release and checksum asset verified")
        elif args.command == "record-deployment":
            token = os.environ.get(args.token_env)
            if not token:
                raise ReleaseValidationError(f"missing GitHub token environment variable {args.token_env}")
            manifest = _read_json(args.manifest)
            deployment_id = record_deployment(
                args.repo, args.tag, args.commit, args.run_url, token, manifest
            )
            print(f"npm deployment bookkeeping verified: {deployment_id}")
        elif args.command == "recover-deployment":
            token = os.environ.get(args.token_env)
            if not token:
                raise ReleaseValidationError(f"missing GitHub token environment variable {args.token_env}")
            recover_deployment_status(
                args.repo,
                args.deployment_id,
                args.tag,
                args.commit,
                args.run_url,
                token,
            )
            print(f"npm deployment status recovered: {args.deployment_id}")
        elif args.command == "manifest-value":
            manifest = _read_json(args.manifest)
            value = manifest.get(args.key)
            if not isinstance(value, (str, int, bool)):
                raise ReleaseValidationError(f"manifest value is not scalar: {args.key}")
            print(str(value).lower() if isinstance(value, bool) else value)
        elif args.command == "manifest-artifact":
            manifest = _read_json(args.manifest)
            print(_manifest_artifact(manifest, args.kind)["path"])
    except ReleaseValidationError as exc:
        print(f"release validation error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
