// Addict Film — премиум-редизайн + локализация RU/EN.
// Фиксированная high-end тёмная тема (не зависит от темы Telegram).

const tg = window.Telegram && window.Telegram.WebApp;  // вне Telegram — null, не падаем
if (tg) {
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor("#050505"); tg.setBackgroundColor("#050505"); } catch (e) {}
}

const screen = document.getElementById("screen");
let me = null;
let _returnTo = () => { setActiveTab("home"); showHome(); };

// ── Локализация ───────────────────────────────────────────────────────────────
function pl(n, f) { const a = Math.abs(n) % 100, b = a % 10; if (a > 10 && a < 20) return f[2]; if (b > 1 && b < 5) return f[1]; if (b === 1) return f[0]; return f[2]; }
const DICT = {
  ru: {
    tagline: "Кино, которое ты любишь",
    search_ph: "Поиск фильмов, сериалов, актёров…",
    chip_popular: "Популярное", chip_top: "Топ сообщества", chip_genres: "Жанры",
    tab_home: "Главная", tab_want: "Хочу", tab_watched: "Смотрел", tab_top: "Мой топ", tab_stats: "Статистика",
    list_want: "Хочу посмотреть", list_watched: "Смотрел", list_top: "Мой топ",
    count_films: (n) => pl(n, ["фильм", "фильма", "фильмов"]),
    rail_empty: "Пока пусто — добавь фильмы через поиск", rail_err: "Не удалось загрузить",
    genres_empty: "Каталог пока пуст",
    genre_empty_t: "Пока пусто", genre_empty_s: "В этом жанре ещё нет фильмов", load_err: "Ошибка загрузки",
    want_empty_t: "Список пуст", want_empty_s: "Добавь фильмы через поиск",
    watched_empty_t: "Пока ничего не просмотрено", watched_empty_s: "Отмечай фильмы «Смотрел»",
    top_empty_t: "Твой топ пуст", top_empty_s: "Оцени просмотренные фильмы",
    my_rating: "Моя оценка", rate_hint: " · тап = «Смотрел(а)»", dir: "реж. ",
    act_want: "🔖 Хочу посмотреть", act_watched: "✅ Смотрел(а)", act_to_want: "↩️ В «Хочу»", act_remove: "🗑 Убрать",
    confirm_remove: (t) => `Убрать «${t}» из своего списка?`,
    search_start_t: "Что смотрим?", search_start_s: "Введи название — минимум 2 буквы",
    search_toomany_t: "Слишком часто", search_toomany_s: "Подожди минуту и попробуй снова",
    search_err_t: "Ошибка поиска",
    search_limited_t: "Поиск временно ограничен", search_limited_s: "Дневной лимит источника. Попробуй позже",
    search_none_t: "Ничего не найдено", search_none_s: "Попробуй год или английское название",
    confirm_add: (t) => `Добавить «${t}» в «Хочу посмотреть»?`, already_in_list: "Уже в твоём списке!",
    stats_title: "Статистика", stats_empty_t: "Пока нет статистики", stats_empty_s: "Добавь фильмы и поставь оценки", calc: "Считаю…",
    tile_watched: "просмотрено", tile_want: "в «Хочу»", tile_avg: "средняя", tile_hours: "часов",
    chart_ratings: "Мои оценки", chart_genres: "Жанры", chart_actors: "Актёры", chart_directors: "Режиссёры",
    year_title: (y) => `Итоги ${y}`, year_avg: "средняя", year_fav_genre: "Любимый жанр — ", year_actor: "Актёр года — ", year_best: "Лучшее",
    auth_err_s: "Открой через кнопку меню бота в Telegram",
    partner_title: "Пара", partner_none_sub: "Добавь партнёра — считайте совместимость вкусов вместе",
    partner_invite_btn: "Добавить партнёра", partner_invited_sub: "Приглашение готово. Отправь ссылку партнёру в Telegram.",
    partner_share_btn: "Поделиться ссылкой", partner_share_text: "Давай отмечать фильмы вместе в Addict Film 💞",
    partner_with: "Пара с", partner_word: "партнёром", partner_compat: "совместимость",
    partner_no_common: "Пока нет фильмов, которые оценили оба",
    partner_matches: "Точных совпадений", partner_best: "Лучший общий", partner_controversial: "Самый спорный", partner_genres: "Общие жанры",
    partner_unpair_btn: "Разорвать пару", partner_unpair_confirm: "Разорвать пару? Личные списки останутся у каждого.",
    partner_code_btn: "У меня есть код", partner_code_ph: "Код партнёра", partner_connect: "Подключить",
    partner_code_hint: "Или отправь партнёру этот код:",
    accept_title: "Приглашение в пару", accept_sub: "Вас зовут отмечать и оценивать фильмы вместе, с общей статистикой совместимости.",
    accept_yes: "Принять", accept_no: "Не сейчас",
    accept_ok: (name) => `Готово! Теперь вы в паре${name ? ` с ${name}` : ""}.`,
    accept_fail_invalid: "Приглашение недействительно или уже использовано.",
    accept_fail_self: "Нельзя принять собственное приглашение 🙂",
    accept_fail_inviter_taken: "У пригласившего уже есть пара.",
    accept_fail_already_paired: "У вас уже есть пара. Сначала разорвите текущую.",
  },
  en: {
    tagline: "Movies you'll love",
    search_ph: "Search movies, TV shows, actors…",
    chip_popular: "Popular", chip_top: "Community Top", chip_genres: "Genres",
    tab_home: "Home", tab_want: "Wishlist", tab_watched: "Watched", tab_top: "My Top", tab_stats: "Stats",
    list_want: "Wishlist", list_watched: "Watched", list_top: "My Top",
    count_films: (n) => (n === 1 ? "film" : "films"),
    rail_empty: "Empty — add films via search", rail_err: "Couldn't load",
    genres_empty: "Catalog is empty yet",
    genre_empty_t: "Empty", genre_empty_s: "No films in this genre yet", load_err: "Loading error",
    want_empty_t: "List is empty", want_empty_s: "Add films via search",
    watched_empty_t: "Nothing watched yet", watched_empty_s: "Mark films as Watched",
    top_empty_t: "Your top is empty", top_empty_s: "Rate the films you've watched",
    my_rating: "My rating", rate_hint: " · tap = Watched", dir: "dir. ",
    act_want: "🔖 Add to wishlist", act_watched: "✅ Watched", act_to_want: "↩️ To wishlist", act_remove: "🗑 Remove",
    confirm_remove: (t) => `Remove "${t}" from your list?`,
    search_start_t: "What are we watching?", search_start_s: "Type a title — at least 2 letters",
    search_toomany_t: "Too many requests", search_toomany_s: "Wait a minute and try again",
    search_err_t: "Search error",
    search_limited_t: "Search temporarily limited", search_limited_s: "Daily source limit. Try later",
    search_none_t: "Nothing found", search_none_s: "Try a year or the English title",
    confirm_add: (t) => `Add "${t}" to your wishlist?`, already_in_list: "Already in your list!",
    stats_title: "Stats", stats_empty_t: "No stats yet", stats_empty_s: "Add films and rate them", calc: "Calculating…",
    tile_watched: "watched", tile_want: "wishlist", tile_avg: "average", tile_hours: "hours",
    chart_ratings: "My ratings", chart_genres: "Genres", chart_actors: "Actors", chart_directors: "Directors",
    year_title: (y) => `${y} in review`, year_avg: "average", year_fav_genre: "Favorite genre — ", year_actor: "Actor of the year — ", year_best: "Best",
    auth_err_s: "Open via the bot's menu button in Telegram",
    partner_title: "Partner", partner_none_sub: "Add a partner — see how your movie tastes match",
    partner_invite_btn: "Add partner", partner_invited_sub: "Invite ready. Send the link to your partner in Telegram.",
    partner_share_btn: "Share link", partner_share_text: "Let's track movies together on Addict Film 💞",
    partner_with: "Paired with", partner_word: "partner", partner_compat: "compatibility",
    partner_no_common: "No films you both rated yet",
    partner_matches: "Exact matches", partner_best: "Best shared", partner_controversial: "Most divisive", partner_genres: "Shared genres",
    partner_unpair_btn: "Unpair", partner_unpair_confirm: "Unpair? Each keeps their personal lists.",
    partner_code_btn: "I have a code", partner_code_ph: "Partner code", partner_connect: "Connect",
    partner_code_hint: "Or send your partner this code:",
    accept_title: "Pairing invite", accept_sub: "You're invited to track and rate movies together, with shared compatibility stats.",
    accept_yes: "Accept", accept_no: "Not now",
    accept_ok: (name) => `Done! You're now paired${name ? ` with ${name}` : ""}.`,
    accept_fail_invalid: "Invite is invalid or already used.",
    accept_fail_self: "You can't accept your own invite 🙂",
    accept_fail_inviter_taken: "The inviter already has a partner.",
    accept_fail_already_paired: "You already have a partner. Unpair first.",
  },
};
let lang = "ru";
try { lang = localStorage.getItem("lang") || ((tg?.initDataUnsafe?.user?.language_code || "").startsWith("en") ? "en" : "ru"); } catch (e) {}
function t(key, ...args) { const v = (DICT[lang] || DICT.ru)[key] ?? DICT.ru[key] ?? key; return typeof v === "function" ? v(...args) : v; }
function setLang(l) { lang = l; try { localStorage.setItem("lang", l); } catch (e) {} applyTabLabels(); showHome(); }
function applyTabLabels() {
  const map = { home: "tab_home", want: "tab_want", watched: "tab_watched", top: "tab_top", stats: "tab_stats" };
  document.querySelectorAll("#tabbar .tab").forEach(b => { const s = b.querySelector("span"); if (s) s.textContent = t(map[b.dataset.tab]); });
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-Init-Data": tg.initData, ...(opts.headers || {}) },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
  return res.json();
}

// ── Утилиты ───────────────────────────────────────────────────────────────────
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function hash(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h; }
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
function skeletonRail(n = 5) { return Array.from({ length: n }, () => `<div class="poster"><div class="art sk"></div><div class="sk sk-line"></div></div>`).join(""); }
function skeletonGrid(n = 6) { return `<div class="grid">${Array.from({ length: n }, () => `<div class="poster"><div class="art sk"></div><div class="sk sk-line"></div></div>`).join("")}</div>`; }
function emptyState(icon, text, sub = "") { return `<div class="empty"><div class="empty-icon">${icon}</div><div class="empty-text">${esc(text)}</div>${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}</div>`; }

function posterTile(m, { onClick, badge } = {}) {
  const card = document.createElement("div");
  card.className = "poster";
  const b = badge !== undefined ? badge : (ratingOf(m) ? `★ ${ratingOf(m)}` : "");
  card.innerHTML = `
    <div class="art">
      ${m.poster_url ? `<img loading="lazy" src="${esc(m.poster_url)}" alt="">` : `<div class="noposter">${esc(m.title)}</div>`}
      ${b ? `<span class="rate">${b}</span>` : ""}
    </div>
    <div class="meta"><div class="t">${esc(m.title)}</div><div class="y">${esc(m.year || "")}</div></div>`;
  if (onClick) card.onclick = onClick;
  return card;
}
function gridOf(items, toCard) { const g = document.createElement("div"); g.className = "grid"; for (const it of items) g.appendChild(toCard(it)); return g; }
function openDetail(id, back) { if (back) _returnTo = back; showDetail(id); }

// ── Главная ───────────────────────────────────────────────────────────────────
async function showHome() {
  window.scrollTo(0, 0);
  screen.innerHTML = `
    <header class="app-head rise d1">
      <div class="brand"><h1>Addict&nbsp;Film</h1><p>${esc(t("tagline"))}</p></div>
      <button class="lang" id="lang-btn" aria-label="Language">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.6 2.4 4 5.6 4 9s-1.4 6.6-4 9c-2.6-2.4-4-5.6-4-9s1.4-6.6 4-9Z"/></svg>
        <b>${lang.toUpperCase()}</b>
      </button>
    </header>
    <div class="search rise d1" id="home-search">
      <span class="icn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg></span>
      <span class="q">${esc(t("search_ph"))}</span>
    </div>
    <div class="chips rise d2">
      <span class="chip active" data-to="sec-pop"><span class="e">🔥</span>${esc(t("chip_popular"))}</span>
      <span class="chip" data-to="sec-top"><span class="e">🏆</span>${esc(t("chip_top"))}</span>
      <span class="chip" data-to="sec-gen"><span class="e">🎭</span>${esc(t("chip_genres"))}</span>
    </div>
    <section class="rise d3" id="sec-pop"><div class="head"><h2>${esc(t("chip_popular"))}</h2></div><div class="rail" id="rail-pop">${skeletonRail(5)}</div></section>
    <section class="rise d4" id="sec-top"><div class="head"><h2>${esc(t("chip_top"))}</h2></div><div class="rail" id="rail-top">${skeletonRail(5)}</div></section>
    <section class="rise d5" id="sec-gen"><div class="head"><h2>${esc(t("chip_genres"))}</h2></div><div class="rail grail" id="rail-gen">${skeletonRail(4)}</div></section>`;

  document.getElementById("lang-btn").onclick = () => setLang(lang === "ru" ? "en" : "ru");
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
    if (!items.length) { el.innerHTML = `<div class="rail-empty">${esc(t("rail_empty"))}</div>`; return; }
    const back = () => { setActiveTab("home"); showHome(); };
    el.replaceChildren(...items.map(m => posterTile(m, { onClick: () => openDetail(m.id, back) })));
  } catch (e) { if (el) el.innerHTML = `<div class="rail-empty">${esc(t("rail_err"))}</div>`; }
}
async function loadGenres() {
  const el = document.getElementById("rail-gen");
  try {
    const { items } = await api("/api/genres");
    if (!el) return;
    if (!items.length) { el.innerHTML = `<div class="rail-empty">${esc(t("genres_empty"))}</div>`; return; }
    el.replaceChildren(...items.map(genreCard));
  } catch (e) { if (el) el.innerHTML = `<div class="rail-empty">—</div>`; }
}
function genreCard(g) {
  const card = document.createElement("div");
  card.className = "genre";
  const grad = GENRE_GRAD[hash(g.name) % GENRE_GRAD.length];
  card.innerHTML = `<div class="gart" style="background:${grad}"><span class="lbl"><b>${esc(g.name)}</b><span>${g.count} ${esc(t("count_films", g.count))}</span></span></div>`;
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
    if (!items.length) { el.innerHTML = emptyState("🎭", t("genre_empty_t"), t("genre_empty_s")); return; }
    const back = () => showGenre(name);
    el.replaceChildren(gridOf(items, m => posterTile(m, { onClick: () => openDetail(m.id, back) })));
  } catch (e) { document.getElementById("gg").innerHTML = emptyState("⚠️", t("load_err"), ""); }
}

// ── Личные списки ─────────────────────────────────────────────────────────────
const STATUS_MAP = { want: "want_to_watch", watched: "watched", top: "top" };
async function showList(tab) {
  window.scrollTo(0, 0);
  const title = tab === "want" ? t("list_want") : tab === "watched" ? t("list_watched") : t("list_top");
  screen.innerHTML = `<div class="page-head"><h1>${esc(title)}</h1></div><div id="list">${skeletonGrid(6)}</div>`;
  const { items } = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=60`);
  const el = document.getElementById("list");
  if (!items.length) {
    el.innerHTML = tab === "want" ? emptyState("🔖", t("want_empty_t"), t("want_empty_s"))
      : tab === "watched" ? emptyState("✅", t("watched_empty_t"), t("watched_empty_s"))
      : emptyState("⭐", t("top_empty_t"), t("top_empty_s"));
    return;
  }
  const back = () => showList(tab);
  el.replaceChildren(gridOf(items, m => posterTile(m, { onClick: () => openDetail(m.id, back), badge: m.my_rating ? `★ ${m.my_rating}` : "" })));
}

// ── Карточка фильма ───────────────────────────────────────────────────────────
async function showDetail(id) {
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="detail"><div class="detail-top">${backBtn()}</div><div class="hero sk"></div><div class="sk sk-line wide"></div><div class="sk sk-line"></div></div>`;
  wireBack(_returnTo);
  const m = await api(`/api/movie/${id}`);
  const myRating = m.my_rating;
  const inList = m.status != null;
  const rateBtns = Array.from({ length: 10 }, (_, i) => i + 1).map(n => `<button data-n="${n}" class="${n === myRating ? "mine" : ""}">${n}</button>`).join("");
  let actions;
  if (m.status == null) actions = `<button data-set="want_to_watch" class="primary">${t("act_want")}</button><button data-set="watched">${t("act_watched")}</button>`;
  else if (m.status === "want_to_watch") actions = `<button data-set="watched" class="primary">${t("act_watched")}</button><button id="del" class="danger">${t("act_remove")}</button>`;
  else actions = `<button data-set="want_to_watch">${t("act_to_want")}</button><button id="del" class="danger">${t("act_remove")}</button>`;
  const genreChips = (m.genres || "").split(",").map(g => g.trim()).filter(Boolean).map(g => `<span class="meta-chip">${esc(g)}</span>`).join("");
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
      ${m.directors ? `<div class="meta-line">${esc(t("dir"))}${esc(m.directors)}</div>` : ""}
      ${ratingChips ? `<div class="rating-chips">${ratingChips}</div>` : ""}
      ${m.plot ? `<p class="plot">${esc(m.plot)}</p>` : ""}
      <div class="rate-label">${esc(t("my_rating"))}${inList ? "" : esc(t("rate_hint"))}</div>
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
  if (del) del.onclick = () => tg.showConfirm(t("confirm_remove", m.title), async ok => {
    if (!ok) return;
    await api(`/api/movie/${id}`, { method: "DELETE" });
    showDetail(id);
  });
}

// ── Поиск ─────────────────────────────────────────────────────────────────────
function showSearch() {
  window.scrollTo(0, 0);
  const start = emptyState("🔍", t("search_start_t"), t("search_start_s"));
  screen.innerHTML = `<div class="search-bar">${backBtn()}<input id="si" placeholder="${esc(t("search_ph"))}" autofocus></div><div id="sr">${start}</div>`;
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
          ? emptyState("⏳", t("search_toomany_t"), t("search_toomany_s"))
          : emptyState("⚠️", t("search_err_t"), String(e.message));
        return;
      }
      if (data.limited) { results.innerHTML = emptyState("⏳", t("search_limited_t"), t("search_limited_s")); return; }
      const items = data.items;
      if (!items.length) { results.innerHTML = emptyState("🤷", t("search_none_t"), t("search_none_s")); return; }
      results.replaceChildren(gridOf(items, it => posterTile(
        { poster_url: it.poster || it.poster_url, title: it.title, year: it.year, imdb_rating: it.rating },
        {
          onClick: () => tg.showConfirm(t("confirm_add", it.title), async ok => {
            if (!ok) return;
            const r = await api("/api/add", { method: "POST", body: JSON.stringify({ src: it.src, ref: it.ref }) });
            if (r.reason === "exists") tg.showAlert(t("already_in_list"));
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
  screen.innerHTML = `<div class="page-head"><h1>${esc(t("stats_title"))}</h1></div><div id="stats"><div class="empty"><div class="empty-sub">${esc(t("calc"))}</div></div></div>`;
  const s = await api("/api/stats");
  const y = s.year;
  const box = document.getElementById("stats");
  if (!s.watched && !s.want) { box.innerHTML = emptyState("📊", t("stats_empty_t"), t("stats_empty_s")); mountPartner(box); return; }
  const hours = Math.floor(s.total_runtime_min / 60);
  const tiles = `<div class="stats-grid">
    ${statTile("🎬", s.watched, t("tile_watched"))}${statTile("🔖", s.want, t("tile_want"))}
    ${statTile("⭐", s.avg_rating ?? "—", t("tile_avg"))}${statTile("⏱", hours, t("tile_hours"))}</div>`;
  const dist = s.rating_dist || [];
  const maxD = Math.max(1, ...dist);
  const hist = dist.some(v => v > 0) ? chartCard(t("chart_ratings"), `<div class="hist">${
    dist.map((c, i) => `<div class="hist-col"><div class="hist-bar-area">${c ? `<div class="hist-val">${c}</div>` : ""}<div class="hist-bar" style="height:${c ? Math.max(6, Math.round(c / maxD * 100)) : 0}%"></div></div><div class="hist-x">${i + 1}</div></div>`).join("")}</div>`) : "";
  const genres = s.top_genres_pct.length ? chartCard(t("chart_genres"), s.top_genres_pct.map(([g, p]) => hbar(g, p + "%", p)).join("")) : "";
  const maxA = s.top_actors.length ? s.top_actors[0][1] : 1;
  const actors = s.top_actors.length ? chartCard(t("chart_actors"), s.top_actors.map(([n, c]) => hbar(n, c, Math.round(c / maxA * 100))).join("")) : "";
  const maxDir = s.top_directors.length ? s.top_directors[0][1] : 1;
  const directors = s.top_directors.length ? chartCard(t("chart_directors"), s.top_directors.map(([n, c]) => hbar(n, c, Math.round(c / maxDir * 100))).join("")) : "";
  const yearCard = y.count ? chartCard(t("year_title", y.year), `
    <div class="year-line"><b>${y.count}</b> ${esc(t("count_films", y.count))}${y.avg_rating ? ` · ${esc(t("year_avg"))} <b>${y.avg_rating}</b>` : ""}</div>
    ${y.top_genre ? `<div class="year-line">${esc(t("year_fav_genre"))}${esc(y.top_genre)}</div>` : ""}
    ${y.top_actor ? `<div class="year-line">${esc(t("year_actor"))}${esc(y.top_actor[0])} <small>(${y.top_actor[1]})</small></div>` : ""}
    ${y.best_titles && y.best_titles.length ? `<div class="year-line">${esc(t("year_best"))} <small>(${y.best_avg})</small>: ${y.best_titles.map(esc).join(", ")}</div>` : ""}`) : "";
  box.innerHTML = tiles + hist + genres + actors + directors + yearCard;
  mountPartner(box);
}

// ── Пара ──────────────────────────────────────────────────────────────────────
async function mountPartner(box) {
  let p;
  try { p = await api("/api/partner"); } catch (e) { return; }
  const card = document.createElement("div");
  card.className = "chart-card partner";
  if (p.status === "paired") {
    let s;
    try { s = await api("/api/partner/stats"); } catch (e) { return; }
    const name = esc(s.partner.name || t("partner_word"));
    card.innerHTML = `
      <div class="chart-title">${t("partner_with")} ${name}</div>
      ${s.agreement != null
        ? `<div class="compat"><div class="compat-num">${s.agreement}%</div><div class="compat-lbl">${esc(t("partner_compat"))} · ${s.rated_together} ${esc(t("count_films", s.rated_together))}</div></div>`
        : `<div class="partner-sub">${esc(t("partner_no_common"))}</div>`}
      ${s.matches ? `<div class="year-line">${esc(t("partner_matches"))}: <b>${s.matches}</b></div>` : ""}
      ${s.best ? `<div class="year-line">${esc(t("partner_best"))}: ${esc(s.best.title)} <small>(${s.best.avg})</small></div>` : ""}
      ${s.controversial ? `<div class="year-line">${esc(t("partner_controversial"))}: ${esc(s.controversial.title)} <small>(${s.controversial.a} / ${s.controversial.b})</small></div>` : ""}
      ${s.top_genres.length ? `<div class="year-line">${esc(t("partner_genres"))}: ${s.top_genres.map(esc).join(", ")}</div>` : ""}
      <button class="pbtn danger" id="p-unpair">${esc(t("partner_unpair_btn"))}</button>`;
    box.prepend(card);
    card.querySelector("#p-unpair").onclick = () => tg.showConfirm(t("partner_unpair_confirm"), async ok => {
      if (!ok) return;
      await api("/api/partner/unpair", { method: "POST" });
      showStats();
    });
  } else if (p.status === "invited") {
    card.innerHTML = `<div class="chart-title">${esc(t("partner_title"))}</div>
      <div class="partner-sub">${esc(t("partner_invited_sub"))}</div>
      <div class="code-hint">${esc(t("partner_code_hint"))}</div>
      <div class="code-box" id="p-copy">${esc(p.code || "")}</div>
      <button class="pbtn primary" id="p-share">${esc(t("partner_share_btn"))}</button>
      <button class="pbtn" id="p-enter">${esc(t("partner_code_btn"))}</button>`;
    box.prepend(card);
    card.querySelector("#p-share").onclick = () => sharePartnerLink(p.link);
    card.querySelector("#p-copy").onclick = () => copyText(p.code);
    card.querySelector("#p-enter").onclick = () => partnerCodeForm(card);
  } else {
    card.innerHTML = `<div class="chart-title">${esc(t("partner_title"))}</div>
      <div class="partner-sub">${esc(t("partner_none_sub"))}</div>
      <button class="pbtn primary" id="p-invite">${esc(t("partner_invite_btn"))}</button>
      <button class="pbtn" id="p-enter">${esc(t("partner_code_btn"))}</button>`;
    box.prepend(card);
    card.querySelector("#p-invite").onclick = async () => {
      const r = await api("/api/partner/invite", { method: "POST" });
      sharePartnerLink(r.link);
      showStats();
    };
    card.querySelector("#p-enter").onclick = () => partnerCodeForm(card);
  }
}
function partnerCodeForm(card) {
  card.innerHTML = `<div class="chart-title">${esc(t("partner_code_btn"))}</div>
    <input class="code-input" id="p-code" placeholder="${esc(t("partner_code_ph"))}" autocomplete="off" autocapitalize="off">
    <button class="pbtn primary" id="p-connect">${esc(t("partner_connect"))}</button>`;
  const input = card.querySelector("#p-code");
  input.focus();
  card.querySelector("#p-connect").onclick = async () => {
    let code = input.value.trim();
    const m = code.match(/inv_[A-Za-z0-9_-]+/);  // если вставили целиком ссылку — вытащим токен
    if (m) code = m[0];
    if (!code) return;
    const r = await api("/api/partner/accept", { method: "POST", body: JSON.stringify({ token: code }) });
    if (r.ok) { tg.HapticFeedback?.notificationOccurred("success"); tg.showAlert(t("accept_ok", r.partner.name), () => showStats()); }
    else tg.showAlert(t("accept_fail_" + r.reason) || t("accept_fail_invalid"));
  };
}
function copyText(txt) {
  try { navigator.clipboard && navigator.clipboard.writeText(txt); tg.HapticFeedback?.impactOccurred("light"); } catch (e) {}
}
function sharePartnerLink(link) {
  const url = "https://t.me/share/url?url=" + encodeURIComponent(link) + "&text=" + encodeURIComponent(t("partner_share_text"));
  if (tg.openTelegramLink) tg.openTelegramLink(url); else window.open(url, "_blank");
}

async function showAcceptInvite(param) {
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="accept">
    <div class="accept-icon">💞</div>
    <div class="accept-title">${esc(t("accept_title"))}</div>
    <div class="accept-sub">${esc(t("accept_sub"))}</div>
    <div class="accept-actions">
      <button class="pbtn primary" id="acc-yes">${esc(t("accept_yes"))}</button>
      <button class="pbtn" id="acc-no">${esc(t("accept_no"))}</button>
    </div></div>`;
  document.getElementById("acc-no").onclick = () => { setActiveTab("home"); showHome(); };
  document.getElementById("acc-yes").onclick = async () => {
    const r = await api("/api/partner/accept", { method: "POST", body: JSON.stringify({ token: param }) });
    if (r.ok) {
      tg.HapticFeedback?.notificationOccurred("success");
      tg.showAlert(t("accept_ok", r.partner.name), () => { setActiveTab("stats"); showStats(); });
    } else {
      tg.showAlert(t("accept_fail_" + r.reason) || t("accept_fail_invalid"));
      setActiveTab("home"); showHome();
    }
  };
}

function statTile(icon, value, label) { return `<div class="tile"><div class="tile-icon">${icon}</div><div class="tile-val">${esc(value)}</div><div class="tile-label">${esc(label)}</div></div>`; }
function chartCard(title, inner) { return `<div class="chart-card"><div class="chart-title">${esc(title)}</div>${inner}</div>`; }
function hbar(label, valueText, pct) { return `<div class="hbar-row"><div class="hbar-label">${esc(label)}</div><div class="hbar-track"><div class="hbar-fill" style="width:${Math.max(4, pct)}%"></div></div><div class="hbar-val">${esc(valueText)}</div></div>`; }

// ── Навигация ─────────────────────────────────────────────────────────────────
function backBtn() { return `<button class="back" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button>`; }
function wireBack(fn) { const b = screen.querySelector(".back"); if (b) b.onclick = fn; }
function setActiveTab(t) { document.querySelectorAll("#tabbar .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === t)); }
function route(tab) { if (tab === "home") showHome(); else if (tab === "stats") showStats(); else showList(tab); }
// Вне Telegram (нет window.Telegram.WebApp) — не падаем, а объясняем.
if (!tg) {
  screen.innerHTML = emptyState("💬", "Откройте в Telegram", "Это мини-приложение работает внутри Telegram");
} else {
  document.querySelectorAll("#tabbar .tab").forEach(btn => { btn.onclick = () => { setActiveTab(btn.dataset.tab); route(btn.dataset.tab); }; });
  applyTabLabels();
  (async () => {
    try {
      me = await api("/api/me");
      const sp = tg.initDataUnsafe?.start_param || "";
      if (sp.startsWith("inv_")) showAcceptInvite(sp);  // пришли по инвайт-ссылке
      else showHome();
    } catch (e) {
      screen.innerHTML = emptyState("⛔", esc(e.message), t("auth_err_s"));
    }
  })();
}
