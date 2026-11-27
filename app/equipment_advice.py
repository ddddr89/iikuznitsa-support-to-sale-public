"""Проверяет пригодность оборудования для рабочих мест и офисной сети."""

import re
from dataclasses import replace

from app.automatic_documents import AMOUNT, number
from app.catalog_advice import price_text
from app.catalog_matching import ProductScope, budget_limit, normalize, product_scope

TEAM = re.compile(rf"\b({AMOUNT})\s+(?:сотрудник\w*|оператор\w*|рабоч\w+ мест\w*)\b")
COMPUTERS = re.compile(rf"\b({AMOUNT}|восьми|семи|шести|пяти|четырех|трех|двух)\s+(?:компьютер\w*|пк)\b")
NETWORK_COUNTS = dict(zip("двух трех четырех пяти шести семи восьми".split(), range(2, 9)))
UPLINK = r"\b(?:роутер\w*|маршрутизатор\w*|uplink)\b"
NO_POE = r"\b(?:без\s+poe\b|poe\s+не\s+нуж\w*)"
TP_LINK = "https://www.tp-link.com/us/business-networking/unmanaged-switch/tl-sg1016d/"


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
        return "Сколько рабочих мест нужно оснастить?", [], []
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
    if match := COMPUTERS.search(text):
        state.facts["computers"] = number(match[1]) or NETWORK_COUNTS.get(match[1])
    if re.search(UPLINK, text):
        state.facts["uplink"] = not bool(re.search(r"без\s+(?:роутер|маршрутизатор)|(?:роутер|маршрутизатор)\w*\s+не\s+нуж", text))
    if budget := budget_limit(text):
        state.facts["budget"] = int(budget)
    if re.search(r"гигабит|\b1\s*гбит", text):
        state.facts["gigabit"] = True
    if re.search(NO_POE, text):
        state.facts["poe"] = False
    elif re.search(r"\bpoe\+?", text):
        state.facts["poe"] = True
    if re.search(r"\bvlan\b", text) and not re.search(r"\b(?:без\s+vlan|vlan\s+не\s+нуж\w*)", text):
        state.facts["vlan"] = True
    computers = state.facts.get("computers")
    uplink = state.facts.get("uplink")
    if not computers:
        return "Сколько устройств нужно подключить кабелем?", [], []
    if uplink is None:
        return (f"Для {computers} компьютеров и отдельного подключения к роутеру нужно минимум {computers + 1} портов. "
                "Будет подключение к роутеру? Достаточно гигабита и нужны ли PoE или отдельные сети VLAN?", [], [])
    minimum = computers + int(uplink)
    extra = product_scope("коммутатор " + state.query + " " + text) or ProductScope("switch")
    if minimum > 128:
        return f"Нужно минимум {minimum} портов. Понадобится несколько коммутаторов или отдельное проектирование сети.", [], []
    port_pattern = r"\b(?:" + "|".join(str(n) for n in range(minimum, 129)) + (
        r")\s*[x×х]\s*(?:gigabit|1\s*гбит)" if state.facts.get("gigabit") else r")\s*[x×х]\s*(?:gigabit|ethernet|1\s*гбит|10/100)")
    additional = (port_pattern,)
    sources = []
    if state.facts.get("vlan"):
        additional += (r"\bvlan\b",)
    if state.facts.get("poe") is False:
        additional += (r"\b(?:без\s+poe|non[- ]poe)\b",)
    elif state.facts.get("poe"):
        additional += (r"\bpoe\b",)
    scope = replace(extra, max_price=state.facts.get("budget"), requirements=(*extra.requirements, *additional))
    items = catalog.search("коммутатор", 3, scope=scope)
    if not items:
        return f"Нужно минимум {minimum} портов. Подтверждённого варианта с указанными условиями не нашёл.", [], []
    if any(re.search(r"\btl-sg1016d\b", normalize(item["name"])) for item in items):
        sources.append({"title": "Характеристики TP-Link TL-SG1016D", "url": TP_LINK})
    lines = [f"Нужно минимум {minimum} портов: {computers} для компьютеров" + (" и один для роутера." if uplink else ".")]
    for item in items:
        specs = re.search(r"\b(\d+)\s*[x×х]\s*(gigabit|ethernet|1\s*гбит|10/100)", normalize(item["description"]))
        detail = f"{specs[1]} портов, {int(specs[1]) - minimum} свободных после подключения" if specs else "число портов подтверждено"
        if re.search(r"\btl-sg1016d\b", normalize(item["name"])):
            detail += ", гигабитный, неуправляемый, без PoE"
        price = price_text(item["price_minor"]) if item["price_minor"] is not None else "цена по запросу"
        lines.append(f"• {item['name']} — {detail}. {price}.")
    return "\n".join(lines), items, sources
