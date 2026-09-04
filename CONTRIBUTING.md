# Contributing

Thanks for contributing to ai-push-hooks.

## Before opening a change

- Use an issue to discuss substantial features or behavior changes first.
- Use a [private security advisory](https://github.com/shanebishop1/ai-push-hooks/security/advisories/new), not an issue, for vulnerabilities.
- Keep changes focused and preserve compatibility with Python 3.10–3.13 and Node.js 18+ for the npm wrapper.
- Never include provider credentials, private repository content, generated transcripts, or `.git/ai-push-hooks/` runtime data.

## Development

From a clone with Python and [uv](https://docs.astral.sh/uv/) installed:

```bash
uv run --no-project --with pytest pytest tests -q
```

Validate both distribution surfaces before submitting package or wrapper changes:

```bash
uv run --no-project --with build python -m build
uv run --no-project --with twine python -m twine check dist/*
npm run test:npm-pack
```

Update documentation and `CHANGELOG.md` for user-visible behavior. A pull request should explain the motivation and security/compatibility impact and list exact validation commands and results. By contributing, you agree that your contribution is licensed under the repository's MIT license.
