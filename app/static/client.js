import { byId, element, leadStatus, money, request, showHistory, status } from "./common.js";

const pageSize = 6;
let query = "";
let offset = 0;
let catalogIntent = 0;
let escalation = false;
let lastQuestion = "";
const history = [];
let session;
let managerCursor = 0;
let pollingManager = false;
const conversation = byId("conversation");
const question = byId("question");
const catalogResults = byId("search-products");
const restoredChat = restoreChat();
for (const suggestion of document.querySelectorAll("[data-question]")) {
  suggestion.addEventListener("click", () => {
    question.value = suggestion.dataset.question;
    question.focus();
  });
}

function addMessage(text, customer = false, author = "Помощник") {
  const message = element("article", undefined, "message" + (customer ? " customer" : ""));
  message.append(element("strong", customer ? "Вы" : author), element("p", text));
  conversation.append(message);
  byId("chat-welcome").hidden = true;
  conversation.hidden = false;
  conversation.scrollTop = conversation.scrollHeight;
  return message;
}

function addDocuments(message, documents) {
  if (!/^[a-f0-9]{32}$/.test(documents?.id ?? "")) return;
  const buttons = element("div", undefined, "row");
  const base = "/api/v1/client/quotes/" + documents.id;
  for (const [path, title] of [["quote.pdf", "КП"], ["invoice.pdf", "Счёт"]]) {
    const link = element("a", title, "secondary");
    link.href = base + "/" + path;
    buttons.append(link);
  }
  message.append(buttons);
  byId("open-my-leads").hidden = false;
}

function renderAnswer(result, intent) {
  const answer = addMessage(result.answer);
  addDocuments(answer, result.documents);
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
      // Некорректная ссылка на источник не мешает показать ответ.
    }
  }
  const products = element("div", undefined, "cards");
  showProducts(products, result.products);
  if (products.children.length) answer.append(products);
  if (result.escalation?.id) byId("open-my-leads").hidden = false;
  if (intent === catalogIntent) {
    query = "";
    offset = 0;
    byId("query").value = "";
    byId("previous").disabled = byId("next").disabled = true;
    showProducts(catalogResults, result.products);
    status("search-status", result.products.length
      ? "Товары из последнего ответа помощника."
      : "Введите название или артикул для поиска.");
  }
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
          const link = element("a", "Карточка товара");
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          row.append(link);
        }
      } catch {
        // Товар можно выбрать даже без корректной ссылки на его страницу.
      }
    }
    const prepare = element("button", "Получить КП и счёт", "secondary");
    prepare.type = "button";
    prepare.addEventListener("click", () => {
      const form = byId("chat-form");
      if (form.querySelector("button").disabled) return;
      question.value = "Подготовьте счёт на товар " + product.sku + ".";
      form.requestSubmit();
    });
    row.append(prepare);
    card.append(row);
    return card;
  });
  container.replaceChildren(...cards);
}

async function search(nextQuery, nextOffset, pageChange = false) {
  const intent = ++catalogIntent;
  const previous = byId("previous");
  const next = byId("next");
  const button = byId("search-form").querySelector("button");
  if (!nextQuery) {
    query = "";
    offset = 0;
    catalogResults.replaceChildren();
    previous.disabled = next.disabled = true;
    status("search-status", "Введите запрос для поиска. Подборки помощника тоже появятся здесь.");
    return;
  }
  const previousDisabled = previous.disabled;
  const nextDisabled = next.disabled;
  previous.disabled = next.disabled = button.disabled = true;
  status("search-status", "Загружаем каталог…");
  try {
    const params = new URLSearchParams({ q: nextQuery, limit: pageSize + 1, offset: nextOffset });
    const { items } = await request("/products?" + params);
    if (intent !== catalogIntent) return;
    showProducts(catalogResults, items.slice(0, pageSize));
    query = nextQuery;
    offset = nextOffset;
    previous.disabled = offset === 0;
    next.disabled = items.length <= pageSize;
    const count = Math.min(items.length, pageSize);
    const range = count === 1 ? `Результат ${offset + 1}` : `Результаты ${offset + 1}–${offset + count}`;
    status("search-status", count ? range : "По этому запросу ничего не найдено.");
    if (pageChange) byId("search-form").scrollIntoView({ block: "nearest" });
  } catch (error) {
    if (intent !== catalogIntent) return;
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
byId("previous").addEventListener("click", () => search(query, offset - pageSize, true));
byId("next").addEventListener("click", () => search(query, offset + pageSize, true));

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

let chatShortcut = "";
submit("chat-form", "chat-status", "Готовим ответ…", async () => {
  const message = chatShortcut || question.value.trim();
  chatShortcut = "";
  if (!message) throw new Error("Опишите задачу.");
  const intent = ++catalogIntent;
  await restoredChat;
  await startSession();
  const sent = addMessage(message, true);
  conversation.setAttribute("aria-busy", "true");
  let result;
  try {
    result = await request("/chat", { body: { message, history: history.slice(-4) } });
  } catch (error) {
    sent.remove();
    if (!conversation.children.length) {
      conversation.hidden = true;
      byId("chat-welcome").hidden = false;
    }
    throw error;
  } finally {
    conversation.removeAttribute("aria-busy");
  }
  history.push({ question: message, answer: result.answer });
  if (history.length > 4) history.shift();
  renderAnswer(result, intent);
  lastQuestion = message;
  if (question.value.trim() === message) question.value = "";
  status("chat-status", "");
});

question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
    event.preventDefault();
    byId("chat-form").requestSubmit();
  }
});

function openHandoff() {
  byId("handoff").hidden = false;
  byId("escalate").setAttribute("aria-expanded", "true");
  byId("escalate-contact").setAttribute("aria-expanded", "true");
}

byId("escalate").addEventListener("click", async () => {
  await restoredChat;
  const form = byId("chat-form");
  if (!form.querySelector("button").disabled) {
    chatShortcut = "Позовите менеджера.";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }
});

function openContactForm() {
  escalation = true;
  byId("lead-message").value ||= lastQuestion || question.value.trim();
  byId("request").textContent = "Передать вопрос менеджеру";
  openHandoff();
  byId("contact").focus();
}

byId("escalate-contact").addEventListener("click", async () => {
  await restoredChat;
  openContactForm();
});

byId("close-handoff").addEventListener("click", () => {
  byId("handoff").hidden = true;
  byId("escalate").setAttribute("aria-expanded", "false");
  byId("escalate-contact").setAttribute("aria-expanded", "false");
  byId("escalate").focus();
});

byId("open-my-leads").addEventListener("click", () => {
  byId("leads-tab").click();
  byId("leads-tab").focus();
});

submit("lead-form", "lead-status", "Передаём заявку…", async (form) => {
  const contact = byId("contact").value.trim();
  const message = byId("lead-message").value.trim();
  const sku = byId("sku").value.trim();
  if (!contact || !message) throw new Error("Укажите контакт и опишите задачу.");
  await startSession();
  const lead = await request("/leads", { body: { contact, message, ...(sku ? { sku } : {}), ...(escalation ? { escalation } : {}) } });
  form.reset();
  byId("contact").value = contact;
  escalation = false;
  byId("request").textContent = "Оставить заявку";
  const result = lead.status === "escalated" ? " ожидает ответа менеджера." : " принята.";
  const duplicate = lead.duplicate ? " Используем ранее созданную заявку." : "";
  status("lead-status", "Заявка № " + lead.id + result + duplicate + " Ответ появится в этой переписке.");
  byId("open-my-leads").hidden = false;
});

async function restoreChat() {
  try {
    const { items } = await request("/client/chat-history?include_times=true");
    history.push(...items.slice(-4).map(({ question, answer }) => ({ question, answer })));
    if (items.length) lastQuestion = items.at(-1).question;
    const timeline = items.flatMap(({ question, answer, created_at, documents }) => [
      { text: question, customer: true, created_at }, { text: answer, created_at, documents },
    ]);
    try {
      const replies = await readManagerMessages();
      timeline.push(...replies.map(({ content, created_at }) => ({
        text: content, author: "Менеджер", created_at,
      })));
    } catch {
      // Следующий опрос повторит загрузку ответов менеджера.
    }
    timeline.sort((first, second) => first.created_at.localeCompare(second.created_at));
    for (const entry of timeline) {
      const message = addMessage(entry.text, entry.customer, entry.author);
      addDocuments(message, entry.documents);
    }
  } catch {
    // При сбое загрузки истории новый диалог остаётся доступен.
  }
}

async function readManagerMessages() {
  const { items } = await request("/client/manager-messages?after=" + managerCursor);
  if (items.length) managerCursor = items.at(-1).id;
  return items;
}

async function refreshManagerMessages() {
  if (document.hidden || pollingManager) return;
  pollingManager = true;
  try {
    await restoredChat;
    let batch;
    do {
      batch = await readManagerMessages();
      for (const item of batch) addMessage(item.content, false, "Менеджер");
    } while (batch.length === 100);
  } catch {
    // После временного сбоя следующий опрос повторит запрос.
  } finally {
    pollingManager = false;
  }
}

setInterval(refreshManagerMessages, 8000);
document.addEventListener("visibilitychange", refreshManagerMessages);

function startSession() {
  session ??= request("/client/leads").finally(() => {
    session = null;
  });
  return session;
}

async function loadMyLeads() {
  const list = byId("my-lead-list");
  const refresh = byId("refresh-my-leads");
  if (refresh.disabled) return;
  const expanded = new Set([...list.querySelectorAll("details[open]")].map((card) => card.dataset.leadId));
  refresh.disabled = true;
  status("my-leads-status", "Загружаем заявки…");
  try {
    const { items } = await startSession();
    list.replaceChildren(...items.map((lead) => {
      const card = element("details", undefined, "card lead-detail");
      card.dataset.leadId = lead.id;
      const summary = element("summary");
      summary.append(element("strong", lead.message), element("span", leadStatus(lead.status), "status-chip"));
      const content = element("div", undefined, "lead-content");
      card.append(summary, content);
      card.addEventListener("toggle", async () => {
        if (!card.open) return;
        content.replaceChildren(element("p", "Загружаем историю…", "meta"));
        try {
          const detail = await request("/client/leads/" + encodeURIComponent(lead.id));
          const history = element("div", undefined, "timeline");
          showHistory(history, detail);
          content.replaceChildren(history);
          for (const document of detail.documents) {
            const row = element("div", undefined, "document-row");
            row.append(element("span", money(document.total_minor, "RUB")));
            const link = element("a", "Скачать документы");
            link.href = "/api/v1/client/quotes/" + encodeURIComponent(document.id) + "/documents.zip";
            row.append(link);
            content.append(row);
          }
        } catch (error) {
          content.replaceChildren(element("p", error.message, "status error"));
        }
      });
      if (expanded.has(lead.id)) card.open = true;
      return card;
    }));
    status("my-leads-status", items.length ? "" : "В этой сессии ещё нет заявок.");
  } catch (error) {
    status("my-leads-status", error.message, true);
  } finally {
    refresh.disabled = false;
  }
}

byId("refresh-my-leads").addEventListener("click", loadMyLeads);
const tabs = [byId("chat-tab"), byId("leads-tab")];
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => {
    for (const item of tabs) {
      const active = item === tab;
      item.setAttribute("aria-selected", active);
      item.tabIndex = active ? 0 : -1;
      byId(item.getAttribute("aria-controls")).hidden = !active;
    }
    if (index === 1) loadMyLeads();
  });
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[1] : tabs[1 - index];
    next.focus();
    next.click();
  });
}
