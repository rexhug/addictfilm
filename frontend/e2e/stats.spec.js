import { expect, test } from "@playwright/test";

const personalStats = {
  watched: 46, want: 31, avg_rating: 7.2, total_runtime_min: 4800,
  rating_dist: [0, 1, 2, 6, 3, 8, 11, 9, 4, 8],
  top_genres_pct: [["Drama", 20, 9], ["Thriller", 14, 6], ["Mystery", 10, 5], ["Action", 8, 4]],
  // Deliberately mixed source ratios: normal portraits fill the compact card,
  // while extreme sources safely fall back to contain rather than cutting a face.
  top_actors: [["Jake Gyllenhaal", 4, "https://image.tmdb.org/tall-person.jpg"], ["Will Poulter", 3, "https://image.tmdb.org/wide-person.jpg"], ["Ed Helms", 2, "https://image.tmdb.org/square-person.jpg"], ["Woody Harrelson", 2, "https://image.tmdb.org/portrait-person.jpg"]],
  top_directors: [["David Fincher", 4, "https://image.tmdb.org/tall-director.jpg"], ["Todd Phillips", 3, "https://image.tmdb.org/wide-director.jpg"], ["Christopher Alexander Longlastname", 2, "https://image.tmdb.org/square-director.jpg"], ["Denis Villeneuve", 2, "https://image.tmdb.org/portrait-director.jpg"]],
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

async function openStats(page, paired = false, options = {}) {
  let settingsState = {
    language: "ru",
    telegram_enabled: true,
    telegram_available: true,
    ...(options.settings || {}),
  };
  const recommendationMovie = {
    id: 1, title: "The Last of Us", title_original: "The Last of Us", year: "2023",
    runtime: "81 min", genres: "Drama, Thriller", rating: 8.5, poster_url: null,
    reasons: ["COZY_TONE", "HIGH_QUALITY"],
    explanation: "Спокойный и светлый тон · Высокая оценка зрителей", role: "best", score: 72,
  };
  let quizItems = [
    recommendationMovie,
    { ...recommendationMovie, id: 3, title: "Inception", role: "reliable", reasons: ["INTELLECTUAL"] },
    { ...recommendationMovie, id: 4, title: "The Hunger Games", role: "unexpected", reasons: ["HIDDEN_GEM"] },
  ];
  const recommendationQuestions = [
    ["q1", "Что тебе сейчас больше всего хочется?", "relax", "Отключить голову и расслабиться"],
    ["r1", "Как именно ты хочешь отдохнуть?", "warm", "Уютная и добрая история"],
    ["r2", "Насколько спокойно должен идти фильм?", "medium", "Нормальный темп, без спешки"],
    ["r3", "Какой конфликт ты сегодня готов терпеть?", "low", "Почти никакой — мне нужен комфорт"],
    ["c1", "Сколько времени у тебя есть?", "any", "Время не важно"],
    ["c2", "Насколько новый фильм ты хочешь?", "any", "Неважно, главное — хороший"],
    ["c3", "Как сегодня выбираем?", "safe", "Надёжный вариант с высоким рейтингом"],
    ["c4", "Ты будешь смотреть один?", "solo", "Один"],
  ];
  let recommendationStep = 0;
  const engineMeta = options.engineMeta || null;
  const recommendationPayload = () => {
    const item = recommendationQuestions[recommendationStep];
    return item
      ? { id: "quiz_test", version: "v1", state: "active", progress: recommendationStep, total: 8,
        pair_available: paired, question: { id: item[0], text: item[1], options: [{ id: item[2], label: item[3] }] },
        ...(engineMeta || {}) }
      : { id: "quiz_test", version: "v1", state: "complete", progress: 8, total: 8, pair_available: paired, question: null, ...(engineMeta || {}) };
  };
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(({ notification, startParam }) => {
    window.__verticalSwipeDisableCalls = 0;
    window.__tgEventHandlers = {};
    if (notification) {
      let permission = notification.permission;
      window.__notificationRequests = 0;
      Object.defineProperty(window, "Notification", { configurable: true, value: {
        get permission() { return permission; },
        async requestPermission() { window.__notificationRequests += 1; permission = notification.requestResult; return permission; },
      } });
    }
    window.Telegram = { WebApp: {
      initData: "test-init-data",
      initDataUnsafe: { user: { id: 1, first_name: "Denys", username: "denys" }, ...(startParam ? { start_param: startParam } : {}) },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {},
      onEvent(name, callback) {
        (window.__tgEventHandlers[name] ||= []).push(callback);
      },
      offEvent(name, callback) {
        window.__tgEventHandlers[name] = (window.__tgEventHandlers[name] || [])
          .filter(handler => handler !== callback);
      },
      disableVerticalSwipes() { window.__verticalSwipeDisableCalls += 1; },
      HapticFeedback: { impactOccurred() {}, notificationOccurred() {} },
      showConfirm(_text, callback) { callback(true); }, showAlert() {}, openTelegramLink() {},
    } };
  }, { notification: options.notification || null, startParam: options.startParam || null });
  await page.route("**/img?**", async route => {
    const source = new URL(route.request().url()).searchParams.get("u") || "";
    const [width, height] = source.includes("wide") ? [960, 540]
      : source.includes("square") ? [720, 720]
        : source.includes("portrait") ? [600, 900]
          : [540, 960];
    await route.fulfill({
      contentType: "image/svg+xml",
      body: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#365f9d"/><circle cx="${width / 2}" cy="${height * .42}" r="${Math.min(width, height) * .23}" fill="#f1b98d"/><rect x="${width * .22}" y="${height * .64}" width="${width * .56}" height="${height * .4}" rx="24" fill="#183152"/></svg>`,
    });
  });
  await page.route("**/api/**", async route => {
    const requestUrl = new URL(route.request().url());
    const path = requestUrl.pathname;
    if (path.startsWith("/api/notifications")) {
      options.onNotificationRequest?.({
        method: route.request().method(),
        path,
        searchParams: requestUrl.searchParams,
      });
    }
    const partnerApi = options.partnerApi;
    if (path === "/api/partner/stats" && options.pairStatsFailure) {
      await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "stats unavailable" }) });
      return;
    }
    const json = path === "/api/me"
      ? { id: 1, label: "Denys", username: "denys", role: null }
      : path === "/api/recommendations/random"
        ? { item: { ...recommendationMovie, role: "random", strategy: "discovery", reasons: ["UNSEEN_PICK", "RANDOM_DISCOVERY"] }, context: "solo" }
      : path === "/api/recommendations/quiz/start"
        // Новый опрос всегда начинается с первого вопроса — как на сервере.
        ? (() => { recommendationStep = 0; return recommendationPayload(); })()
      : path.endsWith("/answer") && path.startsWith("/api/recommendations/quiz/")
        ? (() => { recommendationStep += 1; return recommendationPayload(); })()
      : path.endsWith("/back") && path.startsWith("/api/recommendations/quiz/")
        ? (() => { recommendationStep = Math.max(0, recommendationStep - 1); return recommendationPayload(); })()
      : path.endsWith("/restart") && path.startsWith("/api/recommendations/quiz/")
        ? (() => { recommendationStep = 0; return recommendationPayload(); })()
      : path.endsWith("/results") && path.startsWith("/api/recommendations/quiz/")
        ? { id: "quiz_test", items: quizItems, context: "solo", ...(engineMeta || {}) }
      : path.endsWith("/replace") && path.startsWith("/api/recommendations/quiz/") && options.replaceStatus === 409
        ? (() => {
          route.fulfill({ status: 409, contentType: "application/json",
            body: JSON.stringify({ detail: { code: "SESSION_VERSION_MISMATCH", message: "Подбор обновился" } }) });
          return null;
        })()
      : path.endsWith("/replace") && path.startsWith("/api/recommendations/quiz/")
        ? (() => {
          const body = JSON.parse(route.request().postData() || "{}");
          const replacement = { ...recommendationMovie, id: 9, title: "Fresh Pick", role: body.role, reasons: ["MATCHES_MOOD"] };
          quizItems = quizItems.map(item => (item.id === body.film_id ? replacement : item));
          return { id: "quiz_test", items: quizItems, replacement, context: "solo" };
        })()
      : path === "/api/wishlist/random"
        ? { item: { ...recommendationMovie, id: 11, title: "Wishlist Pick", reasons: ["IN_WISHLIST"] }, cycle: { cycle: 1, wishlist_size: 3, shown_in_cycle: 1, remaining_in_cycle: 2 } }
      : /^\/api\/recommendations\/\d+\/feedback$/.test(path)
        ? { ok: true }
      : path === "/api/movie/1/status"
        ? { ok: true }
      : path === "/api/settings"
        ? (() => {
          if (route.request().method() === "PATCH") {
            const body = JSON.parse(route.request().postData() || "{}");
            if (typeof body.telegram_notifications === "boolean") settingsState.telegram_enabled = body.telegram_notifications;
            if (body.language) settingsState.language = body.language;
          }
          return settingsState;
        })()
      : path === "/api/notifications"
        ? (typeof options.notifications === "function"
          ? options.notifications({ requestUrl, method: route.request().method() })
          : (options.notifications || { items: [], unread_count: 0,
            unread_by_category: { all: 0, pair: 0, films: 0, system: 0 },
            next_before_id: null }))
      : path.startsWith("/api/notifications/")
        ? { ok: true }
      : path === "/api/movie/1"
        ? { id: 1, title: "The Last of Us", title_original: "The Last of Us", year: "2023", poster_url: null, genres: "Drama" }
      : path === "/api/partner"
        ? (partnerApi?.current ? partnerApi.current() : (paired ? { status: "paired", partner: { name: "Kristina", username: "kristina" } } : { status: "none" }))
        : path.startsWith("/api/partner/invite/")
          ? { inviter: { name: "A very very very long inviter name that needs truncation", username: "inviter" } }
        : path === "/api/partner/invite"
          ? (partnerApi?.invite ? partnerApi.invite() : { link: "https://t.me/addictfilmbot?startapp=inv_test", code: "inv_test" })
        : path === "/api/partner/stats" ? pairStats
          : path === "/api/stats/person" ? { items: [{ id: 17, title: "Donnie Darko", year: "2001", poster_url: null, my_rating: 9 }] }
            : personalStats;
    // null означает «маршрут уже ответил сам» (например, смоделированный 409).
    if (json === null) return;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
  });
  await page.goto("/");
  expect(await page.evaluate(() => window.__verticalSwipeDisableCalls)).toBe(1);
  if (options.startParam) return;
  await page.getByRole("button", { name: "Статистика" }).click();
  await expect(page.getByRole("heading", { name: "Мой кинопрофиль" })).toBeVisible();
}

test("home category chips stay optically aligned in the mobile scroll rail", async ({ page }) => {
  await openStats(page);
  await page.getByRole("button", { name: "Главная" }).click();
  const chips = page.locator(".chips .chip");
  await expect(chips).toHaveCount(4);

  const layout = await page.locator(".chips").evaluate((rail) => {
    const chips = [...rail.querySelectorAll(".chip")];
    return {
      scrollable: rail.scrollWidth > rail.clientWidth,
      chips: chips.map((chip) => {
        const chipBox = chip.getBoundingClientRect();
        const iconBox = chip.querySelector(".e").getBoundingClientRect();
        const labelBox = chip.querySelector(".chip-label").getBoundingClientRect();
        return {
          height: chipBox.height,
          // The icon and label may be moved by tiny optical offsets, but
          // must still read as one baseline-aligned control.
          centerDelta: Math.abs(
            (iconBox.top + iconBox.height / 2) - (labelBox.top + labelBox.height / 2),
          ),
          iconWidth: iconBox.width,
          labelGap: labelBox.left - iconBox.right,
        };
      }),
    };
  });
  expect(layout.scrollable).toBeTruthy();
  expect(new Set(layout.chips.map(({ height }) => Math.round(height))).size).toBe(1);
  expect(layout.chips.every(({ height }) => height >= 42)).toBeTruthy();
  expect(layout.chips.every(({ centerDelta }) => centerDelta <= 1.5)).toBeTruthy();
  expect(layout.chips.every(({ iconWidth }) => Math.abs(iconWidth - 18) < 0.1)).toBeTruthy();
  expect(layout.chips.every(({ labelGap }) => labelGap >= 7 && labelGap <= 9)).toBeTruthy();
});

test("adaptive picker completes a mobile-safe eight-question flow without old top UI", async ({ page }) => {
  await openStats(page);
  await page.getByRole("button", { name: "Подбор" }).click();
  await expect(page.getByRole("heading", { name: "Что посмотреть?" })).toBeVisible();
  await expect(page.getByText("Мой топ")).toHaveCount(0);

  await page.getByRole("button", { name: "Умный случайный фильм" }).click();
  await expect(page.getByText("Из фильмов, которых ты ещё не видел · Находка не на слуху")).toBeVisible();
  await expect(page.getByText("Находка", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Уже смотрел" })).toBeVisible();
  await page.getByRole("button", { name: "Другой вариант" }).click();
  await expect(page.locator(".recommendation-film")).toHaveCount(1);
  await page.locator(".sub-head .back").click();

  await page.getByRole("button", { name: "Подбор по настроению" }).click();
  for (let index = 0; index < 8; index += 1) {
    await page.locator(".picker-option").first().click();
  }
  await expect(page.getByText("Лучший выбор")).toBeVisible();
  await expect(page.getByText("Надёжный вариант")).toBeVisible();
  await expect(page.getByText("Неожиданный вариант")).toBeVisible();
  await expect(page.locator(".recommendation-film")).toHaveCount(3);
  await expect(page.getByRole("button", { name: "Открыть фильм" }).first()).toBeVisible();

  await page.setViewportSize({ width: 320, height: 568 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("recommendation cards show localized reason phrases, never raw tags", async ({ page }) => {
  await openStats(page);
  await page.getByRole("button", { name: "Подбор" }).click();
  await page.getByRole("button", { name: "Подбор по настроению" }).click();
  for (let index = 0; index < 8; index += 1) await page.locator(".picker-option").first().click();

  await expect(page.locator(".recommendation-explanation").first())
    .toHaveText("Спокойный и светлый тон · Высокая оценка зрителей");
  await expect(page.locator(".recommendation-explanation").nth(1)).toHaveText("Требует внимания и размышления");
  // Внутренние теги и английский текст в русском интерфейсе — ровно тот дефект,
  // который чинился: «юмор, абсурд, сатира, a flawed lead».
  const explanations = await page.locator(".recommendation-explanation").allTextContents();
  for (const text of explanations) {
    expect(text).not.toMatch(/[A-Za-z_]{4,}/);
  }
});

test("hiding one recommendation replaces it in place instead of leaving the results", async ({ page }) => {
  await openStats(page);
  await page.getByRole("button", { name: "Подбор" }).click();
  await page.getByRole("button", { name: "Подбор по настроению" }).click();
  for (let index = 0; index < 8; index += 1) await page.locator(".picker-option").first().click();
  await expect(page.locator(".recommendation-film")).toHaveCount(3);

  await page.getByRole("button", { name: "Не предлагать" }).first().click();
  // Человек остаётся на результатах: заменилась одна карточка, анкета не потеряна.
  await expect(page.getByText("Лучший выбор")).toBeVisible();
  await expect(page.locator(".recommendation-film")).toHaveCount(3);
  await expect(page.locator(".recommendation-film-copy h2", { hasText: "Fresh Pick" })).toBeVisible();
  await expect(page.locator(".recommendation-film-copy h2", { hasText: "The Last of Us" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Что посмотреть?" })).toHaveCount(0);
});

test("bottom navigation never covers the last recommendation", async ({ page }) => {
  await openStats(page);
  await page.getByRole("button", { name: "Подбор" }).click();
  await page.getByRole("button", { name: "Подбор по настроению" }).click();
  for (let index = 0; index < 8; index += 1) await page.locator(".picker-option").first().click();
  await expect(page.locator(".recommendation-film")).toHaveCount(3);

  for (const width of [320, 375, 390, 430]) {
    await page.setViewportSize({ width, height: 700 });
    // Меряем не позицию скролла, а зарезервированный запас: страница обязана
    // продолжаться ниже последней карточки минимум на высоту плавающей панели.
    // Такая проверка не зависит от того, докрутили мы страницу или нет.
    const clearance = await page.evaluate(() => {
      const cards = [...document.querySelectorAll(".recommendation-film")];
      const last = cards[cards.length - 1].getBoundingClientRect();
      const bar = document.getElementById("tabbar").getBoundingClientRect();
      return {
        reserved: document.body.getBoundingClientRect().bottom - last.bottom,
        barOverlay: window.innerHeight - bar.top,
        horizontal: document.documentElement.scrollWidth <= window.innerWidth,
      };
    });
    expect(clearance.reserved, `таббар накрывает карточку при ${width}px`)
      .toBeGreaterThan(clearance.barOverlay);
    expect(clearance.horizontal).toBeTruthy();
  }
});

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
  const peopleSectionLayout = await page.locator(".people-stats-actors").evaluate((section) => {
    const styles = getComputedStyle(section);
    const rail = section.querySelector(".people-stats-rail");
    const firstCard = rail.querySelector(".person-stat-card").getBoundingClientRect();
    return { background: styles.backgroundColor, borderLeftWidth: styles.borderLeftWidth, firstCardLeft: firstCard.left };
  });
  expect(peopleSectionLayout.background).toBe("rgba(0, 0, 0, 0)");
  expect(peopleSectionLayout.borderLeftWidth).toBe("0px");
  expect(peopleSectionLayout.firstCardLeft).toBeGreaterThanOrEqual(15);
  expect(peopleSectionLayout.firstCardLeft).toBeLessThanOrEqual(21);
  await page.locator(".people-stats-actors").scrollIntoViewIfNeeded();
  await expect(page.locator(".people-stats-actors img[data-person-photo]").first()).toHaveClass(/ready/);
  await page.locator(".people-stats-directors").scrollIntoViewIfNeeded();
  await expect(page.locator(".people-stats-directors img[data-person-photo]").first()).toHaveClass(/ready/);
  const favoriteActorLayout = await page.locator(".people-stats-actors .person-stat-card.is-favorite").evaluate((card) => {
    const cardBox = card.getBoundingClientRect();
    const photoBox = card.querySelector(".person-stat-avatar").getBoundingClientRect();
    const photo = card.querySelector("img[data-person-photo]");
    const badgeInCopy = !!card.querySelector(".person-stat-copy .person-stat-favorite");
    return {
      cardWidth: cardBox.width, photoWidth: photoBox.width, photoHeight: photoBox.height,
      objectFit: getComputedStyle(photo).objectFit, badgeInCopy,
    };
  });
  expect(favoriteActorLayout.photoWidth).toBeGreaterThanOrEqual(favoriteActorLayout.cardWidth - 2);
  expect(favoriteActorLayout.photoHeight).toBeGreaterThan(favoriteActorLayout.cardWidth * 1.2);
  expect(favoriteActorLayout.objectFit).toBe("contain");
  expect(favoriteActorLayout.badgeInCopy).toBeFalsy();
  const actorCardLayout = await page.locator(".people-stats-actors .person-stat-card").evaluateAll((cards) => cards.map((card) => {
    const count = card.querySelector(".person-stat-copy > small");
    const box = card.getBoundingClientRect();
    return { width: box.width, height: box.height, scrollHeight: card.scrollHeight, clientHeight: card.clientHeight, countColor: getComputedStyle(count).color };
  }));
  expect(new Set(actorCardLayout.map(({ width }) => Math.round(width))).size).toBe(1);
  expect(new Set(actorCardLayout.map(({ height }) => Math.round(height))).size).toBe(1);
  expect(actorCardLayout.every(({ scrollHeight, clientHeight }) => scrollHeight <= clientHeight)).toBeTruthy();
  expect(new Set(actorCardLayout.map(({ countColor }) => countColor)).size).toBe(1);
  const visibleCards = await page.locator(".people-stats-actors .people-stats-rail").evaluate((rail) => {
    const first = rail.querySelector(".person-stat-card");
    return rail.clientWidth / first.getBoundingClientRect().width;
  });
  // With the outer section shell gone, the rail deliberately uses the full
  // viewport while retaining its comfortable side inset.
  expect(visibleCards).toBeGreaterThan(2.6);
  expect(visibleCards).toBeLessThan(3);
  const directorCardMetrics = await page.locator(".people-stats-directors .person-stat-card").evaluateAll((cards) => cards.map((card) => ({ scrollHeight: card.scrollHeight, clientHeight: card.clientHeight })));
  expect(directorCardMetrics.every(({ scrollHeight, clientHeight }) => scrollHeight <= clientHeight), JSON.stringify(directorCardMetrics)).toBeTruthy();
  await page.setViewportSize({ width: 430, height: 844 });
  const wideVisibleCards = await page.locator(".people-stats-actors .people-stats-rail").evaluate((rail) => {
    const first = rail.querySelector(".person-stat-card");
    return rail.clientWidth / first.getBoundingClientRect().width;
  });
  expect(wideVisibleCards).toBeGreaterThan(2.6);
  expect(wideVisibleCards).toBeLessThan(3);
  const squarePhoto = page.locator(".people-stats-actors img[data-person-photo]").nth(2);
  await squarePhoto.scrollIntoViewIfNeeded();
  await expect(squarePhoto).toHaveClass(/ready/);
  expect(await squarePhoto.evaluate((photo) => ({ objectFit: getComputedStyle(photo).objectFit, safeFit: photo.classList.contains("person-photo-safe-fit") }))).toEqual({ objectFit: "cover", safeFit: false });
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
    const last = document.querySelector("#stats").lastElementChild?.getBoundingClientRect();
    const bar = document.querySelector("#tabbar")?.getBoundingClientRect();
    return { lastBottom: last?.bottom, barTop: bar?.top };
  });
  // Последний блок не заезжает под таб-бар и не оставляет большого пустого хвоста
  // прокрутки (иначе вернулся бы двойной нижний отступ).
  expect(bounds.lastBottom).toBeLessThanOrEqual(bounds.barTop);
  expect(bounds.barTop - bounds.lastBottom).toBeLessThanOrEqual(120);
});

test("a temporary pair-stats failure keeps the pair tab and never exposes unlinking on the profile", async ({ page }) => {
  await openStats(page, true, { pairStatsFailure: true });
  await expect(page.getByRole("tab", { name: "Мы вместе" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Разорвать пару" })).toHaveCount(0);
  await page.getByRole("tab", { name: "Мы вместе" }).click();
  await expect(page.getByRole("button", { name: "Повторить" })).toBeVisible();
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

test("notification inbox shows unread pair events and follows their deep link", async ({ page }) => {
  await openStats(page, false, { notifications: {
    unread_count: 1,
    next_before_id: null,
    items: [{ id: 42, event_type: "pair.invite.created", read: false, deep_link: "inv_test", created_at: new Date().toISOString(),
      actor: { name: "Kristina", username: "kristina", photo_url: null },
      payload: { title: "Приглашение в пару", body: "Kristina приглашает тебя отмечать фильмы вместе.", action_label: "Открыть" } }],
  } });
  await page.getByRole("button", { name: "Главная" }).click();
  await expect(page.locator("#bell-btn .dot")).toBeVisible();
  await page.getByRole("button", { name: "Уведомления" }).click();
  await expect(page.getByText("Приглашение в пару")).toBeVisible();
  await page.getByRole("button", { name: "Открыть", exact: true }).click();
  await expect(page.getByRole("heading", { name: /Вас зовёт/ })).toBeVisible();
});

test("rating notification uses a star and opens the exact film from row or action", async ({ page }) => {
  let readCalls = 0;
  const notification = {
    id: 77,
    event_type: "pair.film.rated",
    read: false,
    deep_link: "movie:1",
    created_at: new Date().toISOString(),
    actor: { name: "Kristina", username: "kristina", photo_url: null },
    payload: {
      title: "Новая оценка партнёра",
      body: "Новая оценка фильма «The Last of Us» от Kristina. А теперь твоя очередь!",
      action_label: "Оценить",
    },
  };
  await openStats(page, false, {
    notifications: { unread_count: 1, next_before_id: null, items: [notification] },
    onNotificationRequest: ({ method, path }) => {
      if (method === "POST" && path === "/api/notifications/77/read") readCalls += 1;
    },
  });
  await page.getByRole("button", { name: "Главная" }).click();
  await page.getByRole("button", { name: "Уведомления" }).click();
  const row = page.locator('[data-notification-id="77"]');
  await expect(row).toHaveClass(/is-unread/);
  await expect(row.locator(".notification-event-icon path")).toHaveAttribute(
    "d",
    "m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2 7.5 14 3 9.6l6.2-.9L12 3Z",
  );
  await row.click();
  await expect.poll(() => readCalls).toBe(1);
  await expect(page.getByRole("heading", { name: "The Last of Us" })).toBeVisible();
  await page.locator("#d-back-top").click();
  await expect(page.getByRole("heading", { name: "Уведомления" })).toBeVisible();
  await expect(page.locator('[data-notification-id="77"]')).toHaveClass(/is-read/);

  // The action button follows the same strict movie deep link.
  await page.getByRole("button", { name: "Оценить", exact: true }).click();
  // It is already read, so reopening must not emit a redundant read request.
  await expect.poll(() => readCalls).toBe(1);
  await expect(page.getByRole("heading", { name: "The Last of Us" })).toBeVisible();
});

test("notification filters use real categories, preserve five-tab navigation, and stay mobile safe", async ({ page }) => {
  const items = [
    { id: 91, event_type: "pair.invite.accepted", read: false, deep_link: "stats", created_at: new Date().toISOString(),
      payload: { title: "Теперь вы в паре", body: "Общая статистика уже доступна.", action_label: "Статистика" } },
    { id: 90, event_type: "pair.film.rated", read: false, deep_link: "movie:1", created_at: new Date().toISOString(),
      payload: { title: "Новая оценка партнёра", body: "Теперь твоя очередь.", action_label: "Оценить" } },
    { id: 89, event_type: "account.security", read: false, deep_link: "", created_at: new Date().toISOString(),
      payload: { title: "Системное сообщение", body: "Важное обновление.", action_label: "" } },
  ];
  const requestedCategories = [];
  await openStats(page, false, {
    notifications: ({ requestUrl }) => {
      const category = requestUrl.searchParams.get("category") || "all";
      if (requestUrl.searchParams.has("category")) requestedCategories.push(category);
      const filtered = category === "all" ? items : items.filter(item => (
        category === "films" ? item.event_type === "pair.film.rated"
          : category === "pair" ? item.event_type.startsWith("pair.") && item.event_type !== "pair.film.rated"
            : !item.event_type.startsWith("pair.")
      ));
      return {
        items: filtered,
        unread_count: 3,
        unread_by_category: { all: 3, pair: 1, films: 1, system: 1 },
        next_before_id: null,
      };
    },
  });
  await page.getByRole("button", { name: "Главная" }).click();
  await page.getByRole("button", { name: "Уведомления" }).click();
  await expect(page.locator(".notification-card")).toHaveCount(3);
  await expect(page.locator(".notification-filter")).toHaveCount(4);
  await expect(page.locator("#tabbar button")).toHaveCount(5);
  await page.getByRole("button", { name: /Фильмы/ }).click();
  await expect(page.locator(".notification-card")).toHaveCount(1);
  await expect(page.getByText("Новая оценка партнёра")).toBeVisible();
  expect(requestedCategories).toContain("films");

  for (const viewport of [{ width: 320, height: 568 }, { width: 430, height: 932 }]) {
    await page.setViewportSize(viewport);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    const navOverlap = await page.evaluate(() => {
      window.scrollTo(0, document.body.scrollHeight);
      const nav = document.getElementById("tabbar")?.getBoundingClientRect();
      const lastCard = [...document.querySelectorAll(".notification-card")].at(-1)?.getBoundingClientRect();
      return nav && lastCard ? lastCard.bottom <= nav.top : true;
    });
    expect(navOverlap).toBeTruthy();
  }
});

test("notification pagination keeps the active filter and appends without replacing cards", async ({ page }) => {
  const requests = [];
  const makeItem = id => ({
    id, event_type: "pair.film.rated", read: true, deep_link: "movie:1",
    created_at: new Date().toISOString(),
    payload: { title: `Оценка ${id}`, body: "Фильм оценён.", action_label: "Открыть" },
  });
  await openStats(page, false, {
    notifications: ({ requestUrl }) => {
      const category = requestUrl.searchParams.get("category") || "all";
      const before = requestUrl.searchParams.get("before_id");
      if (requestUrl.searchParams.has("category")) requests.push({ category, before });
      return before
        ? { items: [makeItem(98)], unread_count: 0, unread_by_category: { all: 0, pair: 0, films: 0, system: 0 }, next_before_id: null }
        : { items: [makeItem(100), makeItem(99)], unread_count: 0, unread_by_category: { all: 0, pair: 0, films: 0, system: 0 }, next_before_id: 99 };
    },
  });
  await page.getByRole("button", { name: "Главная" }).click();
  await page.getByRole("button", { name: "Уведомления" }).click();
  await page.getByRole("button", { name: /Фильмы/ }).click();
  await expect(page.locator(".notification-card")).toHaveCount(2);
  await page.getByRole("button", { name: "Показать ещё" }).click();
  await expect(page.locator(".notification-card")).toHaveCount(3);
  expect(requests.at(-1)).toEqual({ category: "films", before: "99" });
});

test("mark all read is optimistic and sends one request", async ({ page }) => {
  let readAllCalls = 0;
  await openStats(page, false, {
    notifications: {
      unread_count: 1,
      unread_by_category: { all: 1, pair: 1, films: 0, system: 0 },
      next_before_id: null,
      items: [{ id: 111, event_type: "pair.invite.accepted", read: false, deep_link: "stats",
        created_at: new Date().toISOString(),
        payload: { title: "Теперь вы в паре", body: "Статистика готова.", action_label: "Статистика" } }],
    },
    onNotificationRequest: ({ method, path }) => {
      if (method === "POST" && path === "/api/notifications/read-all") readAllCalls += 1;
    },
  });
  await page.getByRole("button", { name: "Главная" }).click();
  await page.getByRole("button", { name: "Уведомления" }).click();
  await page.getByRole("button", { name: "Прочитать все" }).click();
  await expect(page.locator('[data-notification-id="111"]')).toHaveClass(/is-read/);
  await expect(page.getByRole("button", { name: "Прочитать все" })).toBeDisabled();
  await expect.poll(() => readAllCalls).toBe(1);
});

test("malformed movie notification links fall back to Home", async ({ page }) => {
  await openStats(page, false, { notifications: {
    unread_count: 1,
    next_before_id: null,
    items: [{
      id: 78,
      event_type: "pair.film.rated",
      read: false,
      deep_link: "movie:javascript:alert(1)",
      created_at: new Date().toISOString(),
      actor: { name: "Kristina", username: "kristina", photo_url: null },
      payload: { title: "Новая оценка партнёра", body: "Оцени тоже.", action_label: "Оценить" },
    }],
  } });
  await page.getByRole("button", { name: "Главная" }).click();
  await page.getByRole("button", { name: "Уведомления" }).click();
  await page.locator('[data-notification-id="78"]').click();
  await expect(page.getByRole("heading", { name: "Привет, Denys" })).toBeVisible();
});

test("notification refresh binds once and activated visibility updates the bell", async ({ page }) => {
  let notificationReads = 0;
  let unread = 0;
  await openStats(page, false, {
    notifications: () => {
      notificationReads += 1;
      return { items: [], unread_count: unread, next_before_id: null };
    },
  });
  await page.getByRole("button", { name: "Главная" }).click();
  await expect.poll(() => notificationReads).toBeGreaterThan(0);
  expect(await page.evaluate(() => (window.__tgEventHandlers.activated || []).length)).toBe(1);

  // Rerendering Home must not install a second Telegram listener or timer.
  await page.getByRole("button", { name: "Статистика" }).click();
  await page.getByRole("button", { name: "Главная" }).click();
  expect(await page.evaluate(() => (window.__tgEventHandlers.activated || []).length)).toBe(1);

  unread = 1;
  const readsBeforeActivation = notificationReads;
  await page.evaluate(() => {
    for (const handler of window.__tgEventHandlers.activated || []) handler();
  });
  await expect.poll(() => notificationReads).toBeGreaterThan(readsBeforeActivation);
  await expect(page.locator("#bell-btn .dot")).toBeVisible();

  const readsBeforeVisibility = notificationReads;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => notificationReads).toBeGreaterThan(readsBeforeVisibility);
});

test("settings persists the single Telegram notification preference and language", async ({ page }) => {
  await openStats(page, false);
  await page.getByRole("button", { name: "Настройки" }).click();
  await expect(page.getByRole("heading", { name: "Настройки" })).toBeVisible();
  const toggle = page.getByRole("switch", { name: "Уведомления" });
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await expect(page.getByText("События пары от бота Addict Film · Выключены")).toBeVisible();
  await expect(page.getByRole("switch")).toHaveCount(1);
  await expect(page.getByText("Локальные напоминания на этом устройстве")).toHaveCount(0);
  await page.getByRole("button", { name: "English" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("switch", { name: "Notifications" })).toHaveAttribute("aria-checked", "false");
  await page.getByRole("button", { name: "Back" }).click();
  await expect(page.getByRole("heading", { name: "My movie profile" })).toBeVisible();
});

test("settings disables the Telegram switch when the bot is unavailable", async ({ page }) => {
  await openStats(page, false, { settings: { telegram_enabled: false, telegram_available: false } });
  await page.getByRole("button", { name: "Настройки" }).click();
  const toggle = page.getByRole("switch", { name: "Уведомления" });
  await expect(toggle).toBeDisabled();
  await expect(page.getByText("Бот сейчас недоступен")).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
});

test("settings uses the existing invite and paired management states", async ({ page }) => {
  let partnerState = { status: "none" };
  let partnerReads = 0;
  await openStats(page, false, {
    partnerApi: {
      current: () => { partnerReads += 1; return partnerState; },
      invite: () => {
        partnerState = { status: "invited", link: "https://t.me/addictfilmbot?startapp=inv_settings", code: "inv_settings" };
        return partnerState;
      },
    },
  });
  await page.getByRole("button", { name: "Настройки" }).click();
  await page.getByRole("button", { name: "Создать пару" }).click();
  await expect(page.getByText("Приглашение ожидает принятия")).toBeVisible();
  await expect(page.getByText("inv_settings")).toBeVisible();
  partnerState = { status: "paired", partner: { name: "Kristina", username: "kristina" } };
  await page.locator(".sub-head .back").click();
  await page.getByRole("button", { name: "Настройки" }).click();
  await expect(page.getByText("Ваша пара")).toBeVisible();
  const readsBeforeManage = partnerReads;
  await page.getByRole("button", { name: "Управление парой" }).click();
  await expect(page.getByRole("button", { name: "Разорвать пару" })).toBeVisible();
  expect(partnerReads).toBe(readsBeforeManage);
  await page.locator(".settings-pair-management-back").click();
  await expect(page.getByRole("button", { name: "Управление парой" })).toBeVisible();
});

test("pair invite stays balanced and safe across compact mobile viewports", async ({ page }) => {
  await openStats(page, false, { startParam: "inv_test" });
  await expect(page.getByRole("heading", { name: /Вас зовёт/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Принять" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Не сейчас" })).toBeVisible();
  const assertSafeInvite = async () => {
    const layout = await page.evaluate(() => {
      const card = document.querySelector(".accept").getBoundingClientRect();
      const bar = document.querySelector("#tabbar").getBoundingClientRect();
      const name = document.querySelector(".accept-inviter-name");
      const illustration = document.querySelector(".accept-illustration-img");
      return {
        overflows: document.documentElement.scrollWidth > window.innerWidth,
        cardBottom: card.bottom,
        tabTop: bar.top,
        nameOverflow: name.scrollWidth > name.clientWidth,
        illustrationLoaded: illustration.complete && illustration.naturalWidth > 0,
        illustrationRatio: illustration.naturalWidth / illustration.naturalHeight,
      };
    });
    expect(layout.overflows).toBeFalsy();
    // Keep a real gap even on a compact 320×568 Mini App viewport.  The
    // tab bar has a translucent shadow, so three CSS pixels remains visible.
    expect(layout.cardBottom).toBeLessThanOrEqual(layout.tabTop - 3);
    expect(layout.nameOverflow).toBeTruthy();
    expect(layout.illustrationLoaded).toBeTruthy();
    expect(layout.illustrationRatio).toBeGreaterThan(1.2);
    expect(layout.illustrationRatio).toBeLessThan(1.5);
  };
  await assertSafeInvite();
  await page.setViewportSize({ width: 320, height: 568 });
  await page.reload();
  await expect(page.getByRole("heading", { name: /Вас зовёт/ })).toBeVisible();
  await assertSafeInvite();
  await page.setViewportSize({ width: 430, height: 932 });
  await page.reload();
  await expect(page.getByRole("heading", { name: /Вас зовёт/ })).toBeVisible();
  await assertSafeInvite();
  await page.locator(".accept-illustration-img").evaluate((image) => image.dispatchEvent(new Event("error")));
  await expect(page.locator(".accept-illustration-fallback")).toBeVisible();
  await page.getByRole("button", { name: "Не сейчас" }).click();
  await expect(page.getByRole("button", { name: "Статистика" })).toBeVisible();
});

test("returning from a film keeps list scroll and loaded cards intact", async ({ page }) => {
  // Изолированный харнесс: список «Смотрел» на 24 фильма + деталь любого из них.
  const films = Array.from({ length: 24 }, (_, i) => ({
    id: 100 + i, title: `Film ${i + 1}`, year: `${2000 + i}`, poster_url: null, my_rating: (i % 10) + 1,
  }));
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.Telegram = { WebApp: {
      initData: "test", initDataUnsafe: { user: { id: 1, first_name: "D" } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {}, disableVerticalSwipes() {},
      HapticFeedback: { impactOccurred() {}, notificationOccurred() {} },
      showConfirm(_t, cb) { cb(true); }, showAlert() {}, openTelegramLink() {},
    } };
  });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = path === "/api/me" ? { id: 1, label: "D", role: null }
      : path === "/api/movies" ? { items: films, total: films.length }
        : /^\/api\/movie\/\d+$/.test(path)
          ? { id: +path.split("/").pop(), title: "Film", year: "2010", poster_url: null, genres: "Drama", status: "watched", my_rating: 5 }
          : { items: [], total: 0 };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Смотрел" }).click();
  await expect(page.locator("#list .poster")).toHaveCount(24);

  await page.evaluate(() => window.scrollTo(0, 1200));
  const savedY = await page.evaluate(() => window.scrollY);
  expect(savedY).toBeGreaterThan(400);

  // JS-клик, чтобы Playwright не проскроллил карточку в вид и не сбил позицию.
  await page.locator("#list .poster").nth(9).evaluate(el => el.click());
  await expect(page.locator(".detail-v2")).toBeVisible();
  await page.locator("#d-back-top").click();

  // Список вернулся тем же (без повторной загрузки — нет скелетонов), скролл на месте.
  await expect(page.locator("#list .poster")).toHaveCount(24);
  await expect(page.locator("#list .sk")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(savedY);
});

// ── Крупные (featured) подборки ─────────────────────────────────────────────
function featuredHarness(page, { isAdmin, collections }) {
  page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  page.addInitScript(() => {
    window.Telegram = { WebApp: {
      initData: "test", initDataUnsafe: { user: { id: 1, first_name: "D" } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {}, disableVerticalSwipes() {},
      HapticFeedback: { impactOccurred() {}, notificationOccurred() {}, selectionChanged() {} },
      showConfirm(_t, cb) { cb(true); }, showAlert() {}, openTelegramLink() {},
    } };
    try { localStorage.setItem("addictfilm.adminMode.enabled", "true"); } catch (e) { /* ignore */ }
  });
  return page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = path === "/api/me" ? { id: 1, label: "D", role: isAdmin ? "admin" : null }
      : path === "/api/me/capabilities"
        ? (isAdmin
          ? { is_admin: true, admin_role: "admin", capabilities: ["collections.read", "collections.write", "content.publish"] }
          : { is_admin: false, admin_role: null, capabilities: [] })
      : (path === "/api/collections" || path === "/api/admin/collections") ? { items: collections }
      : path.startsWith("/api/browse") ? { items: [] }
      : path === "/api/genres" ? { items: [] }
      : { items: [] };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
  });
}

const FEATURED = {
  id: 501, title: "Большая подборка", description: "Кино на вечер", display_type: "featured",
  backdrop: "https://img/backdrop.jpg", cover: null, film_count: 9, status: "published",
};
const STANDARD = {
  id: 502, title: "Обычная", description: null, display_type: "standard",
  cover: "https://img/cover.jpg", backdrop: "https://img/cover.jpg", film_count: 4, status: "published",
};

test("featured collection renders as a large card and standard stays in the rail", async ({ page }) => {
  await featuredHarness(page, { isAdmin: false, collections: [FEATURED, STANDARD] });
  await page.goto("/");
  const card = page.locator(".featured-card");
  await expect(card).toHaveCount(1);
  await expect(card.locator(".featured-card-title")).toHaveText("Большая подборка");
  await expect(card.locator(".featured-card-desc")).toHaveText("Кино на вечер");
  await expect(card.locator(".featured-card-meta")).toContainText("9");
  // Одна подборка живёт ровно в одном формате.
  await expect(page.locator("#rail-coll .poster")).toHaveCount(1);
  await expect(page.locator("#rail-coll")).toContainText("Обычная");
  await expect(page.locator("#rail-coll")).not.toContainText("Большая подборка");
  // Обычный пользователь не видит админских контролов.
  await expect(page.locator(".featured-admin")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("no featured area is rendered when there are no featured collections", async ({ page }) => {
  await featuredHarness(page, { isAdmin: false, collections: [STANDARD] });
  await page.goto("/");
  await expect(page.locator("#rail-coll .poster")).toHaveCount(1);
  // Ни секции, ни заголовка, ни пустого места.
  await expect(page.locator("#sec-featured")).toHaveCount(0);
  await expect(page.locator(".featured-card")).toHaveCount(0);
});

test("admin sees featured controls and long titles stay clamped", async ({ page }) => {
  const longTitle = { ...FEATURED, title: "Очень длинное название подборки ".repeat(4) };
  await featuredHarness(page, { isAdmin: true, collections: [longTitle] });
  await page.goto("/");
  await expect(page.locator(".featured-admin")).toHaveCount(1);
  await expect(page.locator(".featured-admin .admin-badge")).toHaveText("Опубликовано");
  // Заголовок обрезается двумя строками и не ломает карточку.
  const box = await page.locator(".featured-card").boundingBox();
  const titleBox = await page.locator(".featured-card-title").boundingBox();
  expect(titleBox.height).toBeLessThan(box.height / 2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("featured card survives a broken backdrop image", async ({ page }) => {
  await featuredHarness(page, { isAdmin: false, collections: [FEATURED] });
  await page.goto("/");
  await expect(page.locator(".featured-card")).toHaveCount(1);
  // Картинку в харнессе никто не отдаёт: приложение могло уже снять её своим
  // обработчиком ошибки. Гасим её принудительно, только если она ещё в DOM, —
  // иначе тест гоняется с самим приложением (так он и флакал в CI).
  await page.evaluate(() => {
    document.querySelector(".featured-card-img")?.dispatchEvent(new Event("error"));
  });
  // Главное: без картинки карточка остаётся читаемой и кликабельной.
  await expect(page.locator(".featured-card-title")).toBeVisible();
  await expect(page.locator(".featured-card-meta")).toBeVisible();
  await expect(page.locator(".featured-card-scrim")).toBeVisible();
});

// ── Единый редактор подборки: состояние переживает переход к поиску фильмов ──
function editorHarness(page, { collections = [], onCreate = null } = {}) {
  page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  page.addInitScript(() => {
    window.__posts = [];
    window.Telegram = { WebApp: {
      initData: "test", initDataUnsafe: { user: { id: 1, first_name: "D" } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {}, disableVerticalSwipes() {},
      HapticFeedback: { impactOccurred() {}, notificationOccurred() {}, selectionChanged() {} },
      showConfirm(_t, cb) { cb(true); }, showAlert() {}, openTelegramLink() {},
    } };
    try { localStorage.setItem("addictfilm.adminMode.enabled", "true"); } catch (e) { /* ignore */ }
  });
  return page.route("**/api/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET") {
      await page.evaluate(([p, b]) => window.__posts.push({ path: p, body: b }),
        [path, request.postData() || ""]);
    }
    if (path === "/api/admin/films/resolve") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        id: 77, title: "Донни Дарко", year: "2001",
        poster_url: "https://img/p.jpg", backdrop_url: "https://img/b.jpg" }) });
      return;
    }
    if (path === "/api/admin/collections" && request.method() === "POST") {
      onCreate?.();
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ id: 900 }) });
      return;
    }
    const json = path === "/api/me" ? { id: 1, label: "D", role: "admin" }
      : path === "/api/me/capabilities"
        ? { is_admin: true, admin_role: "admin", capabilities: ["collections.read", "collections.write", "content.publish"] }
      : path === "/api/search" ? { items: [{ src: "i", ref: "tt0246578", title: "Донни Дарко", year: "2001", poster: null, rating: 8 }] }
      : (path === "/api/collections" || path === "/api/admin/collections") ? { items: collections }
      : path === "/api/admin/collections/900" ? { id: 900, title: "Рапунцель", description: "Мультфильмы на вечер", display_type: "featured", status: "draft", version: 1, items: [] }
      : path.startsWith("/api/browse") ? { items: [] }
      : path === "/api/genres" ? { items: [] }
      : { items: [] };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
  });
}

test("plus opens the editor directly and creates nothing until saved", async ({ page }) => {
  await editorHarness(page);
  await page.goto("/");
  await page.locator("#coll-add-home").click();
  // Сразу полный редактор: промежуточной формы с одним полем названия больше нет.
  await expect(page.locator(".collection-editor")).toBeVisible();
  await expect(page.locator("#ed-title")).toBeVisible();
  await expect(page.locator("#ed-publish")).toBeVisible();
  await expect(page.locator("#coll-title-input")).toHaveCount(0);
  // И ни одной записи в базе до явного сохранения.
  expect(await page.evaluate(() => window.__posts.filter(p => p.path === "/api/admin/collections").length)).toBe(0);
  // Глобальные плавающие элементы не перекрывают редактор.
  await expect(page.locator("#tabbar")).toBeHidden();
  // Индикатор режима остаётся в DOM, но не должен перекрывать редактор.
  await expect(page.locator("#admin-indicator")).toBeHidden();
});

test("editor keeps title, description, format and films across the film picker", async ({ page }) => {
  await editorHarness(page);
  await page.goto("/");
  await page.locator("#coll-add-home").click();

  await page.locator("#ed-title").fill("Рапунцель");
  await page.locator("#ed-desc").fill("Мультфильмы на вечер");
  await page.locator('[data-display="featured"]').click();

  await page.locator("#ed-add-film").click();
  await page.locator("#si").fill("донни");
  await page.locator("#sr .poster").first().click();

  // Вернулись в ТОТ ЖЕ редактор: ничего не сбросилось.
  await expect(page.locator("#ed-title")).toHaveValue("Рапунцель");
  await expect(page.locator("#ed-desc")).toHaveValue("Мультфильмы на вечер");
  await expect(page.locator('[data-display="featured"]')).toHaveClass(/on/);
  await expect(page.locator(".admin-item")).toHaveCount(1);
  await expect(page.locator(".admin-item-copy b")).toHaveText("Донни Дарко");
  // Превью отражает введённое.
  await expect(page.locator("#ed-preview .featured-card-title")).toHaveText("Рапунцель");
});

test("first save creates exactly one collection with its films", async ({ page }) => {
  let creates = 0;
  await editorHarness(page, { onCreate: () => { creates += 1; } });
  await page.goto("/");
  await page.locator("#coll-add-home").click();
  await page.locator("#ed-title").fill("Рапунцель");
  await page.locator("#ed-add-film").click();
  await page.locator("#si").fill("донни");
  await page.locator("#sr .poster").first().click();

  await page.locator("#ed-save").click();
  await expect(page.locator("#ed-badge")).toHaveText("Черновик");
  expect(creates).toBe(1);
  const created = await page.evaluate(() =>
    window.__posts.filter(p => p.path === "/api/admin/collections").map(p => JSON.parse(p.body)));
  expect(created).toHaveLength(1);
  expect(created[0].ordered_film_ids).toEqual([77]);
  expect(created[0].title).toBe("Рапунцель");
  // После сохранения остаёмся в редакторе, а не улетаем на главную.
  await expect(page.locator(".collection-editor")).toBeVisible();
});

test("empty title blocks save and shows a field error", async ({ page }) => {
  await editorHarness(page);
  await page.goto("/");
  await page.locator("#coll-add-home").click();
  await page.locator("#ed-save").click();
  await expect(page.locator("#ed-err-title")).toBeVisible();
  expect(await page.evaluate(() => window.__posts.filter(p => p.path === "/api/admin/collections").length)).toBe(0);
});

test("ordinary user sees no collection creation action", async ({ page }) => {
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.Telegram = { WebApp: {
      initData: "test", initDataUnsafe: { user: { id: 2, first_name: "U" } },
      ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {}, disableVerticalSwipes() {},
      HapticFeedback: { impactOccurred() {}, notificationOccurred() {}, selectionChanged() {} },
      showConfirm(_t, cb) { cb(true); }, showAlert() {}, openTelegramLink() {},
    } };
  });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = path === "/api/me" ? { id: 2, label: "U", role: null }
      : path === "/api/me/capabilities" ? { is_admin: false, admin_role: null, capabilities: [] }
      : { items: [] };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(json) });
  });
  await page.goto("/");
  await expect(page.locator("#coll-add-home")).toHaveCount(0);
  await expect(page.locator("#featured-add")).toHaveCount(0);
});

test("pair sections are open: outer frame removed, inner cards intact", async ({ page }) => {
  await openStats(page, true);
  await page.getByRole("tab", { name: "Мы вместе" }).click();
  await expect(page.locator(".pair-favorites-showcase")).toBeVisible();

  const style = (selector, prop) => page.locator(selector).first()
    .evaluate((el, p) => getComputedStyle(el)[p], prop);

  // Внешняя рамка секций убрана — они лежат на фоне страницы, как «Актёры».
  for (const section of [".pair-favorites-showcase", ".pair-differences-showcase"]) {
    expect(await style(section, "backgroundColor")).toBe("rgba(0, 0, 0, 0)");
    expect(await style(section, "borderTopLeftRadius")).toBe("0px");
    expect(await style(section, "borderLeftWidth")).toBe("0px");
  }
  // А внутренние карточки свои фон и границу сохранили.
  expect(await style(".pair-favorite-card", "backgroundColor")).not.toBe("rgba(0, 0, 0, 0)");
  expect(await style(".pair-difference-card", "backgroundColor")).not.toBe("rgba(0, 0, 0, 0)");
  expect(await style(".pair-difference-card", "borderLeftWidth")).not.toBe("0px");

  // Заголовки выровнены с «Актёрами» по левому краю.
  const left = (selector) => page.locator(selector).first()
    .evaluate(el => Math.round(el.getBoundingClientRect().left));
  const actors = await left(".people-stats-head h2");
  expect(await left(".pair-favorites-showcase .pair-showcase-head h2")).toBe(actors);
  expect(await left(".pair-differences-showcase .pair-showcase-head h2")).toBe(actors);

  // Горизонтальная прокрутка и отсутствие переполнения страницы.
  expect(await page.locator(".pair-favorites-rail").evaluate(el => el.scrollWidth > el.clientWidth)).toBeTruthy();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("profile shows four metric cards with neutral outline icons and no duplicate strip", async ({ page }) => {
  await openStats(page);

  // Дублирующая строка «46 просмотрено | 7.2 средняя | 80 часов» удалена.
  await expect(page.locator(".profile-facts")).toHaveCount(0);

  // Осталась одна сводка — четыре плитки с прежними значениями.
  const tiles = page.locator(".stats-grid .tile");
  await expect(tiles).toHaveCount(4);
  await expect(tiles.nth(0).locator(".tile-val")).toHaveText("46");
  await expect(tiles.nth(1).locator(".tile-val")).toHaveText("31");
  await expect(tiles.nth(2).locator(".tile-val")).toHaveText("7.2");
  await expect(tiles.nth(3).locator(".tile-val")).toHaveText("80");

  // У каждой плитки — иконка из общего outline-реестра, серая и не залитая.
  await expect(page.locator(".stats-grid .tile-icon svg")).toHaveCount(4);
  const iconStyle = await page.locator(".tile-icon").first().evaluate(el => {
    const svg = el.querySelector("svg");
    return { color: getComputedStyle(el).color, stroke: svg.getAttribute("stroke"), fill: svg.getAttribute("fill") };
  });
  expect(iconStyle.stroke).toBe("currentColor");
  expect(iconStyle.fill).toBe("none");
  expect(iconStyle.color).not.toContain("124, 58");   // не фиолетовый по умолчанию

  // Шестерёнка — outline-иконка с достаточной целью нажатия, ведёт в настройки.
  const gear = page.locator("[data-stats-settings]");
  await expect(gear.locator("svg")).toHaveCount(1);
  const box = await gear.boundingBox();
  expect(box.width).toBeGreaterThanOrEqual(44);
  expect(box.height).toBeGreaterThanOrEqual(44);
  await gear.click();
  await expect(page.locator(".settings-page")).toBeVisible();
});

test("metric cards stay readable and overflow-free at 320px", async ({ page }) => {
  await openStats(page);
  await page.setViewportSize({ width: 320, height: 640 });
  await expect(page.locator(".stats-grid .tile")).toHaveCount(4);
  const clipped = await page.locator(".stats-grid").evaluate(grid =>
    [...grid.querySelectorAll(".tile-label")].filter(l => l.scrollWidth > l.clientWidth + 1).length);
  expect(clipped).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("app initializes outside Telegram without throwing", async ({ page }) => {
  // Вне Telegram window.Telegram нет вовсе. Раньше api() читал tg.initData и
  // падал ДО отрисовки экрана «нужен Telegram» — вместо понятного сообщения
  // пользователь видел пустую страницу.
  const errors = [];
  page.on("pageerror", error => errors.push(String(error)));
  // Глушим сам SDK: без него window.Telegram не появляется вовсе — это и есть
  // «открыли ссылку в обычном браузере».
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.route("**/api/**", route => route.fulfill({
    status: 401, contentType: "application/json",
    body: JSON.stringify({ detail: "Требуется Telegram" }),
  }));
  await page.goto("/");
  await page.waitForTimeout(600);
  expect(errors.filter(text => /Cannot read propert|null|undefined/i.test(text))).toEqual([]);
  expect(await page.evaluate(() => typeof window.Telegram)).toBe("undefined");
});

test("inside Telegram the init data header is still sent", async ({ page }) => {
  const headers = [];
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(() => {
    window.Telegram = { WebApp: {
      initData: "query_id=test&user=%7B%22id%22%3A1%7D", initDataUnsafe: { user: { id: 1 } },
      ready() {}, expand() {}, colorScheme: "dark", themeParams: {},
      HapticFeedback: { impactOccurred() {}, notificationOccurred() {} },
      showConfirm(_t, cb) { cb(true); }, showAlert() {}, openTelegramLink() {},
      onEvent() {}, offEvent() {}, setHeaderColor() {}, setBackgroundColor() {},
      BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
    } };
  });
  await page.route("**/api/**", route => {
    headers.push(route.request().headers()["x-init-data"]);
    return route.fulfill({ status: 401, contentType: "application/json", body: "{}" });
  });
  await page.goto("/");
  await page.waitForTimeout(600);
  expect(headers.length).toBeGreaterThan(0);
  expect(headers[0]).toContain("query_id=test");
});


test("wishlist roulette is a separate mode from smart random", async ({ page }) => {
  await openStats(page);
  await page.getByRole("button", { name: "Подбор" }).click();
  // Два разных режима с разными обещаниями: рулетка по своему списку и подбор
  // из каталога. Раньше оба назывались «случайный фильм».
  await expect(page.getByRole("button", { name: /Случайный из «Хочу»/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Умный случайный фильм/ })).toBeVisible();

  await page.getByRole("button", { name: /Случайный из «Хочу»/ }).click();
  await expect(page.locator(".recommendation-film-copy h2", { hasText: "Wishlist Pick" })).toBeVisible();
  await expect(page.getByText("Случайный из «Хочу»", { exact: true })).toBeVisible();
});


test("session from an older engine asks to restart instead of mixing semantics", async ({ page }) => {
  await openStats(page, false, { replaceStatus: 409 });
  await page.getByRole("button", { name: "Подбор" }).click();
  await page.getByRole("button", { name: "Подбор по настроению" }).click();
  for (let index = 0; index < 8; index += 1) await page.locator(".picker-option").first().click();
  await expect(page.locator(".recommendation-film")).toHaveCount(3);

  await page.getByRole("button", { name: "Не предлагать" }).first().click();
  // Досчитывать старую подборку новой логикой нельзя — предлагаем пройти заново.
  await expect(page.locator(".picker-question h1")).toBeVisible();
});


const V2_META = { engine_version: "recommendation-v2", engine_preview: true };

async function runQuiz(page) {
  await page.getByRole("button", { name: "Подбор" }).click();
  await page.getByRole("button", { name: "Подбор по настроению" }).click();
  for (let index = 0; index < 8; index += 1) await page.locator(".picker-option").first().click();
}

test("admin V2 preview badge is shown during the quiz and on the results", async ({ page }) => {
  await openStats(page, false, { engineMeta: V2_META });
  await page.getByRole("button", { name: "Подбор" }).click();
  await page.getByRole("button", { name: "Подбор по настроению" }).click();
  await expect(page.locator(".picker-preview-badge")).toHaveText("V2 PREVIEW");
  for (let index = 0; index < 8; index += 1) await page.locator(".picker-option").first().click();
  await expect(page.locator(".recommendation-film")).toHaveCount(3);
  await expect(page.locator(".picker-preview-badge")).toBeVisible();
});

test("ordinary V1 response shows no preview badge", async ({ page }) => {
  await openStats(page);
  await runQuiz(page);
  await expect(page.locator(".recommendation-film")).toHaveCount(3);
  await expect(page.locator(".picker-preview-badge")).toHaveCount(0);
});

test("engine version alone does not enable the badge", async ({ page }) => {
  // Значок зависит ТОЛЬКО от engine_preview === true: версия сама по себе не
  // делает пользователя администратором.
  await openStats(page, false, { engineMeta: { engine_version: "recommendation-v2" } });
  await runQuiz(page);
  await expect(page.locator(".picker-preview-badge")).toHaveCount(0);
});

test("leaving the quiz clears a stale preview badge", async ({ page }) => {
  await openStats(page, false, { engineMeta: V2_META });
  await runQuiz(page);
  await expect(page.locator(".picker-preview-badge")).toBeVisible();
  await page.locator(".sub-head .back").click();
  await expect(page.getByRole("heading", { name: "Что посмотреть?" })).toBeVisible();
  await expect(page.locator(".picker-preview-badge")).toHaveCount(0);
});

test("preview badge does not overflow a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await openStats(page, false, { engineMeta: V2_META });
  await page.getByRole("button", { name: "Подбор" }).click();
  await page.getByRole("button", { name: "Подбор по настроению" }).click();
  await expect(page.locator(".picker-preview-badge")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});
