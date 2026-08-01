import { expect, test } from "@playwright/test";

// Локализация — не «есть перевод / нет перевода». Ломается она иначе: словари
// разъезжаются по ключам, часть строк живёт мимо словаря, а язык документа
// остаётся русским для англоязычного пользователя. Это здесь и проверяется.

async function openApp(page, { language = "ru" } = {}) {
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(([lang]) => {
    window.localStorage.setItem("lang", lang);
    window.Telegram = { WebApp: {
      initData: "test",
      initDataUnsafe: { user: { id: 1, first_name: "Denys", language_code: lang } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {},
      disableVerticalSwipes() {}, onEvent() {},
      HapticFeedback: { impactOccurred() {} },
    } };
  }, [language]);
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body = {};
    if (path === "/api/me") body = { id: 1, label: "Denys", language, features: {} };
    else if (path === "/api/me/capabilities") body = { is_admin: false, capabilities: [] };
    else if (path === "/api/notifications/unread-count") body = { count: 0 };
    else if (path === "/api/home") body = { rails: [] };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/");
  await expect(page.locator("#tabbar .tab").first()).toBeVisible();
}

function dict(page) {
  return page.evaluate(() => {
    const flatten = table => Object.fromEntries(Object.entries(table).map(([key, value]) => [
      key, typeof value === "function" ? "fn" : value,
    ]));
    return { ru: flatten(DICT.ru), en: flatten(DICT.en) };
  });
}

test("both dictionaries carry exactly the same keys", async ({ page }) => {
  await openApp(page);
  const { ru, en } = await dict(page);
  const ruKeys = Object.keys(ru).sort();
  const enKeys = Object.keys(en).sort();
  // Расхождение ключей тихо роняет интерфейс в другой язык: t() молча берёт
  // русский фолбэк, и англоязычный пользователь получает русскую строку.
  expect(enKeys.filter(key => !ruKeys.includes(key))).toEqual([]);
  expect(ruKeys.filter(key => !enKeys.includes(key))).toEqual([]);
  expect(ruKeys.length).toBeGreaterThan(200);
});

test("the same key never returns identical Russian text in the English dictionary", async ({ page }) => {
  await openApp(page);
  const { ru, en } = await dict(page);
  const cyrillic = /[А-Яа-яЁё]/;
  const untranslated = Object.keys(en).filter(key =>
    typeof en[key] === "string" && cyrillic.test(en[key]) &&
    // Названия языков в переключателе намеренно одинаковы в обоих словарях.
    !["settings_language_ru", "settings_language_en"].includes(key));
  expect(untranslated).toEqual([]);
});

test("Russian copy avoids gender parentheses and formal address to one user", async ({ page }) => {
  await openApp(page);
  const { ru } = await dict(page);
  const values = Object.entries(ru).filter(([, value]) => typeof value === "string");
  expect(values.filter(([, value]) => /\(а\)|\(ся\)/.test(value)).map(([key]) => key)).toEqual([]);
  // «Комьюнити» и «пара» как видимое слово заменены на «Сообщество»/«партнёр».
  expect(values.filter(([, value]) => /Комьюнити/i.test(value)).map(([key]) => key)).toEqual([]);
  expect(values.filter(([, value]) => /Разорвать пару|Пара завершена/i.test(value)).map(([key]) => key)).toEqual([]);
});

test("English copy uses movie and partner consistently", async ({ page }) => {
  await openApp(page);
  const { en } = await dict(page);
  const values = Object.entries(en).filter(([, value]) => typeof value === "string");
  // «film» осталось только в брендовом Addict Film.
  expect(values
    .filter(([, value]) => /\bfilms?\b/i.test(value) && !/Addict Film/.test(value))
    .map(([key]) => key)).toEqual([]);
  expect(values
    .filter(([, value]) => /\bpair(ed|ing)?\b/i.test(value))
    .map(([key]) => key)).toEqual([]);
  expect(values.filter(([, value]) =>
    /Community Top|Available option|Best shared|Catalog is empty yet|Create a pair|Pair ended|Smart random film/i.test(value),
  ).map(([key]) => key)).toEqual([]);
});

test("switching language repaints navigation and the document language", async ({ page }) => {
  await openApp(page, { language: "ru" });
  await expect(page.locator("html")).toHaveAttribute("lang", "ru");
  await expect(page.locator("#tabbar .tab").first().locator("span")).toHaveText("Главная");

  await page.evaluate(() => setLang("en"));
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.locator("#tabbar .tab").first().locator("span")).toHaveText("Home");

  await page.evaluate(() => setLang("ru"));
  await expect(page.locator("html")).toHaveAttribute("lang", "ru");
  await expect(page.locator("#tabbar .tab").first().locator("span")).toHaveText("Главная");
});

test("an English user never sees the Russian navigation while the app starts", async ({ page }) => {
  // Ярлыки вкладок стоят в разметке по-русски как статический фолбэк. Раньше
  // они держались до конца старта: словарь применялся только после /api/me.
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.localStorage.setItem("lang", "en");
    window.Telegram = { WebApp: {
      initData: "test", initDataUnsafe: { user: { id: 1, first_name: "D", language_code: "en" } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {},
      disableVerticalSwipes() {}, onEvent() {}, HapticFeedback: { impactOccurred() {} },
    } };
  });
  // Ответ сервера не приходит вовсе — интерфейс всё равно обязан быть английским.
  await page.route("**/api/**", route => route.abort("failed"));
  await page.goto("/");
  await expect(page.locator("#tabbar .tab").first().locator("span")).toHaveText("Home");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
});

test("the outside-Telegram screen is localized", async ({ page }) => {
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.localStorage.setItem("lang", "en");
    window.Telegram = undefined;
  });
  await page.goto("/");
  await expect(page.locator("#screen")).toContainText("Open Addict Film in Telegram");
  await expect(page.locator("#screen")).not.toContainText("Откройте в Telegram");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
});

test("no known hardcoded copy is left outside the dictionary", async ({ page }) => {
  await openApp(page);
  const source = await page.evaluate(async () => (await fetch("/app.js")).text());
  // Сам словарь по определению полон видимого текста — проверяем ТОЛЬКО код
  // после него: раньше именно там жили строки, обходившие локализацию.
  const renderer = source.slice(source.indexOf("function setLang"));
  for (const literal of [
    'aria-label="Back"', 'aria-label="Share"',
    "Откройте в Telegram", "Больше всего ставишь", "Больше всего ставим",
    "Комьюнити", '"Пользователь"', '"из 10"', '"out of 10"',
  ]) {
    expect(renderer, `hardcoded copy: ${literal}`).not.toContain(literal);
  }
});

test("movie detail labels its controls in the current language", async ({ page }) => {
  const detail = {
    id: 1, title: "Разрушение", year: "2015", genres: "драма", actors: "",
    community: { avg: "8.4", count: 3 }, partner: null,
  };
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.localStorage.setItem("lang", "en");
    window.Telegram = { WebApp: {
      initData: "test",
      initDataUnsafe: { user: { id: 1, first_name: "D", language_code: "en" }, start_param: "film_1" },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {},
      disableVerticalSwipes() {}, onEvent() {}, HapticFeedback: { impactOccurred() {} },
    } };
  });
  await page.route("**/img?**", route => route.fulfill({
    contentType: "image/svg+xml",
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>',
  }));
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body = {};
    if (path === "/api/me") body = { id: 1, label: "D", language: "en", features: {} };
    else if (path === "/api/me/capabilities") body = { is_admin: false, capabilities: [] };
    else if (path === "/api/movie/1") body = detail;
    else if (path === "/api/movie/1/reviews") body = { items: [], partner_review: null, total: 0, next_before_id: null };
    else if (path === "/api/notifications/unread-count") body = { count: 0 };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/");
  await expect(page.locator("#d-back-sticky")).toHaveAttribute("aria-label", "Back");
  await expect(page.locator("#d-more-sticky")).toHaveAttribute("aria-label", "Share");
  await expect(page.locator(".d-rpill.accent .l")).toHaveText("Community");
  await expect(page.locator(".d-rpill.accent .c")).toHaveText("3 ratings");
  await expect(page.locator('.d-rating-option[data-n="7"]')).toHaveAttribute("aria-label", "7 out of 10");
});
