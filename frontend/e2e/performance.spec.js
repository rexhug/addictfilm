import { expect, test } from "@playwright/test";

async function mockTelegram(page, startParam = "") {
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(param => {
    window.Telegram = { WebApp: {
      initData: "test",
      initDataUnsafe: { start_param: param, user: { id: 1, first_name: "Test" } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {}, disableVerticalSwipes() {}, onEvent() {},
      HapticFeedback: { impactOccurred() {} },
    } };
  }, startParam);
}

function json(body) {
  return { contentType: "application/json", body: JSON.stringify(body) };
}

async function openHome(page, { meDelay = 0, requests = [] } = {}) {
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    requests.push({ path: url.pathname + url.search, at: Date.now() });
    if (url.pathname === "/api/me") {
      if (meDelay) await new Promise(resolve => setTimeout(resolve, meDelay));
      requests.push({ path: "/api/me:done", at: Date.now() });
      return route.fulfill(json({ id: 1, label: "Test", features: {} }));
    }
    if (url.pathname === "/api/browse") {
      const popular = url.searchParams.get("sort") === "popular";
      return route.fulfill(json({ items: popular ? [
        { id: 1, title: "One", year: 2020, poster_url: "https://images-na.ssl-images-amazon.com/one.jpg" },
        { id: 2, title: "Two", year: 2021, poster_url: "https://images-na.ssl-images-amazon.com/two.jpg" },
        { id: 3, title: "Three", year: 2022, poster_url: "https://images-na.ssl-images-amazon.com/three.jpg" },
        { id: 4, title: "Four", year: 2023, poster_url: "https://images-na.ssl-images-amazon.com/four.jpg" },
      ] : [] }));
    }
    if (url.pathname === "/api/notifications") return route.fulfill(json({ unread_count: 0 }));
    if (url.pathname === "/api/me/capabilities") return route.fulfill(json({ is_admin: false, capabilities: [] }));
    if (url.pathname === "/api/genres") return route.fulfill(json({ items: [] }));
    if (url.pathname === "/api/collections") return route.fulfill(json({ items: [] }));
    return route.fulfill(json({}));
  });
  await page.route("**/img**", route => route.fulfill({ status: 200, contentType: "image/jpeg", body: Buffer.from([]) }));
  await page.goto("/", { waitUntil: "networkidle" });
  await expect(page.locator("#rail-pop .poster").first()).toBeVisible();
}

test("home requests start in parallel with authentication and first posters are prioritized", async ({ page }) => {
  const requests = [];
  await mockTelegram(page);
  await openHome(page, { meDelay: 250, requests });
  const meDoneAt = requests.find(request => request.path === "/api/me:done").at;
  expect(requests.filter(request => request.path.startsWith("/api/browse") || request.path === "/api/genres" || request.path === "/api/collections").every(request => request.at <= meDoneAt)).toBeTruthy();
  expect(requests.filter(request => request.path.startsWith("/api/browse?sort=popular")).length).toBe(1);
  const images = page.locator("#rail-pop img");
  await expect(images.nth(0)).toHaveAttribute("loading", "eager");
  await expect(images.nth(0)).toHaveAttribute("fetchpriority", "high");
  await expect(images.nth(1)).toHaveAttribute("loading", "eager");
  await expect(images.nth(2)).toHaveAttribute("loading", "eager");
  await expect(images.nth(3)).toHaveAttribute("loading", "lazy");
});

test("deep links do not prefetch unrelated home rails", async ({ page }) => {
  const requests = [];
  await mockTelegram(page, "film_42");
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    requests.push(url.pathname);
    if (url.pathname === "/api/me") return route.fulfill(json({ id: 1, label: "Test", features: {} }));
    if (url.pathname === "/api/me/capabilities") return route.fulfill(json({}));
    return route.fulfill(json({}));
  });
  await page.goto("/", { waitUntil: "networkidle" });
  expect(requests.filter(path => path === "/api/browse" || path === "/api/genres" || path === "/api/collections")).toEqual([]);
  expect(requests).toContain("/api/me/capabilities");
});

test("static shell is present before deferred app scripts execute", async ({ page }) => {
  await mockTelegram(page);
  await page.route("**/app.js*", async route => {
    await new Promise(resolve => setTimeout(resolve, 400));
    await route.continue();
  });
  await page.goto("/", { waitUntil: "commit" });
  await expect(page.locator(".startup-shell")).toBeVisible();
});

test("cacheable reads are deduplicated while mutations invalidate completed cache", async ({ page }) => {
  await mockTelegram(page);
  await page.goto("/", { waitUntil: "networkidle" });
  const result = await page.evaluate(async () => {
    const cache = new Map();
    let calls = 0;
    let release;
    window.fetch = async (_path, opts = {}) => {
      calls += 1;
      if ((opts.method || "GET").toUpperCase() !== "GET") {
        return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
      }
      await new Promise(resolve => { release = resolve; });
      return new Response(JSON.stringify({ calls }), { status: 200, headers: { "Content-Type": "application/json" } });
    };
    const client = window.AddictFilmApi.create({
      getInitData: () => "",
      cacheableRead: path => path.startsWith("/api/browse"),
      cache,
      cacheTtl: 60_000,
    });
    const first = client("/api/browse?sort=popular");
    const second = client("/api/browse?sort=popular");
    const beforeRelease = calls;
    release();
    await Promise.all([first, second]);
    const cached = await client("/api/browse?sort=popular");
    await client("/api/film/1", { method: "POST", body: "{}" });
    return { beforeRelease, callsAfterRead: calls, cached, cacheSize: cache.size };
  });
  expect(result.beforeRelease).toBe(1);
  expect(result.callsAfterRead).toBe(2);
  expect(result.cacheSize).toBe(0);
});
