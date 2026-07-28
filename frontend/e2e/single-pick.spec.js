import { expect, test } from "@playwright/test";

// Полноэкранный экран одного фильма: рулетка «Хочу» и умный случайный.
// Проверяется продуктовое обещание, а не разметка: широкий кадр заполняет блок,
// вертикальный постер НИКОГДА не растягивается по ширине, а битая картинка не
// приводит ни к новому запросу фильма, ни ко второй записи показа.

const BACKDROP = "https://assets.fanart.tv/fanart/movies/1/wide.jpg";
const POSTER = "https://image.tmdb.org/poster.jpg";

const baseMovie = {
  id: 1, title: "The Last of Us", title_original: "The Last of Us", year: "2023",
  runtime: "81 min", genres: "Drama, Thriller", rating: 8.5,
  poster_url: POSTER, backdrop_url: "https://image.tmdb.org/untrusted-wide.jpg",
  reasons: ["COZY_TONE", "HIGH_QUALITY"], role: "random", score: 72,
};

const backdropHero = {
  hero_url: BACKDROP, hero_type: "backdrop", hero_source: "fanart", hero_quality_score: 0.91,
};
const posterHero = {
  hero_url: POSTER, hero_type: "poster_blur", hero_source: "poster", hero_quality_score: 0.5,
};

async function openPicker(page, options = {}) {
  const state = {
    feedback: [],
    wishlistCalls: 0,
    randomCalls: 0,
  };
  const fullscreen = options.fullscreen !== false;
  const wishlistItems = options.wishlistItems
    || [{ ...baseMovie, id: 11, title: "Wishlist Pick", reasons: ["IN_WISHLIST"], ...backdropHero }];
  const randomItems = options.randomItems
    || [{ ...baseMovie, strategy: "discovery", reasons: ["UNSEEN_PICK", "RANDOM_DISCOVERY"], ...posterHero }];

  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.Telegram = { WebApp: {
      initData: "test-init-data",
      initDataUnsafe: { user: { id: 1, first_name: "Denys", username: "denys" } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {},
      disableVerticalSwipes() {},
      HapticFeedback: { impactOccurred() {}, notificationOccurred() {} },
      showConfirm(_text, callback) { callback(true); }, showAlert() {}, openTelegramLink() {},
    } };
  });

  await page.route("**/img?**", async route => {
    const source = new URL(route.request().url()).searchParams.get("u") || "";
    if (options.brokenImages?.some(part => source.includes(part))) {
      await route.abort("failed");
      return;
    }
    const [width, height] = source.includes("wide") ? [1920, 1080] : [600, 900];
    await route.fulfill({
      contentType: "image/svg+xml",
      body: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><rect width="100%" height="100%" fill="#365f9d"/></svg>`,
    });
  });

  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/wishlist/random") {
      const item = wishlistItems[Math.min(state.wishlistCalls, wishlistItems.length - 1)];
      state.wishlistCalls += 1;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        item, cycle: { cycle: 1, wishlist_size: 3, shown_in_cycle: 1, remaining_in_cycle: 2 } }) });
      return;
    }
    if (path === "/api/recommendations/random") {
      const item = randomItems[Math.min(state.randomCalls, randomItems.length - 1)];
      state.randomCalls += 1;
      await route.fulfill({ contentType: "application/json",
        body: JSON.stringify({ item, context: "solo" }) });
      return;
    }
    if (/^\/api\/recommendations\/\d+\/feedback$/.test(path)) {
      state.feedback.push(JSON.parse(route.request().postData() || "{}"));
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true }) });
      return;
    }
    const json = path === "/api/me"
      ? { id: 1, label: "Denys", username: "denys", role: null,
        features: { fullscreen_single_pick: fullscreen } }
      : path.startsWith("/api/movie/")
        ? (route.request().method() === "POST"
          ? { ok: true }
          : { id: 1, title: "The Last of Us", year: "2023", poster_url: POSTER, genres: "Drama" })
        : path === "/api/partner" ? { status: "none" }
          : path === "/api/settings" ? { language: "ru", telegram_enabled: false, telegram_available: false }
            : path === "/api/notifications" ? { items: [], unread_count: 0, next_before_id: null }
              : { items: [] };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Подбор", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Что посмотреть?" })).toBeVisible();
  return state;
}

const openWishlist = page => page.getByRole("button", { name: /Случайный из «Хочу»/ }).click();
const openSmartRandom = page => page.getByRole("button", { name: /Умный случайный фильм/ }).click();

test("wishlist roulette shows a qualified backdrop across the full hero", async ({ page }) => {
  await openPicker(page);
  await openWishlist(page);

  const backdrop = page.locator(".single-pick-backdrop");
  await expect(backdrop).toBeVisible();
  await expect(page.getByRole("heading", { name: "Wishlist Pick" })).toBeVisible();
  await expect(page.locator(".single-pick-label")).toHaveText("Случайный из «Хочу»");

  const hero = page.locator(".single-pick-media-backdrop");
  const heroBox = await hero.boundingBox();
  const imageBox = await backdrop.boundingBox();
  expect(Math.abs(imageBox.width - heroBox.width)).toBeLessThan(2);
  expect(Math.abs(imageBox.height - heroBox.height)).toBeLessThan(2);
  expect(await backdrop.evaluate(node => getComputedStyle(node).objectFit)).toBe("cover");
  // Метаданные фильма читаются поверх затемнения, а не поверх голого кадра.
  await expect(page.locator(".single-pick-media-shade")).toBeAttached();
  for (const name of ["Открыть фильм", "Уже смотрел", "Другой вариант", "Не предлагать"]) {
    await expect(page.getByRole("button", { name })).toBeVisible();
  }
});

test("smart random keeps its strategy label on the fullscreen renderer", async ({ page }) => {
  await openPicker(page);
  await openSmartRandom(page);

  await expect(page.locator(".single-pick-screen")).toBeVisible();
  await expect(page.locator(".single-pick-label")).toHaveText("Находка");
  await expect(page.locator(".picker-result")).toHaveCount(0);
});

test("a poster fallback is centered and never stretched horizontally", async ({ page }) => {
  await openPicker(page);
  await openSmartRandom(page);

  const poster = page.locator(".single-pick-poster");
  const blur = page.locator(".single-pick-poster-blur");
  await expect(poster).toBeVisible();
  // Размытый фон декоративен: он не должен попадать в дерево доступности.
  await expect(blur).toHaveAttribute("aria-hidden", "true");
  await expect(blur).toHaveAttribute("alt", "");
  await expect(poster).toHaveAttribute("alt", /Постер фильма/);

  const posterBox = await poster.boundingBox();
  const heroBox = await page.locator(".single-pick-media-poster").boundingBox();
  expect(posterBox.width).toBeLessThan(heroBox.width * 0.8);
  // Вертикальные пропорции сохранены — ровно то, что ломалось раньше.
  expect(posterBox.height / posterBox.width).toBeGreaterThan(1.35);
  // Резкая и размытая копия — один URL: вторая отрисовка идёт из кэша браузера,
  // а не второй загрузкой по сети.
  expect(await poster.getAttribute("src")).toBe(await blur.getAttribute("src"));
});

test("images are requested at display size, not at proof size", async ({ page }) => {
  // Сохранённые 1920x1080 доказывают пригодность кадра, но блок занимает
  // ~1050 физических пикселей: показывать 1920 значит удвоить трафик впустую.
  await openPicker(page, {
    wishlistItems: [{ ...baseMovie, id: 11, title: "Wishlist Pick",
      hero_url: "https://avatars.mds.yandex.net/get-ott/1/x/1920x1080",
      hero_type: "backdrop", hero_source: "kinopoisk", hero_quality_score: 0.935 }],
  });
  await openWishlist(page);

  const src = await page.locator(".single-pick-backdrop").getAttribute("src");
  const source = decodeURIComponent(new URL(src, "http://x").searchParams.get("u"));
  expect(source).toBe("https://avatars.mds.yandex.net/get-ott/1/x/1280x720");
});

test("the fullscreen poster is not downscaled below the element it fills", async ({ page }) => {
  // Прежде сюда уходил small=true из тайлов: источник 300x450 на элемент шириной
  // ~690 физических пикселей — заметно мыльно.
  const poster = "https://avatars.mds.yandex.net/get-kinopoisk-image/1/y/600x900";
  await openPicker(page, {
    randomItems: [{ ...baseMovie, poster_url: poster, hero_url: poster,
      hero_type: "poster_blur", hero_source: "poster", hero_quality_score: 0.5 }],
  });
  await openSmartRandom(page);

  const src = await page.locator(".single-pick-poster").getAttribute("src");
  const source = decodeURIComponent(new URL(src, "http://x").searchParams.get("u"));
  expect(source).toBe(poster);
  expect(source).not.toContain("300x450");
});

test("the wishlist screen hides an action that cannot change anything", async ({ page }) => {
  // Фильм рулетки уже лежит в «Хочу»: кнопка там показывала галочку, ничего
  // при этом не меняя.
  await openPicker(page);
  await openWishlist(page);
  await expect(page.getByRole("button", { name: "В «Хочу»" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Уже смотрел" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Открыть фильм" })).toBeVisible();
});

test("smart random keeps the wishlist action, where it does change something", async ({ page }) => {
  await openPicker(page);
  await openSmartRandom(page);
  await expect(page.getByRole("button", { name: "В «Хочу»" })).toBeVisible();
});

test("a broken backdrop degrades to the poster without asking for another film", async ({ page }) => {
  const state = await openPicker(page, { brokenImages: ["assets.fanart.tv"] });
  await openWishlist(page);

  await expect(page.locator(".single-pick-poster")).toBeVisible();
  await expect(page.locator(".single-pick-backdrop")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Wishlist Pick" })).toBeVisible();
  expect(state.wishlistCalls).toBe(1);
  expect(state.feedback).toEqual([]);
});

test("a film with no image at all shows a neutral placeholder", async ({ page }) => {
  await openPicker(page, {
    randomItems: [{ ...baseMovie, poster_url: null, backdrop_url: null,
      hero_url: null, hero_type: null, hero_source: null, hero_quality_score: null }],
  });
  await openSmartRandom(page);

  await expect(page.locator(".single-pick-media-empty")).toHaveText("Изображение недоступно");
  await expect(page.getByRole("heading", { name: "The Last of Us" })).toBeVisible();
});

test("another option replaces the film, resets the scroll and switches media mode", async ({ page }) => {
  const state = await openPicker(page, {
    randomItems: [
      { ...baseMovie, title: "First Pick", strategy: "reliable", ...backdropHero },
      { ...baseMovie, id: 2, title: "Second Pick", strategy: "discovery", ...posterHero },
    ],
  });
  await openSmartRandom(page);
  await expect(page.getByRole("heading", { name: "First Pick" })).toBeVisible();
  await expect(page.locator(".single-pick-backdrop")).toBeVisible();

  await page.evaluate(() => window.scrollTo(0, 400));
  await page.getByRole("button", { name: "Другой вариант" }).click();

  await expect(page.getByRole("heading", { name: "Second Pick" })).toBeVisible();
  await expect(page.locator(".single-pick-poster")).toBeVisible();
  await expect(page.locator(".single-pick-backdrop")).toHaveCount(0);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  expect(state.feedback.map(entry => entry.action)).toEqual(["another"]);
  expect(state.feedback[0].mode).toBe("random");
});

test("feedback keeps the wishlist mode on the wishlist screen", async ({ page }) => {
  const state = await openPicker(page);
  await openWishlist(page);

  await page.getByRole("button", { name: "Другой вариант" }).click();
  await expect.poll(() => state.feedback.length).toBe(1);
  await page.getByRole("button", { name: "Не предлагать" }).click();
  await expect.poll(() => state.feedback.length).toBe(2);

  expect(state.feedback.map(entry => entry.action)).toEqual(["another", "rejected"]);
  expect(state.feedback.every(entry => entry.mode === "wishlist")).toBe(true);
  // «Не предлагать» — про один фильм: остаёмся на экране подбора, а не улетаем
  // на стартовое меню.
  await expect(page.locator(".single-pick-screen")).toBeVisible();
});

test("opening a film reports the right mode and leaves the picker reachable", async ({ page }) => {
  const state = await openPicker(page);
  await openWishlist(page);

  await page.getByRole("button", { name: "Открыть фильм" }).click();
  await expect.poll(() => state.feedback.length).toBe(1);
  expect(state.feedback[0]).toMatchObject({ action: "opened", mode: "wishlist" });
});

test("with the feature flag off the legacy card renderer stays in place", async ({ page }) => {
  await openPicker(page, { fullscreen: false });
  await openWishlist(page);

  await expect(page.locator(".recommendation-film")).toBeVisible();
  await expect(page.locator(".single-pick-screen")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Открыть фильм" })).toBeVisible();
});

test("the screen fits a 320px viewport without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await openPicker(page);
  await openWishlist(page);

  await expect(page.locator(".single-pick-screen")).toBeVisible();
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);

  for (const name of ["Открыть фильм", "Уже смотрел", "Не предлагать"]) {
    await expect(page.getByRole("button", { name })).toBeVisible();
  }
  // Прокрутив экран до конца, человек обязан дотянуться до последнего действия:
  // плавающая навигация не должна его накрывать.
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const overlap = await page.evaluate(() => {
    const danger = document.querySelector(".single-pick-danger").getBoundingClientRect();
    const tabbar = document.getElementById("tabbar").getBoundingClientRect();
    return danger.bottom - tabbar.top;
  });
  expect(overlap).toBeLessThanOrEqual(0);
});
