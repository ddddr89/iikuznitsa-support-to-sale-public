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


def network_advice(state, message, catalog):
    text = normalize(message)
    topology = update_topology(state, text)
    if budget := budget_limit(text):
        state.facts["budget"] = int(budget)
    if re.search(r"гигабит|\b1\s*гбит", text):
        state.facts["gigabit"] = True
    if re.search(EXCLUDE_POE, text):
        state.facts["poe"] = "excluded"
    elif re.search(NO_POE, text):
        state.facts.pop("poe", None)
    elif re.search(r"\bpoe\+?", text):
        state.facts["poe"] = "required"
    if re.search(r"\bvlan\b", text):
        state.facts["vlan"] = not bool(re.search(r"\b(?:без\s+vlan|vlan\s+не\s+нуж\w*)", text))
    uplink = state.facts.get("uplink")
    subtotal = sum(v for v in topology.values() if v)
    if not subtotal:
        return "Сколько компьютеров и других устройств нужно подключить кабелем?", [], []
    if uplink is None:
        return (f"С отдельным подключением к роутеру нужно минимум {subtotal + 1} портов. "
                "Будет подключение к роутеру? Нужны гигабитные порты, PoE или отдельные сети VLAN?", [], [])
    minimum = subtotal + int(uplink)
    budget = state.facts.get("budget")
    # Сверяем каждый порт со скоростью, не выводим модель по цифрам в названии.
    scope = ProductScope("switch", max_price=budget)
    candidates = catalog.search("коммутатор", 10000, scope=scope)
    items, comparisons = [], []
    for item in candidates:
        total_ports, gigabit, params = switch_facts(item)
        usable = gigabit if state.facts.get("gigabit") else total_ports
        if usable < minimum:
            continue
        facts = catalog_facts(item)
        if (state.facts.get("poe") == "required" and not re.search(r"802\.3(?:af|at|bt)|\bpoe\s*\+", facts)
                or state.facts.get("poe") == "excluded" and not re.search(r"без\s+poe|non[- ]poe", facts)
                or state.facts.get("vlan") and not re.search(r"802\.1q|vlan\s+(?:поддерживается|есть)", facts)):
            continue
        if item["price_minor"] is None or any(other["name"] == item["name"] for other in items):
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
    lines = [f"Нужно минимум {minimum} портов ({'; '.join(topology_details)})."]
    if not items:
        lines.append("В заданных условиях подходящего коммутатора не нашёл. Могу проверить варианты с другим бюджетом или передать запрос менеджеру.")
    else:
        lines.extend(comparisons)
        if len(items) > 1:
            lines.append("Если важна минимальная цена, подойдёт первый вариант. У всех вариантов хватает портов под указанную схему.")
    return "\n".join(lines), items, []
