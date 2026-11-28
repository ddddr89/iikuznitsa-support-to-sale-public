import gzip
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.catalog import Catalog, catalog_import_lock
from app.catalog_feed import main, open_feed, rows
from app.commercial import Commercial


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

    def test_remote_receipt_and_failures_preserve_last_snapshot_and_lead(self):
        self.path.write_bytes(YML)
        self.load(2)
        commercial = Commercial(self.catalog.db_path)
        commercial.init()
        lead = commercial.create_lead("test@example.invalid", "Проверка каталога")
        now = datetime.now(timezone.utc)
        fresh = YML.replace(b"<yml_catalog>", f'<yml_catalog date="{now.isoformat()}">'.encode())
        argv = ["catalog_feed", str(self.catalog.db_path), "--remote"]

        def fetched(payload):
            def save(_url, destination):
                destination.write_bytes(payload)
                return now.isoformat(), len(payload), "a" * 64
            return save

        with patch("sys.argv", argv), patch.dict("os.environ", {"SOFTLINE_FEED_URL": "https://fixture.invalid"}):
            with patch("app.catalog_feed.download", side_effect=fetched(fresh.replace(b"250.75", b"300.75"))):
                main()
            receipt = self.catalog.remote_sync_status()
            self.assertEqual(receipt["downloaded_at"], now.isoformat())
            self.assertEqual(receipt["item_count"], 2)
            self.assertEqual(self.catalog.status()["source"], "softline_remote_feed")
            for failure in (
                ValueError("Не удалось скачать выгрузку по HTTPS"),
                fetched(fresh.rsplit(b"</offers>", 1)[0]),
                fetched(fresh.split(b'<offer id="102"', 1)[0] + b"</offers></shop></yml_catalog>"),
                fetched(fresh.replace(now.isoformat().encode(), b"2001-01-01T00:00:00+00:00")),
            ):
                with patch("app.catalog_feed.download", side_effect=failure), self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 1)
                self.assertEqual(self.catalog.remote_sync_status(), receipt)
                self.assertEqual(self.catalog.search("Bench monitor")[0]["price_minor"], 30075)
                self.assertEqual(commercial.get_lead(lead["id"])["message"], "Проверка каталога")

    def test_import_lock_blocks_a_concurrent_command(self):
        with catalog_import_lock(self.catalog.db_path):
            with self.assertRaisesRegex(ValueError, "уже выполняется"), catalog_import_lock(self.catalog.db_path):
                self.fail("Повторный импорт получил замок")

    def test_product_parameters_are_retained_and_indexed(self):
        tag = '<param name="Параметр" unit="Гц">120</param><param name="Интерфейсы">DisplayPort</param>'
        self.path.write_bytes(YML.replace(b"<name>Bench monitor</name>", b"<name>Bench monitor</name>" + tag.encode()))
        self.load(2)
        item, = self.catalog.search("DisplayPort")
        self.assertEqual(item["sku"], "101")
        from app.catalog_matching import catalog_parameters
        self.assertEqual(catalog_parameters(item)["Параметр"], [("120", "Гц")])
        self.assertEqual(catalog_parameters(item)["Интерфейсы"], [("DisplayPort", "")])
