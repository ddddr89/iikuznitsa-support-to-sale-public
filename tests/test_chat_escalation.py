from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.catalog import Catalog
from app.commercial import Commercial
from app.knowledge import Knowledge
from app.main import Chat, SESSION_COOKIE, create_app


class ProviderOutageTests(TestCase):
    def test_two_failed_routes_offer_one_escalated_lead_after_consent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            Catalog(path).init()
            commercial = Commercial(path)
            commercial.init()
            Knowledge(path).init()
            with patch.dict("os.environ", {"APP_KNOWLEDGE_DB": str(path)}):
                app = create_app(path)
            chat = next(route.endpoint for route in app.routes if route.path == "/api/v1/chat")
            failure = {
                "provider": "catalog", "answer": "Каталожный ответ.",
                "actions": [{"type": "route_n8n", "status": "error"},
                            {"type": "model_yandex", "status": "error"}],
            }
            token = "A" * 43
            owner = sha256(token.encode()).hexdigest()
            commercial.create_session(owner, 600)
            request = SimpleNamespace(cookies={SESSION_COOKIE: token})
            body = Chat(message="Уточните условия поставки")
            with patch("app.main.reply", side_effect=lambda *_, **__: {**failure}):
                anonymous = chat(body, SimpleNamespace(cookies={}))
                self.assertNotIn("escalation", anonymous)
                self.assertEqual(commercial.list_leads(), [])
                self.assertNotIn("escalation", chat(body, request))
                self.assertEqual(commercial.list_leads(), [])
                for message in ("Да", "Позовите менеджера."):
                    result = chat(Chat(message=message), request)
                    self.assertEqual(result["escalation"]["status"], "escalated")
                    self.assertEqual(result["escalation"]["id"], commercial.list_leads()[0]["id"])
                    self.assertIn({"type": "lead_escalation", "status": "done"}, result["actions"])
            lead, = commercial.list_leads()
            self.assertEqual(lead["status"], "escalated")
            self.assertEqual(lead["message"], body.message)

    def test_public_lead_creation_requires_session_and_limits_attempts(self):
        from fastapi import HTTPException

        from app.main import Lead

        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            commercial = Commercial(path)
            commercial.init()
            with patch.dict("os.environ", {"APP_REQUIRE_LEAD_SESSION": "1"}):
                app = create_app(path)
            submit = next(route.endpoint for route in app.routes if route.path == "/api/v1/leads")
            data = Lead(contact="audit@example.invalid", message="Вопрос")
            with self.assertRaises(HTTPException) as unauthenticated:
                submit(data, SimpleNamespace(cookies={}))
            self.assertEqual(unauthenticated.exception.status_code, 403)
            token = "A" * 43
            owner = sha256(token.encode()).hexdigest()
            commercial.create_session(owner, 600)
            session = SimpleNamespace(cookies={SESSION_COOKIE: token})
            for number in range(5):
                submit(data.model_copy(update={"message": f"Вопрос {number}"}), session)
            with self.assertRaises(HTTPException) as throttled:
                submit(data, session)
            self.assertEqual(throttled.exception.status_code, 429)
            self.assertEqual(len(commercial.list_leads()), 5)

    def test_direct_requests_bypass_the_consultant_and_reuse_the_lead(self):
        phrases = (
            "Здравствуйте. Я хочу обсудить корпоративную закупку непосредственно с менеджером, без AI-консультации",
            "Соедините с человеком",
            "Хочу обсудить закупку с сотрудником",
            "Не хочу общаться с ботом",
            "Менеджеру",
            "Позовите специалиста",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            catalog, commercial = Catalog(path), Commercial(path)
            catalog.init()
            commercial.init()
            Knowledge(path).init()
            with patch.dict("os.environ", {"APP_KNOWLEDGE_DB": str(path)}):
                app = create_app(path)
            chat = next(route.endpoint for route in app.routes if route.path == "/api/v1/chat")
            for index, phrase in enumerate(phrases):
                with self.subTest(phrase=phrase):
                    token = chr(ord("A") + index) * 43
                    owner = sha256(token.encode()).hexdigest()
                    commercial.create_session(owner, 600)
                    request = SimpleNamespace(cookies={SESSION_COOKIE: token})
                    with patch("app.main.reply", side_effect=AssertionError("Модель вызвана")):
                        first = chat(Chat(message=phrase), request)
                        repeat = chat(Chat(message="Позовите менеджера."), request)
                    self.assertEqual(first["actions"], [{"type": "lead_escalation", "status": "done"}])
                    self.assertEqual(first["products"], [])
                    self.assertEqual(first["escalation"], repeat["escalation"])
                    detail = commercial.lead_detail(first["escalation"]["id"], owner)
                    self.assertEqual(detail["message"], phrase)
            self.assertEqual(len(commercial.list_leads()), len(phrases))

    def test_direct_request_in_same_session_creates_open_lead_after_close(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            commercial = Commercial(path)
            commercial.init()
            Knowledge(path).init()
            with patch.dict("os.environ", {"APP_KNOWLEDGE_DB": str(path)}):
                app = create_app(path)
            chat = next(route.endpoint for route in app.routes if route.path == "/api/v1/chat")
            for key, initial in (("X", None), ("Y", "Как проверить статус ранее оформленного заказа?")):
                with self.subTest(initial=initial):
                    token = key * 43
                    owner = sha256(token.encode()).hexdigest()
                    commercial.create_session(owner, 600)
                    request = SimpleNamespace(cookies={SESSION_COOKIE: token})
                    if initial:
                        chat(Chat(message=initial), request)
                    shortcut = Chat(message="Позовите менеджера.")
                    original = chat(shortcut, request)["escalation"]
                    self.assertEqual(chat(shortcut, request)["escalation"], original)
                    commercial.update_lead(original["id"], status="closed", closure_reason="Проверка")
                    reopened = chat(shortcut, request)["escalation"]
                    self.assertNotEqual(reopened["id"], original["id"])
                    self.assertEqual(reopened["status"], "escalated")
                    self.assertEqual(chat(shortcut, request)["escalation"], reopened)
                    self.assertEqual(commercial.lead_detail(original["id"], owner)["status"], "closed")
