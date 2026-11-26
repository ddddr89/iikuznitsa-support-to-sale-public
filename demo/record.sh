#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

port=${DEMO_PORT:-18093}
work=$(mktemp -d)
export DEMO_RECORD=1
export DEMO_OUT="${DEMO_OUT:-.stand/contest-video}"
export DEMO_SKU="${DEMO_SKU:-465223}"
export DEMO_URL="http://127.0.0.1:$port"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$PWD/.stand/contest-browser}"
export APP_DB_PATH="$work/catalog.sqlite3"
export APP_KNOWLEDGE_DB="$work/knowledge.sqlite3"
export APP_QUOTE_TEMPLATE="/home/demon/Данные/Шаблон КП ПАО Софтлайн.pdf"
export MANAGER_TOKEN=fixture-only-token
mkdir -p "$DEMO_OUT"

.venv/bin/python - <<'PY'
import os
from pathlib import Path

from app.catalog import Catalog
from app.knowledge import Knowledge

catalog = Catalog(os.environ["APP_DB_PATH"])
catalog.init()
with catalog._connect() as db:
    source = Path(".stand/catalog.sqlite3").resolve().as_uri() + "?mode=ro"
    db.execute("ATTACH DATABASE ? AS source", (source,))
    db.execute("INSERT INTO catalog_products SELECT * FROM source.catalog_products")
    db.execute("INSERT INTO catalog_vendor_codes SELECT * FROM source.catalog_vendor_codes")
    db.execute("INSERT INTO catalog_snapshot SELECT * FROM source.catalog_snapshot")
    db.execute("INSERT INTO catalog_search(catalog_search) VALUES ('rebuild')")
knowledge = Knowledge(os.environ["APP_KNOWLEDGE_DB"])
knowledge.init()
with knowledge.connection() as db:
    source = Path(".stand/knowledge.sqlite3").resolve().as_uri() + "?mode=ro"
    db.execute("ATTACH DATABASE ? AS source", (source,))
    db.execute("INSERT INTO support_knowledge SELECT * FROM source.support_knowledge")
    db.execute("INSERT INTO support_intents SELECT * FROM source.support_intents")
    db.execute("INSERT INTO support_snapshot SELECT * FROM source.support_snapshot")
PY

set -a
source /home/demon/.config/iikuznitsa/secrets.env
set +a
export N8N_WEBHOOK_URL N8N_WEBHOOK_SECRET
read -r N8N_WEBHOOK_URL < .stand/n8n-webhook-url
read -r N8N_WEBHOOK_SECRET < .stand/n8n-webhook-secret
if [[ -r .stand/n8n-webhook-auth ]]; then
    export N8N_WEBHOOK_AUTH
    read -r N8N_WEBHOOK_AUTH < .stand/n8n-webhook-auth
fi

.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$port" --no-access-log > "$DEMO_OUT/server.log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; rm -rf -- "$work"' EXIT
for attempt in {1..30}; do
    kill -0 "$pid"
    if curl -fs -o /dev/null "$DEMO_URL/api/v1/health"; then
        kill -0 "$pid"
        node demo/flow.mjs | tee "$DEMO_OUT/playwright.log"
        ffprobe -v error -show_entries format=duration -of csv=p=0 "$DEMO_OUT/demo-one-take.webm"
        exit
    fi
    sleep .2
done
exit 1
