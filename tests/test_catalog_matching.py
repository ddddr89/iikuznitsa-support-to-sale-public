from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.catalog import Catalog
from app.commercial import Commercial
from app.knowledge import Knowledge
from app.main import Chat, Turn, create_app


class CatalogMatchingTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "catalog.sqlite3"
        self.catalog = Catalog(path)
        fixtures = [
            ("drive4", "Внешний HDD 4TB", "Внешние HDD", "HDD, USB"),
            ("drive1", "Внешний HDD 1TB", "Внешние HDD", "HDD, USB"),
            ("external-ssd1", "Внешний HDD 1000 GB", "Внешние HDD", "SSD, USB"),
            ("external-ssd05", "Внешний SSD 0,5 ТБ", "Внешние SSD", "SSD"),
            ("internal1", "Внутренний SSD 1 ТБ", "Внутренние твердотельные накопители (SSD)", ""),
            ("case", "Корпус внешний HDD 1TB", "HDD external case", ""),
            ("screen-office", "Монитор 23.8-inch", "Мониторы", "100 Гц"),
            ("screen-game", 'Игровой монитор 27"', "Мониторы", "144 Гц"),
            ("license", 'Лицензия для моноблока 24"', "Лицензии", "игровой монитор, 144 Гц"),
            ("mousepad", "Коврик игровой", "Коврики", 'монитор 24"'),
        ]
        rows = ((sku, dict(sku=sku, name=name, category=category, description=description,
                           url="", price_minor=str(index * 100), currency="RUB"))
                for index, (sku, name, category, description) in enumerate(fixtures, 1))
        self.catalog.replace_snapshot(rows, source="test")
        self.commercial = Commercial(path)
        self.commercial.init()
        Knowledge(path).init()
        with patch.dict("os.environ", {"APP_KNOWLEDGE_DB": str(path), "CHAT_REQUESTS_PER_MINUTE": "0"}):
            app = create_app(path)
        self.chat = next(route.endpoint for route in app.routes if route.path == "/api/v1/chat")

    def skus(self, query, **kwargs):
        return [item["sku"] for item in self.catalog.search(query, **kwargs)]

    def test_external_drive_requires_exact_capacity_and_category(self):
        self.assertEqual(self.skus("Внешний диск на 1 ТБ"), ["drive1", "external-ssd1"])
        self.assertEqual(self.skus("Внешний диск на 1 ТБ", offset=1, limit=1), ["external-ssd1"])

    def test_game_monitor_requires_diagonal_and_gaming_evidence(self):
        self.assertEqual(self.skus("Игровой монитор 23–24 дюйма"), [])
        self.assertEqual(self.skus("Игровой монитор 26-28 дюймов 144 Гц"), ["screen-game"])
        self.assertEqual(self.skus('Монитор 23.8" 100 Гц'), ["screen-office"])
        self.assertEqual(self.skus("Монитор 24 дюйма 165 Гц"), [])

    def test_drive_media_and_capacity_variants(self):
        self.assertEqual(self.skus("Внешний SSD 1 ТБ"), ["external-ssd1"])
        self.assertEqual(self.skus("Внешний SSD 500 ГБ"), ["external-ssd05"])
        self.assertEqual(self.skus("Внешний HDD 4TB"), ["drive4"])

    def test_chat_binds_answer_and_cards_to_latest_verified_search(self):
        request = SimpleNamespace(cookies={})
        with patch("app.main.reply") as model:
            disk = self.chat(Chat(message="Внешний диск на 1 ТБ"), request)
            self.assertEqual(disk["products"][0]["sku"], "drive1")
            self.assertIn("артикул drive1", disk["answer"])
            self.assertIn("2 ₽", disk["answer"])
            monitor = self.chat(Chat(message="Игровой монитор 23–24 дюйма", history=[
                Turn(question="Внешний диск на 1 ТБ", answer=disk["answer"])
            ]), request)
            self.assertEqual(monitor["products"], [])
            self.assertIn("Менеджер может помочь", monitor["answer"])
            model.assert_not_called()
        self.assertNotIn("escalation", monitor)
        self.assertEqual(self.commercial.list_leads(), [])

    def test_model_cards_are_verified_and_follow_answer_order(self):
        model = {"answer": "Подойдёт external-ssd1, ещё есть drive1.", "provider": "yandex",
                 "actions": [], "products": [
                     {"sku": "drive1", "name": "Fake", "price_minor": 99},
                     {"sku": "license"},
                     {"sku": "external-ssd1"},
                     {"sku": "unknown"},
                 ]}
        with patch("app.main.reply", return_value=model):
            result = self.chat(Chat(message="Что посоветуете?"), SimpleNamespace(cookies={}))
        self.assertEqual([p["sku"] for p in result["products"]], ["external-ssd1", "drive1"])
        self.assertEqual(result["products"][1], self.catalog.get("drive1"))
