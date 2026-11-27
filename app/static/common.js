export const byId = (id) => document.getElementById(id);

const errors = {
  401: "Проверьте токен менеджера.",
  404: "Заявка или товар не найдены.",
  409: "У одной из позиций цена по запросу. Уточните цену или уберите позицию.",
  429: "Слишком много запросов. Повторите через минуту.",
  422: "Проверьте заполнение полей.",
  503: "Сервис временно недоступен.",
};

export async function request(path, { body, token, download = false, method } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = "Bearer " + token;
  let response;
  try {
    response = await fetch("/api/v1" + path, {
      method: method ?? (body === undefined ? "GET" : "POST"),
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: "error",
      cache: "no-store",
    });
  } catch {
    throw new Error("Не удалось связаться с сервером. Повторите попытку.");
  }
  if (!response.ok) {
    throw new Error(errors[response.status] ?? "Сервер не смог выполнить запрос.");
  }
  return download ? response.blob() : response.json();
}

const entities = /&(?:#[0-9]{1,8}|#x[0-9a-f]{1,8}|[a-z][a-z0-9]{1,31});/gi;
const entityDecoder = document.createElement("textarea");

export function displayText(value) {
  let decoded = String(value);
  for (let pass = 0; pass < 2; pass++) {
    const next = decoded.replace(entities, (entity) => {
      // Передаём только одну предварительно проверенную HTML-сущность.
      entityDecoder.innerHTML = entity;
      return entityDecoder.value;
    });
    if (next === decoded) break;
    decoded = next;
  }
  return decoded;
}

export function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = displayText(content);
  if (className) node.className = className;
  return node;
}

export function status(id, message, error = false) {
  const node = byId(id);
  node.textContent = message;
  node.classList.toggle("error", error);
}

export function money(minor, currency) {
  return new Intl.NumberFormat("ru-RU", { style: "currency", currency }).format(minor / 100);
}

const leadStatuses = {
  new: "Заявка на оформление",
  escalated: "Ожидает ответа менеджера",
  in_progress: "В работе",
  answered: "Ответ отправлен",
  closed: "Закрыта",
};

export function leadStatus(value) {
  return Object.hasOwn(leadStatuses, value) ? leadStatuses[value] : "Статус неизвестен";
}

export function showHistory(container, lead) {
  const roles = { request: "Обращение", client: "Клиент", assistant: "ИИ-помощник",
    manager: "Менеджер", status: "Изменение статуса", closure_reason: "Причина закрытия" };
  const history = [...lead.history];
  if (!history.some((item) => item.role === "client" && item.content === lead.message)) {
    history.push({ role: "request", content: lead.message, created_at: lead.created_at });
    history.sort((left, right) => left.created_at.localeCompare(right.created_at));
  }
  container.replaceChildren(...history.map(({ role, content, created_at }) => {
    const entry = element("article", undefined, "history-entry");
    const time = new Date(created_at);
    entry.append(element("strong", roles[role]));
    if (!Number.isNaN(time.valueOf())) entry.append(element("span", time.toLocaleString("ru-RU"), "meta"));
    entry.append(element("p", role === "status" ? leadStatus(content) : content));
    return entry;
  }));
}
