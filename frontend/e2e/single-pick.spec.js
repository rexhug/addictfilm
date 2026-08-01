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
    imageRequests: [],
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
      showConfirm(_text, callback) { callback(true); },
      showAlert(text) { (window.__telegramAlerts ||= []).push(String(text)); },
      openTelegramLink() {},
    } };
  });

  await page.route("**/img?**", async route => {
    const source = new URL(route.request().url()).searchParams.get("u") || "";
    state.imageRequests.push(source);
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
    if (/^\/api\/admin\/films\/\d+\/movie-flow$/.test(path)) {
      const body = JSON.parse(route.request().postData() || "{}");
      state.adminMutations.push({ kind: "flow", ...body });
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        id: Number(path.split("/")[4]), movie_flow_state: body.state,
        movie_flow_reason: body.reason, eligible: body.state !== "exclude",
      }) });
      return;
    }
    const wishlistEnvelope = index => ({
      item: wishlistItems[Math.min(index, wishlistItems.length - 1)],
      token: `wishlist-token-${index}`,
      expires_in: 300,
    });
    if (path === "/api/wishlist/random") {
      const item = wishlistEnvelope(state.wishlistCalls).item;
      state.wishlistCalls += 1;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        item,
        cycle: { cycle: 1, wishlist_size: 3, shown_in_cycle: 1, remaining_in_cycle: 2 },
        next: wishlistEnvelope(state.wishlistCalls),
      }) });
      return;
    }
    if (path === "/api/wishlist/random/prepare") {
      await route.fulfill({ contentType: "application/json",
        body: JSON.stringify(wishlistEnvelope(state.wishlistCalls)) });
      return;
    }
    if (path === "/api/wishlist/random/consume") {
      const consumeIndex = state.wishlistCalls;
      if (options.wishlistConsumeGates?.[consumeIndex]) {
        await options.wishlistConsumeGates[consumeIndex];
      }
      const item = wishlistEnvelope(state.wishlistCalls).item;
      state.wishlistCalls += 1;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        item,
        cycle: { cycle: 1, wishlist_size: 3,
          shown_in_cycle: state.wishlistCalls, remaining_in_cycle: Math.max(0, 3 - state.wishlistCalls) },
        next: wishlistEnvelope(state.wishlistCalls),
      }) });
      return;
    }
    if (path === "/api/recommendations/random") {
      const call = state.randomCalls;
      state.randomCalls += 1;
      if (options.randomGates?.[call]) await options.randomGates[call];
      if (options.randomFailures?.includes(call)) {
        await route.fulfill({
          status: 404, contentType: "application/json",
          body: JSON.stringify({ detail: {
            code: "NO_ELIGIBLE_FILMS",
            message: "No eligible films",
            recoverable: true,
          } }),
        });
        return;
      }
      const item = randomItems[Math.min(call, randomItems.length - 1)];
      await route.fulfill({ contentType: "application/json",
        body: JSON.stringify({ item, context: "solo" }) });
      return;
    }
    if (options.quizZero && path === "/api/recommendations/quiz/start") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        id: "zero-session", state: "complete", question: null, progress: 8, total: 8,
      }) });
      return;
    }
    if (options.quizZero && path === "/api/recommendations/quiz/zero-session/results") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        id: "zero-session", state: "complete", items: [],
      }) });
      return;
    }
    if (options.quizZero && path === "/api/recommendations/quiz/zero-session/back") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        id: "zero-session", state: "active", progress: 0, total: 8,
        question: { id: "q1", text: "Что тебе сейчас больше всего хочется?",
          options: [{ id: "relax", label: "Отключить голову" }] },
      }) });
      return;
    }
    if (options.quizZero && path === "/api/recommendations/quiz/zero-session/restart") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        id: "zero-session", state: "active", progress: 0, total: 8,
        question: { id: "q1", text: "Что тебе сейчас больше всего хочется?",
          options: [{ id: "relax", label: "Отключить голову" }] },
      }) });
      return;
    }
    if (/^\/api\/recommendations\/\d+\/feedback$/.test(path)) {
      const feedback = JSON.parse(route.request().postData() || "{}");
      state.feedback.push(feedback);
      if (options.feedbackGates?.[feedback.action]) await options.feedbackGates[feedback.action];
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

const openWishlist = page => page.getByRole("button", { name: /Случайный фильм из «Хочу»/ }).click();
const openSmartRandom = page => page.getByRole("button", { name: /Случайный фильм по твоему вкусу/ }).click();
const MOBILE_VIEWPORTS = [
  { width: 320, height: 568 },
  { width: 360, height: 667 },
  { width: 390, height: 844 },
  { width: 430, height: 932 },
];
const ACTION_NAMES = [
  "Открыть фильм", "В «Хочу»", "Уже смотрел", "Другой вариант", "Не предлагать",
];
// В рулетке фильм уже в «Хочу», поэтому этого действия там нет — и быть не должно.
const WISHLIST_ACTION_NAMES = ACTION_NAMES.filter(name => name !== "В «Хочу»");

async function expectSinglePickFits(page, title, actionNames = ACTION_NAMES) {
  await expect(page.locator(".single-pick-screen")).toBeVisible();
  await expect(page.locator(".picker-head")).toHaveCount(0);
  await expect(page.locator("#tabbar")).toBeHidden();
  await expect(page.locator("body")).toHaveClass(/single-pick-open/);
  await expect(page.locator(".single-pick-back-slot .back")).toBeVisible();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();

  for (const name of actionNames) {
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
  }, actionNames);

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
  await expect(page.locator(".single-pick-label")).toHaveText("Случайный фильм из «Хочу»");

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
  for (const name of WISHLIST_ACTION_NAMES) {
    await expect(page.getByRole("button", { name })).toBeVisible();
  }
  await expect(page.getByRole("button", { name: "В «Хочу»" })).toHaveCount(0);
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

test("admin movie-flow moderation is explicit and does not request another film", async ({ page }) => {
  const state = await openPicker(page, { admin: true });
  await openSmartRandom(page);
  await page.getByRole("button", { name: "Кадр" }).click();
  await page.getByRole("button", { name: "Исключить" }).click();

  await expect.poll(() => state.adminMutations.some(item => item.kind === "flow")).toBe(true);
  expect(state.adminMutations.find(item => item.kind === "flow")).toEqual({
    kind: "flow", state: "exclude", reason: "manual_admin_exclusion",
  });
  expect(state.randomCalls).toBe(1);
  await expect(page.getByRole("heading", { name: "The Last of Us" })).toBeVisible();
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
  await expect(page.locator(".single-pick-label")).toHaveText("Малоизвестная находка");
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

test("another option keeps the current card visible while replacement is pending", async ({ page }) => {
  let releaseReplacement;
  const replacementGate = new Promise(resolve => { releaseReplacement = resolve; });
  const state = await openPicker(page, {
    randomItems: [
      { ...baseMovie, title: "First Pick", strategy: "reliable", ...backdropHero },
      { ...baseMovie, id: 2, title: "Second Pick", strategy: "discovery", ...posterHero },
    ],
    randomGates: { 1: replacementGate },
  });
  await openSmartRandom(page);
  const another = page.getByRole("button", { name: "Другой вариант" });
  await another.click();

  await expect.poll(() => state.randomCalls).toBe(2);
  await expect(page.getByRole("heading", { name: "First Pick" })).toBeVisible();
  await expect(another).toBeDisabled();
  await expect(page.locator(".single-pick-card")).toHaveClass(/is-refreshing/);
  await expect(page.locator(".single-pick-state-screen")).toHaveCount(0);

  releaseReplacement();
  await expect(page.getByRole("heading", { name: "Second Pick" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "First Pick" })).toHaveCount(0);
});

test("failed replacement preserves the card and restores its controls", async ({ page }) => {
  const state = await openPicker(page, {
    randomItems: [{ ...baseMovie, title: "Still Here", strategy: "reliable", ...backdropHero }],
    randomFailures: [1],
  });
  await openSmartRandom(page);
  const another = page.getByRole("button", { name: "Другой вариант" });
  await another.click();

  await expect.poll(() => state.randomCalls).toBe(2);
  await expect(page.getByRole("heading", { name: "Still Here" })).toBeVisible();
  await expect(another).toBeEnabled();
  await expect(another).not.toHaveAttribute("aria-busy");
  await expect(page.locator(".single-pick-card")).not.toHaveClass(/is-refreshing/);
  await expect(page.locator(".single-pick-state-screen")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => window.__telegramAlerts?.length || 0)).toBe(1);
});

test("initial random failure has working retry and never renders a dead end", async ({ page }) => {
  const state = await openPicker(page, {
    randomItems: [{ ...baseMovie, title: "Recovered Pick", strategy: "available", ...posterHero }],
    randomFailures: [0],
  });
  await openSmartRandom(page);

  await expect(page.getByRole("heading", { name: "Не удалось подобрать фильм" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Попробовать ещё раз" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Назад к выбору" })).toBeVisible();
  await page.getByRole("button", { name: "Попробовать ещё раз" }).click();
  await expect(page.getByRole("heading", { name: "Recovered Pick" })).toBeVisible();
  await expect(page.locator(".single-pick-label")).toHaveText("Хороший вариант");
  expect(state.randomCalls).toBe(2);
});

test("initial random failure can return to picker without closing the Mini App", async ({ page }) => {
  await openPicker(page, { randomFailures: [0] });
  await openSmartRandom(page);
  await page.getByRole("button", { name: "Назад к выбору" }).click();

  await expect(page.getByRole("heading", { name: "Что посмотреть?" })).toBeVisible();
  await expect(page.locator("#tabbar")).toBeVisible();
  await expect(page.locator("body")).not.toHaveClass(/single-pick-open/);
});

test("zero quiz results expose all three recovery paths without duplicate restart controls", async ({ page }) => {
  await openPicker(page, { quizZero: true });
  await page.getByRole("button", { name: /Подбор по настроению/ }).click();

  await expect(page.getByRole("button", { name: "Изменить ответы" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Начать заново" })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Случайный фильм по твоему вкусу" })).toBeVisible();
  await page.getByRole("button", { name: "Изменить ответы" }).click();
  await expect(page.getByRole("heading", {
    name: "Что тебе сейчас больше всего хочется?",
  })).toBeVisible();
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

test("wishlist preloads the next image and preserves the card until consume commits", async ({ page }) => {
  let releaseConsume;
  const consumeGate = new Promise(resolve => { releaseConsume = resolve; });
  const firstHero = "https://assets.fanart.tv/fanart/movies/1/first-wide.jpg";
  const secondHero = "https://assets.fanart.tv/fanart/movies/2/second-wide.jpg";
  const state = await openPicker(page, {
    wishlistItems: [
      { ...baseMovie, id: 11, title: "First Wishlist", ...backdropHero, hero_url: firstHero },
      { ...baseMovie, id: 12, title: "Second Wishlist", ...backdropHero, hero_url: secondHero },
    ],
    wishlistConsumeGates: { 1: consumeGate },
  });
  await openWishlist(page);
  await expect(page.getByRole("heading", { name: "First Wishlist" })).toBeVisible();
  await expect.poll(() => state.imageRequests.some(url => url.includes("second-wide"))).toBe(true);

  const another = page.getByRole("button", { name: "Другой вариант" });
  await another.click();
  await expect.poll(() => state.feedback.some(entry => entry.action === "another")).toBe(true);
  await expect(page.getByRole("heading", { name: "First Wishlist" })).toBeVisible();
  await expect(another).toBeDisabled();
  await expect(page.locator(".single-pick-card")).toHaveClass(/is-refreshing/);

  releaseConsume();
  await expect(page.getByRole("heading", { name: "Second Wishlist" })).toBeVisible();
  expect(state.wishlistCalls).toBe(2);
});

test("slow analytics feedback never blocks the next wishlist request", async ({ page }) => {
  let releaseFeedback;
  const feedbackGate = new Promise(resolve => { releaseFeedback = resolve; });
  const state = await openPicker(page, {
    wishlistItems: [
      { ...baseMovie, id: 11, title: "First Wishlist", ...backdropHero },
      { ...baseMovie, id: 12, title: "Second Wishlist", ...posterHero },
    ],
    feedbackGates: { another: feedbackGate },
  });
  await openWishlist(page);
  await page.getByRole("button", { name: "Другой вариант" }).click();

  // The second committed show arrives while the independent analytics request
  // is deliberately still pending.
  await expect.poll(() => state.wishlistCalls).toBe(2);
  await expect(page.getByRole("heading", { name: "Second Wishlist" })).toBeVisible();
  releaseFeedback();
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
    await expectSinglePickFits(page, "Wishlist Pick", WISHLIST_ACTION_NAMES);
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
