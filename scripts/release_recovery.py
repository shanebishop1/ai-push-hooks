"""Fetch and validate an existing release set without republishing it."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable

if __package__:
    from . import release_validation as core
else:
    import release_validation as core


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024


def _github_list(
    url: str,
    token: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Read every page of a GitHub collection without interpolated jq."""

    core._validate_github_api_url(url)
    values: list[dict[str, Any]] = []
    for page in range(1, 1001):
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}per_page=100&page={page}"
        if opener is None:
            value = core._github_get(page_url, token)
        else:
            value = core._github_get(page_url, token, opener=opener)
        if value is None:
            break
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise core.ReleaseValidationError(
                "GitHub API collection returned invalid JSON"
            )
        values.extend(value)
        if len(value) < 100:
            break
    else:
        raise core.ReleaseValidationError(
            "GitHub API collection exceeded pagination bound"
        )
    return values


def _asset_bytes(
    asset: dict[str, Any],
    token: str,
    *,
    repo: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> bytes:
    asset_url = asset.get("url")
    if not isinstance(asset_url, str) or not asset_url:
        raise core.ReleaseValidationError("release asset has no API URL")
    expected_size = asset.get("size")
    if isinstance(expected_size, bool) or (
        expected_size is not None
        and (not isinstance(expected_size, int) or expected_size < 0)
    ):
        raise core.ReleaseValidationError("release asset has an invalid size")
    if expected_size is not None and expected_size > MAX_ARCHIVE_BYTES:
        raise core.ReleaseValidationError(
            "release asset exceeds the recovery size bound"
        )
    core._validate_github_asset_url(
        asset_url,
        repo=repo,
        allow_actions_artifact=True,
    )
    request_args: dict[str, Any] = {
        "headers": core._auth_headers(token, "application/octet-stream"),
        "max_bytes": MAX_ARCHIVE_BYTES,
    }
    if opener is not None:
        request_args["opener"] = opener
    result = core._request_read(asset_url, **request_args)
    if expected_size is not None and len(result.body) != expected_size:
        raise core.ReleaseValidationError("release asset size mismatch")
    digest = asset.get("digest")
    if (
        digest is not None
        and digest != f"sha256:{hashlib.sha256(result.body).hexdigest()}"
    ):
        raise core.ReleaseValidationError("release asset digest mismatch")
    return result.body


def _write_file(root: Path, relative: object, data: bytes) -> None:
    path = core._safe_artifact_path(root, relative, "recovery artifact path")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise core.ReleaseValidationError(
            f"cannot create recovery artifact directory: {relative}"
        ) from exc
    if path.exists() or path.is_symlink():
        raise core.ReleaseValidationError(
            f"recovery artifact path is duplicated: {relative}"
        )
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except OSError as exc:
        raise core.ReleaseValidationError(
            f"cannot write recovery artifact: {relative}"
        ) from exc


def _extract_archive(data: bytes, root: Path) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise core.ReleaseValidationError(
            "Actions release artifact is not a valid ZIP"
        ) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise core.ReleaseValidationError(
                "Actions release artifact has too many entries"
            )
        total_size = sum(info.file_size for info in infos if not info.is_dir())
        if total_size > MAX_EXTRACTED_BYTES:
            raise core.ReleaseValidationError(
                "Actions release artifact exceeds the extracted size bound"
            )
        for info in infos:
            relative = core._safe_relative_path(info.filename, "Actions archive path")
            path = core._safe_artifact_path(root, relative, "Actions archive path")
            mode = (info.external_attr >> 16) & 0o170000
            if info.is_dir():
                if mode not in {0, stat.S_IFDIR}:
                    raise core.ReleaseValidationError(
                        "Actions archive contains an unsafe directory"
                    )
                if path.is_symlink() or (path.exists() and not path.is_dir()):
                    raise core.ReleaseValidationError(
                        "Actions archive contains a conflicting path"
                    )
                path.mkdir(parents=True, exist_ok=True)
                continue
            if mode not in {0, stat.S_IFREG}:
                raise core.ReleaseValidationError(
                    "Actions archive contains a non-regular file"
                )
            try:
                content = archive.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise core.ReleaseValidationError(
                    "cannot read Actions release artifact"
                ) from exc
            if len(content) != info.file_size:
                raise core.ReleaseValidationError("Actions archive entry size mismatch")
            _write_file(root, relative, content)


def _validate_actions_artifact(
    artifact: dict[str, Any], artifact_id: int, tag: str, commit: str
) -> None:
    if artifact.get("id") != artifact_id:
        raise core.ReleaseValidationError(
            "Actions artifact identity does not match the requested ID"
        )
    if artifact.get("name") != f"release-set-{tag}":
        raise core.ReleaseValidationError(
            "Actions artifact name does not match the recovery tag"
        )
    if artifact.get("expired") is not False:
        raise core.ReleaseValidationError(
            "Actions release artifact is expired or has unknown state"
        )
    workflow_run = artifact.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise core.ReleaseValidationError(
            "Actions artifact has no workflow run identity"
        )
    if not isinstance(workflow_run.get("id"), int) or workflow_run["id"] <= 0:
        raise core.ReleaseValidationError(
            "Actions artifact has no valid workflow run ID"
        )
    if workflow_run.get("event") != "push":
        raise core.ReleaseValidationError(
            "Actions artifact was not produced by a push workflow"
        )
    if workflow_run.get("head_sha") != commit:
        raise core.ReleaseValidationError(
            "Actions artifact head SHA does not match the recovery commit"
        )


def _download_actions_release_set(
    repo: str,
    artifact_id: int,
    tag: str,
    commit: str,
    root: Path,
    token: str,
) -> None:
    metadata = core._github_get(
        f"{core.GITHUB_API_URL}/repos/{repo}/actions/artifacts/{artifact_id}", token
    )
    if not isinstance(metadata, dict):
        raise core.ReleaseValidationError("Actions release artifact was not found")
    _validate_actions_artifact(metadata, artifact_id, tag, commit)
    data = _asset_bytes(
        {
            "url": f"{core.GITHUB_API_URL}/repos/{repo}/actions/artifacts/{artifact_id}/zip",
            "size": metadata.get("size_in_bytes"),
            "digest": metadata.get("digest"),
        },
        token,
        repo=repo,
    )
    _extract_archive(data, root)


def _draft_release(repo: str, tag: str, token: str) -> dict[str, Any]:
    releases = [
        item
        for item in _github_list(f"{core.GITHUB_API_URL}/repos/{repo}/releases", token)
        if item.get("tag_name") == tag
    ]
    if len(releases) != 1:
        raise core.ReleaseValidationError(
            "expected one GitHub draft release for the recovery tag"
        )
    release = releases[0]
    if release.get("draft") is not True:
        raise core.ReleaseValidationError(
            "draft asset recovery requires a draft GitHub release"
        )
    if not isinstance(release.get("assets"), list):
        raise core.ReleaseValidationError("GitHub draft release assets are malformed")
    return release


def _release_asset(release: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [
        asset
        for asset in release["assets"]
        if isinstance(asset, dict) and asset.get("name") == name
    ]
    if len(matches) != 1:
        raise core.ReleaseValidationError(
            f"GitHub draft release has no unique asset: {name}"
        )
    return matches[0]


def _download_draft_release_set(
    repo: str,
    tag: str,
    commit: str,
    info: core.VersionInfo,
    root: Path,
    token: str,
) -> None:
    release = _draft_release(repo, tag, token)
    manifest_asset = _release_asset(release, core.MANIFEST_NAME)
    manifest_url = manifest_asset.get("url")
    if not isinstance(manifest_url, str):
        raise core.ReleaseValidationError("release asset has no API URL")
    core._validate_github_asset_url(manifest_url, repo=repo)
    manifest_body = _asset_bytes(manifest_asset, token)
    try:
        manifest = json.loads(manifest_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise core.ReleaseValidationError(
            "checksum manifest asset is invalid JSON"
        ) from exc
    if not isinstance(manifest, dict):
        raise core.ReleaseValidationError("checksum manifest asset is not an object")
    core.validate_manifest(manifest, info, commit)
    _write_file(root, core.MANIFEST_NAME, manifest_body)
    for artifact in manifest["artifacts"]:
        asset = _release_asset(release, artifact["name"])
        asset_url = asset.get("url")
        if not isinstance(asset_url, str):
            raise core.ReleaseValidationError("release asset has no API URL")
        core._validate_github_asset_url(asset_url, repo=repo)
        _write_file(root, artifact["path"], _asset_bytes(asset, token))


def recover_artifacts(
    repo: str,
    tag: str,
    commit: str,
    artifact_id: str,
    source_root: Path,
    root: Path,
    release_channel: Path,
    token: str,
) -> None:
    """Fetch and validate one immutable release set for npm-only recovery."""

    info = _source_release_info(source_root, tag, commit, release_channel)
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise core.ReleaseValidationError(
                "recovery root must be a new empty directory"
            )
    else:
        root.mkdir(parents=True)
    if artifact_id:
        if not artifact_id.isdecimal() or int(artifact_id) <= 0:
            raise core.ReleaseValidationError(
                "artifact ID must be a positive decimal number"
            )
        _download_actions_release_set(repo, int(artifact_id), tag, commit, root, token)
    else:
        _download_draft_release_set(repo, tag, commit, info, root, token)
    manifest = core._read_json(root / core.MANIFEST_NAME)
    core.validate_manifest(manifest, info, commit, root=root)


def _source_release_info(
    source_root: Path,
    tag: str,
    commit: str,
    release_channel: Path,
) -> core.VersionInfo:
    python_version, npm_version, classifiers = core._read_versions(
        source_root / "pyproject.toml", source_root / "package.json"
    )
    policy = core.load_release_channel(release_channel)
    info = core.map_versions(python_version, npm_version, tag, policy)
    status_classifiers = [
        item for item in classifiers if item.startswith("Development Status :: ")
    ]
    if status_classifiers != [policy.python_classifier]:
        raise core.ReleaseValidationError(
            "release channel Python classifier is inconsistent in pyproject.toml"
        )
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise core.ReleaseValidationError("recovery commit must be a full SHA-1")
    return info


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--artifact-id", default="")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--release-channel", type=Path)
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        token = os.environ.get(args.token_env)
        if not token:
            raise core.ReleaseValidationError(
                f"missing GitHub token environment variable {args.token_env}"
            )
        channel_path = args.release_channel or args.source_root / "release-channel.toml"
        recover_artifacts(
            args.repo,
            args.tag,
            args.commit,
            args.artifact_id,
            args.source_root,
            args.root,
            channel_path,
            token,
        )
        print(f"recovery release set verified: {args.root}")
    except core.ReleaseValidationError as exc:
        print(f"release recovery error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
