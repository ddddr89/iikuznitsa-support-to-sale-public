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
if [[ -r "$stand/n8n-webhook-url" && -r "$stand/n8n-webhook-secret" ]]; then
    export N8N_WEBHOOK_URL N8N_WEBHOOK_SECRET
    read -r N8N_WEBHOOK_URL < "$stand/n8n-webhook-url"
    read -r N8N_WEBHOOK_SECRET < "$stand/n8n-webhook-secret"
    if [[ -r "$stand/n8n-webhook-auth" ]]; then
        export N8N_WEBHOOK_AUTH
        read -r N8N_WEBHOOK_AUTH < "$stand/n8n-webhook-auth"
    fi
fi
export APP_DB_PATH="$stand/catalog.sqlite3"
export APP_KNOWLEDGE_DB="$stand/knowledge.sqlite3"
export APP_CHAT_PER_MINUTE="${APP_CHAT_PER_MINUTE:-30}"
export APP_REQUIRE_LEAD_SESSION="${APP_REQUIRE_LEAD_SESSION:-1}"
materials="${APP_MATERIALS_DIR:-/home/demon/Данные}"
template="$materials/Шаблон КП ПАО Софтлайн.pdf"
if [[ -r "$template" ]]; then
    export APP_QUOTE_TEMPLATE="${APP_QUOTE_TEMPLATE:-$template}"
fi
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$1" \
    --ssl-keyfile "$stand/key.pem" --ssl-certfile "$stand/cert.pem" --no-access-log
