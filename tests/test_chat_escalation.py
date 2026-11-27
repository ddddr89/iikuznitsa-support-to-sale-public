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
