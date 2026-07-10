// Личный кинотрекер: списки, оценки, статистика и необязательный режим пары.
// Все запросы авторизуются подписью Telegram initData на бэкенде.

const tg = window.Telegram?.WebApp ?? null;
const screen = document.getElementById("screen");
let me = null;

async function api(path, opts = {}) {
  if (!tg) throw new Error("Открой приложение через Telegram.");
  const response = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Init-Data": tg.initData,
      ...(opts.headers || {}),
    },
  });
  if (!response.ok) {
    throw new Error((await response.json().catch(() => ({}))).detail || response.status);
  }
  return response.json();
}

const STATUS_MAP = { want: "want_to_watch", watched: "watched", top: "top" };

async function showList(tab) {
  screen.innerHTML = `<div class="hint">Загружаю…</div>`;
  try {
    const { items } = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=60`);
    if (!items.length) {
      const text = tab === "want" ? "Добавь фильм через поиск 🔍" : "Здесь пока пусто.";
      screen.innerHTML = `<div class="hint">${text}</div>`;
      return;
    }
    const grid = document.createElement("div");
    grid.className = "grid";
    for (const movie of items) {
      const poster = posterImage(movie.poster_url, "", true);
      const card = document.createElement("div");
      card.className = "poster-card";
      card.innerHTML = `
        ${poster || `<div class="no-poster">${esc(movie.title)}</div>`}
        ${movie.my_rating ? `<span class="badge">⭐ ${esc(movie.my_rating)}</span>` : ""}
        <div class="title">${esc(movie.title)} (${esc(movie.year || "")})</div>`;
      card.onclick = () => { void showDetail(movie.id); };
      grid.appendChild(card);
    }
    screen.replaceChildren(grid);
  } catch (error) {
    showScreenError(error);
  }
}

async function showDetail(id) {
  screen.innerHTML = `<div class="hint">Загружаю…</div>`;
  try {
    const movie = await api(`/api/movie/${id}`);
    const myRating = movie.ratings[me.id];
    const rateButtons = Array.from({ length: 10 }, (_, index) => index + 1)
      .map(number => `<button data-n="${number}" class="${number === myRating ? "mine" : ""}">${number}</button>`)
      .join("");
    screen.innerHTML = `
      <div class="detail">
        ${posterImage(movie.poster_url, "hero")}
        <h2>${esc(movie.title)}${movie.year ? ` · ${esc(movie.year)}` : ""}</h2>
        <div class="meta">
          ${esc(movie.genres || "")}${movie.runtime ? ` · ${esc(movie.runtime)}` : ""}
          ${movie.directors ? `<br>реж. ${esc(movie.directors)}` : ""}
          ${ratingLine(movie)}
        </div>
        ${movie.plot ? `<p class="plot">${esc(movie.plot)}</p>` : ""}
        <div class="rate-row">${rateButtons}</div>
        <div class="actions">
          <button id="toggle-status">${movie.status === "watched" ? "↩️ В «Хочу»" : "✅ Посмотрел"}</button>
          <button id="del" class="danger">🗑 Убрать из списка</button>
        </div>
      </div>`;

    screen.querySelectorAll(".rate-row button").forEach(button => button.onclick = async () => {
      try {
        tg.HapticFeedback?.impactOccurred("light");
        await api(`/api/movie/${id}/rate`, {
          method: "POST", body: JSON.stringify({ rating: Number(button.dataset.n) }),
        });
        await showDetail(id);
      } catch (error) {
        showAlertError(error);
      }
    });
    document.getElementById("toggle-status").onclick = async () => {
      try {
        const status = movie.status === "watched" ? "want_to_watch" : "watched";
        await api(`/api/movie/${id}/status`, { method: "POST", body: JSON.stringify({ status }) });
        await showDetail(id);
      } catch (error) {
        showAlertError(error);
      }
    };
    document.getElementById("del").onclick = () => {
      tg.showConfirm(`Убрать «${movie.title}» только из твоего списка?`, async confirmed => {
        if (!confirmed) return;
        try {
          await api(`/api/movie/${id}`, { method: "DELETE" });
          setActive("want");
          await showList("want");
        } catch (error) {
          showAlertError(error);
        }
      });
    };
  } catch (error) {
    showScreenError(error);
  }
}

function showSearch() {
  screen.innerHTML = `
    <input id="search-input" placeholder="Название фильма или сериала…" autofocus>
    <div id="search-results"></div>`;
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  let timer;
  let requestVersion = 0;
  input.oninput = () => {
    clearTimeout(timer);
    const version = ++requestVersion;
    timer = setTimeout(async () => {
      const query = input.value.trim();
      if (query.length < 2) {
        results.innerHTML = "";
        return;
      }
      results.innerHTML = `<div class="hint">Ищу…</div>`;
      try {
        const { items } = await api(`/api/search?q=${encodeURIComponent(query)}`);
        if (version !== requestVersion) return;
        if (!items.length) {
          results.innerHTML = `<div class="hint">Не найдено. Попробуй английское название или год.</div>`;
          return;
        }
        const grid = document.createElement("div");
        grid.className = "grid";
        for (const item of items) {
          const poster = posterImage(item.poster) || posterImage(item.poster_url);
          const card = document.createElement("div");
          card.className = "poster-card";
          card.innerHTML = `
            ${poster || `<div class="no-poster">${esc(item.title)}</div>`}
            ${item.rating ? `<span class="badge">⭐ ${esc(item.rating)}</span>` : ""}
            <div class="title">${esc(item.title)} (${esc(item.year || "")})</div>`;
          card.onclick = () => {
            tg.showConfirm(`Добавить «${item.title}» в твой список?`, async confirmed => {
              if (!confirmed) return;
              try {
                const result = await api("/api/add", {
                  method: "POST", body: JSON.stringify({ src: item.src, ref: item.ref }),
                });
                if (result.reason === "exists") {
                  tg.showAlert("Этот фильм уже есть в твоём списке.");
                  return;
                }
                tg.HapticFeedback?.notificationOccurred("success");
                setActive("want");
                await showList("want");
              } catch (error) {
                showAlertError(error);
              }
            });
          };
          grid.appendChild(card);
        }
        results.replaceChildren(grid);
      } catch (error) {
        if (version === requestVersion) showInlineError(results, error);
      }
    }, 400);
  };
  input.focus();
}

async function showStats() {
  screen.innerHTML = `<div class="hint">Считаю…</div>`;
  try {
    const data = await api("/api/stats");
    const personal = data.personal;
    const year = data.year;
    const pair = data.pair;
    screen.innerHTML = `
      <div class="stat-block">👤 <b>${esc(me.label)}</b><br>
        🎬 Просмотрено: <b>${personal.watched}</b> · в списке: <b>${personal.want}</b><br>
        ⏱ Экранное время: <b>${Math.floor(personal.total_runtime_min / 60)} ч</b>
        ${personal.avg_rating ? `<br>⭐ Средняя оценка: <b>${personal.avg_rating}</b>` : ""}</div>
      ${renderMetadataStats(personal)}
      ${year.count ? `<div class="stat-block">📅 Итоги ${year.year}: <b>${year.count}</b> фильмов
        ${year.best_rating ? ` · лучший рейтинг <b>${year.best_rating}</b>` : ""}</div>` : ""}
      ${pair ? renderPairStats(data.partner, pair) : `<div class="stat-block">💞 Добавь партнёра — появится общая статистика и совместимость оценок.</div>`}`;
  } catch (error) {
    showScreenError(error);
  }
}

function renderMetadataStats(stats) {
  const blocks = [];
  if (stats.top_genres_pct?.length) {
    blocks.push(`<div class="stat-block">🎭 Любимые жанры:<br>${
      stats.top_genres_pct.map(([name, percent]) => `${esc(name)} — ${percent}%`).join("<br>")}</div>`);
  }
  if (stats.top_actors?.length) {
    blocks.push(`<div class="stat-block">⭐ Часто встречаются:<br>${
      stats.top_actors.map(([name, count]) => `${esc(name)} — ${count}`).join("<br>")}</div>`);
  }
  return blocks.join("");
}

function renderPairStats(partner, pair) {
  const compatibility = pair.compatibility;
  return `
    <div class="stat-block">💞 Ты и <b>${esc(partner.label)}</b><br>
      🎬 Оба отметили просмотренными: <b>${pair.shared_watched}</b>
      ${compatibility.agreement != null ? `<br>🤝 Совместимость: <b>${compatibility.agreement}%</b>
        <br><small>по ${compatibility.count} общим оценкам</small>` : ""}
    </div>
    ${pair.top_genres_pct.length ? `<div class="stat-block">🎭 Общие жанры:<br>${
      pair.top_genres_pct.map(([name, percent]) => `${esc(name)} — ${percent}%`).join("<br>")}</div>` : ""}
    ${pair.top_actors.length ? `<div class="stat-block">⭐ Общие актёры:<br>${
      pair.top_actors.map(([name, count]) => `${esc(name)} — ${count}`).join("<br>")}</div>` : ""}
    ${pair.perfect_match ? `<div class="stat-block">🎯 Совпали во вкусе: ${esc(pair.perfect_match.title)}</div>` : ""}
    ${pair.controversial ? `<div class="stat-block">⚡ Самый спорный: ${esc(pair.controversial.title)}
      (${pair.controversial.first_rating} и ${pair.controversial.second_rating})</div>` : ""}`;
}

async function showPartner() {
  screen.innerHTML = `<div class="hint">Загружаю…</div>`;
  try {
    const data = await api("/api/partner");
    me.partner = data.partner;
    if (data.partner) {
      screen.innerHTML = `
        <div class="partner-card">
          <h2>💞 Вы в паре</h2>
          <p>Твой партнёр: <b>${esc(data.partner.label)}</b>.</p>
          <p>Личные списки остаются приватными, а в статистике видны только общие итоги.</p>
          <button id="disconnect-partner" class="danger">Отключить пару</button>
        </div>`;
      document.getElementById("disconnect-partner").onclick = () => {
        tg.showConfirm("Отключить пару? Личные фильмы и оценки сохранятся.", async confirmed => {
          if (!confirmed) return;
          try {
            await api("/api/partner", { method: "DELETE" });
            me.partner = null;
            tg.showAlert("Пара отключена.");
            await showPartner();
          } catch (error) {
            showAlertError(error);
          }
        });
      };
      return;
    }
    screen.innerHTML = `
      <div class="partner-card">
        <h2>💞 Кино вдвоём</h2>
        <p>Пригласи партнёра, чтобы увидеть совместимость оценок, общие фильмы, жанры и актёров.</p>
        <p>Личные списки по умолчанию останутся приватными.</p>
        <button id="create-invite" class="primary">Создать приглашение</button>
      </div>`;
    document.getElementById("create-invite").onclick = () => { void createPartnerInvite(); };
  } catch (error) {
    showScreenError(error);
  }
}

async function createPartnerInvite() {
  try {
    const invite = await api("/api/partner/invite", { method: "POST" });
    if (!invite.link) {
      screen.innerHTML = `<div class="hint">Ссылка появится после настройки BOT_USERNAME у бота.</div>`;
      return;
    }
    screen.innerHTML = `
      <div class="partner-card">
        <h2>Приглашение готово</h2>
        <p>Отправь ссылку партнёру. Она действует до ${esc(humanDate(invite.expires_at))}.</p>
        <a class="share-link" href="${esc(invite.link)}">Открыть приглашение</a>
      </div>`;
    const shareLink = `https://t.me/share/url?url=${encodeURIComponent(invite.link)}&text=${encodeURIComponent("Давай вести кинотрекер вместе 💞")}`;
    tg.openTelegramLink?.(shareLink);
  } catch (error) {
    showAlertError(error);
  }
}

async function acceptStartInvite() {
  const startParam = tg?.initDataUnsafe?.start_param || new URLSearchParams(window.location.search).get("tgWebAppStartParam");
  if (!startParam?.startsWith("pair_") || me.partner) return;
  const token = startParam.slice("pair_".length);
  try {
    const result = await api("/api/partner/accept", { method: "POST", body: JSON.stringify({ token }) });
    me.partner = result.partner;
    tg.showAlert(`Пара подключена: ${result.partner.label}`);
  } catch (error) {
    tg.showAlert(`Не удалось принять приглашение: ${errorMessage(error)}`);
  }
}

// ── Утилиты ──────────────────────────────────────────────────────────────────
function esc(value) {
  const element = document.createElement("div");
  element.textContent = value ?? "";
  return element.innerHTML;
}
function posterImage(value, className = "", lazy = false) {
  const src = safePosterUrl(value);
  if (!src) return "";
  const classAttr = className ? ` class="${className}"` : "";
  return `<img${classAttr}${lazy ? " loading=\"lazy\"" : ""} src="${esc(src)}" alt="">`;
}
function safePosterUrl(value) {
  if (typeof value !== "string") return "";
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}
function ratingLine(movie) {
  const parts = [];
  if (movie.kp_rating) parts.push(`КП ${esc(movie.kp_rating)}`);
  if (movie.imdb_rating) parts.push(`IMDb ${esc(movie.imdb_rating)}`);
  return parts.length ? `<br>⭐ ${parts.join(" · ")}` : "";
}
function humanDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "скоро" : date.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}
function errorMessage(error) {
  return error instanceof Error && error.message ? error.message : "Ошибка соединения. Попробуй ещё раз.";
}
function showScreenError(error) {
  screen.innerHTML = `<div class="hint">⛔ ${esc(errorMessage(error))}</div>`;
}
function showInlineError(target, error) {
  target.innerHTML = `<div class="hint">⛔ ${esc(errorMessage(error))}</div>`;
}
function showAlertError(error) {
  const message = `Ошибка: ${errorMessage(error)}`;
  if (tg?.showAlert) tg.showAlert(message);
  else showScreenError(error);
}
function setActive(tab) {
  document.querySelectorAll("#tabs button").forEach(button =>
    button.classList.toggle("active", button.dataset.tab === tab));
}

// ── Навигация и запуск ───────────────────────────────────────────────────────
document.querySelectorAll("#tabs button").forEach(button => {
  button.onclick = () => {
    const tab = button.dataset.tab;
    setActive(tab);
    if (tab === "search") showSearch();
    else if (tab === "stats") void showStats();
    else if (tab === "partner") void showPartner();
    else void showList(tab);
  };
});

async function startApp() {
  try {
    me = await api("/api/me");
    await acceptStartInvite();
    await showList("want");
  } catch (error) {
    screen.innerHTML = `<div class="hint">⛔ ${esc(errorMessage(error))}<br>Открой через кнопку меню бота в Telegram.</div>`;
  }
}

if (!tg) {
  document.getElementById("tabs").hidden = true;
  screen.innerHTML = `<div class="hint">Открой приложение через кнопку меню бота в Telegram.</div>`;
} else {
  tg.ready();
  tg.expand();
  void startApp();
}
