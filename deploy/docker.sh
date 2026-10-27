#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE="$REPO/.env.docker"
usage() {
  echo 'Usage: bash deploy/docker.sh up --ip PUBLIC_IPV4 [--https-port 8448] [--console-port 8787] [--model small|base]'
  echo '       bash deploy/docker.sh {login|status|logs|health|console|stop|export FILE}'
}
command=${1:-help}
[[ $# == 0 ]] || shift
if [[ $command == help || $command == --help || $command == -h ]]; then usage; exit 0; fi
command -v docker >/dev/null || { echo 'Docker Engine and Docker Compose v2 must already be installed.' >&2; exit 1; }
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_version=$(docker-compose version --short 2>/dev/null || true)
  compose_version=${compose_version#v}
  compose_major=${compose_version%%.*}
  if [[ $compose_major =~ ^[0-9]+$ ]] && (( compose_major >= 2 )); then
    COMPOSE=(docker-compose)
  else
    echo 'Legacy docker-compose v1 is unsupported. On Ubuntu 24.04: sudo apt-get update && sudo apt-get install docker-compose-v2' >&2
    exit 1
  fi
else
  echo 'Docker is present but Compose is missing.' >&2
  echo 'On Ubuntu 24.04: sudo apt-get update && sudo apt-get install -y docker-compose-v2' >&2
  echo 'For Docker official APT repositories, the package is docker-compose-plugin.' >&2
  exit 1
fi
docker info >/dev/null
compose() { "${COMPOSE[@]}" --project-directory "$REPO" --env-file "$ENV_FILE" -f "$REPO/compose.yaml" "$@"; }
if [[ $command == up ]]; then
  PUBLIC_HOST=''
  HTTPS_PORT=8448
  CONSOLE_PORT=8787
  SPEECH_MODEL=small
  if [[ -f $ENV_FILE ]]; then source "$ENV_FILE"; fi
  while (($#)); do
    [[ $# -ge 2 ]] || { usage; exit 1; }
    case "$1" in
      --ip) PUBLIC_HOST=$2 ;;
      --https-port) HTTPS_PORT=$2 ;;
      --console-port) CONSOLE_PORT=$2 ;;
      --model) SPEECH_MODEL=$2 ;;
      *) usage; exit 1 ;;
    esac
    shift 2
  done
  [[ $PUBLIC_HOST =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo 'Provide --ip PUBLIC_IPV4.' >&2; exit 1; }
  IFS=. read -r -a octets <<< "$PUBLIC_HOST"
  for octet in "${octets[@]}"; do (( 10#$octet <= 255 )) || { echo 'Invalid IPv4 address.' >&2; exit 1; }; done
  for value in "$HTTPS_PORT" "$CONSOLE_PORT"; do
    [[ $value =~ ^[0-9]{1,5}$ ]] && (( 10#$value >= 1024 && 10#$value <= 65535 )) || { echo 'Use ports 1024-65535.' >&2; exit 1; }
  done
  HTTPS_PORT=$((10#$HTTPS_PORT)); CONSOLE_PORT=$((10#$CONSOLE_PORT))
  [[ $HTTPS_PORT != "$CONSOLE_PORT" ]] || { echo 'Device and console ports must differ.' >&2; exit 1; }
  [[ $SPEECH_MODEL == small || $SPEECH_MODEL == base ]] || { echo 'Model must be small or base.' >&2; exit 1; }
  printf 'PUBLIC_HOST=%s\nHTTPS_PORT=%s\nCONSOLE_PORT=%s\nSPEECH_MODEL=%s\n' "$PUBLIC_HOST" "$HTTPS_PORT" "$CONSOLE_PORT" "$SPEECH_MODEL" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  compose config --quiet
  compose build
  compose run --rm --no-deps gateway model
  if ! compose run --rm --no-deps gateway login-status; then
    if [[ ! -t 0 ]]; then
      echo 'Images and volumes are ready. Run interactively: bash deploy/docker.sh login, then bash deploy/docker.sh up' >&2
      exit 2
    fi
    compose run --rm --no-deps gateway login
  fi
  # Recreate both members of the shared network namespace together.
  compose up -d --force-recreate
  for attempt in $(seq 1 40); do
    if compose exec --user 10001:10001 -T gateway python /app/docker/runtime.py health 2>/dev/null; then
      echo "Container HTTPS ready. Device endpoint: wss://$PUBLIC_HOST:$HTTPS_PORT/device"
      echo "Allow inbound TCP $HTTPS_PORT. This test verifies container TLS, not the cloud firewall."
      echo 'Export: bash deploy/docker.sh export ./connection-private.json'
      exit 0
    fi
    sleep 3
  done
  echo 'Not ready. Inspect: bash deploy/docker.sh logs' >&2
  exit 3
fi
[[ -f $ENV_FILE ]] || { echo 'Run the up command with --ip first.' >&2; exit 1; }
case "$command" in
  login)
    # Avoid simultaneous auth.json writes from a running gateway and login helper.
    compose stop ingress gateway
    compose run --rm --no-deps gateway login
    echo 'Login saved. Run: bash deploy/docker.sh up'
    ;;
  status) compose ps ;;
  logs) compose logs -f --tail 100 ;;
  stop) compose stop ;;
  health|console) compose exec --user 10001:10001 -T gateway python /app/docker/runtime.py "$command" ;;
  export)
    [[ $# == 1 ]] || { usage; exit 1; }
    destination=$1
    [[ ! -e $destination ]] || { echo 'Refusing to overwrite an existing export.' >&2; exit 1; }
    temporary=$(mktemp "${destination}.tmp.XXXXXX")
    trap 'rm -f -- "$temporary"' EXIT
    compose exec --user 10001:10001 -T gateway python /app/docker/runtime.py export > "$temporary"
    # Hard link publishes a complete file atomically and refuses concurrent overwrites.
    ln -- "$temporary" "$destination"
    echo "Private connection configuration saved: $destination"
    ;;
  *) usage; exit 1 ;;
esac

