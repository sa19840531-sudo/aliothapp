#!/bin/sh
set -eu

IMAGE_TAG=${1:?用法: deploy-production.sh <image-tag>}
REPOSITORY_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
COMPOSE_FILE=${ALIOTH_COMPOSE_FILE:-$REPOSITORY_ROOT/infra/docker-compose.production.yml}
COMPOSE_ENV=${ALIOTH_COMPOSE_ENV:-/opt/alioth/config/compose.env}
COMPOSE_OVERRIDE_FILE=${ALIOTH_COMPOSE_OVERRIDE_FILE:-/opt/alioth/config/docker-compose.app-qc.yml}
BACKUP_SCRIPT=${ALIOTH_BACKUP_SCRIPT:-/opt/data-services/scripts/backup-all.sh}
HISTORY_FILE=${ALIOTH_DEPLOYMENT_HISTORY:-/opt/alioth/releases/deployment-history.log}

if [ ! -f "$COMPOSE_ENV" ]; then
  echo "缺少 Compose 配置文件: $COMPOSE_ENV" >&2
  exit 1
fi
configured_app_env=$(sed -n 's/^ALIOTH_APP_ENV_FILE=//p' "$COMPOSE_ENV" | tail -n 1)
APP_ENV=${ALIOTH_APP_ENV_FILE:-${configured_app_env:-/opt/alioth/config/alioth.env}}
if [ ! -f "$APP_ENV" ]; then
  echo "缺少应用环境文件: $APP_ENV" >&2
  exit 1
fi

update_image_tag() {
  temporary_file=$(mktemp "${COMPOSE_ENV}.XXXXXX")
  awk -v image_tag="$IMAGE_TAG" '
    BEGIN { updated = 0 }
    /^ALIOTH_IMAGE_TAG=/ {
      print "ALIOTH_IMAGE_TAG=" image_tag
      updated = 1
      next
    }
    { print }
    END {
      if (!updated) {
        print "ALIOTH_IMAGE_TAG=" image_tag
      }
    }
  ' "$COMPOSE_ENV" >"$temporary_file"
  chmod --reference="$COMPOSE_ENV" "$temporary_file"
  mv "$temporary_file" "$COMPOSE_ENV"
}

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

update_image_tag
compose --profile ops config --quiet
compose --profile ops pull

if [ -x "$BACKUP_SCRIPT" ]; then
  sudo "$BACKUP_SCRIPT"
else
  echo "数据库备份脚本不存在或不可执行: $BACKUP_SCRIPT" >&2
  exit 1
fi

# V1 发布允许短暂停机，避免不兼容迁移期间旧进程继续写入。
compose stop alioth-gateway alioth-worker alioth-api || true
compose --profile ops run --rm alioth-migrate
compose --profile ops run --rm alioth-initialize
compose up -d \
  alioth-api \
  alioth-worker \
  alioth-workstation \
  alioth-gateway

ALIOTH_COMPOSE_FILE="$COMPOSE_FILE" \
ALIOTH_COMPOSE_ENV="$COMPOSE_ENV" \
ALIOTH_COMPOSE_OVERRIDE_FILE="$COMPOSE_OVERRIDE_FILE" \
  "$REPOSITORY_ROOT/infra/scripts/verify-production.sh"

mkdir -p "$(dirname -- "$HISTORY_FILE")"
printf '%s image_tag=%s status=verified\n' "$(date -Iseconds)" "$IMAGE_TAG" >>"$HISTORY_FILE"
