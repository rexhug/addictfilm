import { expect, test } from "@playwright/test";

const PRIMARY = "https://st.kp.yandex.net/primary.jpg";
const SECOND = "https://commons.wikimedia.org/second.jpg";
const THIRD = "https://commons.wikimedia.org/third.jpg";

function movie(overrides = {}) {
  return {
    id: 1, title: "Разрушение", year: "2015", genres: "драма",
    actors: "Legacy Lead, Legacy Second",
    actors_photos: JSON.stringify([
      { name: "Legacy Lead", photo_url: "https://st.kp.yandex.net/legacy.jpg" },
    ]),
    cast: [
      {
        person_id: "2", name: "Наоми Уоттс", character: "Karen",
        billing_order: 1, photo_url: SECOND, fallback_photo_urls: [],
      },
      {
        person_id: "1", name: "Джейк Джилленхол", character: "Davis",
        billing_order: 0, photo_url: PRIMARY, fallback_photo_urls: [SECOND, THIRD],
      },
      {
        person_id: "1", name: "Дубликат", billing_order: 2,
        photo_url: THIRD, fallback_photo_urls: [],
      },
    ],
    community: { average: null, count: 0 }, partner: null,
    ...overrides,
  };
}

async function openDetail(
  page,
  { castV2 = true, detail = movie(), broken = [], neutral = [] } = {},
) {
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.Telegram = { WebApp: {
      initData: "test", initDataUnsafe: {
        user: { id: 1, first_name: "Denys" }, start_param: "film_1",
      },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {},
      disableVerticalSwipes() {}, onEvent() {},
      HapticFeedback: { impactOccurred() {} },
    } };
  });
  await page.route("**/img?**", async route => {
    const source = new URL(route.request().url()).searchParams.get("u") || "";
    if (broken.some(value => source.includes(value))) {
      await route.abort("failed");
      return;
    }
    const fill = neutral.some(value => source.includes(value)) ? "#777" : "#345";
    await route.fulfill({
      contentType: "image/svg+xml",
      body: `<svg xmlns="http://www.w3.org/2000/svg" width="330" height="440"><rect width="330" height="440" fill="${fill}"/></svg>`,
    });
  });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body = {};
    if (path === "/api/me") {
      body = { id: 1, label: "Denys", language: "ru", features: { cast_v2: castV2 } };
    } else if (path === "/api/me/capabilities") {
      body = { is_admin: false, capabilities: [] };
    } else if (path === "/api/movie/1") {
      body = detail;
    } else if (path === "/api/movie/1/reviews") {
      body = { items: [], partner_review: null, total: 0, next_before_id: null };
    } else if (path === "/api/notifications/unread-count") {
      body = { count: 0 };
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/");
  await expect(page.locator(".d-cast-item").first()).toBeVisible();
}

test("canonical cast keeps billing order, identity, role and no generic retry", async ({ page }) => {
  await openDetail(page);
  const cards = page.locator(".d-cast-item");
  await expect(cards).toHaveCount(2);
  await expect(cards.nth(0).locator(".n")).toHaveText("Джейк Джилленхол");
  await expect(cards.nth(0).locator(".role")).toHaveText("Davis");
  await expect(cards.nth(0)).toHaveAttribute("data-person-id", "1");
  const image = cards.nth(0).locator("img");
  await expect(image).toHaveAttribute("data-person-photo-src", PRIMARY);
  await expect(image).not.toHaveAttribute("data-img-retry", "");
  await expect(image).toHaveAttribute(
    "data-person-photo-fallbacks", JSON.stringify([SECOND, THIRD]),
  );
});

test("disabled feature flag retains legacy cast", async ({ page }) => {
  await openDetail(page, { castV2: false });
  await expect(page.locator(".d-cast-item")).toHaveCount(1);
  await expect(page.locator(".d-cast-item .n")).toHaveText("Legacy Lead");
});

test("a missing-photo lead stays ahead of a photographed supporting actor", async ({ page }) => {
  await openDetail(page, { detail: movie({ cast: [
    {
      person_id: "1", name: "Lead", billing_order: 0,
      photo_url: null, fallback_photo_urls: [],
    },
    {
      person_id: "2", name: "Supporting", billing_order: 1,
      photo_url: SECOND, fallback_photo_urls: [],
    },
  ] }) });
  const names = page.locator(".d-cast-item .n");
  await expect(names.nth(0)).toHaveText("Lead");
  await expect(names.nth(1)).toHaveText("Supporting");
});

test("first failed portrait loads the second saved candidate", async ({ page }) => {
  await openDetail(page, { broken: ["primary.jpg"] });
  const image = page.locator(".d-cast-item").first().locator("img");
  await image.scrollIntoViewIfNeeded();
  await expect(image).toHaveAttribute("src", /second\.jpg/, { timeout: 5_000 });
});

test("second failed portrait loads the third saved candidate", async ({ page }) => {
  await openDetail(page, { broken: ["primary.jpg", "second.jpg"] });
  const image = page.locator(".d-cast-item").first().locator("img");
  await image.scrollIntoViewIfNeeded();
  await expect(image).toHaveAttribute("src", /third\.jpg/, { timeout: 5_000 });
});

test("all failed candidates leave initials, not a blank image", async ({ page }) => {
  await openDetail(page, { broken: ["primary.jpg", "second.jpg", "third.jpg"] });
  const card = page.locator(".d-cast-item").first();
  await card.scrollIntoViewIfNeeded();
  await expect(card.locator("img")).toHaveCount(0);
  await expect(card.locator(".fb")).toBeVisible();
});

test("a neutral Kinopoisk placeholder advances to fallback", async ({ page }) => {
  await openDetail(page, { neutral: ["primary.jpg"] });
  const image = page.locator(".d-cast-item").first().locator("img");
  await image.scrollIntoViewIfNeeded();
  await expect(image).toHaveAttribute("src", /second\.jpg/, { timeout: 5_000 });
});

test("every rendered actor keeps a visible name", async ({ page }) => {
  await openDetail(page);
  const cards = page.locator(".d-cast-item");
  for (let index = 0; index < await cards.count(); index += 1) {
    await expect(cards.nth(index).locator(".n")).not.toBeEmpty();
  }
});

test("malformed canonical cast falls back without crashing detail", async ({ page }) => {
  await openDetail(page, { detail: movie({ cast: { invalid: true } }) });
  await expect(page.locator(".d-cast-item .n")).toHaveText("Legacy Lead");
  await expect(page.locator(".detail-v2")).toBeVisible();
});

test("cast cards preserve the existing visual dimensions", async ({ page }) => {
  await openDetail(page);
  const dimensions = await page.locator(".d-cast-item").first().evaluate(card => {
    const avatar = card.querySelector(".d-avatar").getBoundingClientRect();
    return { cardWidth: card.getBoundingClientRect().width, avatarWidth: avatar.width };
  });
  expect(dimensions.cardWidth).toBe(84);
  expect(dimensions.avatarWidth).toBe(84);
});

for (const viewport of [
  { width: 320, height: 568 },
  { width: 390, height: 844 },
  { width: 430, height: 932 },
]) {
  test(`cast has no page overflow at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const many = Array.from({ length: 14 }, (_, index) => ({
      person_id: String(index + 1),
      name: `Actor With A Long Name ${index + 1}`,
      billing_order: index,
      photo_url: index % 2 ? SECOND : null,
      fallback_photo_urls: [],
    }));
    await openDetail(page, { detail: movie({ cast: many }) });
    await expect(page.locator(".d-cast-item")).toHaveCount(10);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    );
    expect(overflow).toBe(false);
  });
}
