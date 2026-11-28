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


def named_product(catalog, model, scope=None, *, exact_phrase=False):
    found = catalog.search(model, 25)
    if exact_phrase and "-" in model:
        found = [*catalog.search_by_name(model), *found]
        found = list({item["sku"]: item for item in found}.values())
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


def comparison_codes(question, catalog):
    """Распознаёт составные коды моделей и отделяет производителя от модели."""
    text = normalize_codes(question).translate(str.maketrans("‑–—", "---"))
    serials = re.search(r"\b(?:артикулы|sku)\s*[:№]?\s*(\d{4,12}(?:\s*(?:,|и|/|;|\s)\s*\d{4,12})+)", text)
    if serials:
        return list(dict.fromkeys(re.findall(r"\d+", serials[1])))
    labels = list(dict.fromkeys([*model_codes(text), *re.findall(
        r"\b[a-z]{1,8}-\d[a-z0-9]+(?:-[a-z0-9]+)*\b|\b[a-z]{1,8}-\d\b", text
    )]))
    result = []
    for label in sorted(labels, key=text.index):
        # Производитель вроде A4Tech тоже содержит цифру. Сверяем его с полем vendor.
        if re.fullmatch(r"[a-z]+\d[a-z]+", label) and any(
            normalize_codes(vendor["text"]) == label
            for item in catalog.search(label, 6)
            for vendor in json.loads(item.get("feed_fields") or "{}").get("fields", {}).get("vendor", [])
        ):
            continue
        result.append(label)
    return result


def compare_named_products(catalog, labels):
    """Ищет названные позиции независимо от категории."""
    items = []
    for code in labels:
        candidates = [item] if code.isdigit() and (item := catalog.get(code)) else named_product(catalog, code, exact_phrase=True)
        item = min(candidates, key=lambda row: (row["price_minor"] is None, row["price_minor"] or 0),
                   default=None)
        if item is None:
            return "Одну из моделей не нашёл. Уточните её артикул, чтобы сравнить позиции.", []
        items.append(item)
    if len({item["sku"] for item in items}) != len(items):
        return "Некоторые обозначения ведут к одной позиции. Уточните модели для сравнения.", []
    return "", items


def alternative_scope(scope, category, budget, text):
    template = scope or ProductScope(category)
    brands = {"dell", "philips", "samsung", "asus", "acer", "msi", "lenovo", "benq", "aoc", "lg"}
    constraints = tuple(pattern for pattern in template.requirements
                        if pattern.strip("\\b") not in brands or re.search(r"\bтолько\b", text))
    return replace(template, model=None, max_price=budget, requirements=constraints)
