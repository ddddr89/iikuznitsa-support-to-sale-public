"""Разбирает запрос КП и проекта счёта по подтверждённому товару."""

from dataclasses import dataclass
import re

from app.catalog_matching import MODEL_DETAIL, normalize_codes


DOCUMENT = r"(?:кп|коммерческ\w+ предложени\w*|(?:проект\s+)?счет(?:а|у|ом)?)"
REQUEST = r"(?:подготов\w*|сформир\w*|сдела\w*|выстав\w*|выпиш\w*|пришл\w*|отправ\w*|запрос\w*|дайте|нуж(?:ен|ны|но)|хочу|можно)"
INTENT = re.compile(rf"\b{REQUEST}\b[^.!?]{{0,70}}\b{DOCUMENT}\b|^{DOCUMENT}(?:\s+на\s+[^.!?]+|,?\s+пожалуйста)?[.!?]?$")
SKU_TOKEN = r"[a-z0-9]+(?:[-_][a-z0-9]+)*"
REFERENCE = re.compile(
    rf"\b(?:артикул\w*|sku|код товара|на товар)"
    rf"\s*[:№#]?\s*({SKU_TOKEN})\b", re.IGNORECASE,
)
FOLLOW_UP = "Для подготовки КП и проекта счёта"
SPECIAL_TERMS = re.compile(r"\b(?:скидк\w*|отсрочк\w*|рассрочк\w*|без ндс|ндс\s*\d+|"
                           r"(?:с учетом|включая) доставк\w*|поставк\w* к)\b")
WORDS = {"один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
         "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
AMOUNT = r"(?:[+-]?\d+(?:[.,]\d+)?|" + "|".join(WORDS) + ")"
PRODUCT_COUNT = (r"монитор\w*|ноутбук\w*|накопител\w*|диск\w*|коммутатор\w*|"
                 r"видеокарт\w*|принтер\w*|планшет\w*|сервер\w*|ssd|hdd")
QUANTITY = re.compile(rf"(?<![\w.,+\-])({AMOUNT})\s*(?:штук\w*|шт\.?|единиц\w*|{PRODUCT_COUNT})\b")
LABELLED_QUANTITY = re.compile(rf"\bколичество\s*[:—-]?\s*({AMOUNT})(?![\w.,])")
QUANTITY_ONLY = re.compile(rf"(?:нужн(?:ы|о)\s+|хочу\s+|возьму\s+|количество\s*[:—-]?\s*)?({AMOUNT})(?:\s*(?:штук\w*|шт\.?|единиц\w*))?[.!]?")


@dataclass(frozen=True)
class DocumentRequest:
    sku: str | None
    quantity: int | None
    missing: str | None
    product: dict | None
    handoff: bool = False


def explicit_references(text):
    return {match[1] for match in REFERENCE.finditer(text)
            if not match[0].lower().startswith("на товар") or any(char.isdigit() for char in match[1])}


def number(value):
    if value in WORDS:
        return WORDS[value]
    if re.fullmatch(r"\d{1,3}", value):
        return int(value) or None
    return None


def quantity_in(text, *, standalone=False):
    values = {match[1] for pattern in (QUANTITY, LABELLED_QUANTITY) for match in pattern.finditer(text)}
    if not values and standalone and (match := QUANTITY_ONLY.fullmatch(text)):
        values = {match[1]}
    if not values:
        return False, None
    numbers = {number(value) for value in values}
    return True, next(iter(numbers)) if len(numbers) == 1 else None


def catalog_item(catalog, reference):
    # SKU без вариантов регистра проверяется напрямую. Поиск нужен только
    # для регистра SKU, его ответ принимается исключительно при равенстве.
    item = catalog.get(reference)
    if item:
        return item
    return next((item for item in catalog.search(reference, 1)
                 if item["sku"].casefold() == reference.casefold()), None)


def recently_shown(catalog, history, sku, models):
    """Читает ближайший товарный ответ и количество после него."""
    quantity_seen = False
    quantity = None
    for turn in reversed(history):
        question = normalize_codes(turn["question"])
        answer = turn["answer"]
        present, count = quantity_in(question, standalone=bool(QUANTITY_ONLY.fullmatch(question)))
        references = explicit_references(answer)
        if references and not answer.startswith(FOLLOW_UP):
            candidates = [item for ref in references if (item := catalog_item(catalog, ref))]
            if sku:
                candidates = [item for item in candidates if item["sku"] == sku]
            elif models:
                candidates = [item for item in candidates
                              if models.intersection(MODEL_DETAIL.findall(normalize_codes(item["name"])))]
            if len(candidates) != 1:
                return None, None
            return candidates[0], quantity if quantity_seen else count
        if answer.startswith(FOLLOW_UP) or QUANTITY_ONLY.fullmatch(question):
            if present and not quantity_seen:
                quantity_seen, quantity = True, count
            continue
        break
    return None, quantity


def document_request(message, trusted_history, catalog):
    """Возвращает план выдачи либо уточнение из текущего и серверных ходов."""
    text = normalize_codes(message).strip()
    if re.match(r"^(?:подскажите,?\s+)?(?:как|что нужно|какие документы|где|почему)\b", text):
        return None
    if re.search(r"\bне\s+(?:надо|нужно|хочу|готовьте|формируйте|выставляйте|отправляйте)\b", text):
        return None
    pending = bool(trusted_history and trusted_history[-1]["answer"].startswith(FOLLOW_UP))
    explicit = explicit_references(message)
    prior_need_sku = pending and "артикул" in trusted_history[-1]["answer"]
    if prior_need_sku and re.fullmatch(SKU_TOKEN, text) and (
        re.fullmatch(r"\d{4,12}", text) or catalog_item(catalog, text)
    ):
        explicit = {text}
    quantity_seen, quantity = quantity_in(text, standalone=pending and not bool(explicit))
    if not (INTENT.search(text) or pending and (explicit or quantity_seen)):
        return None
    models = set(MODEL_DETAIL.findall(text)) - set(MODEL_DETAIL.findall(normalize_codes(" ".join(explicit))))
    # Модель в текущей реплике ограничивает допустимый SKU из истории.
    product = catalog_item(catalog, next(iter(explicit))) if len(explicit) == 1 else None
    if product and models and not models.issubset(MODEL_DETAIL.findall(normalize_codes(product["name"]))):
        product = None
    shown, previous_quantity = recently_shown(catalog, trusted_history, product["sku"] if product else None, models)
    if not explicit:
        product = shown
        if pending and not product:
            previous_references = explicit_references(trusted_history[-1]["answer"])
            previous_references |= explicit_references(trusted_history[-1]["question"])
            if len(previous_references) == 1:
                product = catalog_item(catalog, next(iter(previous_references)))
    if not quantity_seen:
        quantity = previous_quantity
    sku = product["sku"] if product else None
    if SPECIAL_TERMS.search(text):
        return DocumentRequest(sku, quantity, "Эти условия нельзя подтвердить по каталогу. Их проверит менеджер.", product, True)
    if not product:
        fields = "артикул и количество товара" if quantity is None else "артикул товара"
        if explicit:
            fields = "один корректный артикул товара" + (" и количество" if quantity is None else "")
        return DocumentRequest(None, quantity, f"{FOLLOW_UP} укажите {fields}.", None)
    if quantity is None:
        return DocumentRequest(sku, None, f"{FOLLOW_UP} укажите количество товара по артикулу {sku} (от 1 до 999).", product)
    if product["price_minor"] is None:
        return DocumentRequest(sku, quantity, "Цена товара по запросу. Её проверит менеджер.", product, True)
    return DocumentRequest(sku, quantity, None, product)


def document_quantity_followup(message, trusted_history, catalog):
    """Подтверждает количество для однозначной карточки, без выдачи документов."""
    text = normalize_codes(message).strip()
    if not (match := QUANTITY_ONLY.fullmatch(text)):
        return None
    if not QUANTITY.search(text) and not LABELLED_QUANTITY.search(text):
        return None
    count = number(match[1])
    product, _ = recently_shown(catalog, trusted_history, None, set())
    if count is None or product is None:
        return None
    return f"Для товара по артикулу {product['sku']} учту количество: {count} шт."
