from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.catalog import Catalog
from app.commercial import Commercial
from app.knowledge import Knowledge
from app.main import Chat, SESSION_COOKIE, Turn, create_app


class ManagerDocumentsFlowTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "fixture.sqlite3"
        Catalog(path).init()
        Knowledge(path).init()
        self.store = Commercial(path)
        self.store.init()
        self.token = "A" * 43
        self.owner = sha256(self.token.encode()).hexdigest()
        self.store.create_session(self.owner, 600)
        self.store.create_session("other", 600)
        with patch.dict("os.environ", {"APP_KNOWLEDGE_DB": str(path), "APP_CHAT_PER_MINUTE": "0"}):
            self.app = create_app(path)
        self.chat = next(route.endpoint for route in self.app.routes if route.path == "/api/v1/chat")
        self.request = SimpleNamespace(cookies={SESSION_COOKIE: self.token})

    def test_priority_reply_changes_status_and_is_session_scoped(self):
        context = Turn(question="Как проверить поставку?", answer="Уточните товар.")
        pending = self.chat(Chat(message="Позовите менеджера", history=[context]), self.request)
        lead_id = pending["escalation"]["id"]
        second = self.chat(Chat(message="Позовите менеджера", history=[
            context, Turn(question="Позовите менеджера", answer=pending["answer"]),
        ]), self.request)
        self.assertEqual(second["escalation"]["id"], lead_id)
        regular = self.store.create_lead("test@example.invalid", "КП", owner=self.owner)
        queue = self.store.lead_queue()
        self.assertEqual(queue["waiting_count"], 1)
        self.assertEqual([item["id"] for item in queue["items"]], [lead_id, regular["id"]])
        answer = "Проверил обращение."
        detail = self.store.update_lead(lead_id, "escalated", answer)
        self.assertEqual(detail["status"], "answered")
        self.assertEqual(self.store.lead_queue()["waiting_count"], 0)
        reply, = self.store.manager_messages(self.owner, 0)
        self.assertEqual(reply["content"], answer)
        self.assertEqual(self.store.manager_messages(self.owner, reply["id"]), [])
        self.assertEqual(self.store.manager_messages("other", 0), [])

    def test_incomplete_catalog_answer_goes_to_manager_only_after_qualification(self):
        first = self.chat(Chat(message="Нужна защита корпоративной почты на 50 сотрудников"), self.request)
        self.assertNotIn("escalation", first)
        result = self.chat(Chat(message="Нужен игровой монитор 24 дюйма, 144 Гц"), self.request)
        self.assertEqual(result["escalation"]["status"], "escalated")
        self.assertEqual(result["products"], [])

    def test_bundle_is_atomic_and_reuses_both_documents(self):
        lead = self.store.create_lead("test@example.invalid", "КП", owner=self.owner)
        lines = [dict(sku="123", name="Монитор", quantity=2, price_minor=100)]
        calls = []

        def render(quote, kind):
            calls.append((quote["total_minor"], kind))
            return ("%PDF-" + kind).encode()

        def fail_invoice(quote, kind):
            if kind == "invoice":
                raise ValueError("test renderer failure")
            return render(quote, kind)

        with self.assertRaises(ValueError):
            self.store.create_quote(lead["id"], lines, fail_invoice)
        self.assertEqual(self.store.lead_detail(lead["id"])["documents"], [])
        calls.clear()
        result = self.store.create_quote(lead["id"], lines, render)
        self.assertEqual(self.store.create_quote(lead["id"], lines, render), result)
        self.assertEqual(calls, [(200, "quote"), (200, "invoice")])
        self.assertEqual(self.store.quote_pdf(result["id"], "quote"), b"%PDF-quote")
        self.assertEqual(self.store.quote_pdf(result["id"], "invoice"), b"%PDF-invoice")
