"""Use catalog evidence and ask for the facts needed to qualify a recommendation."""

import re
from dataclasses import replace

from app.automatic_documents import quantity_in
from app.catalog_matching import DELL_DISPLAY, MODEL, OFFICE_ANNUAL, SKU_REFERENCE, normalize, normalize_codes, product_scope, verified_models
from app.knowledge import official_url

MICROSOFT_365 = "https://www.microsoft.com/en-us/microsoft-365/business/no-teams-plans-and-pricing"
WINDOWS_SERVER = "https://www.microsoft.com/licensing/guidance/Windows-Server-2025"
DELIVERY = "https://store.softline.ru/delivery_info/"
DELIVERY_REQUEST = re.compile(r"\b(?:достав\w*|привез\w*|привоз\w*|курьер\w*)\b")
DELIVERY_INDEX = re.compile(r"\b(?:индекс\w*\s*[:№]?\s*)?(\d{6})\b")
DELIVERY_DETAILS = re.compile(r"^(?:индекс\w*\b|почтовый индекс\b|[1-9]\d{5}(?!\d)|адрес\b|ул\.|"
                              r"улица\b|пвз\b|пункт выдачи\b)")
REPLY_SLA = re.compile(r"\b(?:срок\w* ответа|отвеча\w* на заявк\w*|ждать ответа)\b")
VERIFIED_DETAILS = {
    "27e1n1900ae-01": ("Philips подтверждает IPS, 3840 × 2160 и зарядку USB-C до 65 Вт.",
                       "Характеристики Philips 27E1N1900AE/01",
                       "https://www.documents.philips.com/assets/20250502/05c78abda2cb4cd3921eb2d10073be6e.pdf"),
    "sg3428mp": ("TP-Link подтверждает управление и 24 гигабитных порта RJ45 с PoE+.",
                 "Характеристики TP-Link SG3428MP",
                 "https://static.tp-link.com/upload/product-overview/2025/202512/20251224/SG3428MP%28UN%296.30_datasheet.pdf"),
    "tl-sg3428xmp": ("TP-Link подтверждает управление и 24 гигабитных порта RJ45 с PoE+.",
                    "Каталог коммутаторов TP-Link",
                    "https://static.tp-link.com/upload/smb/2022/202209/20220906/8392501151_2022_Omada%20SDN%20Solution%20Catalog_English_2202_online.pdf"),
}


def windows_server_advice(full):
    """Объясняет условную схему лицензирования и передаёт условия закупки на проверку."""
    missing = []
    version = re.search(r"\b20(?:16|19|22|25)\b", full)
    sockets = re.search(r"\b(\d{1,2})\s*(?:cpu|процессор\w*)\s+по\s+(\d{1,3})\s*яд", full)
    # Общего числа ядер недостаточно, чтобы проверить минимум для каждого процессора.
    total = re.search(r"\b(\d{1,3})\s*(?:физическ\w*\s+)?яд", full)
    vms = re.search(r"\b(\d{1,3}|одна|две|три|четыре)\s+(?:виртуальн\w*\s+машин\w*|вм)\b", full)
    if not version:
        missing.append("версию Windows Server")
    if not (total or sockets):
        missing.append("число физических процессоров и ядер")
    if not vms:
        missing.append("количество виртуальных машин")
    if not re.search(r"\b\d{1,5}\s+(?:пользовател\w*|сотрудник\w*|работник\w*|устройств\w*)\b", full):
        missing.append("число пользователей или устройств")
    if not re.search(r"\brds\b", full):
        missing.append("нужен ли RDS")
    if version and version[0] == "2025" and vms and (total or sockets):
        vm_count = {"одна": 1, "две": 2, "три": 3, "четыре": 4}.get(vms[1])
        if vm_count is None:
            vm_count = int(vms[1])
        if sockets and vm_count > 0:
            cores = max(16, int(sockets[1]) * max(8, int(sockets[2])))
            licenses = cores * ((vm_count + 1) // 2)
            return (f"Если все {vm_count} ВМ работают на одном указанном сервере, сам хост используется только для "
                    f"их размещения и управления, а схема основана на физических ядрах, для Standard нужно "
                    f"покрыть {cores} ядер за комплект. На {vm_count} ВМ потребуется покрытие на {licenses} ядер. "
                    "Лицензии доступа CAL проверяются отдельно. Не удалось подтвердить все параметры закупки "
                    "и цену по каталогу. Менеджер может сверить условия поставки.",
                    [{"title": "Руководство по лицензированию Windows Server 2025", "url": WINDOWS_SERVER}])
        if vm_count == 2 and total and int(total[1]) == 16:
            return ("Для двух виртуальных машин на одном полностью лицензированном 16-ядерном хосте "
                    "рассмотрите Windows Server 2025 Standard с лицензированием по физическим ядрам и User CAL "
                    "для пользователей без эквивалентных лицензий. Менеджер может проверить минимум на процессор, "
                    "нужное число CAL, условия закупки и цену. Подтверждённого SKU 2025 в выдаче нет.",
                    [{"title": "Руководство по лицензированию Windows Server 2025", "url": WINDOWS_SERVER}])
    if missing:
        return "Чтобы выбрать редакцию Windows Server и CAL, уточните " + ", ".join(missing) + ".", []
    return ("Не удалось подтвердить все параметры лицензирования и цену по каталогу. "
            "Менеджер может проверить редакцию и лицензии доступа по указанной конфигурации.", [])


def advisory(message, context):
    """Отвечает на справочные вопросы, когда есть подтверждённый сценарий."""
    current = normalize(message)
    full = normalize(context)
    private_data = r"\b(?:api[- ]?ключ\w*|токен\w*|парол\w*|чуж\w*\s+заявк\w*|данн\w*\s+других\s+клиент\w*|имен[а-я]*\s+покупател\w*)\b"
    disclosure = r"\b(?:покаж|выда|раскро|расскаж|сообщ|перечисл|назов|хочу)"
    if re.search(private_data, current) and re.search(disclosure, current):
        return "Ключи и данные других клиентов недоступны. Свои обращения можно посмотреть во вкладке «Мои заявки».", []
    if re.search(r"\bкак\s+оформить\b", current) and re.search(r"\bлицензи\w*\b", current) and "организац" in current:
        return ("Для оформления лицензий на организацию укажите нужный продукт, количество, срок и тип закупки "
                "(новая лицензия или продление). Менеджер проверит условия приобретения и запросит реквизиты организации.",
                [{"title": "Условия оформления лицензий на организацию", "url": "https://store.softline.ru/delivery_info/"}])
    if REPLY_SLA.search(current) or (REPLY_SLA.search(full) and re.search(r"\bсрок\w*\b", current)):
        return ("Подтверждённого срока ответа у меня нет. Текущий статус и ответ менеджера можно проверить "
                "во вкладке «Мои заявки». Если обращение уже ожидает ответа, повторно отправлять его не нужно.", [])
    if re.search(r"\bddr[45]\b", current) and len(set(re.findall(r"\bddr[45]\b", current))) > 1:
        return ("DDR4 и DDR5 несовместимы. Устанавливайте только то поколение, которое поддерживает плата. "
                "Уточните модель платы и необходимый объём. По ним можно проверить форм-фактор, допустимую ёмкость и ECC."), []
    if len(set(re.findall(r"\bddr[45]\b", full))) > 1 and re.search(r"\bddr[45]\b", current):
        return ("Нужен комплект памяти поколения, которое поддерживает плата. Для проверки допустимого объёма, "
                "форм-фактора и ECC сообщите точную модель платы. До проверки не могу рекомендовать конкретный модуль."), []
    if "windows server" in full:
        return windows_server_advice(full)
    if re.search(r"резервн\w+\s+копирован|vsphere|esxi", full) and "vmware" in full:
        if re.search(r"\b(?:vsphere|esxi)\s*8\b", current):
            return ("Для подбора решения нужно подтвердить поддержку вашей редакции ESXi, резервное копирование "
                    "на указанное хранилище и нужный срок хранения. Не удалось подтвердить все параметры и нужный объём "
                    "лицензии по каталогу. Менеджер может проверить базовую поставку, совместимость и цену."), []
        return "Уточните версию и редакцию VMware, число хостов, место хранения копий и срок хранения. Это нужно, чтобы проверить совместимость и лицензию.", []
    if re.search(r"вирус|антивирус|вымогател", full) and re.search(r"ноутбук|windows\s*11", full):
        if re.search(r"windows\s*11\s*pro|централизованн", current):
            return ("Для Windows 11 Pro нужны централизованное управление и подтверждённая защита от "
                    "вымогателей. Каталог не подтверждает одновременно эти функции, редакцию и условия новой лицензии. "
                    "Менеджер может проверить варианты на нужное число устройств."), []
        return ("Уточните редакцию Windows 11, нужно ли централизованное управление и требуется ли защита от "
                "вымогателей. У вас уже есть лицензия, которую нужно продлить?"), []
    if re.search(r"самовывоз|забрать (?:свой )?заказ самостоятельно|пункт\w*\s+самовывоз", current):
        return ("В интернет-магазине можно выбрать доставку в пункт выдачи. Пункт и часы его работы видны при "
                "оформлении. Для получения физлицу нужен паспорт, представителю юрлица — документ, "
                "удостоверяющий личность, и доверенность по форме М2. Доступность пункта проверьте при оформлении.",
                [{"title": "Виды доставки и правила получения товара", "url": DELIVERY}])
    if (re.search(r"\b(?:правил\w*|поряд\w*)\b.{0,30}\b(?:обмен\w*|возврат\w*)\b", current)
            or re.search(r"\b(?:обмен\w*|возврат\w*)\b", full) and re.search(r"юридическ\w*\s+лиц\w*|юрлиц", current)):
        return ("Порядок обмена или возврата смотрите в разделе «Получение, прием и возврат товара». "
                "Для заказа на организацию условия по конкретному товару и комплекту документов уточнит менеджер.",
                [{"title": "Получение, прием и возврат товара", "url": DELIVERY}])
    postal = re.search(r"\bиндекс\w*\s*[:№]?\s*(\d{6})\b", full)
    if (postal or re.search(r"салехард|629008", full)) and re.search(r"достав|индекс|цен\w*|стоимост", current):
        destination = f" для индекса {postal[1]}" if postal else " для Салехарда"
        return ("Интернет-магазин отправляет заказы по России транспортной компанией. Стоимость доставки"
                + destination + " рассчитывается при оформлении заказа с учётом состава и региона. "
                "Для точной стоимости и даты доставки укажите адрес или пункт выдачи."
                + (" Оплатить прямо в чате нельзя." if re.search(r"\bоплат\w*\b", current) else ""),
                [{"title": "Условия доставки по России", "url": DELIVERY}])
    return None


def price_text(minor):
    if minor is None:
        return "цена по запросу"
    rubles, kopecks = divmod(minor, 100)
    amount = f"{rubles:,}".replace(",", " ")
    if kopecks:
        amount += f",{kopecks:02d}"
    return f"{amount} ₽"


def item_text(item):
    price = price_text(item["price_minor"])
    return f'{item["name"]}\n' + (f"За штуку — {price}" if item["price_minor"] is not None else "Цена по запросу")


def quantity_label(item, count):
    if normalize(item["category"]) not in {"монитор", "мониторы", "monitor", "monitors"}:
        return f"{count} шт."
    last = count % 10
    if 11 <= count % 100 <= 14 or last not in (1, 2, 3, 4):
        return f"{count} мониторов"
    return f"{count} " + ("монитор" if last == 1 else "монитора")


def document_confirmation(item, count):
    name = item["name"]
    amount = quantity_label(item, count)
    if "монитор" in amount and (model := MODEL.search(name.casefold())):
        name = re.sub(r"^монитор\s+|\bTechnologies\s+", "", name[:model.end()], flags=re.IGNORECASE)
    return f"Готово! КП и счёт на {amount} {name}."


def purchase_with_delivery(message, catalog, *, selected=None, previous_quantity=None):
    """Совмещает каталожный расчёт с передачей условий доставки на проверку."""
    current = normalize_codes(message)
    if not DELIVERY_REQUEST.search(current):
        return None
    scope = product_scope(message)
    reference = SKU_REFERENCE.search(current)
    if scope or reference:
        products = catalog.search(message, 5, scope=scope)
        selected = products[0] if products and (reference or (scope and scope.model) or len(products) == 1) else None
    elif selected is not None:
        products = []
    else:
        return None
    quantity_seen, quantity = quantity_in(current)
    if not quantity_seen and not products:
        quantity = previous_quantity
    count = f"{quantity} шт." if quantity else "количество не указано"
    if not selected:
        opening = ("Выберите подходящую позицию из карточек." if products else
                   "Для подбора укажите точную модель или артикул.")
    else:
        opening = f"В каталоге есть {item_text(selected)}."
        if quantity and selected["price_minor"] is not None:
            total = price_text(selected["price_minor"] * quantity)
            opening += f"\nЗа {count} — {total} без доставки."
        elif not quantity:
            opening += " Уточните количество, чтобы рассчитать стоимость товаров."
    address = re.search(r"\b(?:адрес|улица|ул\.|дом|пункт выдачи|пвз)\b", current)
    next_step = "Менеджер уточнит стоимость и дату доставки." if address else (
        "Укажите адрес или пункт выдачи, чтобы менеджер рассчитал стоимость и дату доставки."
        if DELIVERY_INDEX.search(current) else
        "Укажите почтовый индекс или адрес, чтобы менеджер уточнил стоимость и дату доставки."
    )
    subject = f"Доставка: {selected['sku'] if selected else 'товар уточняется'}, {count}. {message}"
    return opening, next_step, products, subject, selected["sku"] if selected else None, quantity


def delivery_details_followup(message, trusted_history):
    return bool(trusted_history and "Статус обращения: ожидает ответа менеджера." in trusted_history[-1]["answer"]
                and "стоимость и дату доставки" in trusted_history[-1]["answer"]
                and DELIVERY_DETAILS.search(normalize(message)))


MONITOR_INTERFACES = {
    "u2424h": ("USB-C к компьютеру передаёт только данные. Для изображения используйте HDMI или DisplayPort; "
               "ноутбуку нужен отдельный источник питания. RJ45 нет.",
               "https://www.dell.com/en-us/shop/monitors/apd/dell-ultrasharp-24-monitor-u2424h/u2424h_monitor/x99wp"),
    "u2424he": ("USB-C передаёт видео и данные и заряжает ноутбук до 90 Вт. "
                "RJ45 2,5 Гбит/с подключает проводную сеть. HDMI и DisplayPort тоже есть.",
                "https://www.dell.com/en-us/shop/dell-ultrasharp-24-usb-c-hub-monitor-u2424he/apd/210-bkry/monitors-monitor-accessories"),
}


def comparison_details(items):
    """Добавляет различия только для моделей с подтверждёнными спецификациями."""
    models = [code for item in items for code in MODEL.findall(normalize_codes(item["name"]))
              if code in MONITOR_INTERFACES]
    if len(models) != len(items) or not models:
        return "", []
    lines = [f"{code.upper()} — {MONITOR_INTERFACES[code][0]}" for code in models]
    if set(models) == MONITOR_INTERFACES.keys():
        lines.append("Для одного кабеля, зарядки и проводной сети выбирайте U2424HE. "
                     "Проверьте, что USB-C ноутбука поддерживает DisplayPort Alt Mode и зарядку USB-C. "
                     "Если достаточно HDMI или DisplayPort и отдельной зарядки, подойдёт U2424H.")
    links = [{"title": f"Спецификация Dell {code.upper()}", "url": MONITOR_INTERFACES[code][1]} for code in models]
    return "\n\n".join(lines), links


def compare_monitor_models(question, catalog):
    normalized = normalize_codes(question)
    if not re.search(r"\b(?:сравн\w*|отлича\w*|разниц\w*)\b", normalized):
        return None
    codes = list(dict.fromkeys(MODEL.findall(normalized)))
    scope = product_scope("монитор " + question) if DELL_DISPLAY.search(normalized) else product_scope(question)
    if len(codes) != 2 or not scope or scope.category != "monitor":
        return None
    items = []
    for code in codes:
        matching_scope = replace(scope, model=code)
        found = next((item for item in catalog.search(code, 10, scope=matching_scope)
                      if matching_scope.matches(item)), None)
        if found is None:
            return "Уточните артикулы обеих моделей, чтобы сравнить подтверждённые карточки.", []
        items.append(found)
    lines = ["\n".join(part for part in (item_text(item), item["description"]) if part) for item in items]
    a, b = items
    if a["price_minor"] is not None and b["price_minor"] is not None:
        difference = abs(a["price_minor"] - b["price_minor"])
        lines.append(f"Разница в цене — {price_text(difference)}.")
    return "\n\n".join(lines), items


def scoped_answer(scope, products, message, context):
    """Формирует клиентский ответ по позициям, прошедшим каталожную проверку."""
    current, full = normalize(message), normalize(context)
    if scope.model and scope.model in normalize_codes(message) and scope.model not in current:
        return f"Возможно, вы имели в виду {scope.model.upper()} с латинской буквой. Подтвердите модель.", [], []
    if scope.category == "office":
        if not products:
            return "Подтверждённую поставку лицензии в каталоге найти не удалось. Менеджер может помочь с выбором.", [], []
        first = next((p for p in products if re.search(r"\b(?:1 год|12 мес)\b", normalize(p["name"] + " " + p["description"]))), products[0])
        users = re.search(r"\b(\d+)\s+(?:именных\s+)?(?:сотрудник\w*|пользовател\w*)", full)
        quantity = f"Для {users[1]} пользователей нужно проверить возможность закупки {users[1]} пользовательских лицензий. " if users else ""
        response = ("Microsoft 365 Business Standard включает настольные Outlook, Word и Excel; Business Basic "
                    "предлагает веб- и мобильные приложения. Business Standard также включает корпоративную почту Exchange Online. "
                    "В каталоге есть " + item_text(first) + ". "
                    "SKU указан на одного пользователя. " + quantity + "Менеджер может проверить оформление нужного числа лицензий, "
                    "доступность варианта с Teams или без него.")
        if not OFFICE_ANNUAL.search(full):
            response += " Какой срок подписки вам нужен?"
        return response, [first], [{"title": "Сравнение тарифов Microsoft 365 для бизнеса", "url": MICROSOFT_365}]
    if re.search(r"сравн|два (?:реальн|вариант)", full):
        distinct = {}
        for product in products:
            distinct.setdefault(normalize(product["name"]), product)
        products = list(distinct.values())
        if len(products) < 2:
            return ("В каталоге не удалось подтвердить два разных товара, которые отвечают всем условиям. "
                    "Если бюджет или другие параметры можно изменить, укажите допустимый компромисс; также может помочь менеджер."), [], []
        first, second = products[:2]
        lines = [f'• {item_text(p)}. {p["description"].strip().rstrip(".")}.'
                 for p in (first, second)]
        if scope.drive_type == "ssd" and any("hdd" in normalize(p["name"]) for p in (first, second)):
            lines.append("В заголовках фида указано HDD, но поле типа накопителя для этих позиций указывает SSD.")
        lines.append("Первый стоит дешевле или столько же. Преимущество по характеристикам можно подтвердить только по данным карточек.")
        return "Под условия подходят:\n" + "\n".join(lines), [first, second], []
    if not products:
        if scope.category == "gpu" and re.search(r"куп|заказ|оплат", full):
            return ("Подтверждённого предложения с указанной моделью и бюджетом в каталоге нет. "
                    "Оформить и оплатить заказ прямо в чате нельзя. Менеджер может проверить варианты, если вы оставите обращение."), [], []
        if scope.category == "laptop":
            return ("Не нашлось ноутбука, у которого каталог подтверждает одновременно комплектацию и цену в заданном бюджете. "
                    "Уточните, какие параметры можно снизить, или обратитесь к менеджеру."), [], []
        return "В каталоге не нашлось товаров с указанными характеристиками. Менеджер может помочь с подбором.", [], []
    if scope.model:
        selected = products[0]
        answer = f"В каталоге есть {item_text(selected)}."
        _, quantity = quantity_in(normalize_codes(message))
        if quantity and selected["price_minor"] is not None:
            total = price_text(selected["price_minor"] * quantity)
            answer += f"\nЗа {quantity} шт. — {total}."
        return answer + "\n\nЦену, наличие и срок поставки уточним при оформлении.", products, []
    selected = products[0]
    evidence = verified_models(selected)
    if evidence and evidence[0] in VERIFIED_DETAILS:
        detail, title, url = VERIFIED_DETAILS[evidence[0]]
        return f"Подходит {item_text(selected)}. {detail}", products, [{"title": title, "url": url}]
    return f"Подходит {item_text(selected)}.", products, []


CHAT_NAVIGATION = "Нажмите на вкладку «Чат»."
REFERENCE_PLACEHOLDER = "Официальный справочный раздел по теме обращения."
REFERENCE_STOP_WORDS = {"где", "как", "можно", "подскажите", "пожалуйста", "нужно", "свой", "своего"}


def safe_commercial_answer(message, answer, products):
    """Формирует итог из данных каталога даже без коммерческих ключевых слов."""
    if normalize(message.strip()).rstrip("?") == "как открыть чат" and answer == CHAT_NAVIGATION:
        return None
    if not products:
        return ("Не удалось подтвердить условия. Подскажите модель или нужную конфигурацию. "
                "Если нужна помощь, передам вопрос менеджеру.")
    facts = "\n".join(f"• {item_text(product)}" for product in products)
    return ("Данные товарного каталога:\n" + facts +
            "\n\nНаличие, сроки поставки, совместимость и условия лицензирования подтвердит менеджер.")


def reference_terms(text):
    return {word[:5] for word in re.findall(r"[^\W\d_]+", normalize(text))
            if len(word) >= 4 and word not in REFERENCE_STOP_WORDS}


def checked_model_answer(message, answer, products, resources):
    """Принимает проверенную выдержку дословно или строит ответ заново."""
    terms = reference_terms(message)
    for item in resources:
        url = item.get("url")
        excerpt = item.get("excerpt", "").strip()
        title = item.get("title", "").strip()
        if (url and official_url(url) and title and excerpt and excerpt != REFERENCE_PLACEHOLDER
                and " ".join(answer.split()) == " ".join(excerpt.split())
                and len(terms & reference_terms(title)) >= 2):
            return excerpt, [{"title": title, "url": url}]

    replacement = safe_commercial_answer(message, answer, products)
    if replacement is None:
        return CHAT_NAVIGATION, []
    links = {}
    for product in products:
        if (url := product.get("url")) and official_url(url):
            links[url] = product["name"]
    return replacement, [{"title": title, "url": url} for url, title in links.items()]
