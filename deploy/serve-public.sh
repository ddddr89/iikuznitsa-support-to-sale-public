#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
stand="$PWD/.stand"
umask 077
exec > "$stand/public.log" 2>&1
exec "$stand/bin/cloudflared" --no-autoupdate tunnel --protocol http2 \
    --origin-ca-pool "$stand/cert.pem" --origin-server-name localhost \
    --url "https://localhost:$1"
