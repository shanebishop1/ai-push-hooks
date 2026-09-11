#!/usr/bin/env bash
set -euo pipefail

# This smoke test deliberately builds and runs the test in a container.  The
# container receives no checkout, home directory, credential directory, SSH
# agent, or Docker socket from the host.
ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
OPENCODE_VERSION="1.18.29"

if ! command -v docker >/dev/null 2>&1; then
    printf 'BLOCKED: Docker CLI is not installed; the real OpenCode smoke test was not run.\n' >&2
    exit 2
fi
if ! docker info >/dev/null 2>&1; then
    printf 'BLOCKED: Docker daemon is unavailable; the real OpenCode smoke test was not run.\n' >&2
    exit 2
fi

context_dir="$(mktemp -d "${TMPDIR:-/tmp}/ai-push-hooks-opencode-smoke.XXXXXX")"
image="ai-push-hooks-opencode-smoke:$$"

cleanup() {
    docker image rm "$image" >/dev/null 2>&1 || true
    rm -rf "$context_dir"
}
trap cleanup EXIT

mkdir -p "$context_dir/src" "$context_dir/smoke"
cp -R "$ROOT_DIR/src/." "$context_dir/src/"
cp "$ROOT_DIR/tests/opencode_contract_smoke.py" "$context_dir/smoke/"

cat >"$context_dir/Dockerfile" <<'DOCKERFILE'
FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b

ARG OPENCODE_VERSION

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates git python3 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --no-fund --no-audit "opencode-ai@${OPENCODE_VERSION}" \
    && test "$(opencode --version)" = "${OPENCODE_VERSION}"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin smoke
COPY --chown=smoke:smoke src /opt/ai-push-hooks/src
COPY --chown=smoke:smoke smoke /opt/ai-push-hooks/smoke

USER smoke
ENV HOME=/home/smoke \
    PYTHONPATH=/opt/ai-push-hooks/src \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /tmp
ENTRYPOINT ["python3", "/opt/ai-push-hooks/smoke/opencode_contract_smoke.py"]
DOCKERFILE

docker build --build-arg "OPENCODE_VERSION=$OPENCODE_VERSION" --tag "$image" "$context_dir"

# Runtime has loopback only.  The model provider is the in-process mock server;
# no external model call can leave this container.  The root filesystem is
# read-only and all mutable state is disposable tmpfs state.
docker run --rm --init --network none --read-only \
    --cap-drop=ALL --security-opt=no-new-privileges \
    --pids-limit 256 --memory 1g --cpus 2 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777 \
    --tmpfs /home/smoke:rw,noexec,nosuid,nodev,mode=700,uid=10001,gid=10001 \
    "$image"
