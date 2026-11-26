#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
stand="$PWD/.stand"
socket="$stand/tmux.sock"
port=${SUPPORT_HTTPS_PORT:-18443}
binary="$stand/bin/cloudflared"
[[ -x "$binary" ]] || { echo 'cloudflared отсутствует в .stand/bin' >&2; exit 1; }
curl -fsS --cacert "$stand/cert.pem" "https://localhost:$port/api/v1/health" >/dev/null
if ! tmux -S "$socket" has-session -t =support-to-sale-public 2>/dev/null; then
    : > "$stand/public.log"
    tmux -S "$socket" new-session -d -s support-to-sale-public \
        bash deploy/serve-public.sh "$port"
fi

for attempt in {1..45}; do
    url=$(sed -nE 's@.*(https://[a-z0-9-]+\.trycloudflare\.com).*@\1@p' "$stand/public.log" | head -1)
    if [[ -n "$url" ]] && curl --max-time 3 -fs "$url/api/v1/health" >/dev/null; then
        printf '%s\n' "$url" > "$stand/public-url"
        printf 'Публичный HTTPS: %s\n' "$url"
        exit 0
    fi
    tmux -S "$socket" has-session -t =support-to-sale-public 2>/dev/null || break
    sleep 1
done
echo 'Туннель не прошёл проверку. Диагностика в .stand/public.log.' >&2
exit 1
