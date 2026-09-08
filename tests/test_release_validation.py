import hashlib
import json
from pathlib import Path

import pytest

from scripts import release_validation as release


class MockResponse:
    def __init__(self, status, body=b"{}", headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self._body


def opener_for(*responses):
    queue = list(responses)

    def opener(_request, timeout):
        assert timeout > 0
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    return opener, queue


@pytest.mark.parametrize(
    ("status", "category"),
    [(200, "ok"), (404, "not_found"), (401, "auth"), (429, "transient"), (503, "transient")],
)
def test_response_classification_is_explicit(status, category):
    assert release.response_class(status) == category


def test_reads_retry_transient_fixture_then_return_success():
    opener, remaining = opener_for(
        MockResponse(503, headers={"Retry-After": "10"}),
        MockResponse(200, b'{"ok": true}'),
    )
    sleeps = []
    result = release._request_read(
        "https://fixture.invalid", timeout=1, opener=opener, sleeper=sleeps.append
    )
    assert result.status == 200
    assert sleeps == [0.25]
    assert not remaining


@pytest.mark.parametrize("status", [429, 500])
def test_exhausted_transient_fixtures_fail_closed(status):
    opener, remaining = opener_for(MockResponse(status), MockResponse(status))
    with pytest.raises(release.ReleaseValidationError, match="transient"):
        release._request_read(
            "https://fixture.invalid",
            timeout=1,
            attempts=2,
            opener=opener,
            sleeper=lambda _delay: None,
        )
    assert not remaining


def test_reads_retry_timeout_with_bounded_attempts():
    opener, remaining = opener_for(TimeoutError(), TimeoutError(), TimeoutError())
    sleeps = []
    with pytest.raises(release.ReleaseValidationError, match="timed out"):
        release._request_read(
            "https://fixture.invalid",
            timeout=1,
            opener=opener,
            sleeper=sleeps.append,
        )
    assert sleeps == [0.25, 0.5]
    assert not remaining


def test_auth_failure_is_not_treated_as_absence():
    opener, _ = opener_for(MockResponse(401))
    with pytest.raises(release.ReleaseValidationError, match="credentials"):
        release._request_read("https://fixture.invalid", timeout=1, opener=opener)


def test_write_never_retries_after_timeout():
    calls = []

    def opener(_request, timeout):
        calls.append(timeout)
        raise TimeoutError()

    with pytest.raises(release.ReleaseValidationError, match="do not retry"):
        release._request_write(
            "https://fixture.invalid",
            data=b"{}",
            headers={},
            timeout=1,
            opener=opener,
        )
    assert calls == [1]


@pytest.mark.parametrize(
    ("python_version", "npm_version", "tag", "dist_tag", "prerelease"),
    [
        ("1.2.3", "1.2.3", "v1.2.3", "latest", False),
        ("1.2.3b1", "1.2.3-beta.1", "v1.2.3-beta.1", "beta", True),
        ("1.2.3rc2", "1.2.3-rc.2", "v1.2.3-rc.2", "next", True),
    ],
)
def test_version_mapping_policy(python_version, npm_version, tag, dist_tag, prerelease):
    info = release.map_versions(python_version, npm_version, tag)
    assert info.npm_dist_tag == dist_tag
    assert info.github_prerelease is prerelease


def test_version_mapping_rejects_unapproved_spelling():
    with pytest.raises(release.ReleaseValidationError, match="version mismatch"):
        release.map_versions("1.2.3b1", "1.2.3b1", "v1.2.3b1")


def test_checked_in_channel_maps_stable_version_to_beta_without_version_special_case():
    policy = release.load_release_channel(Path("release-channel.toml"))
    info = release.map_versions("0.2.0", "0.2.0", "v0.2.0", policy)
    assert policy.version == "0.2.0"
    assert policy.channel == "beta"
    assert policy.python_classifier == "Development Status :: 4 - Beta"
    assert policy.pypi_channel == "none"
    assert info.channel == "beta"
    assert info.npm_dist_tag == "beta"
    assert info.github_prerelease is True


def test_explicit_beta_channel_is_generic_for_other_stable_versions():
    policy = release.ReleaseChannel(
        "9.8.7",
        "beta",
        "beta",
        True,
        "Development Status :: 4 - Beta",
        "none",
    )
    info = release.map_versions("9.8.7", "9.8.7", "v9.8.7", policy)
    assert info.npm_dist_tag == "beta"
    assert info.github_prerelease is True


def test_explicit_pep440_prerelease_mapping_remains_unchanged_with_metadata():
    policy = release.ReleaseChannel(
        "1.2.3b1",
        "beta",
        "beta",
        True,
        "Development Status :: 4 - Beta",
        "none",
    )
    info = release.map_versions("1.2.3b1", "1.2.3-beta.1", "v1.2.3-beta.1", policy)
    assert info.npm_dist_tag == "beta"
    assert info.github_prerelease is True


def test_tag_commit_binding_is_fail_closed(monkeypatch):
    def fake_git(*args):
        return "a" * 40 if "refs/tags/" in args[1] else "b" * 40

    monkeypatch.setattr(release, "_run_git", fake_git)
    with pytest.raises(release.ReleaseValidationError, match="binding mismatch"):
        release.validate_source("v1.2.3", "b" * 40)


def test_registry_identity_and_partial_state_use_exact_hashes(tmp_path, monkeypatch):
    data = b"wheel bytes"
    wheel = tmp_path / "python" / "ai_push_hooks-1.2.3-py3-none-any.whl"
    sdist = tmp_path / "python" / "ai_push_hooks-1.2.3.tar.gz"
    npm = tmp_path / "npm" / "ai-push-hooks-1.2.3.tgz"
    for path, content in ((wheel, data), (sdist, b"sdist"), (npm, b"npm")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    info = release.map_versions("1.2.3", "1.2.3", "v1.2.3")
    manifest = release.create_manifest(tmp_path, info, "a" * 40)
    wheel_hash = hashlib.sha256(data).hexdigest()
    payload = {
        "info": {"name": "ai-push-hooks", "version": "1.2.3"},
        "urls": [
            {
                "filename": wheel.name,
                "digests": {"sha256": wheel_hash},
            }
        ],
    }

    class FakeRegistryResponse(MockResponse):
        pass

    opener, _ = opener_for(FakeRegistryResponse(200, json.dumps(payload).encode()))
    monkeypatch.setattr(
        release,
        "PYPI_URL",
        "https://fixture.invalid/pypi",
    )
    state, missing = release.registry_decision(
        "pypi", manifest, opener=opener, sleeper=lambda _delay: None
    )
    assert state == "partial"
    assert missing == [sdist.name]


def _pypi_manifest():
    return {
        "version": "1.2.3",
        "npm_version": "1.2.3",
        "artifacts": [
            {
                "kind": "wheel",
                "name": "ai_push_hooks-1.2.3-py3-none-any.whl",
                "sha256": "a" * 64,
            },
            {
                "kind": "sdist",
                "name": "ai_push_hooks-1.2.3.tar.gz",
                "sha256": "b" * 64,
            },
        ],
    }


def _pypi_payload(manifest, names):
    return {
        "info": {"name": "ai-push-hooks", "version": manifest["version"]},
        "urls": [
            {
                "filename": name,
                "digests": {
                    "sha256": next(
                        item["sha256"]
                        for item in manifest["artifacts"]
                        if item["name"] == name
                    )
                },
            }
            for name in names
        ],
    }


def test_post_publication_verification_retries_404_then_complete(monkeypatch):
    manifest = _pypi_manifest()
    complete = json.dumps(
        _pypi_payload(manifest, [item["name"] for item in manifest["artifacts"]])
    ).encode()
    opener, remaining = opener_for(MockResponse(404), MockResponse(200, complete))
    logs = []
    monkeypatch.setattr(release, "PYPI_URL", "https://fixture.invalid/pypi")

    release.verify_registry_after_publish(
        "pypi",
        manifest,
        attempts=3,
        deadline=10,
        opener=opener,
        sleeper=lambda _delay: None,
        clock=lambda: 0.0,
        log=logs.append,
    )

    assert not remaining
    assert "state=HTTP 404" in logs[0]
    assert "exact artifact hashes matched" in logs[-1]


def test_post_publication_verification_retries_partial_then_complete(monkeypatch):
    manifest = _pypi_manifest()
    names = [item["name"] for item in manifest["artifacts"]]
    partial = json.dumps(_pypi_payload(manifest, names[:1])).encode()
    complete = json.dumps(_pypi_payload(manifest, names)).encode()
    opener, remaining = opener_for(MockResponse(200, partial), MockResponse(200, complete))
    monkeypatch.setattr(release, "PYPI_URL", "https://fixture.invalid/pypi")

    release.verify_registry_after_publish(
        "pypi",
        manifest,
        attempts=3,
        deadline=10,
        opener=opener,
        sleeper=lambda _delay: None,
        clock=lambda: 0.0,
        log=lambda _message: None,
    )

    assert not remaining


@pytest.mark.parametrize("state", ["404", "partial"])
def test_post_publication_verification_exhausts_incomplete_state(state, monkeypatch):
    manifest = _pypi_manifest()
    names = [item["name"] for item in manifest["artifacts"]]
    response = MockResponse(404) if state == "404" else MockResponse(
        200, json.dumps(_pypi_payload(manifest, names[:1])).encode()
    )
    opener, remaining = opener_for(response, response)
    monkeypatch.setattr(release, "PYPI_URL", "https://fixture.invalid/pypi")

    with pytest.raises(release.ReleaseValidationError, match="exhausted after 2 attempts"):
        release.verify_registry_after_publish(
            "pypi",
            manifest,
            attempts=2,
            deadline=10,
            opener=opener,
            sleeper=lambda _delay: None,
            clock=lambda: 0.0,
            log=lambda _message: None,
        )
    assert not remaining


def test_post_publication_verification_stops_at_deadline(monkeypatch):
    manifest = _pypi_manifest()
    now = [0.0]
    opener, remaining = opener_for(MockResponse(404), MockResponse(404))
    monkeypatch.setattr(release, "PYPI_URL", "https://fixture.invalid/pypi")

    with pytest.raises(release.ReleaseValidationError, match="exhausted after 1 attempts"):
        release.verify_registry_after_publish(
            "pypi",
            manifest,
            attempts=3,
            deadline=0.1,
            opener=opener,
            sleeper=lambda delay: now.__setitem__(0, now[0] + delay),
            clock=lambda: now[0],
            log=lambda _message: None,
        )
    assert len(remaining) == 1


@pytest.mark.parametrize(
    "response",
    [
        MockResponse(401),
        MockResponse(
            200,
            json.dumps(
                {
                    "info": {"name": "ai-push-hooks", "version": "1.2.3"},
                    "urls": [
                        {
                            "filename": "ai_push_hooks-1.2.3-py3-none-any.whl",
                            "digests": {"sha256": "wrong"},
                        }
                    ],
                }
            ).encode(),
        ),
    ],
)
def test_post_publication_verification_fails_immediately_on_auth_or_mismatch(
    response, monkeypatch
):
    manifest = _pypi_manifest()
    opener, remaining = opener_for(response, MockResponse(200))
    monkeypatch.setattr(release, "PYPI_URL", "https://fixture.invalid/pypi")

    with pytest.raises(release.ReleaseValidationError):
        release.verify_registry_after_publish(
            "pypi",
            manifest,
            attempts=3,
            deadline=10,
            opener=opener,
            sleeper=lambda _delay: None,
            clock=lambda: 0.0,
            log=lambda _message: None,
        )
    assert len(remaining) == 1


def test_registry_404_is_absent_and_not_auth_or_outage(tmp_path):
    manifest = {
        "version": "1.2.3",
        "npm_version": "1.2.3",
        "artifacts": [
            {
                "kind": "npm",
                "name": "ai-push-hooks-1.2.3.tgz",
                "sha256": "0" * 64,
                "sha512_integrity": "sha512-not-real",
            }
        ],
    }
    opener, _ = opener_for(MockResponse(404))
    state, missing = release.registry_decision(
        "npm", manifest, opener=opener, sleeper=lambda _delay: None
    )
    assert state == "absent"
    assert missing == ["ai-push-hooks-1.2.3.tgz"]


def _deployment_manifest():
    return {
        "project": "ai-push-hooks",
        "tag": "v1.2.3",
        "commit": "a" * 40,
        "version": "1.2.3",
        "npm_version": "1.2.3",
        "artifacts": [
            {
                "kind": "npm",
                "name": "ai-push-hooks-1.2.3.tgz",
                "sha256": "b" * 64,
                "sha512_integrity": "sha512-expected",
            }
        ],
    }


def test_record_deployment_validates_registry_and_writes_one_status(monkeypatch):
    manifest = _deployment_manifest()
    deployment = {
        "id": 17,
        "environment": release.DEPLOYMENT_ENVIRONMENT,
        "ref": manifest["tag"],
        "sha": manifest["commit"],
        "description": release.DEPLOYMENT_DESCRIPTION,
        "task": release.DEPLOYMENT_TASK,
    }
    writes = []
    monkeypatch.setattr(release, "registry_decision", lambda *_args: ("complete", []))
    monkeypatch.setattr(
        release,
        "_github_get",
        lambda url, _token: [] if url.endswith("?environment=npm&ref=v1.2.3&per_page=100") else [],
    )
    monkeypatch.setattr(release, "_github_write", lambda url, _token, payload: writes.append((url, payload)) or deployment)

    # A create response must also carry the exact identity before status is written.
    result = release.record_deployment(
        "owner/repo", manifest["tag"], manifest["commit"], "https://run", "token", manifest
    )
    assert result == 17
    assert len(writes) == 2
    assert writes[0][1]["task"] == release.DEPLOYMENT_TASK
    assert writes[1][1]["state"] == "success"


def test_record_deployment_rejects_mismatched_existing_deployment(monkeypatch):
    manifest = _deployment_manifest()
    writes = []
    mismatched = {
        "id": 18,
        "environment": release.DEPLOYMENT_ENVIRONMENT,
        "ref": manifest["tag"],
        "sha": "c" * 40,
        "description": release.DEPLOYMENT_DESCRIPTION,
        "task": release.DEPLOYMENT_TASK,
    }
    monkeypatch.setattr(release, "registry_decision", lambda *_args: ("complete", []))
    monkeypatch.setattr(release, "_github_get", lambda *_args: [mismatched])
    monkeypatch.setattr(release, "_github_write", lambda *args: writes.append(args))
    with pytest.raises(release.ReleaseValidationError, match="mismatched identity"):
        release.record_deployment(
            "owner/repo", manifest["tag"], manifest["commit"], "https://run", "token", manifest
        )
    assert writes == []


def test_recover_deployment_requires_exact_registry_and_deployment(monkeypatch):
    manifest = _deployment_manifest()
    deployment = {
        "id": 17,
        "environment": release.DEPLOYMENT_ENVIRONMENT,
        "ref": manifest["tag"],
        "sha": manifest["commit"],
        "description": release.DEPLOYMENT_DESCRIPTION,
        "task": release.DEPLOYMENT_TASK,
    }
    writes = []
    monkeypatch.setattr(release, "_load_release_manifest", lambda *_args: manifest)
    monkeypatch.setattr(release, "registry_decision", lambda *_args: ("complete", []))
    monkeypatch.setattr(
        release,
        "_github_get",
        lambda url, _token: deployment if url.endswith("/deployments/17") else [],
    )
    monkeypatch.setattr(release, "_github_write", lambda url, _token, payload: writes.append((url, payload)) or {})

    release.recover_deployment_status(
        "owner/repo", 17, manifest["tag"], manifest["commit"], "https://run", "token"
    )
    assert len(writes) == 1
    assert writes[0][1]["state"] == "success"


def test_recovery_manifest_is_loaded_from_matching_release_asset(monkeypatch):
    manifest = _deployment_manifest() | {"github_prerelease": False}
    body = json.dumps(manifest).encode()
    asset = {
        "name": release.MANIFEST_NAME,
        "size": len(body),
        "digest": f"sha256:{hashlib.sha256(body).hexdigest()}",
        "url": "https://api.invalid/assets/1",
    }
    monkeypatch.setattr(
        release,
        "_github_get",
        lambda *_args: {
            "tag_name": manifest["tag"],
            "draft": False,
            "prerelease": False,
            "assets": [asset],
        },
    )
    monkeypatch.setattr(
        release,
        "_request_read",
        lambda *_args, **_kwargs: release.HttpResult(200, {}, body),
    )
    assert release._load_release_manifest("owner/repo", manifest["tag"], "token") == manifest


def test_recover_deployment_rejects_registry_mismatch_without_status_write(monkeypatch):
    manifest = _deployment_manifest()
    writes = []
    monkeypatch.setattr(release, "_load_release_manifest", lambda *_args: manifest)
    monkeypatch.setattr(release, "registry_decision", lambda *_args: ("partial", ["artifact"]))
    monkeypatch.setattr(release, "_github_write", lambda *args: writes.append(args))
    with pytest.raises(release.ReleaseValidationError, match="refusing recovery"):
        release.recover_deployment_status(
            "owner/repo", 17, manifest["tag"], manifest["commit"], "https://run", "token"
        )
    assert writes == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "production"),
        ("ref", "v9.9.9"),
        ("sha", "c" * 40),
        ("description", "unrelated task"),
        ("task", "deploy"),
    ],
)
def test_recover_deployment_rejects_unrelated_identity(monkeypatch, field, value):
    manifest = _deployment_manifest()
    deployment = {
        "id": 17,
        "environment": release.DEPLOYMENT_ENVIRONMENT,
        "ref": manifest["tag"],
        "sha": manifest["commit"],
        "description": release.DEPLOYMENT_DESCRIPTION,
        "task": release.DEPLOYMENT_TASK,
    }
    deployment[field] = value
    writes = []
    monkeypatch.setattr(release, "_load_release_manifest", lambda *_args: manifest)
    monkeypatch.setattr(release, "registry_decision", lambda *_args: ("complete", []))
    monkeypatch.setattr(release, "_github_get", lambda *_args: deployment)
    monkeypatch.setattr(release, "_github_write", lambda *args: writes.append(args))
    with pytest.raises(release.ReleaseValidationError, match="deployment identity"):
        release.recover_deployment_status(
            "owner/repo", 17, manifest["tag"], manifest["commit"], "https://run", "token"
        )
    assert writes == []


def test_recover_deployment_404_fails_without_status_write(monkeypatch):
    writes = []
    monkeypatch.setattr(release, "_github_get", lambda *_args: None)
    monkeypatch.setattr(release, "_github_write", lambda *args: writes.append(args))
    with pytest.raises(release.ReleaseValidationError, match="not found"):
        release.recover_deployment_status(
            "owner/repo", 17, "v1.2.3", "a" * 40, "https://run", "token"
        )
    assert writes == []


def test_ensure_github_release_handles_404_and_uploads_manifest_once(tmp_path, monkeypatch):
    manifest_path = tmp_path / release.MANIFEST_NAME
    manifest_path.write_text("{\"tag\": \"v1.2.3\"}\n", encoding="utf-8")
    manifest = {
        "tag": "v1.2.3",
        "commit": "a" * 40,
        "github_prerelease": False,
        "artifacts": [],
    }
    writes = []
    monkeypatch.setattr(release, "validate_source", lambda *_args: None)
    monkeypatch.setattr(release, "_github_get", lambda *_args: None)
    monkeypatch.setattr(
        release,
        "_github_write",
        lambda url, _token, payload: writes.append((url, payload))
        or {
            "id": 9,
            "upload_url": "https://uploads.invalid/releases/9/assets{?name,label}",
            "assets": [],
            "tag_name": "v1.2.3",
            "draft": False,
            "prerelease": False,
        },
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        release,
        "_request_write",
        lambda *_args, **_kwargs: release.HttpResult(
            201,
            {},
            json.dumps(
                {
                    "name": release.MANIFEST_NAME,
                    "size": manifest_path.stat().st_size,
                    "digest": f"sha256:{digest}",
                }
            ).encode(),
        ),
    )
    release.ensure_github_release("owner/repo", manifest, manifest_path, "token")
    assert len(writes) == 1


@pytest.mark.parametrize("message", ["credentials", "transient"])
def test_ensure_github_release_propagates_auth_or_outage(tmp_path, monkeypatch, message):
    monkeypatch.setattr(release, "validate_source", lambda *_args: None)
    monkeypatch.setattr(
        release,
        "_github_get",
        lambda *_args: (_ for _ in ()).throw(release.ReleaseValidationError(message)),
    )
    with pytest.raises(release.ReleaseValidationError, match=message):
        release.ensure_github_release(
            "owner/repo",
            {"tag": "v1.2.3", "commit": "a" * 40, "github_prerelease": False, "artifacts": []},
            tmp_path / "checksum-manifest.json",
            "token",
        )


def test_ensure_github_release_rejects_existing_mismatch(tmp_path, monkeypatch):
    manifest_path = tmp_path / release.MANIFEST_NAME
    manifest_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(release, "validate_source", lambda *_args: None)
    monkeypatch.setattr(
        release,
        "_github_get",
        lambda *_args: {"tag_name": "v9.9.9", "draft": False, "prerelease": False},
    )
    with pytest.raises(release.ReleaseValidationError, match="wrong tag"):
        release.ensure_github_release(
            "owner/repo",
            {"tag": "v1.2.3", "commit": "a" * 40, "github_prerelease": False, "artifacts": []},
            manifest_path,
            "token",
        )
