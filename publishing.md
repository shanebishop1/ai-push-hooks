# Publishing with GitHub Actions + Trusted Publishers

This guide is a reusable setup for shipping packages from GitHub Actions to:

- PyPI only
- npm only
- both PyPI and npm

It is written to avoid long-lived publish tokens and use trusted publishing (OIDC) where possible.

## Goals

- Publish from tagged releases in GitHub Actions.
- Use trusted publisher connections instead of static API tokens.
- Keep one source of truth for versions and release flow.

## Prerequisites

- A GitHub repository with Actions enabled.
- A package that is ready to publish (metadata complete, tests passing).
- Account access on the target registry (PyPI and/or npm).

For npm trusted publishing, use:

- Node >= 22.14.0
- npm CLI >= 11.5.1

Using Node 24 in CI is a safe default.

To avoid GitHub's Node 20 deprecation path for JavaScript actions, set this workflow env:

```yaml
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"
```

This keeps marketplace JavaScript actions (for example `actions/checkout`) on Node 24 runtime.

## High-level flow

1. Bump package version(s).
2. Commit changes.
3. Create and push a tag (for example `v1.2.3`).
4. GitHub Actions validates versions, runs tests, builds artifacts, and publishes.

## Trusted publisher setup

### PyPI trusted publishing

1. Go to PyPI account settings and open publishing/trusted publisher configuration.
2. Add the GitHub repository and workflow mapping.
3. If your project does not exist yet on PyPI, use the pending/new-project trusted publisher flow.
4. In GitHub Actions, ensure the publish job has `id-token: write` permission.

Notes:

- No PyPI API token is required for trusted publishing.
- Workflow and repo values must match exactly.

### npm trusted publishing

1. In npm package settings, add a trusted publisher for GitHub Actions.
2. Configure exact values:
   - GitHub owner/user
   - repository name
   - workflow filename (for example `release.yml`)
   - environment name only if you actually use GitHub Environments
3. In GitHub Actions, publish with OIDC and provenance.

Notes:

- `Environment name` is optional in npm unless you use that environment in your workflow.
- Workflow filename match is strict and case-sensitive.
- Remove legacy publish secrets once trusted publishing works to avoid confusion.

## Required package metadata

### npm

If you publish with provenance, include repository metadata in `package.json`:

```json
{
  "repository": {
    "type": "git",
    "url": "https://github.com/OWNER/REPO"
  }
}
```

The URL must match the provenance source repository. Mismatches can fail publish with provenance validation errors.

### PyPI

Ensure `pyproject.toml` metadata is complete and valid (name, version, requires-python, description/readme, license, etc.).

## Recommended GitHub Actions workflow pattern

Use a tag-triggered workflow:

- Trigger on tags like `v*`.
- Validate tag version equals package version(s).
- Run tests.
- Build artifacts.
- Publish to registries in separate jobs.

Recommended permissions:

- top-level: `contents: read`, `id-token: write`
- publish jobs: explicitly include `id-token: write`

### npm publish step (trusted publishing)

Use Node 24 and OIDC publish:

```yaml
- uses: actions/setup-node@v6
  with:
    node-version: "24"
    registry-url: "https://registry.npmjs.org"

- run: npm publish --access public --provenance
```

### PyPI publish step (trusted publishing)

Use the PyPA action with OIDC:

```yaml
permissions:
  id-token: write

steps:
  - uses: pypa/gh-action-pypi-publish@release/v1
```

## Versioning for dual-publish projects

If you publish to both npm and PyPI from one repo, keep versions synchronized:

- `package.json` version == `pyproject.toml` version == tag version without `v`

Example:

- `package.json`: `1.4.2`
- `pyproject.toml`: `1.4.2`
- git tag: `v1.4.2`

Fail fast in CI if these do not match.

## First release checklist

- Trusted publisher configured on each registry.
- Workflow filename exactly matches registry config.
- OIDC permissions enabled in workflow.
- Package metadata complete (especially npm `repository.url` when using provenance).
- Package name availability confirmed.
- Tests and build pass on CI.

## Troubleshooting

### npm: `ENEEDAUTH`

Usually means trusted publisher mapping is not being used.

Check:

- Node/npm versions meet trusted publishing requirements.
- Trusted publisher values exactly match owner/repo/workflow.
- You configured trusted publisher at the package level where required.
- `id-token: write` is present.

### npm: `E404 Not Found - PUT ...`

Common causes:

- Trusted publisher not correctly linked to the package.
- Package ownership/visibility mismatch.
- Provenance/repository mismatch.

If provenance logs appear but publish still fails, verify `package.json` repository URL exactly matches the source repo.

### npm: `E422 ... Error verifying sigstore provenance bundle`

Usually a provenance metadata mismatch.

Check:

- `package.json` has valid `repository.url`.
- URL matches GitHub repo used by the workflow.

### PyPI publish fails while npm succeeds (or vice versa)

Treat publish jobs independently. Keep separate publish jobs so one registry outage does not block diagnostics for the other.

## Migration from token-based publishing

1. Set up trusted publishers.
2. Verify a full release succeeds.
3. Remove old publish tokens/secrets.
4. Optionally enforce stricter package security settings (for example disallow token-based publish).

## Minimal release command sequence

```bash
git add .
git commit -m "chore: release vX.Y.Z"
git tag vX.Y.Z
git push
git push --tags
```

## Practical advice

- Start with one registry, then add the second.
- Keep publish logic boring and deterministic.
- Prefer strict version checks in CI.
- Keep release workflows small and explicit.
- When debugging trusted publishing, inspect failed action logs first; they usually show whether auth, provenance, or metadata failed.
