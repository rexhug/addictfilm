// Публичный Mini App: мой список → карточка фильма → оценка, поиск, статистика.
// Модель single-user: у каждого свой список; на карточке — моя оценка и
// community-оценка (средняя по всем пользователям). Авторизация — initData.

const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const screen = document.getElementById("screen");
let me = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Init-Data": tg.initData, // подпись Telegram — проверяется на бэке
      ...(opts.headers || {}),
    },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
  return res.json();
}

// ── UI-хелперы ────────────────────────────────────────────────────────────────
function emptyState(icon, text, sub = "") {
  return `<div class="empty"><div class="empty-icon">${icon}</div>
    <div class="empty-text">${esc(text)}</div>${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}</div>`;
}
function skeletonGrid(n = 9) {
  return `<div class="grid">${Array.from({ length: n }, () =>
    `<div class="poster-card"><div class="poster-wrap sk"></div><div class="sk sk-line"></div></div>`).join("")}</div>`;
}
// Единая постер-карточка для всех сеток (списки, каталог, поиск).
function posterCard({ poster, title, year, badge = "", mark = "" }, onClick) {
  const card = document.createElement("div");
  card.className = "poster-card";
  card.innerHTML = `
    <div class="poster-wrap">
      ${poster ? `<img loading="lazy" src="${esc(poster)}">` : `<div class="no-poster">${esc(title)}</div>`}
      ${badge ? `<span class="badge">${badge}</span>` : ""}
      ${mark ? `<span class="badge badge-left">${mark}</span>` : ""}
    </div>
    <div class="title">${esc(title)}${year ? ` <span class="year">${esc(year)}</span>` : ""}</div>`;
  card.onclick = onClick;
  return card;
}
function gridOf(items, toCard) {
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const it of items) grid.appendChild(toCard(it));
  return grid;
}

// ── Экраны ────────────────────────────────────────────────────────────────────
const STATUS_MAP = { want: "want_to_watch", watched: "watched", top: "top" };

async function showList(tab) {
  screen.innerHTML = skeletonGrid();
  const { items } = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=60`);
  if (!items.length) {
    screen.innerHTML = tab === "want" ? emptyState("🔖", "Список пуст", "Добавь фильмы через поиск 🔍")
      : tab === "watched" ? emptyState("✅", "Пока ничего не просмотрено", "Отмечай фильмы «Смотрел(а)»")
      : emptyState("⭐", "Твой топ пуст", "Оцени просмотренные фильмы");
    return;
  }
  screen.replaceChildren(gridOf(items, m => posterCard(
    { poster: m.poster_url, title: m.title, year: m.year, badge: m.my_rating ? `★ ${m.my_rating}` : "" },
    () => showDetail(m.id))));
}

async function showDetail(id) {
  screen.innerHTML = `<div class="detail"><div class="hero sk"></div>
    <div class="sk sk-line wide"></div><div class="sk sk-line"></div></div>`;
  const m = await api(`/api/movie/${id}`);
  const myRating = m.my_rating;
  const inList = m.status != null;
  const rateBtns = Array.from({ length: 10 }, (_, i) => i + 1)
    .map(n => `<button data-n="${n}" class="${n === myRating ? "mine" : ""}">${n}</button>`)
    .join("");
  // Кнопки статуса зависят от того, где фильм у меня сейчас.
  let actions;
  if (m.status == null) {
    actions = `<button data-set="want_to_watch">🔖 Хочу посмотреть</button>
               <button data-set="watched">✅ Смотрел(а)</button>`;
  } else if (m.status === "want_to_watch") {
    actions = `<button data-set="watched">✅ Смотрел(а)</button>
               <button id="del" class="danger">🗑 Убрать</button>`;
  } else {
    actions = `<button data-set="want_to_watch">↩️ В «Хочу»</button>
               <button id="del" class="danger">🗑 Убрать</button>`;
  }
  const genreChips = (m.genres || "").split(",").map(g => g.trim()).filter(Boolean)
    .map(g => `<span class="meta-chip">${esc(g)}</span>`).join("");
  const ratingChips = [
    m.kp_rating ? `<span class="rating-chip">КП <b>${esc(m.kp_rating)}</b></span>` : "",
    m.imdb_rating ? `<span class="rating-chip">IMDb <b>${esc(m.imdb_rating)}</b></span>` : "",
    (m.community && m.community.count)
      ? `<span class="rating-chip community">👥 <b>${m.community.avg}</b> <small>${m.community.count}</small></span>` : "",
  ].join("");
  screen.innerHTML = `
    <div class="detail">
      ${m.poster_url ? `<img class="hero" src="${esc(m.poster_url)}">` : ""}
      <h2>${esc(m.title)}${m.year ? ` · ${esc(m.year)}` : ""}</h2>
      ${genreChips || m.runtime ? `<div class="meta-chips">${genreChips}${
        m.runtime ? `<span class="meta-chip">⏱ ${esc(m.runtime)}</span>` : ""}</div>` : ""}
      ${m.directors ? `<div class="meta-line">реж. ${esc(m.directors)}</div>` : ""}
      ${ratingChips ? `<div class="rating-chips">${ratingChips}</div>` : ""}
      ${m.plot ? `<p class="plot">${esc(m.plot)}</p>` : ""}
      <div class="rate-label">Моя оценка${inList ? "" : " · тап = «Смотрел(а)»"}</div>
      <div class="rate-row">${rateBtns}</div>
      <div class="actions">${actions}</div>
    </div>`;

  screen.querySelectorAll(".rate-row button").forEach(b => b.onclick = async () => {
    tg.HapticFeedback?.impactOccurred("light");
    await api(`/api/movie/${id}/rate`, { method: "POST", body: JSON.stringify({ rating: +b.dataset.n }) });
    showDetail(id);
  });
  screen.querySelectorAll(".actions button[data-set]").forEach(b => b.onclick = async () => {
    tg.HapticFeedback?.impactOccurred("light");
    await api(`/api/movie/${id}/status`, { method: "POST", body: JSON.stringify({ status: b.dataset.set }) });
    showDetail(id);
  });
  const del = document.getElementById("del");
  if (del) del.onclick = () => {
    tg.showConfirm(`Убрать «${m.title}» из своего списка?`, async ok => {
      if (!ok) return;
      await api(`/api/movie/${id}`, { method: "DELETE" });
      showDetail(id);  // фильм остаётся в каталоге, статус сбрасывается
    });
  };
}

// ── Discovery: публичный каталог ──────────────────────────────────────────────
async function showBrowse(sort = "popular", genre = "") {
  const modes = [["popular", "🔥 Популярное"], ["top", "⭐ Топ спильноты"], ["genre", "🎭 Жанры"]];
  screen.innerHTML = `
    <div class="subnav">${modes.map(([k, l]) =>
      `<button data-mode="${k}" class="${k === sort ? "active" : ""}">${l}</button>`).join("")}</div>
    <div id="browse-body">${skeletonGrid()}</div>`;
  screen.querySelectorAll(".subnav button").forEach(b => b.onclick = () => showBrowse(b.dataset.mode));
  const body = document.getElementById("browse-body");

  if (sort === "genre") {
    const { items: gs } = await api("/api/genres");
    if (!gs.length) { body.innerHTML = emptyState("🎭", "Жанров пока нет", "Каталог наполнится, когда добавишь фильмы 🔍"); return; }
    body.innerHTML = `
      <div class="chips">${gs.map(g =>
        `<button class="chip ${g.name === genre ? "active" : ""}" data-g="${esc(g.name)}">${esc(g.name)} <small>${g.count}</small></button>`).join("")}</div>
      <div id="genre-films">${genre ? skeletonGrid(6) : emptyState("👆", "Выбери жанр", "")}</div>`;
    body.querySelectorAll(".chip").forEach(c => c.onclick = () => showBrowse("genre", c.dataset.g));
    if (genre) {
      const { items } = await api(`/api/browse?sort=genre&genre=${encodeURIComponent(genre)}`);
      renderBrowseGrid(document.getElementById("genre-films"), items, "genre");
    }
    return;
  }

  const { items } = await api(`/api/browse?sort=${sort}`);
  renderBrowseGrid(body, items, sort);
}

function renderBrowseGrid(container, items, sort) {
  if (!items.length) {
    container.innerHTML = sort === "top"
      ? emptyState("⭐", "Ещё нет оценок", "Оцени фильм — он попадёт в топ спильноты")
      : emptyState("🌐", "Каталог пуст", "Добавь фильмы через поиск 🔍");
    return;
  }
  container.replaceChildren(gridOf(items, it => {
    const c = it.community || {};
    return posterCard({ poster: it.poster_url, title: it.title, year: it.year,
      badge: c.count ? `👥 ${c.avg}` : "", mark: it.in_list ? "✓" : "" }, () => showDetail(it.id));
  }));
}

function showSearch() {
  const startHint = emptyState("🔍", "Что смотрим?", "Введи название — минимум 2 буквы");
  screen.innerHTML = `
    <input id="search-input" placeholder="Название фильма или сериала…" autofocus>
    <div id="search-results">${startHint}</div>`;
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  let timer;
  input.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (q.length < 2) { results.innerHTML = startHint; return; }
      results.innerHTML = skeletonGrid(6);
      let data;
      try {
        data = await api(`/api/search?q=${encodeURIComponent(q)}`);
      } catch (e) {
        results.innerHTML = String(e.message) === "429"
          ? emptyState("⏳", "Слишком часто", "Подожди минуту и попробуй снова")
          : emptyState("⚠️", "Ошибка поиска", String(e.message));
        return;
      }
      if (data.limited) {
        results.innerHTML = emptyState("⏳", "Поиск временно ограничен", "Дневной лимит источника. Попробуй позже");
        return;
      }
      const items = data.items;
      if (!items.length) { results.innerHTML = emptyState("🤷", "Ничего не найдено", "Попробуй год или английское название"); return; }
      results.replaceChildren(gridOf(items, it => posterCard(
        { poster: it.poster || it.poster_url, title: it.title, year: it.year,
          badge: it.rating ? `⭐ ${it.rating}` : "" },
        () => tg.showConfirm(`Добавить «${it.title}» в «Хочу посмотреть»?`, async ok => {
          if (!ok) return;
          const r = await api("/api/add", { method: "POST", body: JSON.stringify({ src: it.src, ref: it.ref }) });
          if (r.reason === "exists") tg.showAlert("Уже в твоём списке!");
          else { tg.HapticFeedback?.notificationOccurred("success"); setActive("want"); showList("want"); }
        }))));
    }, 400);
  };
  input.focus();
}

async function showStats() {
  screen.innerHTML = `<div class="hint">Считаю…</div>`;
  const s = await api("/api/stats");
  const y = s.year;
  if (!s.watched && !s.want) {
    screen.innerHTML = `<div class="hint">Пока нет статистики. Добавь фильмы и поставь оценки 🌐</div>`;
    return;
  }
  const hours = Math.floor(s.total_runtime_min / 60);

  // KPI-плитки.
  const tiles = `<div class="stats-grid">
    ${statTile("🎬", s.watched, "просмотрено")}
    ${statTile("🔖", s.want, "в «Хочу»")}
    ${statTile("⭐", s.avg_rating ?? "—", "средняя")}
    ${statTile("⏱", hours, "часов")}
  </div>`;

  // Гистограмма моих оценок 1..10.
  const dist = s.rating_dist || [];
  const maxD = Math.max(1, ...dist);
  const hist = dist.some(v => v > 0) ? chartCard("Мои оценки", `<div class="hist">${
    dist.map((c, i) => `<div class="hist-col">
      <div class="hist-bar-area">${c ? `<div class="hist-val">${c}</div>` : ""}
        <div class="hist-bar" style="height:${c ? Math.max(6, Math.round(c / maxD * 100)) : 0}%"></div></div>
      <div class="hist-x">${i + 1}</div></div>`).join("")}</div>`) : "";

  // Горизонтальные бары: жанры (%), актёры/режиссёры (по числу фильмов).
  const genres = s.top_genres_pct.length ? chartCard("Жанры",
    s.top_genres_pct.map(([g, p]) => hbar(g, p + "%", p)).join("")) : "";
  const maxA = s.top_actors.length ? s.top_actors[0][1] : 1;
  const actors = s.top_actors.length ? chartCard("Актёры",
    s.top_actors.map(([n, c]) => hbar(n, c, Math.round(c / maxA * 100))).join("")) : "";
  const maxDir = s.top_directors.length ? s.top_directors[0][1] : 1;
  const directors = s.top_directors.length ? chartCard("Режиссёры",
    s.top_directors.map(([n, c]) => hbar(n, c, Math.round(c / maxDir * 100))).join("")) : "";

  const yearCard = y.count ? chartCard(`Итоги ${y.year}`, `
    <div class="year-line"><b>${y.count}</b> фильмов${y.avg_rating ? ` · средняя <b>${y.avg_rating}</b>` : ""}</div>
    ${y.top_genre ? `<div class="year-line">Любимый жанр — ${esc(y.top_genre)}</div>` : ""}
    ${y.top_actor ? `<div class="year-line">Актёр года — ${esc(y.top_actor[0])} <small>(${y.top_actor[1]})</small></div>` : ""}
    ${y.best_titles && y.best_titles.length ? `<div class="year-line">Лучшее <small>(${y.best_avg})</small>: ${y.best_titles.map(esc).join(", ")}</div>` : ""}`) : "";

  screen.innerHTML = tiles + hist + genres + actors + directors + yearCard;
}

function statTile(icon, value, label) {
  return `<div class="tile"><div class="tile-icon">${icon}</div>
    <div class="tile-val">${esc(value)}</div><div class="tile-label">${label}</div></div>`;
}
function chartCard(title, inner) {
  return `<div class="chart-card"><div class="chart-title">${esc(title)}</div>${inner}</div>`;
}
function hbar(label, valueText, pct) {
  return `<div class="hbar-row">
    <div class="hbar-label">${esc(label)}</div>
    <div class="hbar-track"><div class="hbar-fill" style="width:${Math.max(4, pct)}%"></div></div>
    <div class="hbar-val">${esc(valueText)}</div></div>`;
}

// ── Утилиты ───────────────────────────────────────────────────────────────────
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function setActive(tab) {
  document.querySelectorAll("#tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab));
}

// ── Навигация ─────────────────────────────────────────────────────────────────
document.querySelectorAll("#tabs button").forEach(btn => {
  btn.onclick = () => {
    setActive(btn.dataset.tab);
    const t = btn.dataset.tab;
    if (t === "browse") showBrowse();
    else if (t === "search") showSearch();
    else if (t === "stats") showStats();
    else showList(t);
  };
});

// Старт: проверяем авторизацию и открываем публичный каталог.
(async () => {
  try {
    me = await api("/api/me");
    showBrowse();
  } catch (e) {
    screen.innerHTML = `<div class="hint">⛔ ${esc(e.message)}<br>Открой через кнопку меню бота в Telegram.</div>`;
  }
})();
