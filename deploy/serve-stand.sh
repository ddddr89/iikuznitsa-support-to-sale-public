#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
stand="$PWD/.stand"
secrets=${SUPPORT_SECRETS_FILE:-/home/demon/.config/iikuznitsa/secrets.env}
ulimit -c 0
umask 077
exec > "$stand/server.log" 2>&1
if [[ -r "$secrets" ]]; then
    set -a
    source "$secrets"
    set +a
fi
export MANAGER_TOKEN
read -r MANAGER_TOKEN < "$stand/manager-token"
export APP_DB_PATH="$stand/catalog.sqlite3"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$1" \
    --ssl-keyfile "$stand/key.pem" --ssl-certfile "$stand/cert.pem" --no-access-log
