import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.ai import MAX_CONTEXT_BYTES, MAX_OUTPUT_TOKENS, _json_size, bounded_context, reply
from app.catalog import Catalog
from app.catalog_advice import checked_model_answer, safe_commercial_answer
from app.commercial import Commercial
from app.knowledge import Knowledge
from app.main import Chat, create_app


class CommercialGuardTests(TestCase):
    def test_generated_commercial_claims_use_only_rechecked_catalog_facts(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "db.sqlite"
            Catalog(db).import_csv("data/demo.csv")
            Commercial(db).init()
            Knowledge(db).init()
            with patch.dict("os.environ", {"APP_KNOWLEDGE_DB": str(db)}):
                app = create_app(db)
            chat = next(r.endpoint for r in app.routes if r.path == "/api/v1/chat")
            claims = ("DEMO-MON-01 есть в наличии за 1 000 ₽. Доставим завтра. "
                      "Совместим с любой системой; лицензия включена.")
            with patch("app.main.reply", return_value={
                "provider": "yandex", "answer": claims, "actions": [{"type": "route_n8n", "status": "done"}]
            }):
                result = chat(Chat(message="Уточните цену артикула DEMO-MON-01"),
                              SimpleNamespace(cookies={}))
            self.assertEqual(result["products"][0]["price_minor"], 2399000)
            self.assertIn("23 990 ₽", result["answer"])
            self.assertIn("подтвердит менеджер", result["answer"])
            for term in ("1 000 ₽", "Доставим завтра", "любой системой", "лицензия включена"):
                self.assertNotIn(term, result["answer"])

    def test_unknown_conditions_offer_handoff_and_unrelated_answer_is_retained(self):
        result = safe_commercial_answer("Старт", "Совместим со всеми версиями.", [])
        self.assertIn("Не удалось подтвердить", result)
        self.assertIn("менеджеру", result)
        self.assertIsNone(safe_commercial_answer("Как открыть чат?", "Нажмите на вкладку «Чат».", []))

    def test_free_form_hardware_recommendation_requires_catalog_evidence(self):
        product = {"sku": "42", "name": "Тестовый монитор", "price_minor": 1599000,
                   "url": "https://store.softline.ru/demo/42/"}
        answer, sources = checked_model_answer(
            "Какой монитор взять?", "Берите этот. У него IPS, USB-C и 165 Гц.", [product], [],
        )
        self.assertIn("15 990 ₽", answer)
        for claim in ("IPS", "USB-C", "165 Гц", "Берите этот"):
            self.assertNotIn(claim, answer)
        self.assertEqual(sources, [{"title": product["name"], "url": product["url"]}])
        answer, sources = checked_model_answer("Что выбрать?", "Модель Ultra X — лучший вариант.", [], [])
        self.assertNotIn("Ultra X", answer)
        self.assertEqual(sources, [])

    def test_only_relevant_verbatim_official_excerpt_can_pass(self):
        source = {"title": "Где проверить статус заказа",
                  "excerpt": "Статус заказа можно посмотреть в личном кабинете.",
                  "url": "https://store.softline.ru/delivery_info/"}
        question = "Как проверить статус заказа?"
        answer = source["excerpt"]
        self.assertEqual(checked_model_answer(question, answer, [], [source]),
                         (answer, [{"title": source["title"], "url": source["url"]}]))
        for message, generated, resource in (
            (question, answer + " Обновление происходит через пять минут.", source),
            (question, answer, {**source, "url": "https://invalid.example/delivery"}),
            ("Могу поменять номер телефона?", answer, source),
        ):
            with self.subTest(message=message, resource=resource):
                checked, links = checked_model_answer(message, generated, [], [resource])
                self.assertNotIn(generated, checked)
                self.assertEqual(links, [])

    def test_context_budget_and_n8n_usage_log_exclude_personal_content(self):
        messages = [{"question": "а" * 1800, "answer": "б" * 3500}] * 4
        self.assertLessEqual(_json_size(bounded_context("Запрос", [], [], messages)), MAX_CONTEXT_BYTES)
        with patch.dict("os.environ", {"N8N_WEBHOOK_URL": "http://example.invalid",
                                      "N8N_WEBHOOK_SECRET": "fixture-secret"}, clear=True), patch(
            "app.ai.post_json", return_value={"provider": "yandex", "answer": "Ответ",
                                             "tool_status": "done", "usage": {"input_tokens": 120,
                                                                             "output_tokens": 32}}
        ) as post, self.assertLogs("uvicorn.error", "INFO") as capture:
            result = reply("Личное сообщение", [], history=messages)
        self.assertEqual(result["provider"], "yandex")
        self.assertLessEqual(_json_size(post.call_args.args[1]), MAX_CONTEXT_BYTES)
        line = capture.output[0]
        entry = json.loads(line.split("model_execution ", 1)[1])
        self.assertEqual(entry["model_calls"], 2)
        self.assertEqual(entry["calls"][0]["ordinal"], 2)
        self.assertEqual(entry["observed_input_tokens"], 120)
        self.assertTrue(entry["partial"])
        self.assertIsNone(entry["total_tokens"])
        self.assertNotIn("Личное сообщение", line)
        self.assertNotIn("fixture-secret", line)
        self.assertEqual(MAX_OUTPUT_TOKENS, 650)

    def test_n8n_two_calls_keep_counters_separate_and_compute_cost_once(self):
        counters = [{"model": "yandexgpt-5.1", "usage": {"input_tokens": 216, "output_tokens": 18}},
                    {"model": "yandexgpt-5.1", "usage": {"input_tokens": 319, "output_tokens": 48}}]
        payload = {"provider": "yandex", "answer": "Ответ", "tool_status": "done",
                   "usage": counters[-1]["usage"],
                   "telemetry": {"execution_id": "207", "calls": counters, "message": "secret body"}}
        with patch.dict("os.environ", {"N8N_WEBHOOK_URL": "http://example.invalid",
                                      "N8N_WEBHOOK_SECRET": "fixture-secret"}, clear=True), patch(
            "app.ai.post_json", return_value=payload
        ), self.assertLogs("uvicorn.error", "INFO") as capture:
            result = reply("Проверка", [])
        entry, = [json.loads(line.split("model_execution ", 1)[1]) for line in capture.output]
        self.assertEqual(entry["execution_id"], "207")
        self.assertEqual([(c["input_tokens"], c["output_tokens"]) for c in entry["calls"]], [(216, 18), (319, 48)])
        self.assertEqual(entry["total_tokens"], 601)
        self.assertEqual(entry["estimated_rub_excl_vat"], "0.39409836")
        self.assertNotIn("telemetry", result)
        self.assertNotIn("secret body", capture.output[0])

    def test_fallback_shares_request_id_without_double_counting(self):
        good = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Ответ"}]}],
                "usage": {"input_tokens": 100, "output_tokens": 20}}
        cfg = {"N8N_WEBHOOK_URL": "http://example.invalid", "N8N_WEBHOOK_SECRET": "fixture-secret",
               "YANDEX_API_KEY": "fixture-key", "YANDEX_FOLDER_ID": "fixture-folder", "YANDEX_MODEL": "yandexgpt-5.1"}
        with patch.dict("os.environ", cfg, clear=True), patch(
            "app.ai.post_json", side_effect=[OSError("fixture error"), good]
        ), self.assertLogs("uvicorn.error", "INFO") as capture:
            self.assertEqual(reply("Проверка", [])["provider"], "yandex")
        n8n, direct = [json.loads(line.split("model_execution ", 1)[1]) for line in capture.output]
        self.assertEqual(n8n["request_id"], direct["request_id"])
        self.assertIsNone(n8n["total_tokens"])
        self.assertFalse(n8n["success"])
        self.assertEqual(direct["total_tokens"], 120)
        self.assertTrue(direct["success"])
