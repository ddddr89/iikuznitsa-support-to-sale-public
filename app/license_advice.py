"""Уточнение и каталожная проверка именных запросов на лицензии."""

import re

from app.automatic_documents import number
from app.catalog_advice import item_text, price_text
from app.catalog_matching import catalog_parameters, normalize

# Семейства в фиде, для которых краткое название без слова «лицензия»
# означает запрос на программный продукт. Редакции перечислены отдельно.
FAMILIES = (
    ("mdaemon", r"messaging server|standard|pro"),
    ("communigate pro", r"smb connect|enterprise|community|standard"),
    ("dr.web", r"gateway security suite|desktop security suite|server security suite|security space|katana|curenet|малый бизнес"),
    ("radmin", r"3"),
    ("winrar", None),
    ("microsoft 365", r"business (?:standard|basic|premium)|бизнес (?:стандарт|базовый|премиум)"),
)
EXPLICIT = re.compile(
    r'(?:^|[.!?]\s*|\b(?:нужна|купить|подберите|подбери)\s+)лицензи[яю]\s+(?:на\s+)?'
    r'([^.!?\n,]+?)(?=\s+(?:для|на|сроком)\s|[.!?\n,]|$)'
)
COUNTS = re.compile(r"\b([1-9]\d{0,6})\s*(?:именных\s+)?(пользовател\w*|сотрудник\w*|устройств\w*|рабоч\w* (?:мест\w*|станци\w*)|компьютер\w*|пк)\b")
TERM = re.compile(r"\b(?:(\d+|один|одну|два|две|три|четыре)\s*)?(год(?:а|ов)?|лет|месяц\w*|мес\.?)(?!\w)|\b(бессроч\w*)\b")
PLATFORM = re.compile(r"\b(?:windows(?:\s+(?:7|8|10|11|server))?|linux|macos)\b")
NEW = re.compile(r"\b(?:нов(?:ая|ую|ой)\s+лицензи\w*|первичн\w*|(?:покупаем|берем|приобретаем|закупаем)\s+(?:впервые|в\s+первый\s+раз)|первая\s+покупка|(?:тип лицензии:\s*)(?:базовая|полная версия))\b")
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


def parse_date_number(value):
    return number(value) if value else 1


def terms(text):
    result = set()
    for number, unit, perpetual in TERM.findall(text):
        if perpetual:
            result.add(0)
        elif number or unit == "год":
            result.add((int(number) if number.isdecimal() else parse_date_number(number)) * (1 if unit.startswith("мес") else 12))
    return result


def purchase(text):
    # «Новая лицензия или продление» не является выбором типа закупки.
    result = {kind for kind, pattern in (("new", NEW), ("renew", RENEWAL)) if any(not re.match(r"\s+не\s+подходит", text[match.end():])
                     and not re.search(r"\bне\s+$", text[:match.start()]) for match in pattern.finditer(text))}
    return next(iter(result)) if len(result) == 1 else None


def parameter_reply(text):
    return (COUNTS.search(text) or terms(text) or purchase(text) or UNKNOWN.search(text)
            or PLATFORM.fullmatch(text.rstrip(".!")) or EDITION_REPLY.fullmatch(text.rstrip(".!")))


def seat_unit(label):
    if label.startswith(("пользовател", "сотрудник")):
        return "пользователи"
    if label.startswith("устройств"):
        return "устройства"
    return "компьютеры"


def collect(facts, text, editions):
    requested = {seat_unit(label): int(value) for value, label in COUNTS.findall(text)}
    if not requested and facts.get("asked") == "count" and re.fullmatch(r"[1-9]\d{0,6}[.!]?", text):
        requested = {facts.get("seat_basis", "не уточнено"): int(text.rstrip(".!"))}
    if requested:
        facts["seats"] = requested
    if re.search(r"\b(?:компьютер\w*|рабоч\w* станци\w*|пк)\b", text):
        facts["seat_basis"] = "компьютеры"
    elif re.search(r"\bпользовател\w*\b", text):
        facts["seat_basis"] = "пользователи"
    if kinds := purchase(text):
        facts["kind"] = kinds
    if (durations := terms(text)) and len(durations) == 1:
        facts["months"] = durations.pop()
    if platforms := PLATFORM.findall(text):
        platforms = list(dict.fromkeys(platforms))
        facts["platforms"] = platforms
        facts["platform"] = " / ".join(platforms)
    if edition := named_edition(text, editions):
        facts["edition"] = edition
        facts.pop("edition_unknown", None)
    elif facts.get("asked") == "edition" and UNKNOWN.search(text):
        facts["edition_unknown"] = True
    if re.search(r"\bкомпан\w*|организац\w*|юрлиц\w*|корпоративн\w*", text):
        facts["company"] = True


def candidates(catalog, facts, edition):
    items = []
    for item in catalog.search_by_name(facts["family"]):
        name = normalize(item["name"])
        description = normalize(item["description"])
        parameters = catalog_parameters(item)
        if edition and named_edition(name, facts["editions"]) != edition:
            continue
        if facts.get("company") and re.search(r"физлицо|для дома", " ".join(
            normalize(v) for key in ("Пользователь", "Назначение") for v, _ in parameters.get(key, [])
        )):
            continue
        sizes = COUNTS.findall(name + " " + description)
        if not sizes or any(facts["seats"].get(seat_unit(label)) != int(value) for value, label in sizes):
            continue
        # Явная единица в карточке важнее импортированного общего поля
        # «К-во пользователей до», которое встречается и у пакетов на ПК.
        kinds = " ".join(normalize(value) for value, _ in parameters.get("Тип лицензии", []))
        if purchase(kinds) not in (None, facts["kind"]) or (purchase(kinds) or purchase(name + " " + description)) != facts["kind"]:
            continue
        duration = terms(" ".join(normalize(value) for value, _ in parameters.get("Срок действия", [])))
        duration = duration or terms(name + " " + description)
        if duration != {facts["months"]}:
            continue
        compatibility = normalize(" ".join(value for value, _ in parameters.get("Совместимая архитектура/платформа", [])))
        platforms = facts.get("platforms", [facts["platform"]] if facts.get("platform") else [])
        evidence = compatibility or description
        supported = all(bool(re.search(rf"\b{re.escape(platform)}\b" + (r"(?!\s+server)" if platform == "windows" else ""),
                                       evidence)) for platform in platforms)
        items.append((item, supported))
    return items


def license_answer(message, prior_questions, catalog, *, state=None):
    """Заполняет только параметры покупателя. Хранит единицы лицензирования и неизвестную редакцию."""
    current = normalize(message).strip()
    named = product_in(current)
    stored = state.facts.get("license") if state and state.route == "license" else None
    if not (named or stored or parameter_reply(current)):
        return None
    if stored and not named and not (parameter_reply(current) or stored.get("asked") == "count"
                                     and re.fullmatch(r"[1-9]\d{0,6}[.!]?", current)):
        return None
    if stored and (not named or named[0] == stored["family"]):
        facts = stored
    else:
        facts = {}
        previous_turns = []
        # Совместимость со старыми сессиями и публичным API без cookies.
        if not named:
            for prior in reversed(prior_questions):
                older = normalize(prior)
                if found := product_in(older):
                    named = found
                    previous_turns.append(older)
                    break
                if not parameter_reply(older):
                    break
                previous_turns.append(older)
        if not named:
            return None
        family, editions, edition = named
        facts.update(family=family, editions=editions)
        if edition:
            facts["edition"] = edition
        for turn in reversed(previous_turns):
            collect(facts, turn, editions)
    collect(facts, current, facts["editions"])
    if state and state.route == "license":
        state.facts["license"] = facts
    missing = []
    seats = facts.get("seats", {})
    if not seats or "не уточнено" in seats:
        missing.append(("count", "на сколько пользователей или устройств нужна лицензия"))
    if "kind" not in facts:
        missing.append(("kind", "это новая лицензия или продление"))
    if "months" not in facts:
        missing.append(("term", "на какой срок нужна лицензия (или нужна ли бессрочная)"))
    edition = facts.get("edition")
    if missing:
        facts["asked"] = missing[0][0]
        return "Уточните, пожалуйста: " + "; ".join(q for _, q in missing) + ".", [], False

    matches = candidates(catalog, facts, edition)
    if facts["editions"] and not edition:
        facts["asked"] = "edition"
        if facts["kind"] != "renew":
            return "Уточните, пожалуйста, какая редакция продукта нужна.", [], True
        priced = [(item, supported) for item, supported in matches if item["price_minor"] is not None]
        examples = []
        for item, supported in priced:
            if item["name"] not in [name for name, _, _ in examples]:
                examples.append((item["name"], item["price_minor"], supported))
            if len(examples) == 3:
                break
        text = "Для продления нужно сверить редакцию действующей лицензии."
        if examples and not facts.get("edition_unknown"):
            platform = facts.get("platform")
            text += " Варианты на указанный срок и количество для оценки бюджета. Совместимость с вашей лицензией пока не подтверждена:"
            for name, price, supported in examples:
                status = f" Поддержка {platform.title()} в карточке не подтверждена." if platform and not supported else ""
                text += f"\n• {name} — {price_text(price)}.{status}"
            if len(examples) == 2:
                text += f"\nРазница в цене — {price_text(abs(examples[0][1] - examples[1][1]))}."
        if not facts.get("edition_unknown"):
            text += " Как называется редакция в вашей лицензии или личном кабинете?"
        else:
            text = ("Тогда менеджер может проверить редакцию действующей лицензии. "
                    "Новую лицензию рассмотрим, если вы согласны на этот вариант.")
        return text, [], True

    confirmed = [item for item, supported in matches if supported]
    if confirmed:
        facts["asked"] = None
        distinct = list({item["name"]: item for item in reversed(confirmed)}.values())[::-1][:3]
        return "Подтверждённые позиции:\n" + "\n".join("• " + item_text(item) for item in distinct) + "\nУсловия поставки подтвердит менеджер.", distinct, True
    facts["asked"] = None
    return NO_MATCH + " Менеджер проверит редакцию, условия приобретения и цену. Новую лицензию можем рассмотреть с вашего согласия.", [], True
