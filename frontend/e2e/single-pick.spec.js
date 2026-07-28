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
const coverHero = {
  ...backdropHero, hero_fit: "cover", hero_focus_x: 0.22, hero_focus_y: 0.71,
};
const posterHero = {
  hero_url: POSTER, hero_type: "poster_blur", hero_source: "poster", hero_quality_score: 0.5,
};

async function openPicker(page, options = {}) {
  const state = {
    feedback: [],
    wishlistCalls: 0,
    randomCalls: 0,
    adminMutations: [],
    capabilityLoaded: false,
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
    if (path === "/api/me/capabilities") {
      state.capabilityLoaded = true;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(
        options.admin
          ? { is_admin: true, admin_role: "admin",
            capabilities: ["collections.read", "collections.write", "content.publish", "audit.read"] }
          : { is_admin: false, admin_role: null, capabilities: [] }) });
      return;
    }
    if (/^\/api\/admin\/films\/\d+\/hero-presentation$/.test(path)) {
      const body = JSON.parse(route.request().postData() || "{}");
      state.adminMutations.push({ kind: "hero", ...body });
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        ...backdropHero, hero_fit: body.fit,
        hero_focus_x: body.focus_x, hero_focus_y: body.focus_y,
      }) });
      return;
    }
    if (/^\/api\/admin\/films\/\d+\/poster-display$/.test(path)) {
      const body = JSON.parse(route.request().postData() || "{}");
      state.adminMutations.push({ kind: "poster", ...body });
      const rejected = body.state === "rejected";
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(
        rejected
          ? { hero_url: null, hero_type: null, hero_source: null,
            hero_quality_score: null, hero_fit: null,
            hero_focus_x: null, hero_focus_y: null }
          : { ...posterHero, hero_fit: null, hero_focus_x: null, hero_focus_y: null }) });
      return;
    }
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
  if (options.admin) {
    await expect.poll(() => state.capabilityLoaded).toBe(true);
    await page.waitForTimeout(20);
  }
  await page.getByRole("button", { name: "Подбор", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Что посмотреть?" })).toBeVisible();
  return state;
}

const openWishlist = page => page.getByRole("button", { name: /Случайный из «Хочу»/ }).click();
const openSmartRandom = page => page.getByRole("button", { name: /Умный случайный фильм/ }).click();
const MOBILE_VIEWPORTS = [
  { width: 320, height: 568 },
  { width: 360, height: 667 },
  { width: 390, height: 844 },
  { width: 430, height: 932 },
];
const ACTION_NAMES = [
  "Открыть фильм", "В «Хочу»", "Уже смотрел", "Другой вариант", "Не предлагать",
];

async function expectSinglePickFits(page, title) {
  await expect(page.locator(".single-pick-screen")).toBeVisible();
  await expect(page.locator(".picker-head")).toHaveCount(0);
  await expect(page.locator("#tabbar")).toBeHidden();
  await expect(page.locator("body")).toHaveClass(/single-pick-open/);
  await expect(page.locator(".single-pick-back-slot .back")).toBeVisible();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();

  for (const name of ACTION_NAMES) {
    await expect(page.getByRole("button", { name })).toBeVisible();
  }

  const layout = await page.evaluate(names => {
    const buttons = names.map(name => {
      const button = [...document.querySelectorAll("button")]
        .find(node => node.textContent.trim() === name);
      const rect = button.getBoundingClientRect();
      return { name, top: rect.top, bottom: rect.bottom };
    });
    const titleNode = document.querySelector(".single-pick-title");
    const titleStyle = getComputedStyle(titleNode);
    const titleRect = titleNode.getBoundingClientRect();
    return {
      htmlScrollHeight: document.documentElement.scrollHeight,
      bodyScrollHeight: document.body.scrollHeight,
      viewportHeight: window.innerHeight,
      tabbarInert: document.getElementById("tabbar").inert,
      buttons,
      titleLines: Math.round(titleRect.height / Number.parseFloat(titleStyle.lineHeight)),
    };
  }, ACTION_NAMES);

  expect(layout.htmlScrollHeight).toBeLessThanOrEqual(layout.viewportHeight + 2);
  expect(layout.bodyScrollHeight).toBeLessThanOrEqual(layout.viewportHeight + 2);
  expect(layout.tabbarInert).toBe(true);
  expect(layout.titleLines).toBeLessThanOrEqual(2);
  for (const button of layout.buttons) {
    expect(button.top, `${button.name} starts above the viewport`).toBeGreaterThanOrEqual(-1);
    expect(button.bottom, `${button.name} exceeds the viewport`).toBeLessThanOrEqual(layout.viewportHeight);
  }
}

test("wishlist roulette safely contains a backdrop with no saved fit", async ({ page }) => {
  await openPicker(page);
  await openWishlist(page);

  const backdrop = page.locator(".single-pick-backdrop");
  await expect(backdrop).toBeVisible();
  await expect(page.getByRole("heading", { name: "Wishlist Pick" })).toBeVisible();
  await expect(page.locator(".single-pick-label")).toHaveText("Случайный из «Хочу»");

  const hero = page.locator(".single-pick-media-backdrop");
  await expect(hero).toHaveAttribute("data-hero-fit", "contain");
  const stageBox = await page.locator(".single-pick-backdrop-stage").boundingBox();
  const imageBox = await backdrop.boundingBox();
  expect(await backdrop.evaluate(node => getComputedStyle(node).objectFit)).toBe("contain");
  expect(imageBox.x).toBeGreaterThanOrEqual(stageBox.x - 1);
  expect(imageBox.y).toBeGreaterThanOrEqual(stageBox.y - 1);
  expect(imageBox.x + imageBox.width).toBeLessThanOrEqual(stageBox.x + stageBox.width + 1);
  expect(imageBox.y + imageBox.height).toBeLessThanOrEqual(stageBox.y + stageBox.height + 1);
  // Метаданные фильма читаются поверх затемнения, а не поверх голого кадра.
  await expect(page.locator(".single-pick-media-shade")).toBeAttached();
  for (const name of ["Открыть фильм", "В «Хочу»", "Уже смотрел", "Другой вариант", "Не предлагать"]) {
    await expect(page.getByRole("button", { name })).toBeVisible();
  }
});

test("explicit cover uses the saved focal point", async ({ page }) => {
  await openPicker(page, {
    wishlistItems: [{ ...baseMovie, id: 11, title: "Directed Cover", ...coverHero }],
  });
  await openWishlist(page);
  const hero = page.locator(".single-pick-media-backdrop");
  const sharp = page.locator(".single-pick-backdrop");
  await expect(hero).toHaveAttribute("data-hero-fit", "cover");
  expect(await sharp.evaluate(node => getComputedStyle(node).objectFit)).toBe("cover");
  expect(await sharp.evaluate(node => getComputedStyle(node).objectPosition)).toBe("22% 71%");
});

test("regular users receive no art-direction controls", async ({ page }) => {
  await openPicker(page);
  await openWishlist(page);
  await expect(page.locator("[data-art-open]")).toHaveCount(0);
});

test("admin can preview and save presentation without another recommendation", async ({ page }) => {
  const state = await openPicker(page, { admin: true });
  await openWishlist(page);
  await expect(page.getByRole("button", { name: "Кадр" })).toBeVisible();
  await page.getByRole("button", { name: "Кадр" }).click();
  await page.getByRole("button", { name: "Заполнить экран" }).click();
  const x = page.locator("[data-art-focus-x]");
  await x.fill("0.31");
  await x.dispatchEvent("input");
  await expect(page.locator(".single-pick-media-backdrop")).toHaveAttribute("data-hero-fit", "cover");
  expect(state.wishlistCalls).toBe(1);
  await page.getByRole("button", { name: "Сохранить" }).click();
  await expect.poll(() => state.adminMutations.length).toBe(1);
  expect(state.adminMutations[0]).toMatchObject({ kind: "hero", fit: "cover", focus_x: 0.31 });
  expect(state.wishlistCalls).toBe(1);
});

test("rejecting the exact poster rerenders neutral media only", async ({ page }) => {
  const state = await openPicker(page, { admin: true });
  await openSmartRandom(page);
  await page.getByRole("button", { name: "Кадр" }).click();
  await page.getByRole("button", { name: "Отклонить этот постер" }).click();
  await expect(page.locator(".single-pick-media-empty")).toContainText("Изображение недоступно");
  expect(state.randomCalls).toBe(1);
  expect(state.adminMutations).toEqual([{
    kind: "poster", state: "rejected", reason: "manual_admin_rejection",
  }]);
});

test("ambient image failure leaves the sharp backdrop intact", async ({ page }) => {
  const state = await openPicker(page);
  await openWishlist(page);
  await page.locator(".single-pick-backdrop-ambient").dispatchEvent("error");
  await expect(page.locator(".single-pick-backdrop-ambient")).toHaveCount(0);
  await expect(page.locator(".single-pick-backdrop")).toBeVisible();
  expect(state.wishlistCalls).toBe(1);
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

  await expect(page.locator("body")).toHaveClass(/single-pick-open/);
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

  await page.getByRole("button", { name: "В «Хочу»" }).click();
  await expect.poll(() => state.feedback.length).toBe(1);
  await page.getByRole("button", { name: "Не предлагать" }).click();
  await expect.poll(() => state.feedback.length).toBe(2);

  expect(state.feedback.map(entry => entry.action)).toEqual(["want", "rejected"]);
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
  await expect(page.locator("body")).not.toHaveClass(/single-pick-open/);
  await expect(page.locator(".detail-v2")).toBeVisible();
  await expect(page.locator("#tabbar")).toBeVisible();
});

test("watched opens the regular detail screen outside fullscreen mode", async ({ page }) => {
  await openPicker(page);
  await openWishlist(page);

  await page.getByRole("button", { name: "Уже смотрел" }).click();
  await expect(page.locator("body")).not.toHaveClass(/single-pick-open/);
  await expect(page.locator(".detail-v2")).toBeVisible();
  await expect(page.locator("#tabbar")).toBeVisible();
});

test("back navigation closes fullscreen mode and restores the picker landing", async ({ page }) => {
  await openPicker(page);
  await openWishlist(page);

  await page.locator(".single-pick-back-slot .back").click();
  await expect(page.locator("body")).not.toHaveClass(/single-pick-open/);
  await expect(page.getByRole("heading", { name: "Что посмотреть?" })).toBeVisible();
  await expect(page.locator("#tabbar")).toBeVisible();
});

test("with the feature flag off the legacy card renderer stays in place", async ({ page }) => {
  await openPicker(page, { fullscreen: false });
  await openWishlist(page);

  await expect(page.locator(".recommendation-film")).toBeVisible();
  await expect(page.locator(".single-pick-screen")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Открыть фильм" })).toBeVisible();
});

for (const viewport of MOBILE_VIEWPORTS) {
  test(`backdrop fits ${viewport.width}x${viewport.height} without scrolling`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await openPicker(page);
    await openWishlist(page);
    await expect(page.locator(".single-pick-backdrop")).toBeVisible();
    await expectSinglePickFits(page, "Wishlist Pick");
  });

  test(`poster blur fits ${viewport.width}x${viewport.height} without scrolling`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await openPicker(page);
    await openSmartRandom(page);
    await expect(page.locator(".single-pick-poster")).toBeVisible();
    await expectSinglePickFits(page, "The Last of Us");
  });
}

test("fullscreen loading never flashes the legacy header", async ({ page }) => {
  await openPicker(page);

  let releaseRequest;
  const requestGate = new Promise(resolve => {
    releaseRequest = resolve;
  });

  await page.route("**/api/wishlist/random", async route => {
    await requestGate;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      item: { ...baseMovie, id: 11, title: "Wishlist Pick", ...backdropHero },
      cycle: { cycle: 1, wishlist_size: 3, shown_in_cycle: 1, remaining_in_cycle: 2 },
    }) });
  });

  await openWishlist(page);
  await expect(page.locator(".single-pick-state-screen")).toBeVisible();
  await expect(page.locator(".picker-head")).toHaveCount(0);
  await expect(page.locator("#tabbar")).toBeHidden();

  releaseRequest();
  await expect(page.getByRole("heading", { name: "Wishlist Pick" })).toBeVisible();
});
