#!/usr/bin/env bash
# Build + start (or just build) the UNRAIDnotUnHealthy container with version
# info baked in. Reads commit hash, total commit count, and current UTC time
# from the working tree and passes them as docker build args.
#
# Usage:
#   ./scripts/docker-build.sh                # equivalent to `up -d --build`
#   ./scripts/docker-build.sh up -d --build  # explicit
#   ./scripts/docker-build.sh build          # just build, don't start
#   ./scripts/docker-build.sh down           # stop (no rebuild)
#
# Requires: git, docker, docker compose plugin.

set -euo pipefail
cd "$(dirname "$0")/.."

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "warning: not inside a git work tree; version will report 'unknown'" >&2
  BUILD_COMMIT="unknown"
  BUILD_COMMIT_COUNT="0"
else
  BUILD_COMMIT="$(git rev-parse --short=7 HEAD)"
  BUILD_COMMIT_COUNT="$(git rev-list --count HEAD)"
fi
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

export BUILD_COMMIT BUILD_COMMIT_COUNT BUILD_TIME

# Default sub-command: `up -d --build`. Pass anything you'd normally pass to
# `docker compose` to override (e.g. `build`, `down`, `logs -f`).
ARGS=("$@")
if [[ ${#ARGS[@]} -eq 0 ]]; then
  ARGS=(up -d --build)
fi

echo ">>> UNRAIDnotUnHealthy build ${BUILD_COMMIT_COUNT}+${BUILD_COMMIT} (${BUILD_TIME})"
echo ">>> docker compose ${ARGS[*]}"
docker compose "${ARGS[@]}"

# Reclaim disk from dangling (untagged) images left over from rebuilds.
# Each `up --build` re-tags the image to the new build, orphaning the
# previous one. Filter the prune by the OCI title label set in the
# Dockerfile so cleanup is scoped strictly to OUR project's images.
case " ${ARGS[*]} " in
  *" build "*|*" up "*|*" --build "*)
    out="$(docker image prune -f \
      --filter "label=org.opencontainers.image.title=UNRAIDnotUnHealthy" 2>&1 || true)"
    reclaimed="$(echo "$out" | grep -oE 'Total reclaimed space:.*' | head -1)"
    [[ -n "$reclaimed" ]] && echo ">>> ${reclaimed}"
    ;;
esac
