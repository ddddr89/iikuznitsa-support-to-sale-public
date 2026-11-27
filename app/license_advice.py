"""Уточнение и каталожная проверка именных запросов на лицензии."""

import re

from app.catalog_advice import item_text
from app.catalog_matching import normalize

# Семейства в фиде, для которых краткое название без слова «лицензия»
# означает запрос на программный продукт. Редакции перечислены отдельно.
FAMILIES = (
    ("mdaemon", r"messaging server|standard|pro"),
    ("communigate pro", r"smb connect|enterprise|community|standard"),
    ("dr.web", r"gateway security suite|desktop security suite|server security suite|малый бизнес"),
    ("radmin", r"3"),
    ("winrar", None),
    ("microsoft 365", r"business (?:standard|basic|premium)|бизнес (?:стандарт|базовый|премиум)"),
)
EXPLICIT = re.compile(
    r'(?:^|[.!?]\s*|\b(?:нужна|купить|подберите|подбери)\s+)лицензи[яю]\s+(?:на\s+)?'
    r'([^.!?\n,]+?)(?=\s+(?:для|на|сроком)\s|[.!?\n,]|$)'
)
SEATS = re.compile(r"\b([1-9]\d{0,6})\s*(?:именных\s+)?(?:пользовател\w*|сотрудник\w*|устройств\w*|рабоч\w* мест\w*|пк)\b")
TERM = re.compile(r"\b(?:(\d+)\s*)?(год(?:а|ов)?|лет|месяц\w*|мес\.?)(?!\w)|\b(бессроч\w*)\b")
NEW = re.compile(r"\b(?:нов(?:ая|ую|ой)\s+лицензи\w*|первичн\w*|(?:тип лицензии:\s*)(?:базовая|полная версия))\b")
RENEWAL = re.compile(r"\b(?:продлен\w*|продлить|renewal)\b")
UNKNOWN = re.compile(r"\b(?:не знаю|не определил\w*|не могу уточнить)\b")
NO_MATCH = "Не удалось подтвердить лицензию по указанным параметрам в каталоге."
EDITION_REPLY = re.compile("|".join(rf"\b(?:{editions})\b" for _, editions in FAMILIES if editions))
EDITION_ALIASES = {
    "business standard": "бизнес стандарт", "business basic": "бизнес базовый",
    "business premium": "бизнес премиум",
}


def product_in(text):
    for family, editions in FAMILIES:
        match = re.search(rf"(?<!\w){re.escape(family)}(?!\w)", text)
        if match:
            edition = named_edition(text[match.end():], editions)
            # Задача «нужны Outlook и Word» остаётся подбором по потребности.
            if family == "microsoft 365" and not edition and re.search(r"\b(?:outlook|word|excel|exchange)\b", text):
                return None
            return family, editions, edition
    if "windows server" in text:
        return None  # Для этой модели действуют отдельные правила ядер, ВМ и CAL.
    explicit = EXPLICIT.search(text)
    if explicit:
        family = explicit[1].strip(' «»"')
        if re.match(r"[^\W\d_]", family) and not terms(family) and not purchase(family):
            return family, None, None
    return None


def named_edition(text, editions):
    if not editions:
        return None
    values = {EDITION_ALIASES.get(value, value)
              for value in re.findall(rf"\b(?:{editions})\b", text)}
    return next(iter(values)) if len(values) == 1 else None


def terms(text):
    result = set()
    for number, unit, perpetual in TERM.findall(text):
        if perpetual:
            result.add(0)
        elif number or unit == "год":
            result.add(int(number or 1) * (1 if unit.startswith("мес") else 12))
    return result


def purchase(text):
    # «Новая лицензия или продление» не является выбором типа закупки.
    result = {kind for kind, pattern in (("new", NEW), ("renew", RENEWAL)) if pattern.search(text)}
    return next(iter(result)) if len(result) == 1 else None


def parameter_reply(text):
    return (SEATS.search(text) or terms(text) or purchase(text) or UNKNOWN.search(text)
            or EDITION_REPLY.fullmatch(text.rstrip(".!")))


def license_answer(message, prior_questions, catalog):
    """Собирает только параметры клиента текущего продукта и проверяет их по фиду."""
    current = normalize(message)
    current_product = product_in(current)
    family_info = current_product
    if family_info is None and not parameter_reply(current):
        return None
    turns = [current]
    for prior in reversed(prior_questions):
        older = normalize(prior)
        older_product = product_in(older)
        if older_product:
            if family_info and family_info[0] != older_product[0]:
                break
            family_info = family_info or older_product
            turns.append(older)
            break
        if not parameter_reply(older):
            break
        turns.append(older)
    if family_info is None:
        return None
    family, editions, edition = family_info
    if current_product is None:
        edition = next((value for turn in turns if (value := named_edition(turn, editions))), edition)
    seats = next((sizes for turn in turns if (sizes := set(SEATS.findall(turn)))), set())
    kind = next((value for turn in turns if (value := purchase(turn))), None)
    duration = next((value for turn in turns if (value := terms(turn))), set())
    missing = []
    if len(seats) != 1:
        missing.append("на сколько пользователей или устройств нужна лицензия")
    if kind is None:
        missing.append("это новая лицензия или продление")
    if len(duration) != 1:
        missing.append("на какой срок нужна лицензия (или нужна ли бессрочная)")
    if editions and edition is None:
        missing.append("какая редакция продукта нужна")
    if UNKNOWN.search(current):
        return "Не удалось подтвердить все параметры лицензирования. Менеджер поможет их определить.", [], False
    if missing:
        return "Уточните, пожалуйста: " + "; ".join(missing) + ".", [], False

    # Требуем положительное подтверждение каждого параметра. Например, подписка
    # без указания типа закупки не подтверждает новую лицензию или продление.
    requested_seats = {int(value) for value in seats}
    edition_forms = {edition, EDITION_ALIASES.get(edition)} | {
        key for key, val in EDITION_ALIASES.items() if val == edition
    }
    matches = []
    for item in catalog.search_by_name(family):
        name = normalize(item["name"])
        evidence = name + " " + normalize(item["description"])
        sizes = {int(match) for match in SEATS.findall(evidence)}
        if edition and not any(re.search(rf"\b{re.escape(form)}\b", name) for form in edition_forms if form):
            continue
        if sizes != requested_seats or purchase(evidence) != kind or terms(evidence) != duration:
            continue
        matches.append(item)
    distinct = {normalize(item["name"]) for item in matches}
    if len(distinct) == 1:
        item = matches[0]
        return "По указанным параметрам в каталоге есть " + item_text(item) + ". Условия поставки подтвердит менеджер.", [item], True
    if len(distinct) > 1:
        return "Уточните полное название редакции или комплекта: в каталоге есть несколько вариантов с этими параметрами.", [], True
    return NO_MATCH + " Менеджер проверит редакцию, условия приобретения и цену.", [], True
