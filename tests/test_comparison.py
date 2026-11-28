import json
from unittest import TestCase

from app.automatic_documents import quantity_in
from app.comparison import compare_products, recommend_offer
from app.catalog_matching import normalize_codes


def offer(sku, name, price, params):
    fields = {"param": [{"attributes": {"name": label}, "text": value} for label, value in params]}
    return {"sku": sku, "name": name, "price_minor": price, "description": "16 × Gigabit ports",
            "category": "Коммутаторы", "feed_fields": json.dumps({"fields": fields})}


class ComparisonTests(TestCase):
    def test_feature_precedes_price_and_preference_chooses_confirmed_offer(self):
        offers = [
            offer("1111", "Коммутатор SW100", 300000, []),
            offer("2222", "Коммутатор SW200", 400000, [("Возможности", "Предотвращение петель")]),
        ]
        text, _ = compare_products(offers, "Сравните для защиты сети от петель")
        self.assertTrue(text.startswith("Главное отличие"))
        self.assertLess(text.index("Предотвращение петель"), text.index("Стоимость"))
        self.assertIn("не подтверждена", text)
        self.assertNotIn("16", text)
        chosen, advice = recommend_offer(offers, "Приоритет — защита от петель. Какой лучше?", 500000)
        self.assertEqual(chosen["sku"], "2222")
        self.assertTrue(advice.startswith("Для вашей задачи я рекомендую SW200"))
        self.assertIsNone(recommend_offer(offers, "Какой лучше?")[0])
        self.assertIsNone(recommend_offer(offers, "Какой лучше для защиты от петель?", 100000)[0])

    def test_unit_count_does_not_use_capacity_or_model_number(self):
        phrases = {
            "два внешних жёстких диска размером 2 ТБ": 2,
            "пару мониторов Dell U2424HE на 24 дюйма": 2,
            "возьмём три": 3,
            "пока только один": 1,
            "2 штуки накопителя 4 ТБ": 2,
            "диск 2 ТБ": None,
        }
        for phrase, expected in phrases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(quantity_in(normalize_codes(phrase), standalone=True)[1], expected)

    def test_qualified_tie_uses_price_without_asking_for_known_requirements(self):
        offers = [
            offer("1111", "Коммутатор SW100", 400000, []),
            offer("2222", "Коммутатор SW200", 300000, []),
        ]
        chosen, advice = recommend_offer(offers, "Для офиса, гигабит, до 5000 ₽", 500000, qualified=True)
        self.assertEqual(chosen["sku"], "2222")
        self.assertIn("3 000 ₽", advice)
        self.assertNotIn("?", advice)
        self.assertNotIn("SW100", advice)
