#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
stand="$PWD/.stand"
mkdir -p "$stand"
socket="$stand/tmux.sock"
port=${SUPPORT_HTTPS_PORT:-18443}
if tmux -S "$socket" has-session -t =support-to-sale 2>/dev/null; then
    echo 'HTTPS-стенд уже запущен.'
    exit 0
fi
if [[ ! -s "$stand/cert.pem" || ! -s "$stand/key.pem" ]]; then
    openssl req -x509 -newkey rsa:2048 -sha256 -days 30 -nodes \
        -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
        -keyout "$stand/key.pem" -out "$stand/cert.pem" 2>"$stand/certificate.log"
fi
if [[ ! -s "$stand/manager-token" ]]; then
    openssl rand -hex 32 > "$stand/manager-token"
fi
if [[ ! -e "$stand/catalog.sqlite3" ]]; then
    .venv/bin/python -m app.catalog "$stand/catalog.sqlite3" data/softline-sample.csv \
        --source softline_sample --expected-count 6
fi
tmux -f /dev/null -S "$socket" new-session -d -s support-to-sale bash deploy/serve-stand.sh "$port"
if [[ ${SUPPORT_STAND_PROBE:-1} == 1 ]]; then
    for attempt in {1..30}; do
        if curl -fs --cacert "$stand/cert.pem" "https://localhost:$port/api/v1/health"; then
            printf '\nHTTPS-стенд доступен на https://localhost:%s\n' "$port"
            exit 0
        fi
        tmux -S "$socket" has-session -t =support-to-sale 2>/dev/null || break
        sleep .2
    done
    echo 'Проверка HTTPS не прошла. Диагностика в .stand/server.log.' >&2
    exit 1
fi
