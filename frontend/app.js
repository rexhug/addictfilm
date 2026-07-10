// Addict Film — премиум-редизайн. Ванильный JS + Telegram WebApp SDK.
// Фиксированная high-end тёмная тема (не зависит от темы Telegram).

const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
try { tg.setHeaderColor("#050505"); tg.setBackgroundColor("#050505"); } catch (e) {}

const screen = document.getElementById("screen");
let me = null;
let _returnTo = () => { setActiveTab("home"); showHome(); };  // куда вернёт «назад»

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Init-Data": tg.initData,
      ...(opts.headers || {}),
    },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
  return res.json();
}

// ── Утилиты ───────────────────────────────────────────────────────────────────
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function hash(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h; }
function plural(n, f = ["фильм", "фильма", "фильмов"]) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return f[2];
  if (b > 1 && b < 5) return f[1];
  if (b === 1) return f[0];
  return f[2];
}
function ratingOf(m) {
  const r = m.imdb_rating || m.kp_rating;
  if (r && !isNaN(+r)) return (+r).toFixed(1);
  if (m.community && m.community.count) return m.community.avg;
  return null;
}
const GENRE_GRAD = [
  "radial-gradient(90% 90% at 80% 12%,rgba(214,164,74,.32),transparent 60%),linear-gradient(150deg,#231a0d,#0a0805)",
  "radial-gradient(90% 90% at 80% 12%,rgba(120,140,168,.28),transparent 60%),linear-gradient(150deg,#14171c,#070809)",
  "radial-gradient(90% 90% at 80% 12%,rgba(84,132,178,.32),transparent 60%),linear-gradient(150deg,#0d1620,#05080c)",
  "radial-gradient(90% 90% at 80% 12%,rgba(150,96,190,.28),transparent 60%),linear-gradient(150deg,#171122,#08060c)",
  "radial-gradient(90% 90% at 80% 12%,rgba(196,80,64,.30),transparent 60%),linear-gradient(150deg,#1e1210,#0a0605)",
  "radial-gradient(90% 90% at 80% 12%,rgba(80,150,110,.26),transparent 60%),linear-gradient(150deg,#0e1712,#050807)",
];

function skeletonRail(n = 5) {
  return Array.from({ length: n }, () => `<div class="poster"><div class="art sk"></div><div class="sk sk-line"></div></div>`).join("");
}
function skeletonGrid(n = 6) {
  return `<div class="grid">${Array.from({ length: n }, () => `<div class="poster"><div class="art sk"></div><div class="sk sk-line"></div></div>`).join("")}</div>`;
}
function emptyState(icon, text, sub = "") {
  return `<div class="empty"><div class="empty-icon">${icon}</div><div class="empty-text">${esc(text)}</div>${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}</div>`;
}

// Единая постер-карточка (реальные постеры kinopoisk).
function posterTile(m, { onClick, badge, mark = "" } = {}) {
  const card = document.createElement("div");
  card.className = "poster";
  const b = badge !== undefined ? badge : (ratingOf(m) ? `★ ${ratingOf(m)}` : "");
  card.innerHTML = `
    <div class="art">
      ${m.poster_url ? `<img loading="lazy" src="${esc(m.poster_url)}" alt="">` : `<div class="noposter">${esc(m.title)}</div>`}
      ${b ? `<span class="rate">${b}</span>` : ""}
      ${mark ? `<span class="rate mark">${mark}</span>` : ""}
    </div>
    <div class="meta"><div class="t">${esc(m.title)}</div><div class="y">${esc(m.year || "")}</div></div>`;
  if (onClick) card.onclick = onClick;
  return card;
}
function gridOf(items, toCard) {
  const g = document.createElement("div");
  g.className = "grid";
  for (const it of items) g.appendChild(toCard(it));
  return g;
}
function openDetail(id, back) { if (back) _returnTo = back; showDetail(id); }

// ── Главная ───────────────────────────────────────────────────────────────────
async function showHome() {
  window.scrollTo(0, 0);
  screen.innerHTML = `
    <header class="app-head rise d1">
      <div class="brand"><h1>Addict&nbsp;Film</h1><p>Кино, которое ты любишь</p></div>
      <button class="bell" aria-label="Уведомления">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 6-3 8-3 8h18s-3-2-3-8"/><path d="M13.7 20a2 2 0 0 1-3.4 0"/></svg>
        <span class="dot"></span>
      </button>
    </header>
    <div class="search rise d1" id="home-search">
      <span class="icn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg></span>
      <span class="q">Поиск фильмов, сериалов, актёров…</span>
      <span class="filter"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="4" y1="7" x2="10" y2="7"/><line x1="14" y1="7" x2="20" y2="7"/><circle cx="12" cy="7" r="2"/><line x1="4" y1="12.5" x2="6" y2="12.5"/><line x1="10" y1="12.5" x2="20" y2="12.5"/><circle cx="8" cy="12.5" r="2"/><line x1="4" y1="18" x2="13" y2="18"/><line x1="17" y1="18" x2="20" y2="18"/><circle cx="15" cy="18" r="2"/></svg></span>
    </div>
    <div class="chips rise d2">
      <span class="chip active" data-to="sec-pop"><span class="e">🔥</span>Популярное</span>
      <span class="chip" data-to="sec-top"><span class="e">🏆</span>Топ спильноты</span>
      <span class="chip" data-to="sec-gen"><span class="e">🎭</span>Жанры</span>
    </div>
    <section class="rise d3" id="sec-pop"><div class="head"><h2>Популярное</h2></div><div class="rail" id="rail-pop">${skeletonRail(5)}</div></section>
    <section class="rise d4" id="sec-top"><div class="head"><h2>Топ спильноты</h2></div><div class="rail" id="rail-top">${skeletonRail(5)}</div></section>
    <section class="rise d5" id="sec-gen"><div class="head"><h2>Жанры</h2></div><div class="rail grail" id="rail-gen">${skeletonRail(4)}</div></section>`;

  document.getElementById("home-search").onclick = () => showSearch();
  screen.querySelectorAll(".chips .chip").forEach(c => c.onclick = () => {
    screen.querySelectorAll(".chips .chip").forEach(x => x.classList.toggle("active", x === c));
    document.getElementById(c.dataset.to)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  loadRail("rail-pop", "/api/browse?sort=popular&limit=20");
  loadRail("rail-top", "/api/browse?sort=top&limit=20");
  loadGenres();
}

async function loadRail(id, path) {
  const el = document.getElementById(id);
  try {
    const { items } = await api(path);
    if (!el) return;
    if (!items.length) { el.innerHTML = `<div class="rail-empty">Пока пусто — добавь фильмы через поиск</div>`; return; }
    const back = () => { setActiveTab("home"); showHome(); };
    el.replaceChildren(...items.map(m => posterTile(m, { onClick: () => openDetail(m.id, back) })));
  } catch (e) { if (el) el.innerHTML = `<div class="rail-empty">Не удалось загрузить</div>`; }
}

async function loadGenres() {
  const el = document.getElementById("rail-gen");
  try {
    const { items } = await api("/api/genres");
    if (!el) return;
    if (!items.length) { el.innerHTML = `<div class="rail-empty">Каталог пока пуст</div>`; return; }
    el.replaceChildren(...items.map(genreCard));
  } catch (e) { if (el) el.innerHTML = `<div class="rail-empty">—</div>`; }
}

function genreCard(g) {
  const card = document.createElement("div");
  card.className = "genre";
  const grad = GENRE_GRAD[hash(g.name) % GENRE_GRAD.length];
  card.innerHTML = `<div class="gart" style="background:${grad}"><span class="lbl"><b>${esc(g.name)}</b><span>${g.count} ${plural(g.count)}</span></span></div>`;
  card.onclick = () => showGenre(g.name);
  return card;
}

async function showGenre(name) {
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="sub-head">${backBtn()}<h1>${esc(name)}</h1></div><div id="gg">${skeletonGrid(6)}</div>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  try {
    const { items } = await api(`/api/browse?sort=genre&genre=${encodeURIComponent(name)}`);
    const el = document.getElementById("gg");
    if (!items.length) { el.innerHTML = emptyState("🎭", "Пока пусто", "В этом жанре ещё нет фильмов"); return; }
    const back = () => showGenre(name);
    el.replaceChildren(gridOf(items, m => posterTile(m, { onClick: () => openDetail(m.id, back) })));
  } catch (e) { document.getElementById("gg").innerHTML = emptyState("⚠️", "Ошибка загрузки", ""); }
}

// ── Личные списки (Хочу / Смотрел / Мой топ) ──────────────────────────────────
const STATUS_MAP = { want: "want_to_watch", watched: "watched", top: "top" };
const LIST_TITLE = { want: "Хочу посмотреть", watched: "Смотрел", top: "Мой топ" };

async function showList(tab) {
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="page-head"><h1>${LIST_TITLE[tab]}</h1></div><div id="list">${skeletonGrid(6)}</div>`;
  const { items } = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=60`);
  const el = document.getElementById("list");
  if (!items.length) {
    el.innerHTML = tab === "want" ? emptyState("🔖", "Список пуст", "Добавь фильмы через поиск")
      : tab === "watched" ? emptyState("✅", "Пока ничего не просмотрено", "Отмечай фильмы «Смотрел»")
      : emptyState("⭐", "Твой топ пуст", "Оцени просмотренные фильмы");
    return;
  }
  const back = () => showList(tab);
  el.replaceChildren(gridOf(items, m => posterTile(m, {
    onClick: () => openDetail(m.id, back),
    badge: m.my_rating ? `★ ${m.my_rating}` : "",
  })));
}

// ── Карточка фильма ───────────────────────────────────────────────────────────
async function showDetail(id) {
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="detail"><div class="detail-top">${backBtn()}</div><div class="hero sk"></div><div class="sk sk-line wide"></div><div class="sk sk-line"></div></div>`;
  wireBack(_returnTo);
  const m = await api(`/api/movie/${id}`);
  const myRating = m.my_rating;
  const inList = m.status != null;
  const rateBtns = Array.from({ length: 10 }, (_, i) => i + 1)
    .map(n => `<button data-n="${n}" class="${n === myRating ? "mine" : ""}">${n}</button>`).join("");
  let actions;
  if (m.status == null) {
    actions = `<button data-set="want_to_watch" class="primary">🔖 Хочу посмотреть</button><button data-set="watched">✅ Смотрел(а)</button>`;
  } else if (m.status === "want_to_watch") {
    actions = `<button data-set="watched" class="primary">✅ Смотрел(а)</button><button id="del" class="danger">🗑 Убрать</button>`;
  } else {
    actions = `<button data-set="want_to_watch">↩️ В «Хочу»</button><button id="del" class="danger">🗑 Убрать</button>`;
  }
  const genreChips = (m.genres || "").split(",").map(g => g.trim()).filter(Boolean)
    .map(g => `<span class="meta-chip">${esc(g)}</span>`).join("");
  const ratingChips = [
    m.kp_rating ? `<span class="rating-chip">КП <b>${esc(m.kp_rating)}</b></span>` : "",
    m.imdb_rating ? `<span class="rating-chip">IMDb <b>${esc(m.imdb_rating)}</b></span>` : "",
    (m.community && m.community.count) ? `<span class="rating-chip community">👥 <b>${m.community.avg}</b> <small>${m.community.count}</small></span>` : "",
  ].join("");
  screen.innerHTML = `
    <div class="detail">
      <div class="detail-top">${backBtn()}</div>
      ${m.poster_url ? `<img class="hero" src="${esc(m.poster_url)}" alt="">` : ""}
      <h2>${esc(m.title)}${m.year ? ` · ${esc(m.year)}` : ""}</h2>
      ${genreChips || m.runtime ? `<div class="meta-chips">${genreChips}${m.runtime ? `<span class="meta-chip">⏱ ${esc(m.runtime)}</span>` : ""}</div>` : ""}
      ${m.directors ? `<div class="meta-line">реж. ${esc(m.directors)}</div>` : ""}
      ${ratingChips ? `<div class="rating-chips">${ratingChips}</div>` : ""}
      ${m.plot ? `<p class="plot">${esc(m.plot)}</p>` : ""}
      <div class="rate-label">Моя оценка${inList ? "" : " · тап = «Смотрел(а)»"}</div>
      <div class="rate-row">${rateBtns}</div>
      <div class="actions">${actions}</div>
    </div>`;
  wireBack(_returnTo);

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
      showDetail(id);
    });
  };
}

// ── Поиск ─────────────────────────────────────────────────────────────────────
function showSearch() {
  window.scrollTo(0, 0);
  const start = emptyState("🔍", "Что смотрим?", "Введи название — минимум 2 буквы");
  screen.innerHTML = `<div class="search-bar">${backBtn()}<input id="si" placeholder="Поиск фильмов, сериалов, актёров…" autofocus></div><div id="sr">${start}</div>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  const input = document.getElementById("si");
  const results = document.getElementById("sr");
  let timer;
  input.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (q.length < 2) { results.innerHTML = start; return; }
      results.innerHTML = skeletonGrid(6);
      let data;
      try { data = await api(`/api/search?q=${encodeURIComponent(q)}`); }
      catch (e) {
        results.innerHTML = String(e.message) === "429"
          ? emptyState("⏳", "Слишком часто", "Подожди минуту и попробуй снова")
          : emptyState("⚠️", "Ошибка поиска", String(e.message));
        return;
      }
      if (data.limited) { results.innerHTML = emptyState("⏳", "Поиск временно ограничен", "Дневной лимит источника. Попробуй позже"); return; }
      const items = data.items;
      if (!items.length) { results.innerHTML = emptyState("🤷", "Ничего не найдено", "Попробуй год или английское название"); return; }
      results.replaceChildren(gridOf(items, it => posterTile(
        { poster_url: it.poster || it.poster_url, title: it.title, year: it.year, imdb_rating: it.rating },
        {
          onClick: () => tg.showConfirm(`Добавить «${it.title}» в «Хочу посмотреть»?`, async ok => {
            if (!ok) return;
            const r = await api("/api/add", { method: "POST", body: JSON.stringify({ src: it.src, ref: it.ref }) });
            if (r.reason === "exists") tg.showAlert("Уже в твоём списке!");
            else { tg.HapticFeedback?.notificationOccurred("success"); setActiveTab("want"); showList("want"); }
          }),
        })));
    }, 400);
  };
  input.focus();
}

// ── Статистика ────────────────────────────────────────────────────────────────
async function showStats() {
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="page-head"><h1>Статистика</h1></div><div id="stats"><div class="empty"><div class="empty-sub">Считаю…</div></div></div>`;
  const s = await api("/api/stats");
  const y = s.year;
  const box = document.getElementById("stats");
  if (!s.watched && !s.want) { box.innerHTML = emptyState("📊", "Пока нет статистики", "Добавь фильмы и поставь оценки"); return; }
  const hours = Math.floor(s.total_runtime_min / 60);
  const tiles = `<div class="stats-grid">
    ${statTile("🎬", s.watched, "просмотрено")}${statTile("🔖", s.want, "в «Хочу»")}
    ${statTile("⭐", s.avg_rating ?? "—", "средняя")}${statTile("⏱", hours, "часов")}</div>`;
  const dist = s.rating_dist || [];
  const maxD = Math.max(1, ...dist);
  const hist = dist.some(v => v > 0) ? chartCard("Мои оценки", `<div class="hist">${
    dist.map((c, i) => `<div class="hist-col"><div class="hist-bar-area">${c ? `<div class="hist-val">${c}</div>` : ""}<div class="hist-bar" style="height:${c ? Math.max(6, Math.round(c / maxD * 100)) : 0}%"></div></div><div class="hist-x">${i + 1}</div></div>`).join("")}</div>`) : "";
  const genres = s.top_genres_pct.length ? chartCard("Жанры", s.top_genres_pct.map(([g, p]) => hbar(g, p + "%", p)).join("")) : "";
  const maxA = s.top_actors.length ? s.top_actors[0][1] : 1;
  const actors = s.top_actors.length ? chartCard("Актёры", s.top_actors.map(([n, c]) => hbar(n, c, Math.round(c / maxA * 100))).join("")) : "";
  const maxDir = s.top_directors.length ? s.top_directors[0][1] : 1;
  const directors = s.top_directors.length ? chartCard("Режиссёры", s.top_directors.map(([n, c]) => hbar(n, c, Math.round(c / maxDir * 100))).join("")) : "";
  const yearCard = y.count ? chartCard(`Итоги ${y.year}`, `
    <div class="year-line"><b>${y.count}</b> фильмов${y.avg_rating ? ` · средняя <b>${y.avg_rating}</b>` : ""}</div>
    ${y.top_genre ? `<div class="year-line">Любимый жанр — ${esc(y.top_genre)}</div>` : ""}
    ${y.top_actor ? `<div class="year-line">Актёр года — ${esc(y.top_actor[0])} <small>(${y.top_actor[1]})</small></div>` : ""}
    ${y.best_titles && y.best_titles.length ? `<div class="year-line">Лучшее <small>(${y.best_avg})</small>: ${y.best_titles.map(esc).join(", ")}</div>` : ""}`) : "";
  box.innerHTML = tiles + hist + genres + actors + directors + yearCard;
}
function statTile(icon, value, label) {
  return `<div class="tile"><div class="tile-icon">${icon}</div><div class="tile-val">${esc(value)}</div><div class="tile-label">${label}</div></div>`;
}
function chartCard(title, inner) { return `<div class="chart-card"><div class="chart-title">${esc(title)}</div>${inner}</div>`; }
function hbar(label, valueText, pct) {
  return `<div class="hbar-row"><div class="hbar-label">${esc(label)}</div><div class="hbar-track"><div class="hbar-fill" style="width:${Math.max(4, pct)}%"></div></div><div class="hbar-val">${esc(valueText)}</div></div>`;
}

// ── Навигация ─────────────────────────────────────────────────────────────────
function backBtn() {
  return `<button class="back" aria-label="Назад"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button>`;
}
function wireBack(fn) {
  const b = screen.querySelector(".back");
  if (b) b.onclick = fn;
}
function setActiveTab(t) {
  document.querySelectorAll("#tabbar .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
}
function route(t) {
  if (t === "home") showHome();
  else if (t === "stats") showStats();
  else showList(t);
}
document.querySelectorAll("#tabbar .tab").forEach(btn => {
  btn.onclick = () => { setActiveTab(btn.dataset.tab); route(btn.dataset.tab); };
});

// Старт.
(async () => {
  try {
    me = await api("/api/me");
    showHome();
  } catch (e) {
    screen.innerHTML = emptyState("⛔", esc(e.message), "Открой через кнопку меню бота в Telegram");
  }
})();
