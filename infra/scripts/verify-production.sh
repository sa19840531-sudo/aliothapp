#!/bin/sh
set -eu

REPOSITORY_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
COMPOSE_FILE=${ALIOTH_COMPOSE_FILE:-$REPOSITORY_ROOT/infra/docker-compose.production.yml}
COMPOSE_ENV=${ALIOTH_COMPOSE_ENV:-/opt/alioth/config/compose.env}
COMPOSE_OVERRIDE_FILE=${ALIOTH_COMPOSE_OVERRIDE_FILE:-/opt/alioth/config/docker-compose.app-qc.yml}

if [ ! -f "$COMPOSE_ENV" ]; then
  echo "缺少 Compose 配置文件: $COMPOSE_ENV" >&2
  exit 1
fi

compose() {
  if [ -f "$COMPOSE_OVERRIDE_FILE" ]; then
    docker compose \
      --env-file "$COMPOSE_ENV" \
      -f "$COMPOSE_FILE" \
      -f "$COMPOSE_OVERRIDE_FILE" \
      "$@"
    return
  fi
  docker compose \
    --env-file "$COMPOSE_ENV" \
    -f "$COMPOSE_FILE" \
    "$@"
}

gateway_port=$(sed -n 's/^ALIOTH_GATEWAY_HTTP_PORT=//p' "$COMPOSE_ENV" | tail -n 1)
gateway_port=${gateway_port:-80}
base_url="http://127.0.0.1:${gateway_port}"

compose ps

attempt=1
while [ "$attempt" -le 24 ]; do
  if curl --fail --silent --show-error --max-time 5 "$base_url/api/v1/readyz" >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 24 ]; then
    echo "API 就绪检查超时: $base_url/api/v1/readyz" >&2
    compose logs --tail=200 alioth-api alioth-gateway >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 5
done

for container_name in \
  alioth-api \
  alioth-worker \
  alioth-workstation \
  alioth-gateway; do
  state=$(docker inspect "$container_name" --format '{{.State.Status}}')
  if [ "$state" != "running" ]; then
    echo "容器未运行: $container_name state=$state" >&2
    exit 1
  fi
done

echo "Alioth 基础部署验收通过。"
