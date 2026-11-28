"""Проверяет пригодность оборудования для рабочих мест и офисной сети."""

import re
from dataclasses import replace

from app.automatic_documents import AMOUNT, number
from app.catalog_advice import price_text
from app.catalog_matching import ProductScope, budget_limit, catalog_facts, catalog_parameters, normalize

TEAM = re.compile(rf"\b({AMOUNT})\s+(?:сотрудник\w*|оператор\w*|рабоч\w+ мест\w*)\b")
COMPUTER = r"(?:компьютер\w*|пк|рабоч\w+\s+станци\w*)"
COMPUTERS = re.compile(rf"\b({AMOUNT})\s+(?:(?:нов\w*|дополнительн\w*|будущ\w*|планируем\w*)\s+)*{COMPUTER}\b")
EXPANSION = re.compile(rf"\b(?:еще|плюс|добав\w*|планируем\w*|в перспективе)\s+(?:[а-я]+\s+)?({AMOUNT})(?=\s|[.!?,]|$)")
UPLINK = r"\b(?:роутер\w*|маршрутизатор\w*|uplink)\b"
NO_POE = r"\b(?:без\s+(?:обязательн\w*\s+)?poe\b|poe\s+(?:не\s+нуж\w*|не\s+обязател\w*))"
EXCLUDE_POE = r"\b(?:poe\s+исключ\w*|без\s+poe\b)"
PERIPHERALS = (("принтеры", r"(?:сетев\w*\s+)?(?:принтер\w*|мфу)"),
               ("точки доступа", r"точк\w*\s+доступа"),
               ("камеры", r"(?:ip[- ]?)?камер\w*"),
               ("серверы", r"сервер\w*"))
PORTS = re.compile(r"\b(\d{1,3})\s*[x×х]\s*(gigabit|fast ethernet|ethernet|1\s*гбит|10/100/1000|10/100)(?![\w/])")
PORT_COUNT = re.compile(rf"\b({AMOUNT})\s+(?:гигабитн\w*\s+)?порт\w*\b")
SWITCH_REQUIREMENT = re.compile(r"\b(?:порт\w*|гигабит\w*|poe|vlan|бюджет\w*|лимит\w*)\b")
PORT_PARAM = re.compile(r"число|количество|к-во")
GIGABIT = re.compile(r"gigabit|гигабит|10/100/1000|\b1\s*гбит")
UNKNOWN_SPEC = "в каталоге не указано"


def switch_facts(item):
    """Извлекает число портов по скоростям. Пропуск параметра не равен нулю."""
    params = catalog_parameters(item)
    ports = [int(count) for count, speed in PORTS.findall(normalize(item["description"]))]
    gigabit = [int(count) for count, speed in PORTS.findall(normalize(item["description"])) if GIGABIT.search(speed)]
    for name, values in params.items():
        label = normalize(name)
        if PORT_PARAM.search(label) and "порт" in label:
            for value, _ in values:
                if match := re.fullmatch(r"\d{1,3}", value):
                    ports.append(int(match[0]))
                    if GIGABIT.search(label):
                        gigabit.append(int(match[0]))
    return max(ports, default=0), max(gigabit, default=0), params


def update_topology(state, text):
    current = list(COMPUTERS.finditer(text))
    state.facts.setdefault("topology", {})
    topology = state.facts["topology"]
    if current:
        first = number(current[0][1]) or 0
        if EXPANSION.search(text) and len(current) == 1 and "компьютеры" in topology:
            topology["резерв"] = first
        else:
            topology["компьютеры"] = first
            if len(current) > 1:
                topology["резерв"] = sum(number(match[1]) or 0 for match in current[1:])
    if (growth := EXPANSION.search(text)) and len(current) <= 1 and "компьютеры" in topology:
        topology["резерв"] = number(growth[1]) or 0
    for key, pattern in PERIPHERALS:
        if re.search(rf"\b{pattern}\b", text):
            count = re.search(rf"\b({AMOUNT})\s+{pattern}\b", text)
            topology[key] = number(count[1]) if count else 1
    if re.search(UPLINK, text):
        excluded = re.search(r"без\s+(?:роутер|маршрутизатор)|(?:роутер|маршрутизатор)\w*\s+не\s+нуж|не\s+будет\s+роут", text)
        state.facts["uplink"] = not bool(excluded)
    return topology


def headset_advice(state, message, catalog):
    text = normalize(message)
    if match := TEAM.search(text):
        state.quantity = number(match[1])
    if budget := budget_limit(text):
        state.facts["budget"] = int(budget)
    if interface := re.search(r"\busb(?:[ -][ac])?\b|\bbluetooth\b|\b3[.,]5\s*мм\b", text):
        state.facts["interface"] = interface[0]
    count = state.quantity
    budget = state.facts.get("budget")
    port = state.facts.get("interface")
    if not count:
        question = "Сколько рабочих мест нужно оснастить?" if budget else "Сколько рабочих мест нужно оснастить и какой общий бюджет?"
        return question, [], []
    if not port:
        note = f"На {count} рабочих мест бюджет — до {price_text(budget // count)} за гарнитуру. " if budget else ""
        return note + "Как подключаются гарнитуры к компьютерам: USB, разъём 3,5 мм или Bluetooth?", [], []
    port_pattern = {
        "usb": r"\busb\b", "usb-a": r"\busb(?:\s+[\d.]+)?[ -](?:type-)?a\b",
        "usb-c": r"\busb[ -](?:type-)?c\b", "bluetooth": r"\bbluetooth\b",
    }.get(port.replace(" ", "-"), r"\b3[.,]5\s*мм\b")
    requirements = (r"\b(?:гарнитур\w*|микрофон\w*)\b", port_pattern)
    if port.startswith("usb"):
        # У беспроводной гарнитуры USB в карточке может обозначать только зарядку.
        requirements += (r"\bпроводн\w*\b(?!.*\bбеспроводн\w*\b)",)
    scope = ProductScope("headset", max_price=budget // count if budget else None, requirements=requirements)
    items = catalog.search("гарнитуры", 20, scope=scope)
    choices = []
    for item in items:
        if item["price_minor"] is not None and all(row["name"] != item["name"] for row in choices):
            choices.append(item)
        if len(choices) == 2:
            break
    if not choices:
        return f"Не нашёл гарнитуры с подтверждённым подключением {port.upper()} в заданных условиях.", [], []
    lines = [f"На {count} рабочих мест подойдут эти гарнитуры с подключением {port.upper()}:"]
    for item in choices:
        price = item["price_minor"]
        shape = " Один наушник, если важно слышать коллег." if "1 наушником" in item["name"] else ""
        if "открытое" in normalize(item["description"]):
            shape = " Открытое оформление, если важно слышать окружение."
        lines.append(f"• {item['name']} — {price_text(price)} за штуку, {price_text(price * count)} за комплект.{shape}")
    return "\n".join(lines), choices, []


def update_network_requirements(state, message):
    text = normalize(message)
    topology = update_topology(state, text)
    if (match := PORT_COUNT.search(text)) and (count := number(match[1])):
        state.facts["port_min"] = count
    if (budget := budget_limit(text)) is not None:
        state.facts["budget"] = int(budget)
    if re.search(r"гигабит|\b1\s*гбит", text):
        state.facts["gigabit"] = True
    if re.search(r"\bуправляем\w*\b", text):
        state.facts["managed"] = True
    elif re.search(r"\bнеуправляем\w*\b", text):
        state.facts["managed"] = False
    if re.search(EXCLUDE_POE, text):
        state.facts["poe"] = "excluded"
    elif re.search(NO_POE, text):
        state.facts.pop("poe", None)
    elif re.search(r"\bpoe\+?", text):
        state.facts["poe"] = "plus" if "poe+" in text else "required"
    if re.search(r"\bvlan\b", text):
        state.facts["vlan"] = not bool(re.search(r"\b(?:без\s+vlan|vlan\s+не\s+нуж\w*)", text))
    return topology


def network_advice(state, message, catalog):
    if not state.facts.get("network_initialized"):
        update_network_requirements(state, state.query)
        state.facts["network_initialized"] = True
    topology = update_network_requirements(state, message)
    state.facts["network_searched"] = False
    uplink = state.facts.get("uplink")
    subtotal = sum(v for v in topology.values() if v)
    explicit_minimum = state.facts.get("port_min", 0)
    if not subtotal and not explicit_minimum:
        return "Сколько компьютеров и других устройств нужно подключить кабелем?", [], []
    if uplink is None and not explicit_minimum:
        return (f"С отдельным подключением к роутеру нужно минимум {subtotal + 1} портов. "
                "Будет подключение к роутеру? Нужны гигабитные порты, PoE или отдельные сети VLAN?", [], [])
    minimum = max(explicit_minimum, subtotal + int(bool(uplink)))
    # Для 13–16 подключений рассматриваем стандартный размер в 16 портов.
    capacity = 16 if 12 < minimum < 16 else minimum
    state.facts["network_searched"] = True
    budget = state.facts.get("budget")
    # Сверяем каждый порт со скоростью, не выводим модель по цифрам в названии.
    scope = ProductScope("switch")
    candidates = catalog.search("коммутатор", 10000, scope=scope)
    items, comparisons = [], []
    feasible_ports = False
    prices = []
    for item in candidates:
        total_ports, gigabit, params = switch_facts(item)
        usable = gigabit if state.facts.get("gigabit") else total_ports
        if usable < capacity:
            continue
        feasible_ports = True
        facts = catalog_facts(item)
        if (state.facts.get("poe") == "plus" and not re.search(r"802\.3at|\bpoe\s*\+", facts)
                or state.facts.get("managed") is True and not re.search(r"\bуправляем\w*|\bmanaged\b|layer\s*2\+", facts)
                or state.facts.get("managed") is False and not re.search(r"\bнеуправляем\w*|\bunmanaged\b", facts)
                or state.facts.get("poe") == "required" and not re.search(r"802\.3(?:af|at|bt)|\bpoe\s*\+", facts)
                or state.facts.get("poe") == "excluded" and not re.search(r"без\s+poe|non[- ]poe", facts)
                or state.facts.get("vlan") and not re.search(r"802\.1q|vlan\s+(?:поддерживается|есть)", facts)):
            continue
        if item["price_minor"] is None:
            continue
        prices.append(item["price_minor"])
        if budget is not None and item["price_minor"] > budget or any(other["name"] == item["name"] for other in items):
            continue
        items.append(item)
        details = [f"{usable} портов" + (" Gigabit Ethernet" if state.facts.get("gigabit") else ""),
                   f"{usable - minimum} свободных после подключения"]
        for key, values in params.items():
            if normalize(key) == "кеш-память":
                details.append("буфер " + " ".join(values[0]).strip())
        comparisons.append(f"• {item['name']} — {', '.join(details)}. {price_text(item['price_minor'])}.")
        if len(items) == 3:
            break
    topology_details = [f"{name}: {count}" for name, count in topology.items() if count]
    if uplink:
        topology_details.append("роутер: 1")
    topology_note = " (" + "; ".join(topology_details) + ")" if topology_details else ""
    lines = [f"Нужно минимум {minimum} портов{topology_note}."]
    if capacity != minimum:
        lines.append(f"Рассматриваю модели от {capacity} портов.")
    if budget is not None:
        lines.append(f"Бюджет — до {price_text(budget)}.")
    if not items:
        speed = " гигабитных" if state.facts.get("gigabit") else ""
        if not feasible_ports:
            lines.append(f"В каталоге не подтверждено {capacity} или больше{speed} портов у одной модели.")
        elif prices and budget is not None:
            lines.append(f"В этот бюджет подходящих моделей нет. Минимальная подтверждённая цена — {price_text(min(prices))}.")
        elif state.facts.get("poe") or state.facts.get("vlan") or state.facts.get("managed") is not None:
            lines.append("Не нашёл модель, у которой вместе с нужными портами подтверждены заданные условия по питанию или управлению сетью и цена.")
        else:
            lines.append("Для коммутаторов с нужными портами цена в каталоге не подтверждена.")
    else:
        lines.extend(comparisons)
        if len(items) > 1:
            lines.append("Если важна минимальная цена, подойдёт первый вариант. У всех вариантов хватает портов под указанную схему.")
    return "\n".join(lines), items, []
