from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.catalog import Catalog
from app.commercial import Commercial
from app.knowledge import Knowledge
from app.main import Chat, create_app


class ProviderOutageTests(TestCase):
    def test_two_failed_routes_create_one_escalated_lead(self):
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
            request = SimpleNamespace(cookies={})
            body = Chat(message="Уточните условия поставки")
            with patch("app.main.reply", side_effect=lambda *_, **__: {**failure}):
                for _ in range(2):
                    result = chat(body, request)
                    self.assertEqual(result["escalation"], {"status": "escalated"})
                    self.assertIn({"type": "lead_escalation", "status": "done"}, result["actions"])
            lead, = commercial.list_leads()
            self.assertEqual(lead["status"], "escalated")
            self.assertEqual(lead["message"], body.message)
