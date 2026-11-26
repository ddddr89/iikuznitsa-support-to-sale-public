import { byId, element, money, request, status } from "./common.js";

const pageSize = 10;
let query = "";
let offset = 0;
let escalation = false;
let lastQuestion = "";
const history = [];
const conversation = byId("conversation");
const question = byId("question");

const actionLabels = {
  catalog_search: "Поиск в каталоге",
  knowledge_search: "Поиск в базе знаний",
  tool_catalog_search: "Дополнительный поиск",
  model_yandex: "YandexGPT",
  model_openai: "GPT-5.4 mini",
  route_n8n: "Маршрут n8n",
};
const resultLabels = {
  done: "выполнено",
  error: "ошибка",
};

function addMessage(text, customer = false) {
  const message = element("article", undefined, "message" + (customer ? " customer" : ""));
  message.append(element("strong", customer ? "Вы" : "Помощник"), element("p", text));
  conversation.append(message);
  conversation.hidden = false;
  conversation.scrollTop = conversation.scrollHeight;
  return message;
}

function renderActions(actions) {
  const trace = element("ul", undefined, "trace");
  trace.setAttribute("aria-label", "Действия помощника");
  for (const { type, status } of actions ?? []) {
    if (!Object.hasOwn(actionLabels, type) || !Object.hasOwn(resultLabels, status)) continue;
    const entry = element("li", actionLabels[type] + " · " + resultLabels[status]);
    entry.dataset.state = status;
    trace.append(entry);
  }
  return trace;
}

function renderAnswer(result) {
  const answer = addMessage(result.answer);
  const labels = { yandex: "YandexGPT", openai: "GPT-5.4 mini", catalog: "Ответ по каталогу" };
  if (Object.hasOwn(labels, result.provider)) {
    answer.append(element("p", labels[result.provider], "meta"));
  }
  const trace = renderActions(result.actions);
  if (trace.children.length) answer.append(trace);
  for (const source of result.sources ?? []) {
    try {
      const url = new URL(source.url);
      if (url.protocol !== "https:") continue;
      const summary = element("p", undefined, "meta");
      const link = element("a", source.title);
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      summary.append(link);
      answer.append(summary);
    } catch {
      // Ignore invalid source URLs.
    }
  }
  const products = element("div", undefined, "cards");
  showProducts(products, result.products);
  if (products.children.length) answer.append(products);
  conversation.scrollTop = answer.offsetTop - conversation.offsetTop;
}

function showProducts(container, products) {
  const cards = products.map((product) => {
    const card = element("article", undefined, "card");
    card.append(element("h3", product.name));
    card.append(element("p", [product.category, product.sku].filter(Boolean).join(" · "), "meta"));
    if (product.description) card.append(element("p", product.description));
    const row = element("div", undefined, "row");
    row.append(element("strong", product.price_minor === null ? "Цена по запросу" : money(product.price_minor, product.currency)));
    if (product.url) {
      try {
        const url = new URL(product.url);
        if (["https:", "http:"].includes(url.protocol)) {
          const link = element("a", "Карточка товара ↗");
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          row.append(link);
        }
      } catch {
        // The product remains selectable when the source URL is absent or malformed.
      }
    }
    const select = element("button", "В заявку", "secondary");
    select.type = "button";
    select.addEventListener("click", () => {
      byId("sku").value = product.sku;
      byId("contact").focus();
    });
    row.append(select);
    card.append(row);
    return card;
  });
  container.replaceChildren(...cards);
}

async function search(nextQuery, nextOffset) {
  const previous = byId("previous");
  const next = byId("next");
  const button = byId("search-form").querySelector("button");
  const previousDisabled = previous.disabled;
  const nextDisabled = next.disabled;
  previous.disabled = next.disabled = button.disabled = true;
  status("search-status", "Загружаем каталог…");
  try {
    const params = new URLSearchParams({ q: nextQuery, limit: pageSize + 1, offset: nextOffset });
    const { items } = await request("/products?" + params);
    showProducts(byId("search-products"), items.slice(0, pageSize));
    query = nextQuery;
    offset = nextOffset;
    previous.disabled = offset === 0;
    next.disabled = items.length <= pageSize;
    status("search-status", items.length ? "" : "По этому запросу ничего не найдено.");
  } catch (error) {
    previous.disabled = previousDisabled;
    next.disabled = nextDisabled;
    status("search-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

byId("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!event.currentTarget.querySelector("button").disabled) search(byId("query").value.trim(), 0);
});
byId("previous").addEventListener("click", () => search(query, offset - pageSize));
byId("next").addEventListener("click", () => search(query, offset + pageSize));

function submit(id, statusId, pending, action) {
  byId(id).addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    if (button.disabled) return;
    button.disabled = true;
    status(statusId, pending);
    try {
      await action(form);
    } catch (error) {
      status(statusId, error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

submit("chat-form", "chat-status", "Ожидаем ответ сервера…", async () => {
  const message = question.value.trim();
  if (!message) throw new Error("Опишите задачу.");
  addMessage(message, true);
  const result = await request("/chat", { body: { message, history: history.slice(-4) } });
  history.push({ question: message, answer: result.answer });
  if (history.length > 4) history.shift();
  renderAnswer(result);
  lastQuestion = message;
  if (question.value.trim() === message) question.value = "";
  byId("escalate").hidden = false;
  status("chat-status", "");
});

byId("escalate").addEventListener("click", () => {
  escalation = true;
  byId("lead-message").value ||= lastQuestion;
  byId("request").textContent = "Передать вопрос менеджеру";
  byId("contact").focus();
});

submit("lead-form", "lead-status", "Передаём заявку…", async (form) => {
  const contact = byId("contact").value.trim();
  const message = byId("lead-message").value.trim();
  const sku = byId("sku").value.trim();
  if (!contact || !message) throw new Error("Укажите контакт и опишите задачу.");
  const lead = await request("/leads", { body: { contact, message, ...(sku ? { sku } : {}), ...(escalation ? { escalation } : {}) } });
  form.reset();
  escalation = false;
  byId("request").textContent = "Оставить заявку";
  const result = lead.status === "escalated" ? " передана менеджеру на разбор." : " принята.";
  const duplicate = lead.duplicate ? " Используем ранее созданную заявку." : "";
  status("lead-status", "Заявка № " + lead.id + result + duplicate);
});

async function loadCatalogSource() {
  const labels = {
    demo: "Демонстрационный каталог",
    softline_sample: "Выборка товаров Softline",
    softline_feed: "Фид Softline",
    local_csv: "Каталог из загруженного CSV",
    empty: "Каталог пока пуст",
    unknown: "Источник каталога не указан",
  };
  try {
    const { source, item_count: count, imported_at: importedAt } = await request("/catalog/status");
    if (!Object.hasOwn(labels, source)) return;
    const provenance = byId("catalog-source");
    const details = [labels[source]];
    if (Number.isSafeInteger(count) && source !== "empty") {
      details.push(new Intl.NumberFormat("ru-RU").format(count) + " поз.");
    }
    const imported = new Date(importedAt);
    if (importedAt && !Number.isNaN(imported.getTime())) {
      details.push("Обновлён " + imported.toLocaleDateString("ru-RU"));
    }
    provenance.textContent = details.join(" · ");
    provenance.hidden = false;
  } catch {
    // Catalog search remains available when status is unavailable.
  }
}

loadCatalogSource();
search("", 0);
