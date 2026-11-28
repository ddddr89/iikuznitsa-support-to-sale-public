"""Update the catalog from a supplied HTTPS or local YML storefront export."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
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
from zoneinfo import ZoneInfo

from app.catalog import Catalog, catalog_import_lock


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
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError("Неверный формат HTTPS URL источника") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Для выгрузки требуется HTTPS URL без логина и пароля")
    try:
        headers = {"Accept": "application/xml, text/xml, */*"}
        if authorization := os.getenv("SOFTLINE_FEED_AUTHORIZATION"):
            if not re.fullmatch(r"[ -~]+", authorization):
                raise ValueError("Недопустимый формат авторизации источника")
            headers["Authorization"] = authorization
        request = Request(url, headers=headers)
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError(f"Для полного снимка ожидался HTTP 200, получен {response.status}")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_BYTES:
                raise ValueError("Выгрузка больше допустимого размера")
            with destination.open("wb") as file:
                digest = sha256()
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("Выгрузка больше допустимого размера")
                    file.write(chunk)
                    digest.update(chunk)
            if not size or (length is not None and size != int(length)):
                raise ValueError("Размер полученной выгрузки не совпадает с HTTP-ответом")
            return datetime.now(timezone.utc).isoformat(), size, digest.hexdigest()
    except HTTPError as exc:
        raise ValueError(f"Сервер выгрузки вернул HTTP {exc.code}") from None
    except (OSError, HTTPException):
        raise ValueError("Не удалось скачать выгрузку по HTTPS") from None


def fresh_feed_date(path, downloaded_at):
    """YML без смещения трактуется как московское время поставщика."""
    with open_feed(path) as file:
        _, root = next(iterparse(file, events=("start",)), (None, None))
        if root is None or root.tag != "yml_catalog":
            raise ValueError("Ожидается XML с корнем yml_catalog")
        value = root.get("date", "")
    try:
        timestamp = datetime.fromisoformat(value)
        if len(value) < 16:
            raise ValueError
    except ValueError:
        raise ValueError("В YML отсутствуют дата и время актуальности") from None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    age = datetime.fromisoformat(downloaded_at) - timestamp
    if not -timedelta(minutes=15) <= age <= timedelta(hours=48):
        raise ValueError("Дата актуальности YML вне допустимого окна (48 часов)")
    return timestamp.astimezone(timezone.utc).isoformat()


def field_data(node):
    """Сохраняет текст, атрибуты и вложенные поля оригинального offer."""
    value = {"text": "".join(node.itertext()).strip()}
    if node.attrib:
        value["attributes"] = dict(node.attrib)
    if len(node):
        value["children"] = {tag: [field_data(child) for child in node.findall(tag)]
                             for tag in dict.fromkeys(child.tag for child in node)}
    return value


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
            fields = {tag: [field_data(child) for child in node.findall(tag)]
                      for tag in dict.fromkeys(child.tag for child in node)}
            search_values = []
            ignored = {"url", "picture", "price", "oldprice", "currencyId", "categoryId", "description", "name"}
            for tag, values in fields.items():
                if tag in ignored:
                    continue
                for value in values:
                    attributes = value.get("attributes", {})
                    if not value["text"] or value["text"].casefold() in {"другое или отсутствует", "не указано", "нет данных", "нет", "no", "false", "-"}:
                        continue
                    label = attributes.get("name", tag)
                    unit = attributes.get("unit", "")
                    search_values.append(" ".join(v for v in (label, value["text"], unit) if v))
            yield position, {
                "sku": node.get("id", ""),
                "vendor_code": node.findtext("vendorCode") or "",
                "name": name,
                "category": categories.get(node.findtext("categoryId"), ""),
                "description": description,
                "url": node.findtext("url") or "",
                "price_minor": minor,
                "currency": "RUB",
                "specifications": "\n".join(search_values),
                "feed_fields": json.dumps({"offer": dict(node.attrib), "fields": fields}, ensure_ascii=False),

            }
            parents[-1].remove(node)


def main():
    parser = argparse.ArgumentParser(description="Обновление полного каталога Softline из YML")
    parser.add_argument("database", type=Path)
    parser.add_argument("--file", type=Path, help="Локальный экспорт YML. Иначе используется SOFTLINE_FEED_URL")
    parser.add_argument("--expected-count", type=int, help="Точное число offer, подтверждённое поставщиком")
    parser.add_argument("--remote", action="store_true", help="Только HTTPS: проверка актуальности и квитанция синхронизации")
    args = parser.parse_args()
    url = os.getenv("SOFTLINE_FEED_URL")
    if args.remote and args.expected_count is None and (configured_count := os.getenv("SOFTLINE_EXPECTED_COUNT")):
        try:
            args.expected_count = int(configured_count)
        except ValueError:
            parser.error("SOFTLINE_EXPECTED_COUNT должно быть целым числом")
    if args.expected_count is not None and args.expected_count < 1:
        parser.error("--expected-count должен быть больше нуля")
    if args.file is None and not url:
        parser.error("Не настроен HTTPS-источник: требуется SOFTLINE_FEED_URL")
    if args.remote and args.file is not None:
        parser.error("--remote не допускает локальный файл")
    if not args.remote and args.expected_count is None:
        parser.error("Для ручного импорта требуется --expected-count")
    try:
        with catalog_import_lock(args.database), tempfile.TemporaryDirectory(prefix="support-feed-") as temporary:
            catalog = Catalog(args.database)
            path = args.file
            receipt = None
            minimum = None
            if args.remote:
                catalog.init()
                last = catalog.remote_sync_status()
                if args.expected_count is None:
                    minimum = max(catalog.status()["item_count"], last["item_count"] if last else 0)
                if minimum == 0:
                    raise ValueError("Для первого сетевого импорта требуется подтверждённое --expected-count")
            if path is None:
                path = Path(temporary) / "feed.xml"
                downloaded_at, size, digest = download(url, path)
                if args.remote:
                    feed_date = fresh_feed_date(path, downloaded_at)
                    if last and feed_date < last["feed_date"]:
                        raise ValueError("Полученный YML старше предыдущего успешно загруженного")
                    receipt = (downloaded_at, feed_date, size, digest)
            with open_feed(path) as file:
                count = catalog.replace_snapshot(
                    rows(file), source="softline_remote_feed" if receipt else "softline_feed",
                    expected_count=args.expected_count, minimum_count=minimum, receipt=receipt,
                )
    except (EOFError, OSError, ValueError, ParseError, sqlite3.Error) as error:
        parser.exit(1, f"Ошибка обновления каталога: {error}\n")
    print(f"Каталог Softline обновлён. Загружено SKU: {count}")
    if receipt:
        print(f"HTTPS-получение: {downloaded_at}; дата YML: {feed_date}; байты: {size}; SHA-256: {digest}")


if __name__ == "__main__":
    main()
