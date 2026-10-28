#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# != 1 || ! $1 =~ ^[0-9a-fA-F-]{36}$ ]]; then
  echo '用法: bash deploy/recover-session.sh 日志中无法恢复的会话ID' >&2
  exit 2
fi
if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null && [[ $(docker-compose version --short | sed 's/^v//' | cut -d. -f1) -ge 2 ]]; then
  compose=(docker-compose)
else
  echo '需要 Docker Compose v2。' >&2; exit 1
fi
compose+=(--project-directory "$repo" --env-file "$repo/.env.docker" -f "$repo/compose.yaml")
"${compose[@]}" stop ingress gateway
if ! "${compose[@]}" run --rm -T --no-deps --user 10001:10001 --entrypoint python gateway - "$1" < "$repo/deploy/recover_session.py"; then
  echo '恢复未执行；服务已停止，原配置及会话数据均未删除。' >&2
  exit 1
fi
"${compose[@]}" up -d
printf '%s\n' '服务已重新启动，请等待几十秒后运行：bash deploy/docker.sh health'
