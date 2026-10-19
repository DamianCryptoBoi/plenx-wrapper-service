#!/usr/bin/env bash
# Ubuntu/Debian VPS; run as the user who owns the existing PM2 installation.
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
  printf 'Usage: bash scripts/setup-vps.sh [port]\nDefault port: 8000. Requires preinstalled PM2 and root/sudo access.\n'
  exit 0
fi
port="${1:-8000}"
if (( $# > 1 )) || [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
  printf 'Port must be a number from 1 to 65535.\n' >&2
  exit 1
fi
if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'Run this script on your Ubuntu/Debian VPS.\n' >&2
  exit 1
fi
export PATH="$PATH:$HOME/.local/bin:/usr/sbin:/sbin"
if ! command -v pm2 >/dev/null; then
  printf 'PM2 must already be installed and available on this user\047s PATH.\n' >&2
  exit 1
fi
privileged=()
if (( EUID != 0 )); then
  command -v sudo >/dev/null || { printf 'sudo or root access is required.\n' >&2; exit 1; }
  privileged=(sudo)
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
# PM2 needs ps (procps) to stop the uv process and its Python child together.
if ! command -v curl >/dev/null || ! command -v ufw >/dev/null || ! command -v ps >/dev/null; then
  command -v apt-get >/dev/null || { printf 'This installer requires Ubuntu/Debian (apt-get).\n' >&2; exit 1; }
  "${privileged[@]}" apt-get update
  "${privileged[@]}" apt-get install -y curl ca-certificates ufw procps
fi
if ! command -v uv >/dev/null; then
  curl --fail --silent --show-error --location https://astral.sh/uv/install.sh |
    env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh
fi

uv sync --locked --no-dev
# New state is private; existing account/password files are preserved.
mkdir -p data
chmod 700 data
PLENX_UV_BIN="$(command -v uv)" PLENX_PORT="$port" \
  pm2 startOrRestart ecosystem.config.js --only plenx-service --update-env

healthy=false
for (( attempt=0; attempt<30; attempt++ )); do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:$port/health" >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  pm2 logs plenx-service --lines 30 --nostream
  printf 'Service health check failed; inspect PM2 logs and check whether port %s is in use.\n' "$port" >&2
  exit 1
fi

# Add an allow rule without resetting rules or enabling an inactive firewall.
"${privileged[@]}" ufw allow "$port/tcp"
pm2 save
printf '\nPlenx is running on 0.0.0.0:%s under PM2.\n' "$port"
printf 'Open http://<VPS-IP>:%s/ and set your wrapper password on first visit.\n' "$port"
printf 'Also allow TCP %s in your VPS provider firewall/security group, if present.\n' "$port"
printf 'Logs: pm2 logs plenx-service\nRestart: pm2 restart plenx-service\n'
printf 'For reboot restoration, run pm2 startup once and follow its printed command.\n'
