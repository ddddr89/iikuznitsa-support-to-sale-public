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

try {
  browser = await chromium.launch({
    executablePath: process.env.CHROME_BIN || "/opt/google/chrome/chrome",
    headless: process.env.DEMO_HEADLESS !== "0",
  });
  const page = await browser.newPage({ acceptDownloads: true });
  page.setDefaultTimeout(25_000);
  const contact = `demo-${Date.now()}@example.invalid`;
  const question = `Как оформить счёт на артикул ${sku}?`;
  await page.goto(base);
  await page.locator("#query").fill(sku);
  await page.locator("#search-form button").click();
  const card = page.locator("#search-products article").filter({ hasText: sku }).first();
  await card.getByRole("button", { name: "В заявку" }).click();
  await page.locator("#question").fill(question);
  const response = page.waitForResponse((res) => res.url().endsWith("/api/v1/chat"));
  await page.locator("#chat-form button").click();
  const chat = await (await response).json();
  assert.ok(chat.answer);
  await page.locator("#escalate").click();
  await page.locator("#contact").fill(contact);
  await page.locator("#lead-form button").click();
  await page.getByText(/передана менеджеру на разбор/).waitFor();
  console.log("1/4 Обращение передано менеджеру");

  await page.getByRole("link", { name: "Менеджер" }).click();
  await page.locator("#token").fill(token);
  await page.locator("#access-form button").click();
  const lead = page.locator("#lead-list article").filter({ hasText: contact });
  await lead.getByText(/Эскалация/).waitFor();
  await lead.getByRole("button", { name: "Подготовить КП" }).click();
  await page.locator('[name="quantity"]').fill("2");
  const quoteWait = page.waitForResponse((res) => res.url().endsWith("/manager/quotes"));
  await page.getByRole("button", { name: "Сформировать КП" }).click();
  const quote = await (await quoteWait).json();
  assert.ok(quote.total_minor > 0);
  const downloadWait = page.waitForEvent("download");
  await page.locator("#download-invoice").click();
  const invoice = await downloadWait;
  const invoicePath = resolve(output, "invoice.pdf");
  await invoice.saveAs(invoicePath);
  const invoiceText = execFileSync("pdftotext", [invoicePath, "-"], { encoding: "utf8" });
  assert.match(invoiceText, /Счёт на оплату/);
  const total = (quote.total_minor / 100).toFixed(2).replace(".", ",");
  assert.ok(invoiceText.replace(/\s/g, "").includes(total), "Итог в PDF совпадает с суммой КП");
  console.log("2/4 Счёт PDF скачан и проверен");

  const topic = "Как проверить статус конкурсного счёта?";
  const answer = "Статус конкурсного счёта проверяет менеджер по номеру заявки.";
  await page.locator("#knowledge-question").fill(topic);
  await page.locator("#knowledge-answer").fill(answer);
  await page.locator("#knowledge-anonymized").check();
  await page.locator("#knowledge-form button").click();
  await page.getByText("Ответ подтверждён и сохранён.").waitFor();
  console.log("3/4 Ответ сохранён в базе знаний");

  await page.getByRole("link", { name: "Клиент" }).click();
  await page.locator("#question").fill(topic);
  const follow = page.waitForResponse((res) => res.url().endsWith("/api/v1/chat"));
  await page.locator("#chat-form button").click();
  const reused = await (await follow).json();
  assert.equal(reused.answer, answer);
  assert.equal(reused.provider, "knowledge");
  assert.ok(reused.actions.some(({ type }) => type === "knowledge_confirmed"));
  console.log("4/4 Подтверждённый ответ найден");
  console.log(`Готово за ${((Date.now() - started) / 1000).toFixed(1)} с`);
} finally {
  clearTimeout(deadline);
  await browser?.close();
}
