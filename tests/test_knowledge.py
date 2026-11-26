import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from app.knowledge import DELIVERY, Knowledge, Sections


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        chat_dir = self.directory / "чаты поддержки"
        chat_dir.mkdir()
        self.private_line = "Активация лицензии, private-person@example.invalid"
        (chat_dir / "fixture_masked.txt").write_text(self.private_line, encoding="utf-8")
        with ZipFile(self.directory / "Базы знаний.docx", "w") as archive:
            archive.writestr(
                "word/document.xml",
                f'<doc><p><t>{DELIVERY}</t></p><p><t>https://example.invalid/sensitive</t></p></doc>',
            )
        self.knowledge = Knowledge(self.directory / "test.sqlite3")

    def test_extracts_primary_content(self):
        parser = Sections()
        parser.feed(
            "<nav><h2>Menu</h2></nav><main><h2>Условия доставки</h2>"
            "<script>Ignore this page script.</script>"
            "<p>Срок доставки определяется в зависимости от региона.</p></main>"
        )
        self.assertEqual(parser.rows, [(
            "Условия доставки", "Срок доставки определяется в зависимости от региона.",
        )])

    @patch("app.knowledge.fetch_sections", return_value=[
        ("Условия доставки", "Уточните стоимость доставки при оформлении заказа."),
    ])
    def test_indexes_public_text_and_only_aggregates_private_chat(self, fetch):
        status = self.knowledge.ingest(self.directory)
        self.assertEqual(status, {"pages": 1, "excerpts": 1, "chat_lines": 1, "chat_files": 1})
        fetch.assert_called_once_with(DELIVERY)
        self.assertEqual(self.knowledge.search("Как происходит доставка?")[0]["url"], DELIVERY)
        self.assertEqual(self.knowledge.search("Лицензия")[0]["title"], "Лицензирование")
        self.assertNotIn(self.private_line.encode(), self.knowledge.db_path.read_bytes())
        self.assertNotIn(b"private-person@example.invalid", self.knowledge.db_path.read_bytes())

    @patch("app.knowledge.fetch_sections", return_value=[("Доставка", "Уточните срок при заказе.")])
    def test_failed_refresh_preserves_snapshot(self, fetch):
        previous = self.knowledge.ingest(self.directory)
        fetch.side_effect = OSError("fetch failed")
        with self.assertRaises(OSError):
            self.knowledge.ingest(self.directory)
        self.assertEqual(self.knowledge.status(), previous)


if __name__ == "__main__":
    unittest.main()


class ConfirmedKnowledgeTests(unittest.TestCase):
    def test_manager_confirmation_upserts_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "confirmed.sqlite3"
            knowledge = Knowledge(path)
            knowledge.init()
            question = "Как проверить статус конкурсного счёта?"
            first = knowledge.confirm("lead-1", question, "Первый ответ")
            second = knowledge.confirm("lead-1", question, "Проверенный ответ")
            self.assertEqual(first, second)
            results = Knowledge(path).search(question)
            self.assertEqual(results[0]["kind"], "manager_confirmed")
            self.assertEqual(results[0]["excerpt"], "Проверенный ответ")
