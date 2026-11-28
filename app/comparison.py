"""Объясняет существенные различия по подтверждённым характеристикам."""

from dataclasses import dataclass
import re

from app.catalog_advice import price_text
from app.catalog_matching import catalog_parameters, normalize, normalize_codes
from app.product_advice import model_codes

COMPARE_REQUEST = re.compile(r"\b(?:сравн\w*|отлича\w*|отличи\w*|разниц\w*)\b")
PRICE_ONLY = re.compile(r"^(?:а\s+)?(?:какая\s+)?разниц\w*\s+в\s+цене[?!. ]*$|"
                        r"\b(?:сравн\w*\b[^.!?]*\bпо цене|только цен\w*)[?!. ]*$")
FOLLOWUP = re.compile(r"\b(?:зарядк\w*|питан\w*|проводн\w* сеть|одним кабелем|ethernet|"
                      r"для ноутбук\w*|для планшет\w*|для смартфон\w*|что выбрать|какой лучше)\b")

# Дополняет карточку сведениями о назначении отдельных USB-C, которых нет в YML.
# Это данные портов, а правила объяснения одинаковы для всех устройств.
USB_PORT_EVIDENCE = {
    "u2424h": (False, 0, 15, "https://www.dell.com/en-us/shop/monitors/apd/dell-ultrasharp-24-monitor-u2424h/u2424h_monitor/x99wp"),
    "u2424he": (True, 90, 15, "https://www.dell.com/en-us/shop/dell-ultrasharp-24-usb-c-hub-monitor-u2424he/apd/210-bkry/monitors-monitor-accessories"),
}


@dataclass(frozen=True)
class Aspect:
    title: str
    fields: str
    interest: str
    benefit: str


ASPECTS = (
    Aspect("Порты", r"^порты$|число портов|количество портов|интерфейсы client", r"порт|гигабит|расширен", "Количество портов определяет, сколько устройств можно подключить с учётом порта для роутера."),
    Aspect("Возможности сети", r"^возможности$|poe|уровень коммутатора", r"poe|петл|петел|управлен|трафик|multicast|сет[ьи]", "Сверьте нужную сетевую функцию перед покупкой. Пустое поле карточки не подтверждает её отсутствие."),
    Aspect("Видео и питание через USB‑C", r"power delivery|видео.*usb|usb.*видео|мощность зарядки", r"usb|заряд|питан|видео|одним кабел", "Для видео и питания одним кабелем обе стороны должны поддерживать эти функции. Наличие USB‑C само по себе их не подтверждает."),
    Aspect("Подключение", r"^подключение$|интерфейс|разъем", r"подключ|usb|hdmi|ethernet|кабел", "Подбирайте интерфейс под разъёмы и возможности подключаемого устройства."),
    Aspect("Конструкция гарнитуры", r"^тип$|звуковое оформление", r"наушник|коллег|окружен|звонк|гарнитур", "Один наушник удобен, когда нужно одновременно разговаривать и слышать, что происходит рядом."),
    Aspect("Микрофон", r"микрофон", r"микрофон|переговор|звонк", "Крепление влияет на то, как можно расположить микрофон. Оно само по себе не говорит о качестве звука."),
    Aspect("Память и накопитель", r"оперативн|озу|установленного ssd|емкость накопителя", r"памят|ssd|хранили|многозадач", "Объём памяти важен для работы с несколькими приложениями, а объём SSD — для программ и файлов."),
    Aspect("Процессор и графика", r"модель процессора|видеопроцессор", r"процессор|быстр|производит|график|монтаж", "По названиям процессоров и графики нельзя обещать производительность в конкретной программе."),
    Aspect("Мобильность", r"вес нетто|время работы|емкость аккумулятора", r"поездк|вес|автоном|легк|мобильн", "Если часто носите устройство с собой, обращайте внимание на вес и заявленное время работы."),
    Aspect("Экран", r"тип матрицы|разрешение экрана|диагональ|частота обновления", r"экран|диагонал|изображен|разрешен", "Размер экрана влияет на рабочее пространство, а разрешение — на детализацию."),
    Aspect("Регулировки", r"регулировк|поворот|vesa", r"подставк|эргоном|поворот|креплен", "Сверьте нужный диапазон регулировки или крепление для вашего рабочего места."),
    Aspect("Лицензия и совместимость", r"назначение|редакция|модель лицензирования|тип лицензии|срок действия|совместимая архитектура|к-во пользователей", r"лиценз|совместим|срок|пользовател|функци", "Для выбора лицензии важны нужные функции, число пользователей, платформа и срок."),
    Aspect("Буфер", r"кеш-память", r"буфер|очеред|нагрузк", "Размер буфера может быть важен при всплесках трафика. По нему одному скорость не определяют."),
)


def item_label(item):
    name = item["name"]
    hyphenated = re.search(r"\b[a-z]{1,8}-\d[a-z0-9]*(?:-[a-z0-9]+)*\b", normalize_codes(name))
    codes = model_codes(name)
    return hyphenated[0].upper() if hyphenated else codes[0].upper() if codes else name


def pricing(items, labels):
    lines = ["Стоимость: " + ", ".join(
        f"{label} — {price_text(item['price_minor']) if item['price_minor'] is not None else 'по запросу'}"
        for label, item in zip(labels, items)
    ) + "."]
    if len(items) == 2 and all(item["price_minor"] is not None for item in items):
        lines.append(f"Разница в цене — {price_text(abs(items[0]['price_minor'] - items[1]['price_minor']))}.")
    return " ".join(lines)


def usb_comparison(parameters, labels, question):
    profiles = [USB_PORT_EVIDENCE.get(label.casefold()) for label in labels]
    if len(profiles) != 2 or not all(profiles) or profiles[0][:2] == profiles[1][:2]:
        return None
    # Используем дополнение только для карточек с USB-C; наличие разъёма само
    # по себе не позволяет заключить, что он передаёт видео или питает источник.
    if any(params and not re.search(r"usb[^,;]*type.c|usb.c", normalize(str(params)))
           for params in parameters):
        return None
    for facts, profile in zip(parameters, profiles):
        explicit = " ".join(value for label, entries in facts.items()
                            if re.search(r"power delivery|видео.*usb|usb.*видео|мощность зарядки", normalize(label))
                            for value, _ in entries)
        if explicit:
            return None
    better = next((i for i, profile in enumerate(profiles) if profile[0]), None)
    if better is None:
        return None
    other = 1 - better
    power = profiles[better][1]
    lines = [
        f"Главное отличие — подключение одним кабелем. У {labels[better]} соответствующий USB‑C передаёт изображение и данные "
        f"и подаёт питание до {power} Вт. У {labels[other]} USB‑C к источнику передаёт данные, а для видео нужен HDMI или DisplayPort.",
        "Что это даёт: можно подключить совместимое устройство без отдельного видеокабеля. "
        "Это могут быть ноутбук, планшет или смартфон, если его USB‑C поддерживает видеовыход. "
        f"У обеих моделей отдельный USB‑C для аксессуаров даёт до {profiles[better][2]} Вт.",
        f"Что выбрать: {labels[better]} для одного кабеля, если устройство поддерживает видео и приём питания по USB‑C. "
        f"Если достаточно HDMI или DisplayPort, подойдёт {labels[other]}.",
    ]
    if re.search(r"rj45|ethernet|проводн\w* сет", question) and any(
        re.search(r"rj\s*-?45|ethernet", normalize(value))
        for entries in parameters[better].values() for value, _ in entries
    ):
        lines[1] += f" У {labels[better]} в каталоге также указан Ethernet RJ45."
    sources = [{"title": f"Спецификация USB‑C {label}", "url": profile[3]}
               for label, profile in zip(labels, profiles)]
    return lines, sources


def offer_facts(item):
    params = catalog_parameters(item)
    if re.search(r"коммутатор|switch", normalize(item["category"])):
        ports = re.findall(r"\b\d+\s*[×xх]\s*(?:gigabit|fast ethernet|10/100(?:/1000)?)", item["description"], re.I)
        if ports:
            params["Порты"] = [(port, "") for port in ports]
    return params


def aspect_values(aspect, params):
    return tuple(dict.fromkeys(value if not unit or normalize(value).endswith(normalize(unit)) else f"{value} {unit}"
                               for key, entries in params.items() if re.search(aspect.fields, normalize(key))
                               for value, unit in entries))


def compare_products(items, question, *, recommendation_only=False):
    if len(items) < 2:
        return "", []
    labels = [item_label(item) for item in items]
    if len(set(labels)) < len(labels):
        labels = [item["name"] for item in items]
    goal = normalize_codes(question)
    cost = pricing(items, labels)
    if PRICE_ONLY.search(goal):
        return cost, []
    params = [offer_facts(item) for item in items]
    usb = usb_comparison(params, labels, goal)
    if usb:
        lines, sources = usb
        if recommendation_only:
            return "\n\n".join(lines[1:]), sources
        return "\n\n".join([*lines, cost]), sources

    rows = [(aspect, [aspect_values(aspect, facts) for facts in params]) for aspect in ASPECTS
            if aspect.title != "Конструкция гарнитуры" or all("гарнитур" in normalize(item["name"]) for item in items)]
    rows = [(aspect, values) for aspect, values in rows if len({frozenset(v) for v in values}) > 1]
    rows.sort(key=lambda row: (not bool(re.search(row[0].interest, goal)),
                               sum(not entry for entry in row[1])))
    if not rows:
        return "Каталог не подтверждает значимых различий для выбора. Если важна конкретная функция, уточните её.\n\n" + cost, []
    focus, primary = rows[0]
    shared = set.intersection(*(set(value) for value in primary))
    first = [tuple(value for value in entry if value not in shared) for entry in primary]
    lines = [f"Главное отличие — {focus.title.lower()}. " + ". ".join(
        f"{label}: {', '.join(values) if values else 'дополнительные сведения не указаны'}"
        for label, values in zip(labels, first)
    ) + "."]
    benefit = focus.benefit
    if focus.title == "Возможности сети" and re.search(r"петель|петл", normalize(" ".join(value for entry in first for value in entry))):
        benefit = "Предотвращение петель помогает избежать сбоев, если кабели случайно замкнут сеть в кольцо. У остальных моделей эта функция в каталоге не подтверждена."
    lines.append(f"Что это даёт: {benefit}")
    for aspect, values in rows[1:3]:
        common = set.intersection(*(set(value) for value in values))
        parts = [", ".join(value for value in entry if value not in common) or "дополнительных сведений нет"
                 for entry in values]
        lines.append(f"{aspect.title}: " + " · ".join(f"{label} — {value}" for label, value in zip(labels, parts)) + ".")
    if focus.title == "Порты" and (counts := [max((int(n) for n in re.findall(r"\b(\d+)\s*[×xх]", " ".join(entry))), default=0)
                                             for entry in primary]) and all(counts) and len(set(counts)) > 1:
        best = counts.index(max(counts))
        lines.append(f"Что выбрать: {labels[best]}, если нужен запас портов. Если устройств меньше, подойдёт вариант с достаточным числом портов.")
    elif focus.title == "Конструкция гарнитуры" and (index := next(
        (i for i, values in enumerate(first) if re.search(r"1 наушник", normalize(" ".join(values)))), None,
    )) is not None:
        lines.append(f"Что выбрать: {labels[index]}, если хотите слышать коллег во время звонка.")
    elif focus.title == "Возможности сети" and (index := next(
        (i for i, values in enumerate(first) if re.search(r"петел|петл|igmp|poe", normalize(" ".join(values)))), None,
    )) is not None:
        lines.append(f"Что выбрать: {labels[index]}, если для вашей сети важны указанные функции.")
    else:
        lines.append("Что выбрать: ориентируйтесь на это различие. Если принципиальный параметр не указан, его стоит уточнить перед заказом.")
    if not recommendation_only:
        lines.append(cost)
    return "\n\n".join(lines), []

RECOMMEND_REQUEST = re.compile(
    r"\b(?:како[йя]|каку[юю]|что|какой вариант|какой из)\b[^.!?]{0,85}"
    r"\b(?:лучш\w*|посовету\w*|рекоменду\w*|выбрат\w*)\b|"
    r"\b(?:посоветуйте|порекомендуйте)\s+(?:один|лучш\w*)\b"
)
ACCEPT_SELECTION = re.compile(r"^(?:(?:да|хорошо|отлично)[,.! ]+)?(?:беру|возьму|выбираю)\s+(?:его|ее|этот|эту|рекомендованн\w*)[.! ]*$")

# Каждое правило содержит предпочтение клиента и положительное подтверждение.
# Размер цены участвует в выборе только при явно указанном приоритете экономии.
PREFERENCES = (
    (r"петел|петл", "Возможности сети", r"предотвращение петель", "В каталоге указано предотвращение петель"),
    (r"multicast|мультикаст|igmp", "Возможности сети", r"igmp", "Указана поддержка IGMP snooping для группового трафика"),
    (r"poe", "Возможности сети", r"802\.3(?:af|at|bt)|\bpoe\+", "Указано питание подключаемых устройств по Ethernet"),
    (r"слыша\w* коллег|слышать|окружен|одним ухом", "Конструкция гарнитуры", r"одн\w+ наушник|1 наушник", "Один наушник оставляет второе ухо свободным, чтобы слышать коллег"),
    (r"rj45|ethernet|проводн\w* сет", "Подключение", r"rj\s*-?45|ethernet", "Есть разъём для проводной сети"),
    (r"видео.*usb|одним кабелем|usb.*видео", "Видео и питание через USB‑C", r"видео|displayport alt mode", "Каталог подтверждает передачу изображения через USB‑C"),
    (r"настольн\w* приложен|без интернет", "Лицензия и совместимость", r"настольн\w*|офлайн", "Указана поддержка настольных приложений или работы без интернета"),
)


def recommend_offer(items, task, budget=None, *, qualified=False):
    """Выбирает из проверенного подбора или уточняет ещё непроверенное требование."""
    goal = normalize_codes(task)
    if not items:
        return None, "Какие из предложенных товаров вы хотите сравнить?"
    if budget is not None:
        items = [item for item in items if item["price_minor"] is not None and item["price_minor"] <= budget]
        if not items:
            return None, "Среди предложенных товаров нет позиции с подтверждённой ценой в этом бюджете. Какой предел допустим?"
    records = [offer_facts(item) for item in items]
    facts = [{aspect.title: aspect_values(aspect, record) for aspect in ASPECTS} for record in records]
    scores = [0] * len(items)
    reasons = [[] for _ in items]
    for request, field, evidence, benefit in PREFERENCES:
        if not re.search(request, goal) or request == r"poe" and re.search(r"без\s+poe|poe\s+не\s+нуж", goal):
            continue
        for i, details in enumerate(facts):
            if re.search(evidence, normalize(" ".join(details[field]))):
                scores[i] += 2
                reasons[i].append(benefit)
    if re.search(r"(?<!не )\b(?:дешевле|экономия|сэкономить|минимальная цена|минимальн\w* бюджет)\b", goal):
        priced = [(item["price_minor"], i) for i, item in enumerate(items) if item["price_minor"] is not None]
        if priced:
            _, index = min(priced)
            scores[index] += 1
            reasons[index].append("Это самый доступный из предложенных вариантов")
    if re.search(r"памят\w*|многозадач|много.*приложен", goal):
        amount = [re.search(r"(\d+)\s*(?:гб|gb)\b", normalize(" ".join(
            aspect_values(Aspect("", r"озу|оперативн", "", ""), record)
        ))) for record in records]
        if all(amount):
            totals = [int(match[1]) for match in amount]
            if len(set(totals)) > 1:
                index = totals.index(max(totals))
                scores[index] += 2
                reasons[index].append(f"{totals[index]} ГБ оперативной памяти дают больше места для одновременно открытых приложений")
    if re.search(r"вес|легче|поездк|носить с собой|мобильн", goal):
        weights = []
        for details in facts:
            match = re.search(r"(\d+(?:[.,]\d+)?)\s*кг\b", " ".join(details["Мобильность"]))
            weights.append(float(match[1].replace(",", ".")) if match else None)
        if all(weight is not None for weight in weights) and len(set(weights)) > 1:
            index = weights.index(min(weights))
            scores[index] += 2
            reasons[index].append(f"При весе {str(weights[index]).replace('.', ',')} кг его удобнее носить с собой")
    highest = max(scores)
    tied = [i for i, score in enumerate(scores) if score == highest]
    economy_tie = qualified and len(tied) > 1
    best = min(tied, key=lambda i: (items[i]["price_minor"] is None, items[i]["price_minor"] or 0)) if economy_tie else tied[0]
    item = items[best]
    if not qualified and len(items) > 1 and (highest == 0 or len(tied) > 1):
        columns = next((aspect.title for aspect in ASPECTS if len({
            frozenset(detail[aspect.title]) for detail in facts
        }) > 1), None)
        prompts = {"Возможности сети": "Нужна ли защита от петель в вашей сети?",
                   "Подключение": "Каким разъёмом вы будете пользоваться?",
                   "Конструкция гарнитуры": "Важно ли слышать коллег во время звонка?",
                   "Мобильность": "Будете ли вы регулярно носить устройство с собой?",
                   "Лицензия и совместимость": "Какая платформа или версия должна поддерживаться?",
                   "Экран": "Какой размер или разрешение экрана для вас важнее?"}
        question = prompts.get(columns, "Какое свойство для вас важнее всего при выборе?")
        if columns == "Подключение" and "hdmi" in goal and any(
            "displayport" in normalize(" ".join(detail["Подключение"])) for detail in facts
        ):
            question = "Кроме HDMI, нужен ли вам DisplayPort?"
        return None, "По подтверждённым данным один вариант пока не выделяется для вашей задачи. " + question
    label = item["name"] if qualified else item_label(item)
    parts = [f"Для вашей задачи я рекомендую {label}."]
    if reasons[best]:
        supporting = []
        if match := re.search(r"\b(\d+)\s*[×xх]\s*gigabit", normalize(" ".join(facts[best]["Порты"]))):
            supporting.append(f"В каталоге указано {match[1]} гигабитных портов")
        if any("1 наушник" in normalize(value) for value in facts[best]["Конструкция гарнитуры"]):
            connection = next((value for value in facts[best]["Подключение"] if "usb" in normalize(value)), None)
            if connection:
                supporting.append(f"Подключение к совместимому устройству через {connection}")
        parts.extend("• " + reason + "." for reason in (reasons[best] + supporting)[:2])
    elif qualified:
        screen = facts[best]["Экран"]
        field = next((facts[best][name] for name in ("Порты", "Подключение", "Микрофон",
                     "Память и накопитель", "Лицензия и совместимость") if facts[best][name]), ())
        if screen and re.search(r"офис|работ|учеб|документ", goal):
            detail = ", ".join(screen[:3])
            parts.append(f"Для работы с документами подойдёт экран: {detail}.")
        elif field:
            parts.append("По карточке подтверждено: " + ", ".join(field[:2]) + ".")
        else:
            parts.append("Модель подходит под указанные вами условия.")
    else:
        parts.append("Из предложенных вариантов осталась одна позиция.")
    if economy_tie:
        prices = [items[i]["price_minor"] for i in tied if items[i]["price_minor"] is not None]
        if len(set(prices)) > 1:
            parts.append("По важным для вашей задачи условиям преимущества более дорогих вариантов не подтверждены.")
        elif len(prices) == len(tied):
            parts.append("По указанным условиям варианты равнозначны и стоят одинаково.")
    price = item["price_minor"]
    if price is not None:
        if budget is not None and price <= budget:
            parts.append(f"• Цена {price_text(price)} укладывается в указанный бюджет.")
        else:
            parts.append(f"Цена — {price_text(price)}.")
    cheaper = [other for other in items if other["sku"] != item["sku"]
               and price is not None and other["price_minor"] is not None and other["price_minor"] < price]
    if cheaper:
        alternative = min(cheaper, key=lambda row: row["price_minor"])
        parts.append(f"Для экономии можно рассмотреть {item_label(alternative)}, если подтверждённое преимущество {item_label(item)} для вас необязательно.")
    return item, "\n\n".join(parts)
