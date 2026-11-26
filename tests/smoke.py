"""Run against the demo server using only the standard library."""

import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = os.getenv("SMOKE_URL", "http://127.0.0.1:18080")
TOKEN = os.environ["MANAGER_TOKEN"]


def call(path, body=None, *, auth=False, status=200):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(body).encode() if body is not None else None
    try:
        response = urlopen(Request(BASE + path, data, headers), timeout=20)
    except HTTPError as error:
        response = error
    with response:
        assert response.status == status, (path, response.status, status)
        raw = response.read()
        if response.headers.get_content_type() == "application/json":
            return json.loads(raw)
        return raw


def main():
    prefix = "/api/v1"
    assert call(prefix + "/health")["status"] == "ok"
    assert call("/") and call("/manager")
    assert call("/openapi.json")["paths"]
    assert len(call(prefix + "/products?limit=1&offset=1")["items"]) == 1
    product, = call(prefix + "/products?q=DEMO-MON-01&limit=1")["items"]
    assert product["sku"] == "DEMO-MON-01"
    chat = call(prefix + "/chat", {"message": "монитор"})
    assert chat["provider"] == "catalog" and chat["products"]
    call(prefix + "/chat", {"message": " "}, status=422)
    lead = call(prefix + "/leads", {
        "contact": "<test@example.invalid>", "message": "Нужны два монитора",
        "sku": product["sku"],
    }, status=201)
    call(prefix + "/manager/leads", status=401)
    assert any(row["id"] == lead["id"] for row in call(prefix + "/manager/leads", auth=True)["items"])
    quote_path = prefix + "/manager/quotes"
    payload = {"lead_id": lead["id"], "items": [{"sku": product["sku"], "quantity": 2}]}
    quote = call(quote_path, payload, auth=True, status=201)
    assert quote["total_minor"] == product["price_minor"] * 2
    call(quote["download_url"], status=401)
    document = call(quote["download_url"], auth=True).decode()
    assert "&lt;test@example.invalid&gt;" in document and "47980,00 ₽" in document
    payload["items"][0]["sku"] = "DEMO-BACKUP-01"
    call(quote_path, payload, auth=True, status=409)
    payload["items"][0]["sku"] = "UNKNOWN"
    call(quote_path, payload, auth=True, status=404)
    print("Проверки API пройдены")


if __name__ == "__main__":
    main()
