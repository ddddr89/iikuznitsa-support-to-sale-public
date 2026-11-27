import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const { chromium } = await import(pathToFileURL(resolve(
  process.env.PLAYWRIGHT_MODULE || ".stand/tools/node_modules/playwright/index.mjs"
)));
const base = process.env.AUDIT_URL || "http://127.0.0.1:18092";
const output = process.env.AUDIT_OUT || ".stand/audit/after";
const token = process.env.MANAGER_TOKEN;
assert.ok(["127.0.0.1", "localhost"].includes(new URL(base).hostname), "Use an isolated local server");
assert.ok(token, "Set the isolated manager token");
await mkdir(output, { recursive: true });

const browser = await chromium.launch({ executablePath: process.env.CHROME_BIN || "/opt/google/chrome/chrome" });
const pageErrors = [];
try {
  for (const width of [1280, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, acceptDownloads: true });
    const page = await context.newPage();
    page.on("pageerror", (error) => pageErrors.push(error.message));
    for (const [name, path] of [["home", "/"], ["client", "/client"], ["manager", "/manager"]]) {
      await page.goto(base + path);
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
      if (name === "client") assert.equal(await page.locator("#catalog-source").count(), 0);
      if (name === "home") {
        const overlap = await page.evaluate(() => {
          const text = document.querySelector(".product-name").getBoundingClientRect();
          const badge = document.querySelector(".prototype").getBoundingClientRect();
          return text.left < badge.right && text.right > badge.left && text.top < badge.bottom && text.bottom > badge.top;
        });
        assert.equal(overlap, false, `Header labels overlap at ${width}px`);
      }
      await page.screenshot({ path: resolve(output, `${width}-${name}.png`), fullPage: true });
    }

    if (width === 320) {
      await context.close();
      continue;
    }

    await page.goto(base + "/client");
    assert.equal(await page.locator("#search-products article").count(), 0);
    assert.equal(await page.locator("#handoff").isVisible(), false);
    await page.getByRole("tab", { name: "Чат" }).press("ArrowRight");
    await page.getByText("В этой сессии ещё нет заявок.").waitFor();
    await page.getByRole("tab", { name: "Мои заявки" }).press("ArrowLeft");
    await page.getByRole("button", { name: "Ноутбук для офиса" }).click();
    assert.equal(await page.locator("#question").inputValue(), "Подберите ноутбук для офиса");
    await page.route("**/api/v1/chat", (route) => route.fulfill({ status: 503 }));
    const question = "Уточните стоимость монитора DEMO-MON-01";
    await page.locator("#question").fill(question);
    await page.locator("#chat-form button").click();
    await page.locator("#chat-status.error").waitFor();
    assert.equal(await page.locator(".message.customer").count(), 0);
    assert.equal(await page.locator("#question").inputValue(), question);
    await page.unroute("**/api/v1/chat");
    await page.locator("#chat-form button").click();
    await page.locator("#conversation .message:not(.customer)").last().waitFor();
    assert.equal(await page.getByRole("list", { name: "Действия помощника" }).count(), 0);
    assert.equal(await page.locator(".message.customer").count(), 1);
    await page.getByText("Товары из последнего ответа помощника.").waitFor();
    await page.reload();
    await page.locator(".message.customer").getByText(question).waitFor();
    assert.equal(await page.locator(".message.customer").count(), 1);

    await page.route("**/api/v1/products?*", (route) => {
      const params = new URL(route.request().url()).searchParams;
      if (params.get("q") !== "audit paging") return route.continue();
      const offset = Number(params.get("offset"));
      const items = Array.from({ length: offset ? 1 : 7 }, (_, index) => ({
        sku: `PAGE-${offset + index + 1}`, name: `Тестовая карточка ${offset + index + 1}`,
        price_minor: 1000, currency: "RUB",
      }));
      return route.fulfill({ json: { items } });
    });
    await page.locator("#query").fill("audit paging");
    await page.locator("#search-form button").click();
    await page.getByText("Результаты 1–6", { exact: true }).waitFor();
    assert.equal(await page.locator("#search-products article").count(), 6);
    await page.getByRole("button", { name: "Далее", exact: true }).click();
    await page.getByText("Результат 7", { exact: true }).waitFor();
    assert.equal(await page.locator("#search-products article").count(), 1);
    await page.getByRole("button", { name: "Назад", exact: true }).click();
    await page.getByText("Результаты 1–6", { exact: true }).waitFor();

    await page.locator("#query").fill("DEMO-MON-01");
    await page.locator("#search-form button").click();
    await page.locator("#search-products article").filter({ hasText: "DEMO-MON-01" })
      .getByRole("button", { name: "В заявку" }).click();
    await page.getByRole("button", { name: "Позвать менеджера" }).click();
    assert.ok(await page.locator(".chat-panel #handoff").isVisible());
    assert.equal(await page.locator("#lead-message").inputValue(), question);
    await page.screenshot({ path: resolve(output, `${width}-client-handoff.png`), fullPage: true });
    const contact = `audit-${width}@example.invalid`;
    await page.locator("#contact").fill(contact);
    await page.locator("#lead-form button").click();
    await page.getByText(/ожидает ответа менеджера/).waitFor();

    await page.getByRole("tab", { name: "Мои заявки" }).click();
    const lead = page.locator("#my-lead-list details").filter({ hasText: question });
    await lead.locator("summary").click();
    await lead.getByText("ИИ-помощник", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Обновить", exact: true }).click();
    await lead.getByText("ИИ-помощник", { exact: true }).waitFor();
    assert.ok(await lead.evaluate((card) => card.open));
    await page.screenshot({ path: resolve(output, `${width}-my-leads.png`), fullPage: true });

    await page.getByRole("link", { name: "Менеджер" }).click();
    assert.equal((await page.request.get(base + "/api/v1/manager/leads")).status(), 401);
    await page.locator("#token").fill(token);
    await page.getByRole("button", { name: "Войти" }).click();
    const request = page.locator("#lead-list article").filter({ hasText: contact });
    const open = request.getByRole("button", { name: "Открыть заявку" });
    await open.click();
    await page.locator("#lead-history").getByText("ИИ-помощник", { exact: true }).waitFor();
    const waiting = request.getByText("Требует ответа", { exact: true });
    await waiting.waitFor();
    const before = Number((await page.locator("#waiting-count").textContent()).match(/\d+$/)[0]);
    await page.locator("#lead-state").selectOption("in_progress");
    await page.locator("#lead-update-form button").click();
    await request.getByText(/В работе/).waitFor();
    await waiting.waitFor();
    assert.equal(Number((await page.locator("#waiting-count").textContent()).match(/\d+$/)[0]), before);

    const customerChat = await context.newPage();
    customerChat.on("pageerror", (error) => pageErrors.push(error.message));
    await customerChat.goto(base + "/client");
    await customerChat.locator(".message.customer").getByText(question).waitFor();
    const reply = `Проверили запрос на ширине ${width} пикселей.`;
    await page.locator("#manager-reply").fill(reply);
    await page.locator("#lead-update-form button").click();
    await request.getByText(/Ответ отправлен/).waitFor();
    assert.equal(await waiting.count(), 0);
    assert.equal(Number((await page.locator("#waiting-count").textContent()).match(/\d+$/)[0]), before - 1);
    await customerChat.bringToFront();
    await customerChat.locator("#conversation").getByText(reply, { exact: true }).waitFor({ timeout: 15000 });
    await customerChat.close();
    await page.bringToFront();

    await page.getByRole("button", { name: "Добавить позицию" }).click();
    assert.equal(await page.locator("#quote-items input[name=sku]").count(), 2);
    await page.getByRole("button", { name: "Убрать" }).last().click();
    assert.equal(await page.locator("#quote-items input[name=sku]").count(), 1);
    await page.getByRole("button", { name: "Подготовить документы" }).click();
    await page.locator("#saved-documents").getByRole("button", { name: "Скачать документы", exact: true }).waitFor();
    await open.click();
    await page.locator("#saved-documents").waitFor();
    const downloaded = page.waitForEvent("download");
    await page.locator("#saved-documents").getByRole("button", { name: "Скачать документы", exact: true }).click();
    assert.match((await downloaded).suggestedFilename(), /^documents-.*\.zip$/);
    await page.screenshot({ path: resolve(output, `${width}-manager-workspace.png`), fullPage: true });
    await page.locator("#manager-reply").fill("Закрытая черновая заметка");
    await page.getByRole("button", { name: "Выйти" }).click();
    assert.equal(await page.locator("#lead-history").textContent(), "");
    assert.equal(await page.locator("#manager-reply").inputValue(), "");
    assert.equal(await page.locator("#saved-document-list").textContent(), "");
    await context.close();
  }
  assert.deepEqual(pageErrors, []);
  console.log("Chrome: 1280/390/320 px, клавиатурная навигация, повтор после 503, восстановление диалога, очередь, ответ без перезагрузки, ZIP и очистка данных после выхода — OK");
} finally {
  await browser.close();
}
