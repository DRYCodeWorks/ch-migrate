#!/usr/bin/env bash
# Run the access workflow from the built wheel alone, against a ClickHouse 26.3 container
# this script owns.
#
#   scripts/clean_wheel_access.sh
#
# 1. Builds the wheel from this checkout.
# 2. Creates a venv in a temporary directory and installs only that wheel, with its
#    dependencies pinned to uv.lock. Nothing is imported from the checkout.
# 3. Starts its own clickhouse/clickhouse-server:26.3 container on a random loopback port.
# 4. In a new project: init, bootstrap, new, up; access plan (0); adds a role and a user
#    holding it to access.yaml; access plan (1); access apply; access plan (0);
#    access check (0). Every exit code is checked.
# 5. Removes the container and its volumes with `docker rm -fv`, even on failure.
#
# Exits non-zero at the first step that fails. Needs Docker and uv.
set -euo pipefail

IMAGE="clickhouse/clickhouse-server:26.3"
PYTHON_VERSION="3.10"
# How long the server has to answer once its container starts, and the bounds on each
# readiness request within that.
READY_SECONDS=60
CONNECT_SECONDS=2
REQUEST_SECONDS=5
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/chm-wheel.XXXXXX")" && pwd -P)
CONTAINER="chm-wheel-${WORK##*.}"
VENV="$WORK/venv"
PROJECT="$WORK/project"

cleanup() {
  docker rm -fv "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

step() {
  echo "==> $*"
}

secret() {
  od -An -tx1 -N16 /dev/urandom | tr -d ' \n'
}

build_and_install() {
  step "Building the wheel"
  (cd "$REPO" && uv build -q --wheel --out-dir "$WORK/dist")
  local wheels=("$WORK"/dist/*.whl)
  [ "${#wheels[@]}" -eq 1 ] || { echo "Expected one wheel, found ${#wheels[@]}" >&2; exit 1; }
  step "Installing $(basename "${wheels[0]}") alone into a Python $PYTHON_VERSION venv"
  (cd "$REPO" && uv export -q --locked --no-dev --no-emit-project --no-hashes \
    --output-file "$WORK/constraints.txt")
  uv venv -q --python "$PYTHON_VERSION" "$VENV"
  uv pip install -q --python "$VENV/bin/python" --constraint "$WORK/constraints.txt" \
    "${wheels[0]}"
  "$VENV/bin/python" -I - "$VENV" <<'PY'
import os
import sys

import ch_migrate

where = os.path.realpath(ch_migrate.__file__)
if not where.startswith(os.path.realpath(sys.argv[1]) + os.sep):
    sys.exit(f"ch_migrate was imported from {where}, outside the venv")
print(f"ch_migrate {ch_migrate.__version__} from {os.path.dirname(where)}")
PY
}

start_server() {
  step "Starting $IMAGE as $CONTAINER"
  CLICKHOUSE_PASSWORD="$ADMIN_PASSWORD" docker run --detach --name "$CONTAINER" \
    --publish 127.0.0.1::8123 --env CLICKHOUSE_PASSWORD \
    --env CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 "$IMAGE" >/dev/null
  local address
  address=$(docker port "$CONTAINER" 8123/tcp | head -n 1)
  PORT=${address##*:}
  wait_for_server
}

# Ask the server for its version until it answers, for up to READY_SECONDS. Every curl is
# bounded: a server that accepts a connection but never answers can't hold the loop past
# its deadline, or keep the EXIT trap from removing the container.
wait_for_server() {
  local version left deadline=$((SECONDS + READY_SECONDS))
  while left=$((deadline - SECONDS)); [ "$left" -gt 0 ]; do
    if version=$(curl -sf --connect-timeout "$CONNECT_SECONDS" \
      --max-time "$((left < REQUEST_SECONDS ? left : REQUEST_SECONDS))" \
      -u "default:$ADMIN_PASSWORD" --data-binary "SELECT version()" "http://127.0.0.1:$PORT/"); then
      echo "ClickHouse $version on 127.0.0.1:$PORT"
      return
    fi
    sleep 0.5
  done
  echo "ClickHouse wasn't ready in $READY_SECONDS s" >&2
  exit 1
}

# Run the installed ch-migrate in the project, with nothing inherited from the caller's
# environment, so no CH_* variable or PYTHONPATH can leak in.
chm() {
  (cd "$PROJECT" && env -i PATH="$VENV/bin:/usr/bin:/bin" HOME="$WORK" PYTHONUTF8=1 \
    NO_COLOR=1 "$VENV/bin/ch-migrate" "$@")
}

# expect CODE ARGS...: run `ch-migrate ARGS...` and fail unless it exits CODE.
expect() {
  local want=$1 got=0
  shift
  step "ch-migrate $* (expect exit $want)"
  chm "$@" || got=$?
  if [ "$got" -ne "$want" ]; then
    echo "FAIL: ch-migrate $* exited $got, expected $want" >&2
    exit 1
  fi
}

write_project_files() {
  cat >"$PROJECT/config.yaml" <<EOF
project:
  name: wheel_check

defaults:
  port: $PORT
  secure: false
  admin_user: default

environments:
  dev:
    host: 127.0.0.1
    database: wheel_check_dev
    migration_user: migration_dev
EOF
  cat >"$PROJECT/.env.local" <<EOF
CH_DEV_ADMIN_PASSWORD=$ADMIN_PASSWORD
CH_DEV_MIGRATION_PASSWORD=$(secret)
CH_DEV_DASHBOARDS_PASSWORD=$(secret)
EOF
}

fill_migration() {
  local up down
  up=$(find "$PROJECT/migrations/sql" -name '*.up.sql')
  down=$(find "$PROJECT/migrations/sql" -name '*.down.sql')
  [ -f "$up" ] && [ -f "$down" ] || { echo "Expected one .up.sql and one .down.sql" >&2; exit 1; }
  cat >"$up" <<'EOF'
CREATE TABLE IF NOT EXISTS {db}.logs (id UInt64)
ENGINE = MergeTree ORDER BY id;
ALTER TABLE {db}.logs ADD COLUMN IF NOT EXISTS status String;
EOF
  echo 'DROP TABLE IF EXISTS {db}.logs;' >"$down"
}

declare_role_and_user() {
  step "Adding a role and a user holding it to access.yaml"
  cat >>"$PROJECT/access.yaml" <<'EOF'

roles:
  analyst:
    grants:
      - SELECT, SHOW TABLES ON {db}.*

users:
  dashboards:
    password: {secret: dashboards_password}
    roles: [analyst]
EOF
}

main() {
  ADMIN_PASSWORD=$(secret)
  build_and_install
  start_server
  mkdir "$PROJECT"
  expect 0 init --name wheel_check
  write_project_files
  expect 0 bootstrap dev
  expect 0 new dev add_status --table logs
  fill_migration
  expect 0 up dev
  expect 0 access plan dev
  declare_role_and_user
  expect 1 access plan dev
  expect 0 access apply dev
  expect 0 access plan dev
  expect 0 access check dev
  step "Every step exited as expected"
}

main "$@"
