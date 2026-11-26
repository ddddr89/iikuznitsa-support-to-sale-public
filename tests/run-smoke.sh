#!/usr/bin/env bash
set -euo pipefail

temp_dir=$(mktemp -d)
export APP_DB_PATH="$temp_dir/smoke.sqlite3"
export MANAGER_TOKEN=fixture-only-token
port=${SMOKE_PORT:-18080}
export SMOKE_URL="http://127.0.0.1:$port"
unset N8N_WEBHOOK_URL N8N_WEBHOOK_SECRET
unset YANDEX_API_KEY YANDEX_FOLDER_ID OPENAI_API_KEY
export SUPPORT_SECRETS_FILE=/dev/null

.venv/bin/python -m app.catalog "$APP_DB_PATH" data/demo.csv
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$port" --no-access-log &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true' EXIT

for attempt in {1..35}; do
    kill -0 "$server_pid"
    if curl -fs -o /dev/null "$SMOKE_URL/api/v1/health"; then
        kill -0 "$server_pid"
        .venv/bin/python tests/smoke.py
        .venv/bin/python tests/contest.py
        exit
    fi
    sleep .15
done

exit 1
