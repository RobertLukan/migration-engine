#!/usr/bin/env bash
# Build dist-offline/ migration-engine offline package (linux/amd64) for lab deploy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="${MIGRATION_ENGINE_VERSION:-2.2.2}"
REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIST="$ROOT/dist-offline"
BUNDLE="$DIST/migration-bundle-amd64.tar"
PKG="$DIST/migration-engine-offline-v${VERSION}-amd64.tar.gz"

mkdir -p "$DIST"

echo "==> Pulling ghcr.io/robertlukan/migration-engine:${VERSION} (linux/amd64)"
if docker pull --platform linux/amd64 "ghcr.io/robertlukan/migration-engine:${VERSION}"; then
  docker tag "ghcr.io/robertlukan/migration-engine:${VERSION}" migration-engine:latest
  docker tag "ghcr.io/robertlukan/migration-engine:${VERSION}" "migration-engine:${VERSION}"
else
  echo "==> GHCR pull failed; building locally (linux/amd64 via compose platform)"
  docker compose build \
    --build-arg "MIGRATION_ENGINE_VERSION=${VERSION}" \
    --build-arg "MIGRATION_ENGINE_GIT_REVISION=${REV}"
  docker tag migration-engine:latest "migration-engine:${VERSION}"
fi

echo "==> Pulling redis:7-alpine (linux/amd64)"
docker pull --platform linux/amd64 redis:7-alpine

echo "==> Architecture check"
docker image inspect migration-engine:latest --format 'migration-engine:latest arch={{.Architecture}}'
docker image inspect redis:7-alpine --format 'redis:7-alpine arch={{.Architecture}}'

echo "==> Saving ${BUNDLE}"
rm -f "$BUNDLE"
docker save migration-engine:latest "migration-engine:${VERSION}" redis:7-alpine -o "$BUNDLE"
chmod 600 "$BUNDLE"

cp -f "$ROOT/docker-compose.yml" "$DIST/docker-compose.yml"
cp -f "$ROOT/config.docker.example.yaml" "$DIST/config.docker.example.yaml"

cat > "$DIST/INSTALL.txt" <<EOF
Migration Engine offline deploy (linux/amd64)
Build: v${VERSION} (suite footer, no WinRM/Horizon, import-metadata firmware/resources)
Git: ${REV}

This package is for an x86_64 Linux host. Do not load it on ARM.
Do not docker commit helper containers into migration-engine:latest.

Contents
  migration-bundle-amd64.tar   Images: migration-engine:latest, migration-engine:${VERSION}, redis:7-alpine (amd64)
  docker-compose.yml           API + worker + Redis (entrypoint pinned)
  config.docker.example.yaml   Template only

On the production host (clean start)
  1. Keep your existing config.docker.yaml. Do not overwrite it with the example.
     Required in that file:
       redis.url: redis://redis:6379/0
       proxmox.host: <real PVE address, not 10.0.0.2>
       ui.password and ui.session_secret (>=16 chars)
     Optional firmware defaults:
       proxmox.default_firmware: uefi|seabios
       proxmox.default_guest_profile: windows|linux
       proxmox.default_enable_vtpm: null|true|false  (null = UEFI+windows only)
  2. Replace docker-compose.yml with the one from this package.
  3. Wipe the old stack and load this bundle:

       docker compose down --remove-orphans || true
       docker rm -f \$(docker ps -aq --filter name=migration) 2>/dev/null || true
       docker image rm migration-engine:latest migration-engine:${VERSION} redis:7-alpine 2>/dev/null || true
       docker load -i migration-bundle-amd64.tar
       docker image inspect redis:7-alpine --format 'arch={{.Architecture}}'
       docker image inspect migration-engine:latest --format 'arch={{.Architecture}}'
       # both must print amd64

       # if you do not yet have config.docker.yaml:
       #   cp config.docker.example.yaml config.docker.yaml && edit it
       docker compose up -d
       docker compose ps
       docker compose logs --tail 20 worker
       curl -sS --max-time 5 http://127.0.0.1:8000/version
       # expect "version": "${VERSION}"

Worker should stay Up (not Restarting) and log: Worker up (v2):
API should be Up (healthy).

Never run docker commit on these images.
EOF

echo "==> Packing ${PKG}"
rm -f "$PKG"
tar -C "$DIST" -czf "$PKG" \
  INSTALL.txt \
  docker-compose.yml \
  config.docker.example.yaml \
  migration-bundle-amd64.tar

ls -lah "$BUNDLE" "$PKG"
echo "DONE"
echo "Copy to offline host: $PKG"
echo "Or use folder: $DIST"
