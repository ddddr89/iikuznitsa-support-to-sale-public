import gzip
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.catalog import Catalog
from app.catalog_feed import open_feed, rows


YML = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE yml_catalog SYSTEM "shops.dtd">
<yml_catalog><shop>
  <categories><category id="1">Monitors</category></categories>
  <offers>
    <offer id="101" available="true">
      <name>Bench monitor</name><vendorCode>M27-X</vendorCode>
      <price>250.75</price><currencyId>RUR</currencyId><categoryId>1</categoryId>
    </offer>
    <offer id="102" available="false">
      <name>Second bench monitor</name><vendorCode>M27-X</vendorCode>
      <price>250</price><currencyId>RUB</currencyId><categoryId>1</categoryId>
    </offer>
  </offers>
</shop></yml_catalog>
"""


class CatalogFeedTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "export.xml"
        self.catalog = Catalog(self.root / "catalog.sqlite3")

    def load(self, expected_count):
        with open_feed(self.path) as stream:
            return self.catalog.replace_snapshot(
                rows(stream), source="softline_feed", expected_count=expected_count,
            )

    def test_gzip_by_signature_and_manufacturer_code(self):
        self.path.write_bytes(gzip.compress(YML))
        self.assertEqual(self.load(2), 2)
        items = self.catalog.search("m27-x")
        self.assertEqual([item["sku"] for item in items], ["101", "102"])
        self.assertEqual(items[0]["price_minor"], 25075)
        self.assertIsNone(items[1]["price_minor"])
        self.assertEqual(items[0]["category"], "Monitors")
        self.assertEqual(self.catalog.search("M27-X", 1, 1), items[1:])
        self.assertEqual(self.catalog.search("артикул 101"), items[:1])

    def test_named_product_stays_in_long_natural_language_query(self):
        self.path.write_bytes(YML.replace(b"<name>Bench monitor</name>", b"<name>ViewTech monitor</name>"))
        self.load(2)
        items = self.catalog.search("Нужен ViewTech Standard. Подбери подходящий monitor.")
        self.assertEqual([item["sku"] for item in items], ["101"])

    def test_failed_count_preserves_products_and_manufacturer_codes(self):
        self.path.write_bytes(YML)
        self.load(2)
        self.path.write_bytes(YML.replace(b"M27-X", b"OTHER"))
        with self.assertRaisesRegex(ValueError, "Число SKU не совпало"):
            self.load(3)
        self.assertEqual(self.catalog.status()["item_count"], 2)
        self.assertEqual(len(self.catalog.search("M27-X")), 2)
        self.assertEqual(self.catalog.search("OTHER"), [])
