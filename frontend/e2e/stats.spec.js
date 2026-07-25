import { expect, test } from "@playwright/test";

const personalStats = {
  watched: 46, want: 31, avg_rating: 7.2, total_runtime_min: 4800,
  rating_dist: [0, 1, 2, 6, 3, 8, 11, 9, 4, 8],
  top_genres_pct: [["Drama", 20, 9], ["Thriller", 14, 6], ["Mystery", 10, 5], ["Action", 8, 4]],
  top_actors: [["Jake Gyllenhaal", 4, null], ["Will Poulter", 3, null], ["Ed Helms", 2, null]],
  top_directors: [["David Fincher", 4, null], ["Todd Phillips", 3, null], ["Christopher Nolan", 2, null]],
  year: { year: 2026, count: 46, avg_rating: 7.2, top_genre: "Drama", top_actor: null, best_films: [] },
};

const pairStats = {
  ...personalStats,
  partner: { name: "Kristina", username: "kristina", avatar_url: null },
  agreement: 91, rated_together: 24, matches: 16,
  common_favorites: [{ film_id: 1, title: "The Last of Us", avg: 10, poster_url: null }],
  disagreements: [{ film_id: 2, title: "Parasites", a: 5, b: 8, diff: 3, poster_url: null }],
};

async function openStats(page, paired = false) {
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.__verticalSwipeDisableCalls = 0;
    window.Telegram = { WebApp: {
      initData: "test-init-data",
      initDataUnsafe: { user: { id: 1, first_name: "Denys", username: "denys" } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {},
      disableVerticalSwipes() { window.__verticalSwipeDisableCalls += 1; },
      HapticFeedback: { impactOccurred() {}, notificationOccurred() {} },
      showConfirm(_text, callback) { callback(true); }, showAlert() {}, openTelegramLink() {},
    } };
  });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = path === "/api/me"
      ? { id: 1, label: "Denys", username: "denys", role: null }
      : path === "/api/partner"
        ? (paired ? { status: "paired", partner: { name: "Kristina", username: "kristina" } } : { status: "none" })
        : path === "/api/partner/stats" ? pairStats : personalStats;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
  });
  await page.goto("/");
  expect(await page.evaluate(() => window.__verticalSwipeDisableCalls)).toBe(1);
  await page.getByRole("button", { name: "Статистика" }).click();
  await expect(page.getByRole("heading", { name: "Мой кинопрофиль" })).toBeVisible();
}

test("personal profile fits a 390px phone without a hidden final card", async ({ page }) => {
  await openStats(page);
  await expect(page.getByText("Как ты оцениваешь фильмы")).toBeVisible();
  await expect(page.getByText("Мы вместе")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();

  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const bounds = await page.evaluate(() => {
    const last = document.querySelector(".year-card")?.getBoundingClientRect();
    const bar = document.querySelector("#tabbar")?.getBoundingClientRect();
    return { lastBottom: last?.bottom, barTop: bar?.top };
  });
  expect(bounds.lastBottom).toBeLessThanOrEqual(bounds.barTop);
});

test("pair profile is reachable and key movie cards remain interactive", async ({ page }) => {
  await openStats(page, true);
  await page.getByRole("tab", { name: "Мы вместе" }).click();
  await expect(page.getByRole("heading", { name: "Мы вместе" })).toBeVisible();
  await expect(page.getByText("Совместимость вкусов")).toBeVisible();
  await expect(page.locator('[data-film-id="1"]')).toContainText("The Last of Us");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});
