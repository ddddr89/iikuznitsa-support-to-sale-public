from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.knowledge import AUTO_METHOD, DELIVERY, Knowledge, ORDER_STATUS_FACT, ORDER_STATUS_TITLE


class AutomaticKnowledgeTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.knowledge = Knowledge(Path(root.name) / "knowledge.sqlite3")
        self.knowledge.init()
        self.question = "Где посмотреть статус заказа?"
        self.paraphrase = "Как проверить состояние моего заказа?"
        self.completion = "Спасибо, вопрос решён"
        self.history = [{"question": self.question, "answer": ORDER_STATUS_FACT}]
        with self.knowledge.connection() as db:
            db.execute("INSERT INTO support_knowledge VALUES (?, ?, ?)",
                       (ORDER_STATUS_TITLE, ORDER_STATUS_FACT, DELIVERY))

    def test_learns_canonical_fact_once_and_uses_matching_paraphrase(self):
        self.assertIsNone(self.knowledge.automatic_answer(self.paraphrase))
        initial = self.knowledge.official_candidate(self.question)
        self.assertEqual(initial["excerpt"], ORDER_STATUS_FACT)
        learned = self.knowledge.learn_from_completion(self.completion, self.history)
        self.assertEqual(initial["kind"], "knowledge_source")
        self.assertEqual(learned, {**initial, "kind": "knowledge_automatic"})
        self.assertEqual(self.knowledge.automatic_answer(self.paraphrase), learned)
        self.assertIsNone(self.knowledge.learn_from_completion(self.completion, self.history))
        with self.knowledge.connection() as db:
            rows = db.execute("SELECT * FROM support_automatic").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["method"], AUTO_METHOD)
        self.assertTrue(rows[0]["created_at"].endswith("+00:00"))
        self.assertNotIn(self.question, tuple(rows[0]))

    def test_needs_explicit_completion_and_exact_trusted_source_answer(self):
        for text, history in (
            ("Спасибо", self.history),
            ("Вопрос решён?", self.history),
            ("Вопрос решён? Но где мой заказ 1234?", self.history),
            (self.completion, []),
            (self.completion, [{"question": self.question, "answer": "Мы доставим его завтра"}]),
            (self.completion, [{"question": self.question + " Мой email private@example.com", "answer": ORDER_STATUS_FACT}]),
        ):
            self.assertIsNone(self.knowledge.learn_from_completion(text, history))
        self.assertIsNone(self.knowledge.automatic_answer("Покажите статус заказа 1234"))

    def test_removed_source_and_conflicting_review_block_reuse(self):
        self.knowledge.learn_from_completion(self.completion, self.history)
        self.knowledge.confirm("lead-fixture", ORDER_STATUS_TITLE, "Этот вопрос требует уточнения")
        self.assertIsNone(self.knowledge.automatic_answer(self.paraphrase))
        self.assertIsNone(self.knowledge.official_candidate(self.question))
        self.knowledge.confirm("lead-fixture", ORDER_STATUS_TITLE, ORDER_STATUS_FACT)
        self.assertIsNotNone(self.knowledge.automatic_answer(self.paraphrase))
        with self.knowledge.connection() as db:
            db.execute("DELETE FROM support_knowledge")
        self.assertIsNone(self.knowledge.automatic_answer(self.paraphrase))

    def test_checkout_fragment_omits_fields_from_the_full_source(self):
        from app.knowledge import AUTO_TOPICS, ORDER_START
        title, canonical, url = AUTO_TOPICS[ORDER_START]
        with self.knowledge.connection() as db:
            db.execute("INSERT INTO support_knowledge VALUES (?, ?, ?)",
                       (title, canonical + " Затем введите контактные данные.", url))
        fact = self.knowledge.official_candidate("Как оформить заказ на сайте?")
        self.assertEqual(fact["excerpt"], canonical)
        self.assertNotIn("контактные данные", fact["excerpt"])
