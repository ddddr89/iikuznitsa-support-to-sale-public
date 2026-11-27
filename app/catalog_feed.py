"""Update the catalog from a supplied HTTPS or local YML storefront export."""

import argparse
from contextlib import contextmanager
import gzip
import os
from http.client import HTTPException
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree.ElementTree import ParseError, iterparse

from app.catalog import Catalog


MAX_BYTES = 512 * 1024 * 1024


@contextmanager
def open_feed(path):
    """Определяет gzip по сигнатуре, поскольку расширение выгрузки может отличаться."""
    with path.open("rb") as raw:
        if raw.peek(2).startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=raw) as stream:
                yield stream
        else:
            yield raw


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(url, destination):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Для выгрузки требуется HTTPS URL без логина и пароля")
    try:
        request = Request(url, headers={"Accept": "application/xml, text/xml, */*"})
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_BYTES:
                raise ValueError("Выгрузка больше допустимого размера")
            with destination.open("wb") as file:
                remaining = MAX_BYTES
                while chunk := response.read(1024 * 1024):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError("Выгрузка больше допустимого размера")
                    file.write(chunk)
    except HTTPError as exc:
        raise ValueError(f"Сервер выгрузки вернул HTTP {exc.code}") from None
    except (OSError, HTTPException):
        raise ValueError("Не удалось скачать выгрузку по HTTPS") from None


def rows(file):
    """Разбирает YML потоком, оставляя в памяти текущее предложение и справочник категорий."""
    parents = []
    categories = {}
    count = 0
    for event, node in iterparse(file, events=("start", "end")):
        if event == "start":
            if not parents and node.tag != "yml_catalog":
                raise ValueError("Ожидается XML с корнем yml_catalog")
            parents.append(node)
            continue
        parents.pop()
        tags = tuple(parent.tag for parent in parents)
        if tags == ("yml_catalog", "shop", "categories") and node.tag == "category":
            categories[node.get("id")] = node.text or ""
            parents[-1].remove(node)
        elif tags == ("yml_catalog", "shop", "offers") and node.tag == "offer":
            count += 1
            position = f"YML, предложение {count}"
            price = (node.findtext("price") or "").strip()
            currency = (node.findtext("currencyId") or "").strip().upper()
            if price and not re.fullmatch(r"[0-9]{1,17}(?:\.[0-9]{1,2})?", price):
                raise ValueError(f"{position}: неверный формат цены")
            rubles, _, kopecks = price.partition(".")
            minor = str(int(rubles) * 100 + int(kopecks.ljust(2, "0"))) if price else ""
            if node.get("available", "").lower() == "false" or currency not in {"RUB", "RUR"}:
                minor = ""
            name = (node.findtext("name") or "").strip()
            if not name:
                name = " ".join(
                    value for tag in ("typePrefix", "vendor", "model")
                    if (value := (node.findtext(tag) or "").strip())
                )
            description_node = node.find("description")
            description = "".join(description_node.itertext()) if description_node is not None else ""
            yield position, {
                "sku": node.get("id", ""),
                "vendor_code": node.findtext("vendorCode") or "",
                "name": name,
                "category": categories.get(node.findtext("categoryId"), ""),
                "description": description,
                "url": node.findtext("url") or "",
                "price_minor": minor,
                "currency": "RUB",
            }
            parents[-1].remove(node)


def main():
    parser = argparse.ArgumentParser(description="Обновление полного каталога Softline из YML")
    parser.add_argument("database", type=Path)
    parser.add_argument("--file", type=Path, help="Локальный экспорт YML. Иначе используется SOFTLINE_FEED_URL")
    parser.add_argument("--expected-count", type=int, required=True, help="Число offer в снимке по данным поставщика")
    args = parser.parse_args()
    url = os.getenv("SOFTLINE_FEED_URL")
    if args.expected_count < 1:
        parser.error("--expected-count должен быть больше нуля")
    if args.file is None and not url:
        parser.error("Укажите --file либо задайте SOFTLINE_FEED_URL в окружении")
    try:
        with tempfile.TemporaryDirectory(prefix="support-feed-") as temporary:
            path = args.file
            if path is None:
                path = Path(temporary) / "feed.xml"
                download(url, path)
            with open_feed(path) as file:
                count = Catalog(args.database).replace_snapshot(
                    rows(file), source="softline_feed", expected_count=args.expected_count,
                )
    except (EOFError, OSError, ValueError, ParseError, sqlite3.Error) as error:
        parser.exit(1, f"Ошибка обновления каталога: {error}\n")
    print(f"Каталог Softline обновлён. Загружено SKU: {count}")


if __name__ == "__main__":
    main()
