#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_BRANCH="dev"
DEPLOY_BRANCH="$DEFAULT_BRANCH"
DEPLOY_COMMIT=""

usage() {
  cat <<'EOF'
用法:
  redeploy-local-production.sh [branch]
  redeploy-local-production.sh --branch <branch> [--commit <full-sha>]

从远端同步指定分支，在部署服务器本地构建 Commit SHA 镜像，并依次执行：
备份、Migration、初始化、服务重建、基础验收和发布历史记录。

指定 --commit 时，仅允许部署属于远端目标分支的完整 40 位 Commit SHA。

环境变量:
  ALIOTH_FORCE_REDEPLOY=1  当前 Commit 未变化时仍重新部署
  ALIOTH_BUILD_PULL=1      构建镜像时拉取最新基础镜像
  ALIOTH_COMPOSE_OVERRIDE_FILE  可选 Compose 覆盖文件路径
EOF
}

if [[ $# -eq 1 && "$1" != -* ]]; then
  DEPLOY_BRANCH=$1
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)
      if [[ $# -lt 2 ]]; then
        echo "--branch 缺少参数。" >&2
        usage >&2
        exit 2
      fi
      DEPLOY_BRANCH=$2
      shift 2
      ;;
    --commit)
      if [[ $# -lt 2 ]]; then
        echo "--commit 缺少参数。" >&2
        usage >&2
        exit 2
      fi
      DEPLOY_COMMIT=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$DEPLOY_BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] ||
  [[ "$DEPLOY_BRANCH" == *".."* ]]; then
  echo "非法部署分支: $DEPLOY_BRANCH" >&2
  exit 2
fi

if [[ -n "$DEPLOY_COMMIT" && ! "$DEPLOY_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "--commit 必须是完整的 40 位十六进制 Commit SHA。" >&2
  exit 2
fi

REPOSITORY_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
COMPOSE_FILE=${ALIOTH_COMPOSE_FILE:-$REPOSITORY_ROOT/infra/docker-compose.production.yml}
COMPOSE_ENV=${ALIOTH_COMPOSE_ENV:-/opt/alioth/config/compose.env}
COMPOSE_OVERRIDE_FILE=${ALIOTH_COMPOSE_OVERRIDE_FILE:-/opt/alioth/config/docker-compose.app-qc.yml}
BACKUP_SCRIPT=${ALIOTH_BACKUP_SCRIPT:-/opt/data-services/scripts/backup-all.sh}
HISTORY_FILE=${ALIOTH_DEPLOYMENT_HISTORY:-/opt/alioth/releases/deployment-history.log}
LOCK_FILE=${ALIOTH_DEPLOYMENT_LOCK:-/tmp/alioth-production-deploy.lock}
VERIFY_SCRIPT=$REPOSITORY_ROOT/infra/scripts/verify-production.sh
IMAGE_CONFIG_CHECK_SCRIPT=$REPOSITORY_ROOT/infra/scripts/validate-compose-images.py

CURRENT_STAGE="前置检查"
OLD_TAG=""
NEW_TAG=""

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "缺少部署命令: $1" >&2
    exit 1
  fi
}

read_env_value() {
  local key=$1
  sed -n "s/^${key}=//p" "$COMPOSE_ENV" | tail -n 1
}

update_image_tag() {
  local image_tag=$1
  local temporary_file
  temporary_file=$(mktemp "${COMPOSE_ENV}.XXXXXX")
  awk -v image_tag="$image_tag" '
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
  # 保留原配置权限，不依赖仅 GNU chmod 支持的 --reference。
  python3 -c 'import os, stat, sys; os.chmod(sys.argv[2], stat.S_IMODE(os.stat(sys.argv[1]).st_mode))' \
    "$COMPOSE_ENV" "$temporary_file"
  mv "$temporary_file" "$COMPOSE_ENV"
}

compose() {
  if [[ -f "$COMPOSE_OVERRIDE_FILE" ]]; then
    ALIOTH_IMAGE_TAG="$NEW_TAG" CI_REGISTRY_IMAGE="$IMAGE_PREFIX" docker compose \
      --env-file "$COMPOSE_ENV" \
      -f "$COMPOSE_FILE" \
      -f "$COMPOSE_OVERRIDE_FILE" \
      "$@"
    return
  fi
  ALIOTH_IMAGE_TAG="$NEW_TAG" CI_REGISTRY_IMAGE="$IMAGE_PREFIX" docker compose \
    --env-file "$COMPOSE_ENV" \
    -f "$COMPOSE_FILE" \
    "$@"
}

validate_image_config() {
  compose --profile ops config --format json |
    python3 -c "$IMAGE_CONFIG_CHECK_CODE" \
      --image-prefix "$IMAGE_PREFIX" \
      --image-tag "$NEW_TAG"
}

deployed_images_match() {
  local container_name expected_image actual_image
  while read -r container_name expected_image; do
    if ! actual_image=$(docker inspect "$container_name" --format '{{.Config.Image}}'); then
      echo "无法读取当前容器镜像: $container_name" >&2
      return 1
    fi
    if [[ "$actual_image" != "$expected_image" ]]; then
      echo "容器镜像版本不一致: $container_name expected=$expected_image actual=$actual_image" >&2
      return 1
    fi
  done <<EOF
alioth-api $IMAGE_PREFIX/api:$NEW_TAG
alioth-worker $IMAGE_PREFIX/api:$NEW_TAG
alioth-workstation $IMAGE_PREFIX/workstation:$NEW_TAG
alioth-gateway $IMAGE_PREFIX/gateway:$NEW_TAG
EOF
}

record_history() {
  local status=$1
  mkdir -p "$(dirname -- "$HISTORY_FILE")"
  printf '%s old=%s image_tag=%s status=%s stage=%s\n' \
    "$(date -Iseconds)" \
    "${OLD_TAG:-none}" \
    "${NEW_TAG:-none}" \
    "$status" \
    "${CURRENT_STAGE// /_}" \
    >>"$HISTORY_FILE"
}

on_error() {
  local exit_code=$?
  trap - ERR
  record_history "failed"
  echo "部署失败: stage=${CURRENT_STAGE} exit_code=${exit_code}" >&2
  if [[ -n "$OLD_TAG" && "$OLD_TAG" != "$NEW_TAG" ]]; then
    if [[ -f "$COMPOSE_OVERRIDE_FILE" ]]; then
      cat >&2 <<EOF
上一版本镜像仍保留。确认当前数据库结构兼容旧版本后，可执行：
  sed -i 's/^ALIOTH_IMAGE_TAG=.*/ALIOTH_IMAGE_TAG=${OLD_TAG}/' ${COMPOSE_ENV}
  docker compose --env-file ${COMPOSE_ENV} -f ${COMPOSE_FILE} -f ${COMPOSE_OVERRIDE_FILE} up -d --force-recreate --wait --wait-timeout 240 alioth-api alioth-worker alioth-workstation alioth-gateway
EOF
    else
      cat >&2 <<EOF
上一版本镜像仍保留。确认当前数据库结构兼容旧版本后，可执行：
  sed -i 's/^ALIOTH_IMAGE_TAG=.*/ALIOTH_IMAGE_TAG=${OLD_TAG}/' ${COMPOSE_ENV}
  docker compose --env-file ${COMPOSE_ENV} -f ${COMPOSE_FILE} up -d --force-recreate --wait --wait-timeout 240 alioth-api alioth-worker alioth-workstation alioth-gateway
EOF
    fi
  fi
  exit "$exit_code"
}

trap on_error ERR

if [[ $(id -u) -eq 0 ]]; then
  echo "禁止使用 root 执行本脚本，请使用 sudo -u alioth -H。" >&2
  exit 1
fi

for command_name in git docker curl awk sed flock sudo python3; do
  require_command "$command_name"
done

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "已有 Alioth 发布任务正在执行: $LOCK_FILE" >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_ENV" ]]; then
  echo "缺少 Compose 配置文件: $COMPOSE_ENV" >&2
  exit 1
fi
if [[ ! -w "$COMPOSE_ENV" ]]; then
  echo "部署用户无法更新 Compose 配置文件: $COMPOSE_ENV" >&2
  exit 1
fi
if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "缺少生产 Compose 文件: $COMPOSE_FILE" >&2
  exit 1
fi
if [[ ! -x "$VERIFY_SCRIPT" ]]; then
  echo "基础验收脚本不存在或不可执行: $VERIFY_SCRIPT" >&2
  exit 1
fi
if [[ ! -r "$IMAGE_CONFIG_CHECK_SCRIPT" ]]; then
  echo "编排镜像校验脚本不存在或不可读: $IMAGE_CONFIG_CHECK_SCRIPT" >&2
  exit 1
fi
# 固定本次部署脚本的校验器，切换到较早 Commit 时不能丢失校验代码。
IMAGE_CONFIG_CHECK_CODE=$(cat "$IMAGE_CONFIG_CHECK_SCRIPT")

cd "$REPOSITORY_ROOT"
if [[ -n $(git status --porcelain) ]]; then
  echo "部署目录存在未提交修改，禁止覆盖：" >&2
  git status --short >&2
  exit 1
fi

OLD_TAG=$(read_env_value ALIOTH_IMAGE_TAG)
IMAGE_PREFIX=$(read_env_value CI_REGISTRY_IMAGE)
APP_ENV=$(read_env_value ALIOTH_APP_ENV_FILE)
APP_ENV=${APP_ENV:-/opt/alioth/config/alioth.env}

if [[ -z "$OLD_TAG" || -z "$IMAGE_PREFIX" ]]; then
  echo "compose.env 必须配置 ALIOTH_IMAGE_TAG 和 CI_REGISTRY_IMAGE。" >&2
  exit 1
fi
if [[ ! -r "$APP_ENV" ]]; then
  echo "部署用户无法读取应用环境文件: $APP_ENV" >&2
  exit 1
fi

CURRENT_STAGE="同步代码"
REMOTE_BRANCH_REF="refs/remotes/origin/$DEPLOY_BRANCH"
git fetch origin \
  "+refs/heads/$DEPLOY_BRANCH:$REMOTE_BRANCH_REF"

if [[ -n "$DEPLOY_COMMIT" ]]; then
  NEW_TAG=$(git rev-parse --verify "$DEPLOY_COMMIT^{commit}")
  if ! git merge-base --is-ancestor "$NEW_TAG" "$REMOTE_BRANCH_REF"; then
    echo "指定 Commit 不属于远端分支 origin/$DEPLOY_BRANCH: $NEW_TAG" >&2
    exit 1
  fi
else
  NEW_TAG=$(git rev-parse --verify "$REMOTE_BRANCH_REF^{commit}")
fi

git checkout --detach "$NEW_TAG"

CURRENT_STAGE="校验编排镜像版本"
validate_image_config

if [[ "$OLD_TAG" == "$NEW_TAG" && "${ALIOTH_FORCE_REDEPLOY:-0}" != "1" ]]; then
  if deployed_images_match; then
    echo "当前部署已是最新版本: $NEW_TAG"
    echo "如需强制重建，请设置 ALIOTH_FORCE_REDEPLOY=1。"
    exit 0
  fi
  echo "配置标签未变，但实际容器镜像尚未一致，继续重建服务。"
fi

CURRENT_STAGE="校验编排"
compose --profile ops config --quiet

CURRENT_STAGE="构建镜像"
BUILD_OPTIONS=(--progress=plain)
if [[ "${ALIOTH_BUILD_PULL:-0}" == "1" ]]; then
  BUILD_OPTIONS+=(--pull)
fi

docker build "${BUILD_OPTIONS[@]}" \
  -f infra/docker/api/Dockerfile.production \
  -t "$IMAGE_PREFIX/api:$NEW_TAG" .
docker build "${BUILD_OPTIONS[@]}" \
  -f infra/docker/workstation/Dockerfile \
  -t "$IMAGE_PREFIX/workstation:$NEW_TAG" .
docker build "${BUILD_OPTIONS[@]}" \
  -f infra/docker/gateway/Dockerfile \
  -t "$IMAGE_PREFIX/gateway:$NEW_TAG" .

CURRENT_STAGE="发布前备份"
sudo -n "$BACKUP_SCRIPT"

CURRENT_STAGE="切换镜像标签"
update_image_tag "$NEW_TAG"
compose --profile ops config --quiet
validate_image_config

CURRENT_STAGE="停止写入服务"
compose stop alioth-gateway alioth-worker alioth-api

CURRENT_STAGE="数据库迁移"
compose --profile ops run --rm alioth-migrate

CURRENT_STAGE="初始化业务数据"
compose --profile ops run --rm alioth-initialize

CURRENT_STAGE="重建常驻服务"
compose up -d \
  --force-recreate \
  --wait \
  --wait-timeout 240 \
  alioth-api \
  alioth-worker \
  alioth-workstation \
  alioth-gateway

CURRENT_STAGE="校验容器镜像版本"
if ! deployed_images_match; then
  exit 1
fi

CURRENT_STAGE="基础部署验收"
ALIOTH_IMAGE_TAG="$NEW_TAG" \
CI_REGISTRY_IMAGE="$IMAGE_PREFIX" \
ALIOTH_COMPOSE_FILE="$COMPOSE_FILE" \
ALIOTH_COMPOSE_ENV="$COMPOSE_ENV" \
ALIOTH_COMPOSE_OVERRIDE_FILE="$COMPOSE_OVERRIDE_FILE" \
  "$VERIFY_SCRIPT"

CURRENT_STAGE="完成"
record_history "base_verified"

echo "Alioth 手工重部署完成。"
echo "OLD_TAG=$OLD_TAG"
echo "NEW_TAG=$NEW_TAG"
echo "真实 Workstation 任务仍需单独验收。"
