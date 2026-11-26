export const byId = (id) => document.getElementById(id);

const errors = {
  401: "Проверьте токен менеджера.",
  404: "Заявка или товар не найдены.",
  409: "У одной из позиций цена по запросу. Уточните цену или уберите позицию.",
  422: "Проверьте заполнение полей.",
  503: "Сервис временно недоступен.",
};

export async function request(path, { body, token, download = false } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = "Bearer " + token;
  let response;
  try {
    response = await fetch("/api/v1" + path, {
      method: body === undefined ? "GET" : "POST",
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

export function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
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
