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
  await page.route("https://telegram.org/js/telegram-web-app.js", route => route.fulfill({ body: "" }));
  await page.addInitScript(({ notification, startParam }) => {
    window.__verticalSwipeDisableCalls = 0;
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
    const path = new URL(route.request().url()).pathname;
    const partnerApi = options.partnerApi;
    if (path === "/api/partner/stats" && options.pairStatsFailure) {
      await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "stats unavailable" }) });
      return;
    }
    const json = path === "/api/me"
      ? { id: 1, label: "Denys", username: "denys", role: null }
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
        ? (options.notifications || { items: [], unread_count: 0, next_before_id: null })
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
    const last = document.querySelector(".year-card")?.getBoundingClientRect();
    const bar = document.querySelector("#tabbar")?.getBoundingClientRect();
    return { lastBottom: last?.bottom, barTop: bar?.top };
  });
  expect(bounds.lastBottom).toBeLessThanOrEqual(bounds.barTop);
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
