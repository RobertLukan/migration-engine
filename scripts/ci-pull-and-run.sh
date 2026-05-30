#!/usr/bin/env bash
# Run on your Docker VM (cron/systemd timer). Clones or updates the repo, then runs tests in Docker.
#
# First-time setup:
#   export CI_REPO_DIR=/opt/migration-engine
#   git clone <internal-git-url> "$CI_REPO_DIR"
#   chmod +x scripts/ci-pull-and-run.sh
#
# Cron example (daily 06:00):
#   0 6 * * * CI_REPO_DIR=/opt/migration-engine /opt/migration-engine/scripts/ci-pull-and-run.sh >>/var/log/migration-engine-ci.log 2>&1
#
# Env:
#   CI_REPO_DIR  — path to git clone (default: parent of scripts/ when run from repo)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CI_REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
COMPOSE_FILE="$ROOT/docker-compose.ci.yml"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Expected $COMPOSE_FILE (set CI_REPO_DIR to your clone)." >&2
  exit 1
fi

cd "$ROOT"

if [[ -d .git ]]; then
  git pull --ff-only
else
  echo "Not a git clone: $ROOT (no .git). Clone the repo here first." >&2
  exit 1
fi

docker compose -f docker-compose.ci.yml build ci
docker compose -f docker-compose.ci.yml run --rm ci
