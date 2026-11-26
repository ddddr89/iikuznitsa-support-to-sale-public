import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from app.ai import YANDEX_URL, reply

PRODUCT = {
    "sku": "DEMO-001", "name": "Монитор", "category": "Мониторы",
    "description": "Описание", "price_minor": 350000,
}
Y_CFG = {"YANDEX_API_KEY": "test-key", "YANDEX_FOLDER_ID": "test-folder"}
HOOK_CFG = {"N8N_WEBHOOK_URL": "http://localhost/example", "N8N_WEBHOOK_SECRET": "test-hook"}
ANSWER = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Ответ"}]}]}


class AiTest(unittest.TestCase):
    def test_catalog_fallback_no_credentials(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(reply("Монитор", [PRODUCT])["provider"], "catalog")

    def test_hook_success(self):
        with patch.dict("os.environ", HOOK_CFG, clear=True), patch(
            "app.ai.post_json", return_value={"answer": "Ответ", "provider": "yandex"},
        ) as provider:
            response = reply("Монитор", [PRODUCT])
        self.assertEqual(response["actions"][0], {"type": "route_n8n", "status": "done"})
        self.assertEqual(provider.call_args.args[0], HOOK_CFG["N8N_WEBHOOK_URL"])

    def test_basic_auth_ingress_keeps_legacy_secret_header_out(self):
        config = HOOK_CFG | {"N8N_WEBHOOK_AUTH": "basic"}
        hook_answer = {"answer": "Ответ", "provider": "yandex"}
        with patch.dict("os.environ", config, clear=True), patch(
            "app.ai.post_json", return_value=hook_answer,
        ) as provider:
            response = reply("Монитор", [PRODUCT])
        self.assertEqual(response["provider"], "yandex")
        headers = provider.call_args.args[2]
        self.assertEqual(set(headers), {"Authorization"})
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_hook_catalog_tool_preserves_results_and_action(self):
        found = {**PRODUCT, "sku": "REAL-2", "currency": "RUB"}
        payload = {"answer": "Ответ", "provider": "yandex", "tool_status": "done", "products": [found]}
        with patch.dict("os.environ", HOOK_CFG, clear=True), patch("app.ai.post_json", return_value=payload):
            response = reply("Монитор", [PRODUCT])
        self.assertEqual(response["products"], [found, PRODUCT])
        self.assertIn({"type": "tool_catalog_search", "status": "done"}, response["actions"])

    def test_hook_error_falls_back_to_direct_yandex(self):
        with patch.dict("os.environ", Y_CFG | HOOK_CFG, clear=True), patch(
            "app.ai.post_json", side_effect=[URLError("unavailable"), ANSWER],
        ):
            response = reply("Монитор", [PRODUCT])
        self.assertEqual(response["provider"], "yandex")
        self.assertEqual(response["actions"][0], {"type": "route_n8n", "status": "error"})

    def test_yandex_catalog_tool_round_trip(self):
        call = {
            "type": "function_call", "call_id": "call1", "name": "search_catalog",
            "arguments": '{"query": "Монитор"}',
        }
        request_payloads = []

        def post(url, body, _headers, *, timeout):
            self.assertEqual(url, YANDEX_URL)
            request_payloads.append(json.loads(json.dumps(body)))
            return {"output": [call]} if len(request_payloads) == 1 else ANSWER

        def lookup(query, limit):
            self.assertEqual((query, limit), ("Монитор", 5))
            return [PRODUCT]

        with patch.dict("os.environ", Y_CFG, clear=True), patch("app.ai.post_json", side_effect=post):
            response = reply("Подбери экран", [], search=lookup)
        self.assertEqual(response["provider"], "yandex")
        self.assertEqual(response["products"], [PRODUCT])
        self.assertIn({"type": "tool_catalog_search", "status": "done"}, response["actions"])
        self.assertEqual(request_payloads[1]["tool_choice"], "none")

    def test_wrong_tool_is_never_executed(self):
        output = {"output": [{
            "type": "function_call", "call_id": "x", "name": "create_quote",
            "arguments": '{"query": "Монитор"}',
        }]}
        with patch.dict("os.environ", Y_CFG, clear=True), patch("app.ai.post_json", return_value=output):
            response = reply("Монитор", [], search=lambda *_: self.fail("Unexpected search"))
        self.assertEqual(response["provider"], "catalog")
        self.assertIn({"type": "tool_catalog_search", "status": "error"}, response["actions"])
