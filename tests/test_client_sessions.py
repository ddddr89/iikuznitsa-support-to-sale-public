from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.commercial import Commercial


class ClientSessionsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Commercial(Path(temporary.name) / "session.sqlite3")
        self.store.init()

    def test_sessions_are_scoped_even_when_contact_and_request_match(self):
        first = self.store.create_lead("same@example.invalid", "Монитор", owner="first")
        second = self.store.create_lead("same@example.invalid", "Монитор", owner="second")
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(self.store.create_lead("same@example.invalid", "Монитор", owner="first")["duplicate"])
        self.assertEqual([lead["id"] for lead in self.store.client_leads("second")], [second["id"]])
        with self.assertRaises(KeyError):
            self.store.lead_detail(first["id"], "second")

    def test_session_expiry_is_enforced_by_server(self):
        self.store.create_session("expired", -1)
        self.assertFalse(self.store.active_session("expired"))
        self.store.create_session("active", 60)
        self.assertTrue(self.store.active_session("active"))

    def test_chat_history_retains_only_last_twelve_turns_in_own_session(self):
        for index in range(14):
            self.store.record_turn("first", f"Вопрос {index}", f"Ответ {index}")
        self.store.record_turn("second", "Другой вопрос", "Другой ответ")
        history = self.store.chat_history("first")
        self.assertEqual(len(history), 12)
        self.assertEqual(history[0]["question"], "Вопрос 2")
        self.assertEqual(history[-1]["answer"], "Ответ 13")
        self.assertEqual(self.store.chat_history("second"), [
            {"question": "Другой вопрос", "answer": "Другой ответ"},
        ])

    def test_history_includes_actual_turn_and_manager_reply(self):
        self.store.record_turn("first", "Вопрос", "Ответ помощника")
        lead = self.store.create_lead("test@example.invalid", "Монитор", owner="first")
        self.store.update_lead(lead["id"], "answered", "Готово к проверке")
        detail = self.store.lead_detail(lead["id"], "first")
        self.assertNotIn("contact", detail)
        self.assertEqual(detail["status"], "answered")
        self.assertEqual([item["role"] for item in detail["history"]], ["client", "assistant", "status", "manager"])
