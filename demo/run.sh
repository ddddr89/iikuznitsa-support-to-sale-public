#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

port=${DEMO_PORT:-18081}
run_dir=$(mktemp -d)
export APP_DB_PATH="$run_dir/demo.sqlite3"
export APP_KNOWLEDGE_DB="$APP_DB_PATH"
export MANAGER_TOKEN=fixture-only-token
export DEMO_URL="http://127.0.0.1:$port"
export DEMO_OUT="${DEMO_OUT:-.stand/demo}"
mkdir -p "$DEMO_OUT"
unset YANDEX_API_KEY YANDEX_FOLDER_ID OPENAI_API_KEY N8N_WEBHOOK_URL N8N_WEBHOOK_SECRET
.venv/bin/python -m app.catalog "$APP_DB_PATH" data/demo.csv
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$port" --no-access-log > "$DEMO_OUT/server.log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true' EXIT
for attempt in {1..30}; do
    kill -0 "$pid"
    if curl -fs -o /dev/null "$DEMO_URL/api/v1/health"; then
        kill -0 "$pid"
        node demo/flow.mjs | tee "$DEMO_OUT/playwright.log"
        exit
    fi
    sleep .2
done
exit 1
