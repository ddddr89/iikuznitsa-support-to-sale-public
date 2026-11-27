import assert from "node:assert/strict";
import { readFile, mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";

const { chromium } = await import(pathToFileURL(resolve(
  process.env.PLAYWRIGHT_MODULE || ".stand/tools/node_modules/playwright/index.mjs"
)));
const base = process.env.DEMO_URL || "http://127.0.0.1:18081";
const sku = process.env.DEMO_SKU || "DEMO-MON-01";
const token = process.env.MANAGER_TOKEN || (await readFile(process.env.DEMO_TOKEN_FILE, "utf8")).trim();
const output = resolve(process.env.DEMO_OUT || ".stand/demo");
const deadline = setTimeout(() => {
  console.error("Демонстрация превысила 180 секунд");
  process.exit(1);
}, 180_000);
const started = Date.now();
await mkdir(output, { recursive: true });
let browser;
let context;
let video;
const recording = process.env.DEMO_RECORD === "1";
const pause = (page, milliseconds) => recording ? page.waitForTimeout(milliseconds) : Promise.resolve();

try {
  browser = await chromium.launch({
    executablePath: process.env.CHROME_BIN || "/opt/google/chrome/chrome",
    headless: process.env.DEMO_HEADLESS !== "0",
  });
  const viewport = process.env.DEMO_MOBILE === "1" ? { width: 390, height: 844 } : { width: 1280, height: 900 };
  context = await browser.newContext({
    acceptDownloads: true, viewport,
    ...(recording ? { recordVideo: { dir: output, size: viewport } } : {}),
  });
  const page = await context.newPage();
  video = page.video();
  page.setDefaultTimeout(25_000);
  const contact = `demo-${Date.now()}@example.invalid`;
  const question = `Как оформить счёт на артикул ${sku}?`;
  await page.goto(base);
  await page.screenshot({ path: resolve(output, "home.png"), fullPage: true });
  await pause(page, 5000);
  await page.getByRole("link", { name: /Я клиент.*Открыть чат/ }).click();
  if (recording) {
    await page.locator("#question").fill("Хотим защитить корпоративную почту и файлы на 50 сотрудников. Что нужно уточнить?");
    await pause(page, 2800);
    const first = page.waitForResponse((res) => res.url().endsWith("/api/v1/chat"));
    await page.locator("#chat-form button").click();
    const openQuestion = await (await first).json();
    assert.equal(openQuestion.provider, "yandex");
    await page.locator(".message:not(.customer)").last().waitFor();
    await pause(page, 8500);
  }
  await page.locator("#query").fill(sku);
  await page.locator("#search-form button").click();
  const card = page.locator("#search-products article").filter({ hasText: sku }).first();
  await card.getByRole("button", { name: "Получить КП и счёт" }).waitFor();
  if (recording) await page.locator("#chat-form").scrollIntoViewIfNeeded();
  await pause(page, 2600);
  await page.locator("#question").fill(question);
  const response = page.waitForResponse((res) => res.url().endsWith("/api/v1/chat"));
  await page.locator("#chat-form button").click();
  const chat = await (await response).json();
  assert.ok(chat.answer);
  if (recording) {
    assert.equal(chat.provider, "yandex");
    assert.ok(chat.actions.some((action) => action.type === "route_n8n" && action.status === "done"));
  }
  await page.locator("#conversation .message:not(.customer)").last().waitFor();
  assert.equal(await page.getByRole("list", { name: "Действия помощника" }).count(), 0);
  assert.ok(chat.actions.some((action) => action.type === "catalog_search" && action.status === "done"));
  await page.screenshot({ path: resolve(output, "client.png"), fullPage: true });
  await pause(page, 8500);
  await page.locator("#escalate").click();
  await page.locator("#sku").fill(sku);
  await page.locator("#contact").fill(contact);
  await page.locator("#lead-form button").click();
  const receipt = page.getByText(/ожидает ответа менеджера/);
  await receipt.waitFor();
  const leadId = (await receipt.textContent()).match(/[a-f0-9]{32}/)[0];
  await page.getByRole("tab", { name: "Мои заявки" }).click();
  const ownLead = page.locator("#my-lead-list details").filter({ hasText: question });
  await ownLead.locator("summary").click();
  await ownLead.getByText("ИИ-помощник", { exact: true }).last().waitFor();
  await pause(page, 6000);
  console.log("1/4 Обращение передано менеджеру");

  await page.getByRole("link", { name: "Менеджер" }).click();
  await page.locator("#token").fill(token);
  await page.locator("#access-form button").click();
  const lead = page.locator("#lead-list article").filter({ hasText: contact });
  await lead.getByText(/Ожидает ответа менеджера/).waitFor();
  await lead.getByRole("button", { name: "Открыть заявку" }).click();
  await page.locator("#lead-history").getByText("ИИ-помощник", { exact: true }).last().waitFor();
  const managerAnswer = "Документы по вашему обращению доступны в этой вкладке.";
  await page.locator("#lead-state").selectOption("answered");
  await page.locator("#manager-reply").fill(managerAnswer);
  await page.locator("#lead-update-form button").click();
  await page.getByText("Изменения сохранены.").waitFor();
  await page.locator('[name="quantity"]').fill("2");
  const quoteWait = page.waitForResponse((res) => res.url().endsWith("/manager/quotes"));
  await page.getByRole("button", { name: "Подготовить документы" }).click();
  const quote = await (await quoteWait).json();
  assert.ok(quote.total_minor > 0);
  const downloadWait = page.waitForEvent("download");
  await page.locator("#saved-documents").getByRole("button", { name: "Скачать документы" }).first().click();
  const documents = await downloadWait;
  const archivePath = resolve(output, "documents.zip");
  await documents.saveAs(archivePath);
  const total = (quote.total_minor / 100).toFixed(2).replace(".", ",");
  for (const name of ["Коммерческое предложение.pdf", "Проект счёта.pdf"]) {
    const pdf = execFileSync("python3", ["-c", "from zipfile import ZipFile; import sys; sys.stdout.buffer.write(ZipFile(sys.argv[1]).read(sys.argv[2]))", archivePath, name]);
    const text = execFileSync("pdftotext", ["-", "-"], { input: pdf, encoding: "utf8" });
    assert.ok(text.replace(/\s/g, "").includes(total), "Итог PDF совпадает с суммой пакета");
  }
  await pause(page, 6200);
  console.log("2/4 Архив с обоими PDF скачан и проверен");

  const topic = "Как проверить статус конкурсного счёта?";
  const answer = "Статус конкурсного счёта проверяет менеджер по номеру заявки.";
  await page.locator("#knowledge-question").fill(topic);
  await page.locator("#knowledge-answer").fill(answer);
  await page.locator("#knowledge-anonymized").check();
  await page.locator("#knowledge-form button").click();
  await page.getByText("Ответ подтверждён и сохранён.").waitFor();
  await pause(page, 5200);
  console.log("3/4 Ответ сохранён в базе знаний");
  await page.screenshot({ path: resolve(output, "manager.png"), fullPage: true });

  await page.getByRole("link", { name: "Клиент" }).click();
  await page.getByRole("tab", { name: "Мои заявки" }).click();
  const reopened = page.locator("#my-lead-list details").filter({ hasText: question });
  await reopened.locator("summary").click();
  await reopened.getByText(managerAnswer).waitFor();
  await reopened.getByText("Ответ отправлен").first().waitFor();
  const clientDownload = page.waitForEvent("download");
  await reopened.getByRole("link", { name: "Скачать документы" }).click();
  assert.match((await clientDownload).suggestedFilename(), /documents-.*\.zip$/);
  await page.screenshot({ path: resolve(output, "my-leads.png"), fullPage: true });
  await pause(page, 7300);
  const outsider = await browser.newPage();
  await outsider.request.get(base + "/api/v1/client/leads");
  for (const path of ["/client/leads/" + leadId, "/client/quotes/" + quote.id + "/documents.zip"]) {
    assert.equal((await outsider.request.get(base + "/api/v1" + path)).status(), 404);
  }
  assert.equal((await outsider.request.patch(base + "/api/v1/manager/leads/" + leadId,
    { data: { status: "closed" } })).status(), 401);
  await outsider.close();
  console.log("Доступ к заявке и комплекту документов из другой сессии отклонён");
  await page.getByRole("tab", { name: "Чат" }).click();
  await page.locator("#question").fill(topic);
  const follow = page.waitForResponse((res) => res.url().endsWith("/api/v1/chat"));
  await page.locator("#chat-form button").click();
  const reused = await (await follow).json();
  assert.equal(reused.answer, answer);
  assert.equal(reused.provider, "knowledge");
  assert.ok(reused.actions.some(({ type }) => type === "knowledge_confirmed"));
  console.log("4/4 Подтверждённый ответ найден");
  await pause(page, 7500);
  if (recording) {
    await context.close();
    await video.saveAs(resolve(output, "demo-one-take.webm"));
    console.log("Непрерывная видеозапись сохранена.");
  } else {
    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await mobile.route("**/api/v1/products?*", (route) => route.fulfill({ json: { items: [{
      sku: "SAFE-1", name: "Монитор &amp;quot;Офис&amp;quot;",
      description: "&lt;img src=x onerror=alert(1)&gt;",
      category: "Офис &amp; дом", url: "", price_minor: null, currency: "RUB",
    }] } }));
    await mobile.goto(base + "/client");
    await mobile.locator("#query").fill("SAFE-1");
    await mobile.locator("#search-form button").click();
    const previewCard = mobile.locator("#search-products article").first();
    assert.equal(await previewCard.locator("h3").textContent(), 'Монитор "Офис"');
    assert.match(await previewCard.textContent(), /<img src=x onerror=alert\(1\)>/);
    assert.equal(await previewCard.locator("img").count(), 0);
    assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth), 390);
    await mobile.screenshot({ path: resolve(output, "mobile-client.png"), fullPage: true });
  }
  console.log(`Готово за ${((Date.now() - started) / 1000).toFixed(1)} с`);
} finally {
  clearTimeout(deadline);
  await browser?.close();
}
