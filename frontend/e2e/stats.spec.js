import { expect, test } from "@playwright/test";

const personalStats = {
  watched: 46, want: 31, avg_rating: 7.2, total_runtime_min: 4800,
  rating_dist: [0, 1, 2, 6, 3, 8, 11, 9, 4, 8],
  top_genres_pct: [["Drama", 20, 9], ["Thriller", 14, 6], ["Mystery", 10, 5], ["Action", 8, 4]],
  top_actors: [["Jake Gyllenhaal", 4, null], ["Will Poulter", 3, null], ["Ed Helms", 2, null], ["Woody Harrelson", 2, null]],
  top_directors: [["David Fincher", 4, null], ["Todd Phillips", 3, null], ["Christopher Nolan", 2, null], ["Denis Villeneuve", 2, null]],
  year: { year: 2026, count: 46, avg_rating: 7.2, top_genre: "Drama", top_actor: null, best_films: [] },
};

const pairStats = {
  ...personalStats,
  partner: { name: "Kristina", username: "kristina", avatar_url: null },
  agreement: 91, rated_together: 24, matches: 16,
  common_favorites: [
    { film_id: 1, title: "The Last of Us", avg: 10, poster_url: null },
    { film_id: 3, title: "Inception", avg: 9, poster_url: null },
    { film_id: 4, title: "The Hunger Games", avg: 8.5, poster_url: null },
  ],
  disagreements: [
    { film_id: 2, title: "Parasites", a: 5, b: 8, diff: 3, poster_url: null },
    { film_id: 5, title: "Apex", a: 4, b: 7, diff: 3, poster_url: null },
    { film_id: 6, title: "Ritual", a: 6, b: 8, diff: 2, poster_url: null },
  ],
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
      : path === "/api/movie/1"
        ? { id: 1, title: "The Last of Us", title_original: "The Last of Us", year: "2023", poster_url: null, genres: "Drama" }
      : path === "/api/partner"
        ? (paired ? { status: "paired", partner: { name: "Kristina", username: "kristina" } } : { status: "none" })
        : path === "/api/partner/stats" ? pairStats
          : path === "/api/stats/person" ? { items: [{ id: 17, title: "Donnie Darko", year: "2001", poster_url: null, my_rating: 9 }] }
            : personalStats;
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
  await expect(page.getByText("Посмотреть всех")).toHaveCount(0);
  await expect(page.locator(".people-stats-actors .person-stat-card")).toHaveCount(4);
  await expect(page.locator(".people-stats-directors .person-stat-card")).toHaveCount(4);
  await expect(page.locator(".people-stats-actors .person-stat-card.is-favorite")).toHaveCount(1);
  await expect(page.locator(".people-stats-directors .person-stat-card.is-favorite")).toHaveCount(1);
  await expect(page.locator(".people-stats-actors .person-stat-favorite")).toHaveCount(1);
  await expect(page.locator(".people-stats-directors .person-stat-favorite")).toHaveCount(1);
  await expect(page.getByText("Чаще всего встречаются в просмотренных фильмах")).toBeVisible();
  await expect(page.getByText("Чаще всего среди просмотренных фильмов")).toBeVisible();
  expect(await page.locator(".people-stats-actors .people-stats-rail").evaluate((rail) => rail.scrollWidth > rail.clientWidth)).toBeTruthy();
  const favoriteActorLayout = await page.locator(".people-stats-actors .person-stat-card.is-favorite").evaluate((card) => {
    const cardBox = card.getBoundingClientRect();
    const photoBox = card.querySelector(".person-stat-avatar").getBoundingClientRect();
    return { cardWidth: cardBox.width, photoWidth: photoBox.width, photoHeight: photoBox.height };
  });
  expect(favoriteActorLayout.photoWidth).toBeGreaterThanOrEqual(favoriteActorLayout.cardWidth - 2);
  expect(favoriteActorLayout.photoHeight).toBeGreaterThan(favoriteActorLayout.cardWidth * .7);
  await page.locator(".people-stats-actors .person-stat-card").first().click();
  await expect(page.getByRole("heading", { name: "Фильмы с Jake Gyllenhaal" })).toBeVisible();
  await expect(page.locator(".poster .meta .t", { hasText: "Donnie Darko" })).toBeVisible();
  await page.locator(".sub-head .back").click();
  await expect(page.getByRole("heading", { name: "Мой кинопрофиль" })).toBeVisible();
  await page.getByRole("button", { name: "Показать ещё" }).click();
  await expect(page.getByText("Action")).toBeVisible();
  await expect(page.getByRole("button", { name: "Свернуть" })).toBeVisible();
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
  await expect(page.getByText("Итоги 2026")).toHaveCount(0);
  await expect(page.getByText("Показать все")).toHaveCount(0);
  await expect(page.locator(".pair-favorite-card")).toHaveCount(3);
  await expect(page.locator(".pair-difference-card")).toHaveCount(3);
  await expect(page.locator('[data-film-id="1"]')).toContainText("The Last of Us");
  expect(await page.locator(".pair-favorites-rail").evaluate(el => el.scrollWidth > el.clientWidth)).toBeTruthy();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.locator('[data-film-id="1"]').click();
  await expect(page.getByRole("heading", { name: "The Last of Us" })).toBeVisible();
});
