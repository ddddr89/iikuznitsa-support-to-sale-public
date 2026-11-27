from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.commercial import Commercial


class WaitingQueueTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Commercial(Path(directory.name) / "queue.sqlite3")
        self.store.init()

    def test_queue_paginates_120_leads_and_keeps_old_waiting_leads_first(self):
        created = [
            self.store.create_lead("test@example.invalid", f"Обращение {i}", "SKU-1",
                                   escalation=i in {1, 2, 120}, owner="customer")
            for i in range(1, 121)
        ]
        self.store.update_lead(created[0]["id"], "in_progress")
        self.store.update_lead(created[2]["id"], "closed")
        for index in range(100, 120):
            lead = self.store.lead_detail(created[index]["id"], "customer")
            self.assertEqual((lead["message"], lead["sku"]), (f"Обращение {index + 1}", "SKU-1"))
        pages = [self.store.lead_queue(page, 25) for page in range(1, 6)]
        rows = [row for page in pages for row in page["items"]]
        self.assertEqual([len(page["items"]) for page in pages], [25, 25, 25, 25, 20])
        self.assertEqual({row["id"] for row in rows}, {lead["id"] for lead in created})
        self.assertEqual([row["id"] for row in rows[:3]],
                         [created[i]["id"] for i in (119, 1, 0)])
        self.assertTrue(all(page["total"] == 120 and page["waiting_count"] == 3 for page in pages))
        self.assertEqual(self.store.lead_queue(1, 25, "open")["total"], 119)
        self.assertEqual(self.store.lead_queue(1, 25, "closed")["total"], 1)
        self.assertEqual(self.store.lead_queue(2, 2, "waiting")["items"][0]["id"], created[0]["id"])
        self.store.update_lead(created[0]["id"], answer="Ответ")
        filtered = self.store.lead_queue(2, 2, "waiting")
        self.assertEqual((filtered["page"], filtered["pages"], filtered["waiting_count"]), (1, 1, 2))

    def test_taking_question_does_not_clear_priority_until_reply(self):
        question = self.store.create_lead("test@example.invalid", "Срок доставки", escalation=True, owner="owner")
        ordinary = self.store.create_lead("test@example.invalid", "Подготовить КП", owner="owner")
        self.store.update_lead(ordinary["id"], "in_progress")
        working = self.store.update_lead(question["id"], "in_progress")
        self.assertEqual(working["status"], "in_progress")
        self.assertTrue(working["requires_reply"])
        queue = self.store.lead_queue()
        self.assertEqual(queue["waiting_count"], 1)
        self.assertEqual([item["id"] for item in queue["items"]], [question["id"], ordinary["id"]])
        self.assertFalse(queue["items"][1]["requires_reply"])
        with self.assertRaisesRegex(ValueError, "Введите ответ"):
            self.store.update_lead(question["id"], "answered")
        self.assertEqual(self.store.manager_messages("owner", 0), [])
        answered = self.store.update_lead(question["id"], answer="Уточнили срок доставки")
        self.assertEqual(answered["status"], "answered")
        self.assertFalse(answered["requires_reply"])
        self.assertEqual(self.store.lead_queue()["waiting_count"], 0)
        self.assertEqual([message["content"] for message in self.store.manager_messages("owner", 0)],
                         ["Уточнили срок доставки"])
        self.assertEqual(self.store.manager_messages("other", 0), [])
        self.assertEqual([item["content"] for item in answered["history"] if item["role"] == "status"],
                         ["in_progress", "answered"])

    def test_closure_without_reply_requires_internal_reason(self):
        question = self.store.create_lead("test@example.invalid", "Срок поставки", escalation=True, owner="owner")
        lead_id = question["id"]
        with self.assertRaisesRegex(ValueError, "причину закрытия"):
            self.store.update_lead(lead_id, "closed")
        self.assertEqual(self.store.lead_queue()["waiting_count"], 1)
        closed = self.store.update_lead(lead_id, "closed", closure_reason="Клиент отменил обращение")
        self.assertEqual(closed["status"], "closed")
        self.assertFalse(closed["requires_reply"])
        self.assertEqual(self.store.lead_queue()["waiting_count"], 0)
        self.assertTrue(any(row["role"] == "closure_reason" for row in closed["history"]))
        self.assertFalse(any(row["role"] == "closure_reason" for row in self.store.lead_detail(lead_id, "owner")["history"]))
        self.assertEqual(self.store.manager_messages("owner", 0), [])

    def test_legacy_working_lead_is_read_without_rewriting_history(self):
        lead = self.store.create_lead("test@example.invalid", "Ранее созданный вопрос", escalation=True)
        lead_id = lead["id"]
        with self.store.connection() as db:
            db.execute("UPDATE leads SET requires_reply = NULL, status = 'in_progress' WHERE id = ?", (lead_id,))
        self.store.init()
        self.assertTrue(self.store.lead_detail(lead_id)["requires_reply"])
        self.assertEqual(self.store.lead_queue()["waiting_count"], 1)
        with self.store.connection() as db:
            self.assertIsNone(db.execute("SELECT requires_reply FROM leads WHERE id = ?", (lead_id,)).fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM lead_activity WHERE lead_id = ?", (lead_id,)).fetchone()[0], 0)
