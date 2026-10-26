#!/usr/bin/env bash
# Personal server installation. No credentials are copied from the development PC.
set -Eeuo pipefail
umask 077
DOMAIN=''
MODEL=small
CODEX_VERSION=${CODEX_VERSION:-0.163.0}
CADDY_VERSION=${CADDY_VERSION:-}
ROOT=/opt/codex-pocket
STATE=/var/lib/codex-pocket
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd /
die() { echo "ERROR: $*" >&2; exit 1; }
usage() { echo 'Usage: sudo bash deploy/install.sh --domain pocket.example.com [--model small|base]'; }
while (($#)); do
  case "$1" in
    --domain) [[ $# -ge 2 ]] || die 'Missing domain'; DOMAIN=$2; shift 2 ;;
    --model) [[ $# -ge 2 ]] || die 'Missing model'; MODEL=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown argument: $1" ;;
  esac
done
[[ ${EUID} -eq 0 ]] || die 'Run with sudo.'
[[ $DOMAIN =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,63}$ ]] || die 'Provide a DNS hostname (not a raw IP). Point its A record at this server first.'
[[ $MODEL == small || $MODEL == base ]] || die 'Model must be small or base.'
[[ $CODEX_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'Invalid CODEX_VERSION.'
source /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
  *) die 'Supported: Ubuntu 22.04/24.04 or Debian 12/13. Do not run this installer on TencentOS/CentOS.' ;;
esac
command -v systemctl >/dev/null || die 'systemd is required.'
if [[ ! -f /etc/codex-pocket/managed ]]; then
  if command -v ss >/dev/null && ss -H -ltn | awk '{print $4}' | grep -Eq ':(80|443|8787)$'; then
    die 'Port 80, 443 or 8787 is occupied. Existing sites will not be overwritten.'
  fi
fi
case "$(uname -m)" in x86_64) NODE_ARCH=x64; CADDY_ARCH=amd64 ;; aarch64|arm64) NODE_ARCH=arm64; CADDY_ARCH=arm64 ;; *) die 'Supported CPU: x86_64 or arm64.' ;; esac
echo '[1/7] Installing system dependencies'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl xz-utils tar unzip rsync python3 python3-venv python3-dev build-essential fonts-noto-cjk git bubblewrap
install -d -m 755 "$ROOT/tools" "$ROOT/releases" /etc/codex-pocket
id codex-pocket >/dev/null 2>&1 || useradd --system --create-home --home-dir "$STATE" --shell /usr/sbin/nologin codex-pocket
id codex-pocket-tls >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/codex-pocket-tls --shell /usr/sbin/nologin codex-pocket-tls
install -d -o codex-pocket -g codex-pocket -m 700 "$STATE" "$STATE/runtime" "$STATE/workspace" "$STATE/models" "$STATE/.codex"
install -d -o codex-pocket-tls -g codex-pocket-tls -m 700 /var/lib/codex-pocket-tls
TEMP=$(mktemp -d)
trap 'rm -rf -- "$TEMP"' EXIT
echo '[2/7] Installing checksum-verified Node.js 22 LTS and Codex'
curl -fL --retry 3 https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt -o "$TEMP/node-checksums.txt"
NODE_ARCHIVE=$(awk -v pattern="-linux-${NODE_ARCH}.tar.xz" 'index($2,pattern) && substr($2,length($2)-length(pattern)+1)==pattern {print $2;exit}' "$TEMP/node-checksums.txt")
[[ $NODE_ARCHIVE =~ ^node-v22\.[0-9]+\.[0-9]+-linux-(x64|arm64)\.tar\.xz$ ]] || die 'Could not resolve official Node.js archive.'
curl -fL --retry 3 "https://nodejs.org/dist/latest-v22.x/$NODE_ARCHIVE" -o "$TEMP/$NODE_ARCHIVE"
(cd "$TEMP" && grep -F "  $NODE_ARCHIVE" node-checksums.txt | sha256sum -c -)
NODE_DIRECTORY=${NODE_ARCHIVE%.tar.xz}
tar -xJf "$TEMP/$NODE_ARCHIVE" -C "$ROOT/tools"
ln -sfn "$ROOT/tools/$NODE_DIRECTORY" "$ROOT/tools/node"
export PATH="$ROOT/tools/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
npm install --prefix "$ROOT/tools/codex" "@openai/codex@$CODEX_VERSION" --omit=dev --no-audit --no-fund
chmod -R a+rX "$ROOT/tools"
echo '[3/7] Installing checksum-verified Caddy in a dedicated service'
if [[ -z $CADDY_VERSION ]]; then
  curl -fL --retry 3 https://api.github.com/repos/caddyserver/caddy/releases/latest -o "$TEMP/caddy-release.json"
  CADDY_VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tag_name"].removeprefix("v"))' "$TEMP/caddy-release.json")
fi
[[ $CADDY_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'Invalid stable Caddy version.'
CADDY_ARCHIVE="caddy_${CADDY_VERSION}_linux_${CADDY_ARCH}.tar.gz"
CADDY_BASE="https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}"
curl -fL --retry 3 "$CADDY_BASE/caddy_${CADDY_VERSION}_checksums.txt" -o "$TEMP/caddy-checksums.txt"
curl -fL --retry 3 "$CADDY_BASE/$CADDY_ARCHIVE" -o "$TEMP/$CADDY_ARCHIVE"
CADDY_CHECK=$(grep -F "  $CADDY_ARCHIVE" "$TEMP/caddy-checksums.txt")
CADDY_DIGEST=$(awk '{print $1}' <<< "$CADDY_CHECK")
case ${#CADDY_DIGEST} in
  64) (cd "$TEMP" && printf '%s\n' "$CADDY_CHECK" | sha256sum -c -) ;;
  128) (cd "$TEMP" && printf '%s\n' "$CADDY_CHECK" | sha512sum -c -) ;;
  *) die 'Unrecognized Caddy checksum format.' ;;
esac
install -d -m 755 "$ROOT/tools/caddy"
tar -xzf "$TEMP/$CADDY_ARCHIVE" -C "$TEMP" caddy
install -m 755 "$TEMP/caddy" "$ROOT/tools/caddy/caddy"
echo '[4/7] Preparing application release and Python environment'
RELEASE="$ROOT/releases/$(date -u +%Y%m%dT%H%M%S)-$$"
install -d -m 755 "$RELEASE"
for directory in bridge deploy certificates; do
  rsync -a --exclude runtime --exclude __pycache__ "$REPO/$directory/" "$RELEASE/$directory/"
done
install -m 644 "$REPO/setup-speech.py" "$RELEASE/setup-speech.py"
find "$RELEASE" -type d -exec chmod 755 {} +
find "$RELEASE" -type f -exec chmod 644 {} +
chmod 755 "$RELEASE/deploy/pocketctl" "$RELEASE/deploy/install.sh"
python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install -r "$RELEASE/bridge/requirements.txt"
"$ROOT/venv/bin/python" -m py_compile "$RELEASE/bridge/bridge.py"
chmod -R a+rX "$ROOT/venv" "$RELEASE"
MODEL_DIR="$STATE/models/whisper-$MODEL"
if [[ ! -f "$MODEL_DIR/model.bin" ]]; then
  runuser -u codex-pocket -- env HOME="$STATE" HF_HOME="$STATE/huggingface" "$ROOT/venv/bin/python" "$RELEASE/setup-speech.py" --model "$MODEL" --output "$MODEL_DIR"
fi
if [[ ! -f "$STATE/workspace/AGENTS.md" ]]; then
  install -o codex-pocket -g codex-pocket -m 600 "$REPO/assistant-workspace/AGENTS.md" "$STATE/workspace/AGENTS.md"
fi
echo '[5/7] Configuring private state and HTTPS ingress'
cat > /etc/codex-pocket/pocket.env <<ENV
HOME=$STATE
CODEX_HOME=$STATE/.codex
CODEX_BINARY=$ROOT/tools/codex/node_modules/.bin/codex
PATH=$PATH
POCKET_STATE_DIR=$STATE/runtime
POCKET_WORKSPACE=$STATE/workspace
POCKET_MODEL_DIR=$MODEL_DIR
POCKET_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
POCKET_ASR_THREADS=4
POCKET_PUBLIC_HOST=$DOMAIN
POCKET_PUBLIC_PORT=443
POCKET_CA_FILE=$ROOT/app/certificates/isrgrootx1.pem
HF_HOME=$STATE/huggingface
TOKENIZERS_PARALLELISM=false
ENV
chmod 600 /etc/codex-pocket/pocket.env
cat > /etc/codex-pocket/Caddyfile <<CADDY
{
  admin off
  persist_config off
}
$DOMAIN {
  tls {
    issuer acme {
      dir https://acme-v02.api.letsencrypt.org/directory
    }
  }
  @device path /device
  reverse_proxy @device 127.0.0.1:8787
  @health path /healthz
  reverse_proxy @health 127.0.0.1:8787
  respond 404
}
CADDY
chmod 644 /etc/codex-pocket/Caddyfile
"$ROOT/tools/caddy/caddy" validate --config /etc/codex-pocket/Caddyfile --adapter caddyfile
if [[ ! -f "$STATE/.codex/config.toml" ]]; then
  printf 'cli_auth_credentials_store = "file"\n' > "$STATE/.codex/config.toml"
  chown codex-pocket:codex-pocket "$STATE/.codex/config.toml"
  chmod 600 "$STATE/.codex/config.toml"
fi
systemctl stop codex-pocket.service codex-pocket-tls.service 2>/dev/null || true
ln -sfn "$RELEASE" "$ROOT/app.next"
mv -Tf "$ROOT/app.next" "$ROOT/app"
install -m 644 "$RELEASE/deploy/codex-pocket.service" /etc/systemd/system/codex-pocket.service
install -m 644 "$RELEASE/deploy/codex-pocket-tls.service" /etc/systemd/system/codex-pocket-tls.service
ln -sfn "$ROOT/app/deploy/pocketctl" /usr/local/sbin/pocketctl
touch /etc/codex-pocket/managed
systemctl daemon-reload
systemctl enable codex-pocket.service codex-pocket-tls.service
echo '[6/7] Checking personal Codex authentication'
if ! runuser -u codex-pocket -- env HOME="$STATE" CODEX_HOME="$STATE/.codex" PATH="$PATH" "$ROOT/tools/codex/node_modules/.bin/codex" login status; then
  if [[ -t 0 ]]; then
    echo 'Complete the official OpenAI device-code login shown next. Do not paste credentials into this repository.'
    runuser -u codex-pocket -- env HOME="$STATE" CODEX_HOME="$STATE/.codex" PATH="$PATH" "$ROOT/tools/codex/node_modules/.bin/codex" login --device-auth
  else
    echo 'Installation is prepared; authentication is required. Run: sudo pocketctl login' >&2
    exit 2
  fi
fi
echo '[7/7] Starting services and checking readiness'
systemctl restart codex-pocket.service codex-pocket-tls.service
READY=false
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8787/healthz >/dev/null; then READY=true; break; fi
  sleep 2
done
[[ $READY == true ]] || die 'Gateway did not become ready. Run: sudo pocketctl logs'
echo "Gateway ready. Public device endpoint: wss://$DOMAIN/device"
echo 'Allow TCP 80/443 in Tencent security groups and your existing host firewall.'
PUBLIC_READY=false
for attempt in $(seq 1 12); do
  if curl -fSs --connect-timeout 5 --max-time 10 "https://$DOMAIN/healthz"; then PUBLIC_READY=true; break; fi
  sleep 5
done
if [[ $PUBLIC_READY == true ]]; then
  echo; echo 'Public HTTPS readiness passed.'
else
  echo 'Local service is ready, but PUBLIC HTTPS is not verified. Check DNS, security groups, host firewall and TLS logs.' >&2
  exit 3
fi
echo 'Export connection configuration: sudo pocketctl export /path/to/connection.json'
echo 'Existing export files are not overwritten; keep exported device keys private.'
