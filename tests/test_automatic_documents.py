from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.automatic_documents import document_quantity_followup, document_request
from app.catalog import Catalog


def turn(question, answer):
    return {"question": question, "answer": answer}


class AutomaticDocumentsTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.catalog = Catalog(Path(directory.name) / "catalog.sqlite3")
        items = (
            ("465223", "Монитор Dell U2424H", "2617300"),
            ("462252", "Монитор Dell U2424HE", "3466800"),
            ("NO-PRICE", "Экран LX604PRO", ""),
        )
        self.catalog.replace_snapshot(
            ((sku, dict(sku=sku, name=name, price_minor=price, currency="RUB",
                        url="", category="Мониторы", description="")) for sku, name, price in items),
            source="demo", expected_count=3,
        )
        self.known = turn("Нужен Dell U2424H", "Нашёл Dell U2424H, артикул 465223.")

    def test_uses_server_card_and_standalone_quantity_then_document_intent(self):
        quantity = document_quantity_followup("Три штуки", [self.known], self.catalog)
        self.assertIn("465223", quantity)
        self.assertIsNone(document_request("Три штуки", [self.known], self.catalog))
        plan = document_request("Подготовьте счёт", [self.known, turn("Три штуки", quantity)], self.catalog)
        self.assertEqual((plan.sku, plan.quantity, plan.missing), ("465223", 3, None))
        self.assertEqual(plan.product["price_minor"], 2617300)

    def test_explicit_sku_then_quantity_after_clarification(self):
        initial = "Сформируйте КП, артикул 465223"
        plan = document_request(initial, [], self.catalog)
        self.assertIn("количество товара", plan.missing)
        clarified = document_request("3", [turn(initial, plan.missing)], self.catalog)
        self.assertEqual((clarified.sku, clarified.quantity, clarified.missing), ("465223", 3, None))

    def test_quantity_from_the_named_product_question_is_reused_for_invoice(self):
        prior = turn("Нужны три монитора Dell U2424H. Есть они в каталоге? Сколько будет стоить весь заказ?",
                     "В каталоге есть Dell U2424H, артикул 465223 — 26 173 ₽.")
        plan = document_request("Подготовьте счёт", [prior], self.catalog)
        self.assertEqual((plan.sku, plan.quantity, plan.missing), ("465223", 3, None))

    def test_quantity_first_is_carried_while_waiting_for_sku(self):
        initial = "Подготовьте счёт на две штуки"
        plan = document_request(initial, [], self.catalog)
        self.assertIn("артикул товара", plan.missing)
        clarified = document_request("465223", [turn(initial, plan.missing)], self.catalog)
        self.assertEqual((clarified.sku, clarified.quantity, clarified.missing), ("465223", 2, None))

    def test_does_not_infer_a_card_from_loose_catalog_match(self):
        plan = document_request("Подготовьте счёт, артикул U2424H, 3 штуки", [], self.catalog)
        self.assertIsNone(plan.sku)
        self.assertIn("корректный артикул", plan.missing)
        alternative = document_request("Подготовьте КП на Dell U2424HE, три штуки", [self.known], self.catalog)
        self.assertIsNone(alternative.sku)
        self.assertIn("артикул", alternative.missing)
        contradiction = document_request("Подготовьте КП на U2424HE, артикул 465223, 3 шт", [], self.catalog)
        self.assertIsNone(contradiction.sku)

    def test_duplicate_or_stale_context_does_not_silently_select_card(self):
        options = turn("Какие есть мониторы?", "Варианты: артикул 465223; артикул 462252.")
        self.assertIsNone(document_quantity_followup("2 штуки", [options], self.catalog))
        self.assertIsNone(document_request("Подготовьте счёт на 2 шт", [options], self.catalog).sku)
        picked = document_request("Подготовьте счёт на U2424HE, 2 шт", [options], self.catalog)
        self.assertEqual(picked.sku, "462252")
        unrelated = turn("Как поставить приложение?", "Откройте инструкцию.")
        self.assertIsNone(document_request("Подготовьте счёт на 2 штуки", [self.known, unrelated], self.catalog).sku)

    def test_no_price_and_invalid_or_ambiguous_quantities_do_not_issue(self):
        no_price = document_request("Подготовьте счёт, SKU NO-PRICE, 2 шт", [], self.catalog)
        self.assertIn("цена", no_price.missing.lower())
        self.assertIsNone(no_price.product["price_minor"])
        for amount in ("0 шт", "1000 штук", "-2 шт", "2,5 шт", "2 шт или 3 шт"):
            with self.subTest(amount=amount):
                plan = document_request(f"Подготовьте счёт, артикул 465223, {amount}", [], self.catalog)
                self.assertIsNone(plan.quantity)
                self.assertIn("количество товара", plan.missing)

    def test_howto_and_cancellation_are_not_issuance(self):
        for question in ("Как подготовить счёт?", "Что нужно для получения документов?",
                         "Не нужно отправлять мне счёт", "Нужен счётчик"):
            with self.subTest(question=question):
                self.assertIsNone(document_request(question, [self.known], self.catalog))
