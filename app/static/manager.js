import { byId, element, leadStatus, money, request, showHistory, status } from "./common.js";

let token = "";
let selected = null;
let savedDocuments = [];
let session = 0;
let queue = [];
const leadFilter = byId("lead-filter");
const workspace = byId("workspace");
const accessForm = byId("access-form");
const refresh = byId("refresh");
const quoteForm = byId("quote-form");
const knowledgeForm = byId("knowledge-form");
const leadUpdateForm = byId("lead-update-form");
const items = byId("quote-items");
const add = byId("add-item");

function clearQuote() {
  status("quote-status", "");
  status("download-status", "");
}

function updateAddButton() {
  add.disabled = items.children.length >= 50;
}

function addItem(sku = "") {
  const item = element("div", undefined, "quote-item");
  const skuLabel = element("label", "Артикул");
  const skuInput = element("input");
  skuInput.name = "sku";
  skuInput.value = sku;
  skuInput.maxLength = 200;
  skuInput.required = true;
  skuLabel.append(skuInput);
  const quantityLabel = element("label", "Количество");
  const quantity = element("input");
  quantity.name = "quantity";
  quantity.type = "number";
  quantity.min = "1";
  quantity.max = "999";
  quantity.value = "1";
  quantity.required = true;
  quantityLabel.append(quantity);
  const remove = element("button", "Убрать", "secondary");
  remove.type = "button";
  remove.addEventListener("click", () => {
    item.remove();
    if (items.children.length === 0) addItem();
    else updateAddButton();
    clearQuote();
  });
  item.append(skuLabel, quantityLabel, remove);
  items.append(item);
  updateAddButton();
  return skuInput;
}

add.addEventListener("click", () => {
  if (items.children.length >= 50) return;
  clearQuote();
  addItem().focus();
});
quoteForm.addEventListener("input", clearQuote);

function showLeads(leads = queue, waitingCount) {
  queue = leads;
  if (waitingCount !== undefined) byId("waiting-count").textContent = `Ожидают ответа: ${waitingCount}`;
  const visible = queue.filter((lead) => leadFilter.value === "all" ||
    (lead.status === "closed") === (leadFilter.value === "closed"));
  const cards = visible.map((lead) => {
    const waiting = lead.requires_reply;
    const card = element("article", undefined, waiting ? "card waiting-lead" : "card");
    const date = new Date(lead.created_at);
    const metadata = [leadStatus(lead.status), lead.sku, Number.isNaN(date.getTime()) ? "" : date.toLocaleString("ru-RU")];
    card.append(element("h3", "Заявка № " + lead.id));
    if (waiting) card.append(element("span", "Требует ответа", "status-chip waiting-badge"));
    card.append(element("p", metadata.filter(Boolean).join(" · "), "meta"));
    card.append(element("p", lead.contact), element("p", lead.message));
    const choose = element("button", "Открыть заявку", "secondary");
    choose.type = "button";
    choose.addEventListener("click", () => {
      selected = lead;
      clearQuote();
      byId("selection").textContent = "Заявка № " + lead.id;
      items.replaceChildren();
      quoteForm.hidden = false;
      knowledgeForm.reset();
      knowledgeForm.hidden = false;
      status("knowledge-status", "");
      addItem(lead.sku || "");
      loadLeadDetail(lead);
    });
    const row = element("div", undefined, "row");
    row.append(choose);
    card.append(row);
    return card;
  });
  byId("lead-list").replaceChildren(...(cards.length ? cards : [element("p", "Заявок пока нет.")]));
}

leadFilter.addEventListener("change", () => showLeads());

function resetSelection() {
  selected = null;
  quoteForm.hidden = true;
  knowledgeForm.reset();
  knowledgeForm.hidden = true;
  byId("lead-details").hidden = true;
  byId("lead-history").replaceChildren();
  leadUpdateForm.reset();
  leadUpdateForm.hidden = true;
  status("lead-update-status", "");
  showSavedDocuments([]);
  items.replaceChildren();
  byId("selection").textContent = "Выберите заявку из списка.";
  clearQuote();
}

function showSession(active) {
  accessForm.hidden = active;
  byId("manager-session").hidden = !active;
  workspace.hidden = !active;
}

byId("sign-out").addEventListener("click", () => {
  session += 1;
  token = "";
  queue = [];
  leadFilter.value = "open";
  refresh.disabled = false;
  resetSelection();
  byId("lead-list").replaceChildren();
  showSession(false);
  status("access-status", "Вы вышли из кабинета.");
  byId("token").focus();
});

accessForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = accessForm.querySelector("button");
  if (button.disabled) return;
  const entered = byId("token").value.trim();
  if (!entered) return;
  button.disabled = true;
  status("access-status", "Проверяем доступ и загружаем заявки…");
  try {
    const { items: leads, waiting_count: waiting } = await request("/manager/leads", { token: entered });
    session += 1;
    token = entered;
    accessForm.reset();
    showLeads(leads, waiting);
    showSession(true);
    status("access-status", "Заявок загружено: " + leads.length);
    refresh.focus();
  } catch (error) {
    status("access-status", error.message, true);
  } finally {
    button.disabled = false;
  }
});

refresh.addEventListener("click", async () => {
  const currentSession = session;
  refresh.disabled = true;
  status("access-status", "Обновляем заявки…");
  try {
    const { items: leads, waiting_count: waiting } = await request("/manager/leads", { token });
    if (currentSession !== session) return;
    showLeads(leads, waiting);
    if (selected && !leads.some((lead) => lead.id === selected.id)) resetSelection();
    status("access-status", "Заявок загружено: " + leads.length);
  } catch (error) {
    if (currentSession === session) status("access-status", error.message, true);
  } finally {
    if (currentSession === session) refresh.disabled = false;
  }
});

quoteForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = quoteForm.querySelector('button:not([type])');
  if (button.disabled || selected === null || !token) return;
  const currentSelection = selected;
  const currentSession = session;
  const rows = [...items.children].map((item) => ({
    sku: item.querySelector('[name="sku"]').value.trim(),
    quantity: item.querySelector('[name="quantity"]').valueAsNumber,
  }));
  clearQuote();
  if (rows.some((row) => !row.sku)) {
    status("quote-status", "Заполните артикул в каждой позиции.", true);
    return;
  }
  const fields = quoteForm.querySelectorAll("input, button");
  fields.forEach((field) => field.disabled = true);
  status("quote-status", "Формируем КП и проект счёта…");
  try {
    const result = await request("/manager/quotes", { token, body: { lead_id: currentSelection.id, items: rows } });
    if (selected !== currentSelection || currentSession !== session) return;
    status("quote-status", "Документы № " + result.id + " · " + money(result.total_minor, result.currency));
    showSavedDocuments([result, ...savedDocuments.filter((document) => document.id !== result.id)]);
  } catch (error) {
    if (selected === currentSelection && currentSession === session) status("quote-status", error.message, true);
  } finally {
    fields.forEach((field) => field.disabled = false);
    updateAddButton();
  }
});

function showSavedDocuments(documents) {
  savedDocuments = documents;
  const section = byId("saved-documents");
  const list = byId("saved-document-list");
  section.hidden = documents.length === 0;
  list.replaceChildren(...documents.map((document) => {
    const row = element("div", undefined, "document-row");
    row.append(element("span", money(document.total_minor, "RUB")));
    const button = element("button", "Скачать документы", "secondary");
    button.type = "button";
    button.addEventListener("click", () => downloadDocuments(button, document.id));
    row.append(button);
    return row;
  }));
}

async function downloadDocuments(button, id) {
  const currentSelection = selected;
  const currentSession = session;
  if (!token || button.disabled) return;

  button.disabled = true;
  status("download-status", "Загружаем документы…");
  try {
    const path = "/manager/quotes/" + encodeURIComponent(id) + "/documents.zip";
    const file = await request(path, { token, download: true });
    if (currentSession !== session || selected !== currentSelection) return;
    const url = URL.createObjectURL(file);
    const link = element("a");
    link.href = url;
    link.download = "documents-" + id + ".zip";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status("download-status", "Скачивание началось.");
  } catch (error) {
    if (currentSession === session && selected === currentSelection) status("download-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

knowledgeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const lead = selected;
  const currentSession = session;
  const button = knowledgeForm.querySelector("button");
  if (!lead || !token || button.disabled) return;
  button.disabled = true;
  try {
    await request("/manager/leads/" + encodeURIComponent(lead.id) + "/knowledge", {
      token,
      method: "PUT",
      body: {
        question: byId("knowledge-question").value.trim(),
        answer: byId("knowledge-answer").value.trim(),
        anonymized: byId("knowledge-anonymized").checked,
      },
    });
    if (selected === lead && currentSession === session) status("knowledge-status", "Ответ подтверждён и сохранён.");
  } catch (error) {
    if (selected === lead && currentSession === session) status("knowledge-status", error.message, true);
  } finally {
    button.disabled = false;
  }
});

function updateClosureField() {
  const closing = byId("lead-state").value === "closed";
  const reason = byId("closure-reason");
  byId("closure-reason-field").hidden = !closing;
  reason.required = closing && Boolean(selected?.requires_reply) && !byId("manager-reply").value.trim();
}

byId("lead-state").addEventListener("change", updateClosureField);
byId("manager-reply").addEventListener("input", updateClosureField);

async function loadLeadDetail(lead) {
  const currentSession = session;
  const section = byId("lead-details");
  section.hidden = false;
  leadUpdateForm.hidden = true;
  leadUpdateForm.reset();
  byId("lead-history").replaceChildren();
  showSavedDocuments([]);
  status("lead-update-status", "Загружаем историю…");
  try {
    const detail = await request("/manager/leads/" + encodeURIComponent(lead.id), { token });
    if (currentSession !== session || selected !== lead) return;
    showHistory(byId("lead-history"), detail);
    showSavedDocuments(detail.documents);
    byId("lead-state").value = detail.status;
    lead.requires_reply = detail.requires_reply;
    updateClosureField();
    leadUpdateForm.hidden = false;
    status("lead-update-status", "");
  } catch (error) {
    if (currentSession === session && selected === lead) status("lead-update-status", error.message, true);
  }
}

leadUpdateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const lead = selected;
  const currentSession = session;
  const button = leadUpdateForm.querySelector("button");
  if (!lead || !token || button.disabled) return;
  const answer = byId("manager-reply").value.trim();
  const state = byId("lead-state").value;
  const reason = state === "closed" ? byId("closure-reason").value.trim() : "";
  button.disabled = true;
  try {
    const path = "/manager/leads/" + encodeURIComponent(lead.id);
    const detail = await request(path, {
      token, method: "PATCH", body: { status: state, ...(answer ? { answer } : {}),
        ...(reason ? { closure_reason: reason } : {}) },
    });
    if (currentSession !== session || selected !== lead) return;
    showHistory(byId("lead-history"), detail);
    showSavedDocuments(detail.documents);
    byId("lead-state").value = detail.status;
    byId("manager-reply").value = "";
    byId("closure-reason").value = "";
    lead.requires_reply = detail.requires_reply;
    updateClosureField();
    status("lead-update-status", "Изменения сохранены.");
    try {
      const { items, waiting_count: waiting } = await request("/manager/leads", { token });
      if (currentSession === session) showLeads(items, waiting);
    } catch {
      // Сохранённое обращение доступно даже при сбое обновления списка.
    }
  } catch (error) {
    if (currentSession === session && selected === lead) status("lead-update-status", error.message, true);
  } finally {
    button.disabled = false;
  }
});
