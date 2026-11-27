from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.catalog import Catalog
from app.catalog_matching import product_scope
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
            ("t9", "Внутренний SSD Samsung Portable T9 1 ТБ", "Внутренние твердотельные накопители (SSD)", "SSD • USB 3.2 • 1 ТБ"),
            ("nvme", "SSD Internal NVMe M.2 PCIe 4.0 1 ТБ", "Внутренние твердотельные накопители (SSD)", "SSD • NVMe • 1 ТБ"),
            ("sata", "SSD SATA M.2 1 ТБ", "Внутренние твердотельные накопители (SSD)", "SSD • SATA • 1 ТБ"),
            ("fhd", 'Монитор 27"', "Мониторы", '27" IPS 1920 × 1080, USB-C 65 Вт'),
            ("uhd", 'Монитор 27"', "Мониторы", '27" IPS 3840 × 2160, USB-C 65 Вт'),
            ("unknown-watts", 'Монитор 27"', "Мониторы", '27" IPS 3840 × 2160, USB-C'),
            ("dell", 'Монитор Dell U2424H 23.8-inch', "Мониторы", '23.8"'),
            ("dell-similar", 'Монитор Dell U2424HE 23.8-inch', "Мониторы", '23.8"'),
            ("dell-p", 'Монитор Dell Technologies P2425HE 23.8-inch', "Мониторы", '23.8"'),
            ("basic", "Microsoft 365 Бизнес базовый (на 1 рабочее место)", "Офисные пакеты программ Microsoft Office", ""),
            ("standard", "Microsoft 365 бизнес стандарт (лицензия 1 год), цена за 1 лицензию", "Офисные пакеты программ Microsoft Office", "Подписка"),
            ("poe", "Коммутатор Demo", "Коммутаторы", "Managed, 24 × Gigabit ports, PoE+"),
            ("poe-unknown", "Коммутатор Demo", "Коммутаторы", "Managed, 24 × Gigabit ports"),
            ("gpu-wrong", "Видеокарта RTX 3050", "Видеокарты", ""),
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

    def test_joint_nvme_filter_checks_category_interface_and_budget(self):
        query = "Нужен внутренний SSD NVMe M.2 на 1 ТБ, PCIe 4.0, до {} ₽"
        self.assertEqual(self.skus(query.format(100)), ["nvme"])
        self.assertEqual(self.skus(query.format(10)), [])

    def test_joint_monitor_filter_rejects_fhd_and_unknown_power(self):
        query = "Монитор 27 дюймов, 4K, IPS, USB-C с зарядкой ноутбука от 65 Вт"
        self.assertEqual(self.skus(query), ["uhd"])

    def test_exact_monitor_identifier_and_confusable_confirmation(self):
        request = SimpleNamespace(cookies={})
        self.assertEqual(self.skus("Нужен монитор Dell U2424Н, три штуки"), ["dell"])
        first = self.chat(Chat(message="Нужен монитор Dell U2424Н, три штуки"), request)
        self.assertEqual(first["products"], [])
        self.assertIn("Подтвердите модель", first["answer"])
        second = self.chat(Chat(message="Да, имел в виду Dell U2424H, три штуки", history=[
            Turn(question="Нужен монитор Dell U2424Н, три штуки", answer=first["answer"]),
        ]), request)
        self.assertEqual(second["products"][0]["sku"], "dell")
        self.assertIn("Наличие нужного количества", second["answer"])

    def test_office_desktop_and_per_user_term_select_standard(self):
        query = "Microsoft 365 для 15 сотрудников на год. Нужны настольные Outlook и Word"
        result = self.chat(Chat(message=query), SimpleNamespace(cookies={}))
        self.assertEqual([p["sku"] for p in result["products"]], ["standard"])
        self.assertIn("15 пользовательских лицензий", result["answer"])

    def test_comparison_rejects_candidates_above_budget(self):
        query = "Сравните два внешних SSD на 1 ТБ стоимостью до 2 ₽"
        result = self.chat(Chat(message=query), SimpleNamespace(cookies={}))
        self.assertEqual(result["products"], [])
        self.assertIn("два разных товара", result["answer"])

    def test_laptop_component_does_not_change_product_type(self):
        query = "Ноутбук с 32 ГБ RAM, SSD 1 ТБ и RTX 4070 за 25 000 ₽"
        self.assertEqual(product_scope(query).category, "laptop")
        self.assertEqual(self.skus(query), [])

    def test_switch_and_gpu_exclude_wrong_candidates(self):
        self.assertEqual(self.skus("Управляемый коммутатор на 24 гигабитных порта с PoE+"), ["poe"])
        self.assertEqual(self.skus("Видеокарта RTX 5090 до 20 000 ₽"), [])

    def test_windows_server_waits_for_requirements_instead_of_wrong_license(self):
        req = SimpleNamespace(cookies={})
        first = self.chat(Chat(message="Какую лицензию Windows Server выбрать для 20 сотрудников?"), req)
        self.assertEqual(first["products"], [])
        self.assertIn("число физических процессоров", first["answer"])
        second = self.chat(Chat(message="Windows Server 2025, 16 ядер, две виртуальные машины", history=[
            Turn(question="Какую лицензию Windows Server выбрать для 20 сотрудников?", answer=first["answer"]),
        ]), req)
        self.assertEqual(second["products"], [])
        self.assertIn("2025 Standard", second["answer"])
        self.assertIn("User CAL", second["answer"])

    def test_endpoint_protection_does_not_return_automation_or_server_license(self):
        req = SimpleNamespace(cookies={})
        question = "Нужно защитить 50 ноутбуков Windows 11 от вирусов. Что выбрать?"
        with patch("app.main.reply") as model:
            first = self.chat(Chat(message=question), req)
            second = self.chat(Chat(message="Windows 11 Pro, нужно централизованное управление",
                                    history=[Turn(question=question, answer=first["answer"])]), req)
            model.assert_not_called()
        self.assertEqual(first["products"], [])
        self.assertEqual(second["products"], [])
        self.assertIn("Менеджер может проверить", second["answer"])

    def test_people_count_routes_to_mail_advice_not_operator(self):
        request = SimpleNamespace(cookies={})
        with patch("app.main.reply") as model:
            advice = self.chat(Chat(message="Нужна защита почты на 40 человек от фишинга."), request)
            operator = self.chat(Chat(message="Нужна помощь менеджера."), request)
            model.assert_not_called()
        self.assertIn("Где размещена почта", advice["answer"])
        self.assertIn("Позвать менеджера", operator["answer"])

    def test_ddr4_ddr5_compatibility_returns_no_unverified_modules(self):
        result = self.chat(Chat(message="Нужна оперативная память DDR5 для платы, которая поддерживает только DDR4"),
                           SimpleNamespace(cookies={}))
        self.assertEqual(result["products"], [])
        self.assertIn("несовместимы", result["answer"])

    def test_implicit_monitor_category_still_checks_price_and_display_specs(self):
        scope = product_scope("Ищу Philips 27 дюймов 4K IPS USB-C с зарядкой от 65 Вт до 20 500 ₽")
        self.assertEqual(scope.category, "monitor")
        item = dict(name="Philips 27 дюймов 4K", category="Мониторы",
                    description="IPS USB-C 65 Вт", price_minor=2010000, url="")
        self.assertTrue(scope.matches(item))
        self.assertFalse(scope.matches({**item, "name": "Philips 27 дюймов Full HD"}))
        self.assertFalse(scope.matches({**item, "description": "IPS USB-C 45 Вт"}))
        self.assertFalse(scope.matches({**item, "price_minor": 2050001}))
        self.assertIsNone(product_scope("Нужен телевизор Philips 27 дюймов 4K IPS"))

    def test_exact_samsung_model_with_more_than_one_number_block(self):
        scope = product_scope("Покажи монитор Samsung LC34G55TWWIXCI")
        self.assertTrue(scope.matches(dict(name='Samsung LC34G55TWWIXCI 34"', category="Мониторы",
                                           description="", url="", price_minor=1)))
        self.assertFalse(scope.matches(dict(name='Samsung S27CG550EI 27"', category="Мониторы",
                                            description="", url="", price_minor=1)))

    def test_drive_brand_family_and_maximum_price_are_joint_filters(self):
        scope = product_scope("Western Digital Elements Portable внешний HDD 4TB максимум 23000 ₽")
        exact = dict(name="Внешний HDD Western Digital Elements Portable 4TB", category="Внешние HDD",
                     description="HDD, 4 ТБ", url="", price_minor=2209300)
        self.assertTrue(scope.matches(exact))
        self.assertFalse(scope.matches({**exact, "name": "Внешний HDD ADATA 4TB"}))
        self.assertFalse(scope.matches({**exact, "price_minor": 2300001}))

    def test_recommendation_matches_model_despite_inflected_and_abbreviated_answer(self):
        from app.main import recommended_products
        stored = self.catalog.get("dell")
        similar = self.catalog.get("dell-similar")
        cards = recommended_products(self.catalog, [similar, stored], "Dеll U2424H — чёрный монитор.")
        self.assertEqual([p["sku"] for p in cards], ["dell"])


    def test_named_external_family_without_explicit_category(self):
        scope = product_scope("Сколько стоит Western Digital My Book 6TB?")
        self.assertEqual(scope.category, "external")
        self.assertTrue(scope.matches(dict(name="Western Digital My Book 6 TB", category="Внешние HDD",
                                           description="HDD, 6 ТБ", url="", price_minor=1)))
        self.assertFalse(scope.matches(dict(name="Western Digital Elements 6 TB", category="Внешние HDD",
                                            description="HDD, 6 ТБ", url="", price_minor=1)))

    def test_short_dell_monitor_query_returns_catalog_presence_not_stock(self):
        request = SimpleNamespace(cookies={})
        with patch("app.main.reply") as model:
            result = self.chat(Chat(message="Dell P2425HE, нужен именно HE."), request)
        model.assert_not_called()
        self.assertEqual([item["sku"] for item in result["products"]], ["dell-p"])
        self.assertIn("В каталоге есть", result["answer"])
        self.assertIn("Наличие нужного количества и срок поставки подтвердит менеджер", result["answer"])
        self.assertNotIn("Есть в наличии", result["answer"])
        self.assertIsNone(product_scope("Вызови поиск по каталогу для Dell P2425HE."))

    def test_windows_server_followup_consumes_known_parameters(self):
        request = SimpleNamespace(cookies={})
        question = "Windows Server: 8 работников и три ВМ. Что потребуется?"
        initial = self.chat(Chat(message=question), request)
        self.assertNotIn("количество виртуальных машин", initial["answer"])
        self.assertNotIn("число пользователей или устройств", initial["answer"])
        followup = self.chat(Chat(
            message="Windows Server 2025, 2 CPU по 12 ядер, 3 ВМ, 8 пользователей, без RDS.",
            history=[Turn(question=question, answer=initial["answer"])],
        ), request)
        self.assertIn("24 ядер за комплект", followup["answer"])
        self.assertIn("на 48 ядер", followup["answer"])
        self.assertIn("Менеджер может", followup["answer"])
        self.assertEqual(followup["products"], [])

    def test_documented_pickup_and_return_keep_customer_topic(self):
        from app.catalog_advice import advisory

        pickup, links = advisory("Можно забрать заказ самостоятельно и что взять для получения?", "")
        self.assertIn("пункт выдачи", pickup)
        self.assertIn("доверенность по форме М2", pickup)
        self.assertEqual(links[0]["url"], "https://store.softline.ru/delivery_info/")
        followup = "Если приобрели на юридическое лицо, где уточнить порядок?"
        answer, _ = advisory(followup, "Где посмотреть правила обмена оборудования?\n" + followup)
        self.assertIn("обмена или возврата", answer)
        self.assertNotIn("бухгалтерские документы", answer)

    def test_delivery_quote_remains_unconfirmed_after_postal_index(self):
        from app.catalog_advice import advisory

        question = "Доставляете в Якутск по индексу 677000?"
        followup = "Цена доставки известна или её уточнит менеджер?"
        answer, _ = advisory(followup, question + "\n" + followup)
        self.assertIn("677000", answer)
        self.assertIn("Точную сумму и срок в этом чате подтвердить не могу", answer)
        self.assertIn("при оформлении или у менеджера", answer)

    def test_unknown_manager_sla_explains_how_to_check_existing_request(self):
        request = SimpleNamespace(cookies={})
        question = "За сколько дней обычно отвечают на заявку?"
        with patch("app.main.reply") as model:
            first = self.chat(Chat(message=question), request)
            second = self.chat(Chat(message="Если подтверждённого срока нет, что делать?", history=[
                Turn(question=question, answer=first["answer"]),
            ]), request)
        model.assert_not_called()
        self.assertIn("Мои заявки", second["answer"])
        self.assertIn("повторно отправлять его не нужно", second["answer"])
        self.assertNotIn("escalation", second)
