"""Выделяет чёткий подбор и точную модель до общего поиска по словам."""

from dataclasses import replace
import json
import re

from app.catalog_advice import item_text, price_text
from app.catalog_matching import MODEL_DETAIL, ProductScope, STARTS, budget_limit, catalog_parameters, normalize_codes

PURPOSE = re.compile(r"\b(?:офис\w*|работ\w*|печата\w*|печати|звонк\w*|переговор\w*|учеб\w*|игр\w*|игров\w*|монтаж\w*|дизайн\w*|программирован\w*|архив\w*|дома|домашн\w*)\b")
CATEGORY_QUESTION = re.compile(r"\b(?:есть|подобр\w*|подбер\w*|подбор\w*|выбер\w*|выбрат\w*|посовет\w*|нуж\w*|ищ\w*|помог\w*|куп\w*)\b")
FEATURE_LABELS = re.compile(r"диагональ|разрешение|тип матрицы|интерфейс|порт|оперативн|частота экрана|установленного ssd|емкость диска")
JUNK_CODES = re.compile(r"^(?:usb\d*|hdmi\d*|nvme\d*|ddr\d|pcie\d*|ssd\d*|hdd\d*|wifi\d*|rtx\d*|[0-9]+(?:gb|tb|hz|k|inch)|m2)$")


def qualification_prompt(category, requirements, *, has_budget, has_purpose, has_spec):
    missing = []
    if not has_purpose and not has_spec:
        missing.append("для чего нужен товар")
    if not has_budget:
        missing.append("какой бюджет")
    if category == "monitor" and not re.search(r"\b(?:диагонал\w*|дюйм\w*|4k|full\s*hd)\b", requirements or ""):
        missing.append("какая диагональ удобна")
    elif category == "printer" and not re.search(r"\b(?:цветн\w*|черно-бел\w*|монохром\w*)\b", requirements or ""):
        missing.append("нужна ли цветная печать")
    return "Подскажите, пожалуйста, " + ", ".join(missing[:-1]) + (" и " if len(missing) > 1 else "") + missing[-1] + "."


def category_for(text):
    current = normalize_codes(text)
    mentions = [(m.start(), category) for category, pattern in STARTS if (m := re.search(pattern, current))]
    if mentions:
        category = min(mentions)[1]
        if category == "drive":
            return "external" if re.search(r"внешн|портативн|hdd", current) else "ssd"
        return category
    return None


def model_codes(text):
    found = []
    for code in MODEL_DETAIL.findall(normalize_codes(text)):
        if re.search(r"[a-z]", code) and len(code) >= 4 and not JUNK_CODES.fullmatch(code):
            found.append(code)
    return list(dict.fromkeys(found))


def short_facts(item, expression=FEATURE_LABELS):
    selected = []
    for name, entries in catalog_parameters(item).items():
        if expression.search(normalize_codes(name)):
            values = ", ".join(" ".join((value, unit)).strip() for value, unit in entries)
            selected.append(f"{name}: {values}")
        if len(selected) == 3:
            break
    return "; ".join(selected) or item["description"].strip()[:165]


def named_product(catalog, model, scope=None):
    found = catalog.search(model, 25)
    result = []
    for item in found:
        exact_name = re.search(rf"(?<![\w-]){re.escape(model)}(?![\w-])", normalize_codes(item["name"]))
        raw = item.get("feed_fields") or ""
        # Разрешаем артикул производителя через штатный поиск. Модель со сходным
        # префиксом в названии не выдаётся за запрошенную.
        if not exact_name:
            values = json.loads(raw or "{}").get("fields", {}).get("vendorCode", [])
            if not any(normalize_codes(value["text"]) == model for value in values):
                continue
        if scope and not scope.matches(item):
            continue
        result.append(item)
    return result


def shortlist(catalog, scope, purpose, quantity=None):
    desired = None
    if purpose and scope.category == "monitor":
        if re.search(r"дизайн|монтаж", purpose):
            desired = re.compile(r"3840\s*[x×х]\s*2160|\b4k\b")
        elif re.search(r"офис|учеб", purpose):
            desired = re.compile(r"\b(?:ips|fullhd|1920\s*[x×х]\s*1080)\b")
    if desired is None:
        desired = re.compile(r"(?!x)x")
    pool = catalog.search("подбор", 10000, scope=scope)
    distinct = {}
    for item in pool:
        if item["price_minor"] is None or not scope.matches(item):
            continue
        key = normalize_codes(item["name"])
        distinct.setdefault(key, item)
    # Сначала полезный для задачи подтверждённый признак, затем информативные
    # карточки. Цена используется для устойчивого порядка при равных условиях.
    ranked = sorted(distinct.values(), key=lambda item: (
        not bool(desired.search(normalize_codes(short_facts(item)))),
        not bool(short_facts(item)), item["price_minor"], item["sku"],
    ))
    return ranked[:3]


def recommendation(items, quantity=None, *, alternatives=False):
    if len(items) == 1:
        title = "Точная модель не найдена. Нашёл один подходящий аналог:" if alternatives else "Нашёл один вариант, который отвечает всем условиям:"
    else:
        title = "Точная модель не найдена. Под ваши условия подходят такие аналоги:" if alternatives else "Под ваши условия подходят:"
    lines = [title]
    facts = [short_facts(item) for item in items]
    for item, fact in zip(items, facts):
        line = f"• {item_text(item).replace(chr(10), ' · ')}"
        if quantity and quantity > 1:
            line += f" За {quantity} шт. — {price_text(item['price_minor'] * quantity)}."
        if fact:
            line += " " + fact
        lines.append(line)
    if len(items) > 1:
        if facts[0] and len(set(facts)) > 1:
            lines.append("Первый вариант: " + facts[0] + ". Выбирайте другой, если из перечисленных отличий важнее его характеристика.")
        else:
            lines.append("По указанным характеристикам преимуществ не видно. Сравните цены и проверьте наличие перед заказом.")
    return "\n\n".join(lines)


def compare_named_products(catalog, labels):
    """Для сравнения ищет ровно две названные позиции независимо от категории."""
    items = []
    for code in labels:
        candidates = named_product(catalog, code)
        item = min(candidates, key=lambda row: (row["price_minor"] is None, row["price_minor"] or 0),
                   default=None)
        if item is None:
            return "Одну из моделей не нашёл. Уточните её артикул, чтобы сравнить обе позиции.", []
        items.append(item)
    if items[0]["sku"] == items[1]["sku"]:
        return "Оба обозначения ведут к одной позиции каталога.", []
    return "\n\n".join(item_text(item) for item in items), items


def alternative_scope(scope, category, budget, text):
    template = scope or ProductScope(category)
    brands = {"dell", "philips", "samsung", "asus", "acer", "msi", "lenovo", "benq", "aoc", "lg"}
    constraints = tuple(pattern for pattern in template.requirements
                        if pattern.strip("\\b") not in brands or re.search(r"\bтолько\b", text))
    return replace(template, model=None, max_price=budget, requirements=constraints)
