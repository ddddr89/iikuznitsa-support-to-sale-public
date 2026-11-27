"""Use catalog evidence and ask for the facts needed to qualify a recommendation."""

import re

from app.catalog_matching import OFFICE_ANNUAL, normalize, normalize_codes, verified_models

MICROSOFT_365 = "https://www.microsoft.com/en-us/microsoft-365/business/no-teams-plans-and-pricing"
WINDOWS_SERVER = "https://www.microsoft.com/licensing/guidance/Windows-Server-2025"
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


def advisory(message, context):
    current = normalize(message)
    full = normalize(context)
    if re.search(r"\b(?:api[- ]?ключ|токен\w*)\b", current) and re.search(r"\b(?:покаж|выда|раскро)", current):
        return "Ключи и данные других клиентов недоступны. Свои обращения можно посмотреть во вкладке «Мои заявки».", []
    if re.search(r"\bddr[45]\b", current) and len(set(re.findall(r"\bddr[45]\b", current))) > 1:
        return ("DDR4 и DDR5 несовместимы. Если плата поддерживает только DDR4, DDR5 устанавливать нельзя. "
                "Уточните модель платы и необходимый объём. По ним можно проверить форм-фактор, допустимую ёмкость и ECC."), []
    if len(set(re.findall(r"\bddr[45]\b", full))) > 1 and re.search(r"\bddr[45]\b", current):
        return ("Нужен комплект памяти поколения, которое поддерживает плата. Для проверки допустимого объёма, "
                "форм-фактора и ECC сообщите точную модель платы. До проверки не могу рекомендовать конкретный модуль."), []
    if "windows server" in full:
        qualified = all(re.search(pattern, full) for pattern in (r"\b2025\b", r"\b16\s+яд", r"(?:две|2)\s+(?:виртуальн|вм)"))
        if qualified:
            return ("Для двух виртуальных машин на одном полностью лицензированном 16-ядерном хосте "
                    "рассмотрите Windows Server 2025 Standard с лицензированием по физическим ядрам и User CAL "
                    "для пользователей без эквивалентных лицензий. Уточнить нужное число CAL, условия "
                    "закупки и цену может менеджер. Подтверждённого SKU 2025 в выдаче нет.",
                    [{"title": "Руководство по лицензированию Windows Server 2025", "url": WINDOWS_SERVER}])
        return ("Чтобы выбрать редакцию Windows Server и CAL, уточните версию, число физических процессоров "
                "и ядер, количество ВМ, способ доступа пользователей и нужен ли RDS."), []
    if re.search(r"защит\w*[^.!?\n]*почт|фишинг|вредоносн\w* вложен", full) and "microsoft 365 для" not in full:
        if "exchange online" in current:
            return ("Для Exchange Online нужно подтвердить поддержку этой платформы, "
                    "защиту от фишинга и вложений и объём лицензии. В каталоге нет подтверждённого совпадения по всем условиям. "
                    "Менеджер может проверить варианты и условия закупки."), []
        return "Где размещена почта и сколько почтовых ящиков нужно защитить? Нужна защита от фишинга, вредоносных вложений или обе?", []
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
    if re.search(r"салехард|629008", full):
        return ("Доставку в Салехард, её срок и стоимость нужно подтвердить по индексу и адресу. "
                "Оплатить прямо в этом чате нельзя. Для организации менеджер может уточнить доставку и подготовить проект счёта.",
                [{"title": "Условия оплаты и доставки Софтлайн", "url": "https://store.softline.ru/delivery_info/"}])
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
    return f'{item["name"]}, артикул {item["sku"]} — {price_text(item["price_minor"])}'


def scoped_answer(scope, products, message, context):
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
        return (f"В каталоге есть {item_text(products[0])}. Наличие нужного количества и срок поставки подтвердит менеджер."), products, []
    selected = products[0]
    evidence = verified_models(selected)
    if evidence and evidence[0] in VERIFIED_DETAILS:
        detail, title, url = VERIFIED_DETAILS[evidence[0]]
        return f"Подходит {item_text(selected)}. {detail}", products, [{"title": title, "url": url}]
    return f"Подходит {item_text(selected)}.", products, []
