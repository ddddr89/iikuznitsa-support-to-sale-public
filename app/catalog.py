"""SQLite catalog and atomic CSV snapshot importer."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import csv
from itertools import islice
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit

from app.catalog_matching import CATEGORIES, product_scope

FIELDS = ("sku", "name", "category", "description", "url", "price_minor", "currency")
MAX_PRICE_MINOR = (2**53 - 1) // (50 * 999)

QUERY_STOP_WORDS = {
    "а", "в", "для", "до", "есть", "и", "из", "или", "как", "какие", "какой",
    "мне", "на", "найди", "нужен", "нужна", "нужны", "о", "от", "по", "подбери",
    "for", "the", "with", "покажи", "при", "с", "у", "что", "это", "подберите", "покажите", "пожалуйста",
    "sku", "артикул", "артикулу", "артикулом", "позицию",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_snapshot (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    source TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_products (
    sku TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    url TEXT NOT NULL,
    price_minor INTEGER CHECK (price_minor >= 0),
    currency TEXT NOT NULL CHECK (currency = 'RUB')
);
CREATE INDEX IF NOT EXISTS catalog_sku_nocase ON catalog_products (sku COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS catalog_vendor_codes (
    code TEXT NOT NULL COLLATE NOCASE,
    sku TEXT NOT NULL REFERENCES catalog_products (sku),
    PRIMARY KEY (code, sku)
) WITHOUT ROWID;
CREATE VIRTUAL TABLE IF NOT EXISTS catalog_search USING fts5(
    sku, name, category, description, content='catalog_products',
    tokenize='unicode61'
);
"""


class Catalog:
    def __init__(self, db_path: str | Path):
        self.db_path = db_path

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def init(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def search(self, q: str, limit: int = 20, offset: int = 0) -> list[dict]:
        q = q.strip()
        with self._connect() as connection:
            connection.execute("BEGIN")
            if not q:
                rows = connection.execute(
                    "SELECT * FROM catalog_products ORDER BY sku LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                return [dict(row) for row in rows]

            exact = connection.execute(
                """SELECT * FROM catalog_products WHERE sku = ? COLLATE NOCASE
                   ORDER BY (sku = ?) DESC LIMIT 1""",
                (q, q),
            ).fetchone()
            if exact is not None:
                return [] if offset else [dict(exact)]

            aliases = self._vendor_matches(connection, q, limit, offset)
            if aliases is not None:
                return aliases

            scope = product_scope(q)
            if scope:
                categories = CATEGORIES[scope.category]
                placeholders = ", ".join("?" for _ in categories)
                rows = connection.execute(
                    f"""SELECT p.* FROM catalog_products AS p
                        WHERE p.category IN ({placeholders})
                        ORDER BY p.price_minor IS NULL, p.price_minor, p.sku""",
                    categories,
                )
                matches = (dict(row) for row in rows if scope.matches(row))
                return list(islice(matches, offset, min(offset + limit, sys.maxsize)))

            tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", q.lower())))[:32]
            if not tokens:
                return []
            terms = [token for token in tokens if token not in QUERY_STOP_WORDS]
            tokens = terms or tokens
            meaningful = [token for token in tokens if not token.isdecimal()]
            tokens = meaningful or tokens
            for token in tokens:
                if re.fullmatch(r"[a-z][a-z0-9]*[0-9][a-z0-9]*", token):
                    aliases = self._vendor_matches(connection, token, limit, offset)
                    if aliases is not None:
                        return aliases
            alternatives = " OR ".join(f'"{token}"*' for token in tokens)
            anchor = next((token for token in tokens if re.fullmatch(r"[a-z]{3,}", token)), None)
            query = alternatives
            if anchor and len(tokens) > 1:
                anchor_query = f'"{anchor}"*'
                if connection.execute(
                    "SELECT 1 FROM catalog_search WHERE catalog_search MATCH ? LIMIT 1",
                    (anchor_query,),
                ).fetchone():
                    query = f"{anchor_query} AND ({alternatives})"
            rows = connection.execute(
                """
                SELECT products.*
                FROM catalog_search
                JOIN catalog_products AS products ON products.rowid = catalog_search.rowid
                WHERE catalog_search MATCH ?
                ORDER BY bm25(catalog_search, 12, 8, 4, 1), products.sku
                LIMIT ? OFFSET ?
                """,
                (query, limit, offset),
            )
            return [dict(row) for row in rows]

    def _vendor_matches(self, connection, code, limit, offset):
        if not connection.execute(
            "SELECT 1 FROM catalog_vendor_codes WHERE code = ? LIMIT 1", (code,),
        ).fetchone():
            return None
        matches = connection.execute(
            """
            SELECT p.*
            FROM catalog_vendor_codes AS v
            JOIN catalog_products AS p ON p.sku = v.sku
            WHERE v.code = ?
            ORDER BY p.price_minor IS NULL, p.sku
            LIMIT ? OFFSET ?
            """,
            (code, limit, offset),
        )
        return [dict(match) for match in matches]

    def get(self, sku: str) -> dict | None:
        return self.get_many([sku]).get(sku)

    def get_many(self, skus: list[str]) -> dict[str, dict]:
        placeholders = ", ".join("?" for _ in skus)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM catalog_products WHERE sku IN ({placeholders})",
                skus,
            )
            return {row["sku"]: dict(row) for row in rows}

    def status(self) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source, item_count, imported_at FROM catalog_snapshot"
            ).fetchone()
            if row is not None:
                return dict(row)
            count, = connection.execute("SELECT count(*) FROM catalog_products").fetchone()
            return {"source": "unknown" if count else "empty", "item_count": count, "imported_at": None}

    def import_csv(self, csv_path: str | Path, *, source="local_csv", expected_count=None) -> int:
        """Replace the current catalog only after all rows and the index are valid."""
        with open(csv_path, encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, strict=True)
            if len(reader.fieldnames or []) != len(FIELDS) or set(reader.fieldnames) != set(FIELDS):
                raise ValueError(f"Ожидаются столбцы CSV: {', '.join(FIELDS)}")
            return self.replace_snapshot(
                ((f"CSV, строка {reader.line_num}", row) for row in reader),
                source=source,
                expected_count=expected_count,
            )

    def replace_snapshot(self, rows, *, source, expected_count=None) -> int:
        self.init()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
            connection.execute("DELETE FROM catalog_vendor_codes")
            connection.execute("DELETE FROM catalog_products")
            count = 0
            for position, row in rows:
                try:
                    connection.execute(
                        "INSERT INTO catalog_products VALUES (?, ?, ?, ?, ?, ?, ?)",
                        _parse_row(row),
                    )
                    code = row.get("vendor_code", "").strip()
                    if code:
                        connection.execute(
                            "INSERT INTO catalog_vendor_codes VALUES (?, ?)",
                            (code, row["sku"].strip()),
                        )
                except (ValueError, sqlite3.IntegrityError) as error:
                    raise ValueError(f"{position}: неверные данные или повтор SKU") from error
                count += 1
            if not count:
                raise ValueError("Снимок каталога пуст")
            if expected_count is not None and count != expected_count:
                raise ValueError(f"Число SKU не совпало: ожидалось {expected_count}, получено {count}")
            connection.execute(
                "INSERT INTO catalog_search(catalog_search) VALUES ('rebuild')"
            )
            connection.execute(
                """INSERT INTO catalog_snapshot VALUES (1, ?, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET source=excluded.source,
                   item_count=excluded.item_count, imported_at=excluded.imported_at""",
                (source, count, datetime.now(timezone.utc).isoformat()),
            )
            return count


def _parse_row(row: dict) -> tuple:
    if None in row or any(row[field] is None for field in FIELDS):
        raise ValueError("Неверное число полей")
    sku, name, category, description, url, price, currency = (
        row[field].strip() for field in FIELDS
    )
    if not sku or len(sku) > 200 or not name or currency != "RUB":
        raise ValueError("Требуются SKU, название и валюта RUB")
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Ожидается HTTP(S) URL")
    if price and not re.fullmatch(r"[0-9]{1,19}", price):
        raise ValueError("Цена должна быть целым числом")
    price_minor = int(price) if price else None
    if price_minor is not None and price_minor > MAX_PRICE_MINOR:
        raise ValueError("Цена слишком велика для расчёта суммы КП")
    return sku, name, category, description, url, price_minor, currency


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка снимка каталога из CSV")
    parser.add_argument("db", type=Path)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--source", choices=("local_csv", "softline_feed", "softline_sample", "demo"))
    args = parser.parse_args()
    source = args.source
    if not source:
        source = "demo" if args.csv.resolve() == Path("data/demo.csv").resolve() else "local_csv"
    if source == "softline_feed" and (args.expected_count is None or args.expected_count < 1):
        parser.error("Для полного экспорта Softline укажите --expected-count")
    try:
        count = Catalog(args.db).import_csv(args.csv, source=source, expected_count=args.expected_count)
    except (OSError, ValueError, csv.Error, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")
    print(f"Загружено SKU: {count}")


if __name__ == "__main__":
    main()
