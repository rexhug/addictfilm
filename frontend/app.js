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
let _heroSource = null;      // {rect, src} стартовой точки hero-transition, захватывается в posterTile()
let _detailScrollHandler = null;  // текущий scroll-listener страницы фильма (снимается при уходе)

// ── Локализация ───────────────────────────────────────────────────────────────
function pl(n, f) { const a = Math.abs(n) % 100, b = a % 10; if (a > 10 && a < 20) return f[2]; if (b > 1 && b < 5) return f[1]; if (b === 1) return f[0]; return f[2]; }
const DICT = {
  ru: {
    tagline: "Кино, которое ты любишь",
    search_ph: "Поиск фильмов, сериалов, актёров…",
    chip_popular: "Популярное", chip_top: "Топ сообщества", chip_genres: "Жанры", chip_collections: "Подборки",
    collections_title: "Подборки", collections_empty_t: "Пока нет подборок",
    collections_empty_s: "Загляни позже", collections_empty_admin_s: "Создай первую подборку",
    collections_title_ph: "Название подборки", collections_create_btn: "Создать",
    coll_confirm_add: (t) => `Добавить «${t}» в подборку?`, coll_already_in: "Уже в этой подборке",
    coll_remove_confirm: (t) => `Убрать «${t}» из подборки?`, coll_add_film_btn: "+ Добавить фильм",
    coll_edit_hint: "Тап на фильм — убрать из подборки",
    coll_delete_btn: "Удалить подборку", coll_delete_confirm: (t) => `Удалить подборку «${t}»? Фильмы останутся в каталоге.`,
    tab_home: "Главная", tab_want: "Хочу", tab_watched: "Смотрел", tab_top: "Мой топ", tab_stats: "Статистика",
    list_want: "Хочу посмотреть", list_watched: "Смотрел", list_top: "Мой топ",
    count_films: (n) => pl(n, ["фильм", "фильма", "фильмов"]),
    rail_empty: "Пока пусто — добавь фильмы через поиск", rail_err: "Не удалось загрузить",
    genres_empty: "Каталог пока пуст",
    genre_empty_t: "Пока пусто", genre_empty_s: "В этом жанре ещё нет фильмов", load_err: "Ошибка загрузки",
    want_empty_t: "Список пуст", want_empty_s: "Добавь фильмы через поиск",
    watched_empty_t: "Пока ничего не просмотрено", watched_empty_s: "Отмечай фильмы «Смотрел»",
    top_empty_t: "Твой топ пуст", top_empty_s: "Оцени просмотренные фильмы",
    my_rating: "Моя оценка", rate_hint: " · тап = «Смотрел(а)»", dir: "Режиссёр ",
    act_want: "Хочу посмотреть", act_watched: "Отметить как просмотрено", act_to_want: "В «Хочу»", act_remove: "Убрать из списка",
    already_watched_link: "Уже смотрел? Отметить",
    my_review: "Мой отзыв", comment_ph: "Написать отзыв…",
    cast_title: "Актёры", share_text: (title) => `Смотри «${title}» в Addict Film`,
    confirm_remove: (t) => `Убрать «${t}» из своего списка?`,
    search_start_t: "Что смотрим?", search_start_s: "Введи название — минимум 2 буквы",
    search_toomany_t: "Слишком часто", search_toomany_s: "Подожди минуту и попробуй снова",
    search_err_t: "Ошибка поиска",
    search_limited_t: "Поиск временно ограничен", search_limited_s: "Дневной лимит источника. Попробуй позже",
    search_none_t: "Ничего не найдено", search_none_s: "Попробуй год или английское название",
    confirm_add: (t) => `Добавить «${t}» в «Хочу посмотреть»?`, already_in_list: "Уже в твоём списке!",
    stats_title: "Статистика", my_stats: "Моя статистика", stats_empty_t: "Пока нет статистики", stats_empty_s: "Добавь фильмы и поставь оценки", calc: "Считаю…",
    tile_watched: "просмотрено", tile_want: "в «Хочу»", tile_avg: "средняя", tile_hours: "часов",
    chart_ratings: "Мои оценки", chart_genres: "Жанры", chart_actors: "Актёры", chart_directors: "Режиссёры",
    year_title: (y) => `Итоги ${y}`, year_avg: "средняя", year_fav_genre: "Любимый жанр — ", year_actor: "Актёр года — ", year_best: "Лучшее",
    auth_err_s: "Открой через кнопку меню бота в Telegram",
    partner_title: "Пара", partner_none_sub: "Добавь партнёра — считайте совместимость вкусов вместе",
    partner_invite_btn: "Добавить партнёра", partner_invited_sub: "Приглашение готово. Отправь ссылку партнёру в Telegram.",
    partner_share_btn: "Поделиться ссылкой", partner_share_text: "Давай смотреть фильмы вместе ❤️",
    partner_with: "Пара с", partner_word: "партнёром", partner_compat: "совместимость",
    partner_no_common: "Пока нет фильмов, которые оценили оба",
    partner_matches: "Точных совпадений", partner_best: "Лучший общий", partner_controversial: "Самый спорный", partner_genres: "Общие жанры",
    partner_unpair_btn: "Разорвать пару", partner_unpair_confirm: "Разорвать пару? Личные списки останутся у каждого.",
    partner_code_btn: "У меня есть код", partner_code_ph: "Код партнёра", partner_connect: "Подключить",
    partner_code_hint: "Или отправь партнёру этот код:",
    pair_empty: "Добавляйте фильмы вместе — здесь появится ваша совместная статистика",
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
    chip_popular: "Popular", chip_top: "Community Top", chip_genres: "Genres", chip_collections: "Collections",
    collections_title: "Collections", collections_empty_t: "No collections yet",
    collections_empty_s: "Check back later", collections_empty_admin_s: "Create your first collection",
    collections_title_ph: "Collection name", collections_create_btn: "Create",
    coll_confirm_add: (t) => `Add "${t}" to the collection?`, coll_already_in: "Already in this collection",
    coll_remove_confirm: (t) => `Remove "${t}" from the collection?`, coll_add_film_btn: "+ Add film",
    coll_edit_hint: "Tap a film to remove it from the collection",
    coll_delete_btn: "Delete collection", coll_delete_confirm: (t) => `Delete collection "${t}"? Films stay in the catalog.`,
    tab_home: "Home", tab_want: "Wishlist", tab_watched: "Watched", tab_top: "My Top", tab_stats: "Stats",
    list_want: "Wishlist", list_watched: "Watched", list_top: "My Top",
    count_films: (n) => (n === 1 ? "film" : "films"),
    rail_empty: "Empty — add films via search", rail_err: "Couldn't load",
    genres_empty: "Catalog is empty yet",
    genre_empty_t: "Empty", genre_empty_s: "No films in this genre yet", load_err: "Loading error",
    want_empty_t: "List is empty", want_empty_s: "Add films via search",
    watched_empty_t: "Nothing watched yet", watched_empty_s: "Mark films as Watched",
    top_empty_t: "Your top is empty", top_empty_s: "Rate the films you've watched",
    my_rating: "My rating", rate_hint: " · tap = Watched", dir: "Director ",
    act_want: "Want to watch", act_watched: "Mark as watched", act_to_want: "To wishlist", act_remove: "Remove from list",
    already_watched_link: "Already seen it? Mark watched",
    my_review: "My review", comment_ph: "Write a review…",
    cast_title: "Cast", share_text: (title) => `Watch "${title}" on Addict Film`,
    confirm_remove: (t) => `Remove "${t}" from your list?`,
    search_start_t: "What are we watching?", search_start_s: "Type a title — at least 2 letters",
    search_toomany_t: "Too many requests", search_toomany_s: "Wait a minute and try again",
    search_err_t: "Search error",
    search_limited_t: "Search temporarily limited", search_limited_s: "Daily source limit. Try later",
    search_none_t: "Nothing found", search_none_s: "Try a year or the English title",
    confirm_add: (t) => `Add "${t}" to your wishlist?`, already_in_list: "Already in your list!",
    stats_title: "Stats", my_stats: "My stats", stats_empty_t: "No stats yet", stats_empty_s: "Add films and rate them", calc: "Calculating…",
    tile_watched: "watched", tile_want: "wishlist", tile_avg: "average", tile_hours: "hours",
    chart_ratings: "My ratings", chart_genres: "Genres", chart_actors: "Actors", chart_directors: "Directors",
    year_title: (y) => `${y} in review`, year_avg: "average", year_fav_genre: "Favorite genre — ", year_actor: "Actor of the year — ", year_best: "Best",
    auth_err_s: "Open via the bot's menu button in Telegram",
    partner_title: "Partner", partner_none_sub: "Add a partner — see how your movie tastes match",
    partner_invite_btn: "Add partner", partner_invited_sub: "Invite ready. Send the link to your partner in Telegram.",
    partner_share_btn: "Share link", partner_share_text: "Let's watch movies together ❤️",
    partner_with: "Paired with", partner_word: "partner", partner_compat: "compatibility",
    partner_no_common: "No films you both rated yet",
    partner_matches: "Exact matches", partner_best: "Best shared", partner_controversial: "Most divisive", partner_genres: "Shared genres",
    partner_unpair_btn: "Unpair", partner_unpair_confirm: "Unpair? Each keeps their personal lists.",
    partner_code_btn: "I have a code", partner_code_ph: "Partner code", partner_connect: "Connect",
    partner_code_hint: "Or send your partner this code:",
    pair_empty: "Add films together — your shared stats will show here",
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
// Постеры грузим через наш прокси /img — работает даже если CDN блокируется у клиента.
function posterSrc(u) { return u ? "/img?u=" + encodeURIComponent(u) : ""; }
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
      <div class="noposter">${esc(m.title)}</div>
      ${m.poster_url ? `<img loading="lazy" src="${posterSrc(m.poster_url)}" alt="" onerror="this.remove()">` : ""}
      ${b ? `<span class="rate">${b}</span>` : ""}
    </div>
    <div class="meta"><div class="t">${esc(m.title)}</div><div class="y">${esc(m.year || "")}</div></div>`;
  if (onClick) card.onclick = () => {
    // Захватываем стартовую точку для hero-transition ДО того, как экран будет уничтожен.
    const img = card.querySelector(".art img");
    _heroSource = img && img.currentSrc ? { rect: card.querySelector(".art").getBoundingClientRect(), src: img.currentSrc } : null;
    onClick();
  };
  return card;
}
function gridOf(items, toCard) { const g = document.createElement("div"); g.className = "grid"; for (const it of items) g.appendChild(toCard(it)); return g; }
function openDetail(id, back) { if (back) _returnTo = back; showDetail(id); }

// ── Главная ───────────────────────────────────────────────────────────────────
async function showHome() {
  unwireDetailScroll();
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
      <span class="chip" id="chip-coll"><span class="e">🎬</span>${esc(t("chip_collections"))}</span>
    </div>
    <section class="rise d3" id="sec-pop"><div class="head"><h2>${esc(t("chip_popular"))}</h2></div><div class="rail" id="rail-pop">${skeletonRail(5)}</div></section>
    <section class="rise d4" id="sec-top"><div class="head"><h2>${esc(t("chip_top"))}</h2></div><div class="rail" id="rail-top">${skeletonRail(5)}</div></section>
    <section class="rise d5" id="sec-gen"><div class="head"><h2>${esc(t("chip_genres"))}</h2></div><div class="rail grail" id="rail-gen">${skeletonRail(4)}</div></section>`;

  document.getElementById("lang-btn").onclick = () => setLang(lang === "ru" ? "en" : "ru");
  document.getElementById("home-search").onclick = () => showSearch();
  screen.querySelectorAll(".chips .chip[data-to]").forEach(c => c.onclick = () => {
    screen.querySelectorAll(".chips .chip[data-to]").forEach(x => x.classList.toggle("active", x === c));
    document.getElementById(c.dataset.to)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  // «Подборки» — не якорь на секцию, а переход на отдельный экран.
  document.getElementById("chip-coll").onclick = () => showCollections();

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
  unwireDetailScroll();
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

// ── Подборки (публичный просмотр + in-app админка для editor/admin) ───────────
function canEditCollections() { return !!(me && (me.role === "admin" || me.role === "editor")); }

function collectionCard(c) {
  // Переиспользует ту же карточку/CSS, что и обычный постер — обложка вместо рейтинга
  // показывает количество фильмов.
  const card = document.createElement("div");
  card.className = "poster";
  card.innerHTML = `
    <div class="art">
      <div class="noposter">${esc(c.title)}</div>
      ${c.cover ? `<img loading="lazy" src="${posterSrc(c.cover)}" alt="" onerror="this.remove()">` : ""}
      <span class="rate">${c.film_count} ${esc(t("count_films", c.film_count))}</span>
    </div>
    <div class="meta"><div class="t">${esc(c.title)}</div></div>`;
  card.onclick = () => showCollectionDetail(c.id);
  return card;
}

async function showCollections() {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  const canEdit = canEditCollections();
  screen.innerHTML = `<div class="sub-head">${backBtn()}<h1>${esc(t("collections_title"))}</h1>
    ${canEdit ? `<button class="back" id="coll-add" aria-label="+">+</button>` : ""}</div>
    <div id="cc">${skeletonGrid(6)}</div>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  if (canEdit) document.getElementById("coll-add").onclick = () => createCollectionFlow();
  try {
    const { items } = await api("/api/collections");
    const el = document.getElementById("cc");
    if (!items.length) {
      el.innerHTML = emptyState("🎬", t("collections_empty_t"),
        canEdit ? t("collections_empty_admin_s") : t("collections_empty_s"));
      return;
    }
    el.replaceChildren(gridOf(items, collectionCard));
  } catch (e) { document.getElementById("cc").innerHTML = emptyState("⚠️", t("load_err"), ""); }
}

function createCollectionFlow() {
  const el = document.getElementById("cc");
  if (!el) return;
  el.innerHTML = `<div class="chart-card">
    <input class="code-input" id="coll-title-input" placeholder="${esc(t("collections_title_ph"))}" autocomplete="off">
    <button class="pbtn primary" id="coll-create-btn">${esc(t("collections_create_btn"))}</button>
  </div>`;
  const input = document.getElementById("coll-title-input");
  input.focus();
  document.getElementById("coll-create-btn").onclick = async () => {
    const title = input.value.trim();
    if (!title) return;
    const r = await api("/api/admin/collections", { method: "POST", body: JSON.stringify({ title }) });
    tg.HapticFeedback?.notificationOccurred("success");
    showCollectionDetail(r.id);  // сразу открываем — удобно накидать фильмов
  };
}

async function showCollectionDetail(id) {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  const canEdit = canEditCollections();
  screen.innerHTML = `<div class="sub-head">${backBtn()}<h1 id="cd-title">…</h1></div>
    ${canEdit ? `<div class="partner-sub" style="padding:0 20px 10px;">${esc(t("coll_edit_hint"))}</div>` : ""}
    <div id="cdg">${skeletonGrid(6)}</div>
    ${canEdit ? `<div style="padding:14px 20px 4px;">
        <button class="pbtn primary" id="cd-add">${esc(t("coll_add_film_btn"))}</button>
        <button class="pbtn danger" id="cd-delete">${esc(t("coll_delete_btn"))}</button>
      </div>` : ""}`;
  wireBack(() => showCollections());
  if (canEdit) {
    document.getElementById("cd-add").onclick = () => showSearch({ type: "collection", id });
    document.getElementById("cd-delete").onclick = () => {
      const title = document.getElementById("cd-title").textContent;
      tg.showConfirm(t("coll_delete_confirm", title), async ok => {
        if (!ok) return;
        await api(`/api/admin/collections/${id}`, { method: "DELETE" });
        showCollections();
      });
    };
  }
  try {
    const c = await api(`/api/collections/${id}`);
    document.getElementById("cd-title").textContent = c.title;
    const el = document.getElementById("cdg");
    if (!c.items.length) { el.innerHTML = emptyState("🎬", t("genre_empty_t"), t("genre_empty_s")); return; }
    const back = () => showCollectionDetail(id);
    const onTile = canEdit
      ? (m) => tg.showConfirm(t("coll_remove_confirm", m.title), async ok => {
          if (!ok) return;
          await api(`/api/admin/collections/${id}/films/${m.id}`, { method: "DELETE" });
          showCollectionDetail(id);
        })
      : (m) => openDetail(m.id, back);
    el.replaceChildren(gridOf(c.items, m => posterTile(m, { onClick: () => onTile(m) })));
  } catch (e) { document.getElementById("cdg").innerHTML = emptyState("⚠️", t("load_err"), ""); }
}

// ── Личные списки ─────────────────────────────────────────────────────────────
const STATUS_MAP = { want: "want_to_watch", watched: "watched", top: "top" };
async function showList(tab) {
  unwireDetailScroll();
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
function initials(name) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
}
function shareSvg() { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v14"/></svg>`; }

function unwireDetailScroll() {
  if (_detailScrollHandler) { window.removeEventListener("scroll", _detailScrollHandler); _detailScrollHandler = null; }
}
function wireDetailScroll(backdropH) {
  const backdrop = document.getElementById("d-backdrop-img");
  const posterWrap = document.getElementById("d-poster-wrap");
  const sticky = document.getElementById("d-sticky");
  let ticking = false;
  const onScroll = () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      const y = Math.max(0, window.scrollY);
      const p = Math.min(1, y / backdropH);
      if (backdrop) { backdrop.style.opacity = String(1 - p); backdrop.style.transform = `scale(${1 + p * 0.06})`; }
      if (posterWrap) posterWrap.style.transform = `scale(${Math.max(.78, 1 - p * 0.22)})`;
      if (sticky) sticky.classList.toggle("show", y > backdropH - 44);
      ticking = false;
    });
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  _detailScrollHandler = onScroll;
}

// Hero-transition: конкретный тайл каталога «превращается» в постер карточки.
// Ghost-элемент — обычный <img>, летит transform-ом (только translate+scale) поверх
// уже отрендеренного экрана; реальный постер на это время скрыт (opacity), чтобы
// не было двойного изображения. Закрытие — см. closeDetailThen ниже.
function runHeroTransition() {
  if (!_heroSource) return;
  const target = document.querySelector(".d-poster");
  if (!target) { _heroSource = null; return; }
  const endRect = target.getBoundingClientRect();
  const startRect = _heroSource.rect;
  const ghost = document.createElement("div");
  ghost.className = "hero-ghost";
  ghost.style.width = `${startRect.width}px`;
  ghost.style.height = `${startRect.height}px`;
  ghost.style.transform = `translate(${startRect.left}px,${startRect.top}px)`;
  ghost.innerHTML = `<img src="${_heroSource.src}">`;
  document.body.appendChild(ghost);
  target.style.opacity = "0";
  const sx = endRect.width / startRect.width, sy = endRect.height / startRect.height;
  requestAnimationFrame(() => {
    ghost.style.transition = "transform .32s cubic-bezier(.2,.8,.2,1)";
    ghost.style.transform = `translate(${endRect.left}px,${endRect.top}px) scale(${sx},${sy})`;
    ghost.addEventListener("transitionend", () => { ghost.remove(); target.style.opacity = "1"; }, { once: true });
  });
  _heroSource = null;
}
// Закрытие карточки: экран целиком уходит вниз со сжатием — архитектура приложения
// не хранит предыдущий экран в DOM (полная перерисовка на каждой навигации), поэтому
// точный обратный shared-element недостижим без переписывания роутинга; symmetric
// по ощущению «сжатие в точку выхода» — тот же transform/opacity словарь, что и открытие.
function closeDetailThen(fn) {
  unwireDetailScroll();
  const el = screen.querySelector(".detail-v2");
  if (!el) { fn(); return; }
  el.style.transition = "transform .22s cubic-bezier(.2,.8,.2,1), opacity .22s";
  el.style.transformOrigin = "center top";
  el.style.transform = "scale(.96) translateY(10px)";
  el.style.opacity = "0";
  setTimeout(fn, 190);
}

async function showDetail(id) {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="detail-v2">
    <div class="d-backdrop sk"></div>
    <div class="d-body"><div class="d-poster-wrap"><div class="d-poster sk"></div></div>
      <div class="sk sk-line wide"></div><div class="sk sk-line"></div></div>
    <div class="d-floatctrls" style="position:fixed;top:0;left:0;right:0;padding:calc(10px + env(safe-area-inset-top)) 14px 0;z-index:41;">${backBtn()}</div>
  </div>`;
  wireBack(() => closeDetailThen(_returnTo));
  const m = await api(`/api/movie/${id}`);
  renderDetail(id, m);
}

function renderDetail(id, m) {
  const genres = (m.genres || "").split(",").map(g => g.trim()).filter(Boolean).join(" · ");
  const metaParts = [m.year, m.age_rating, m.runtime].filter(Boolean);
  const bdUrl = m.backdrop_url || m.poster_url;
  // actors_photos — те же имена, что в actors, но с фото (только с kinopoisk-пути,
  // см. search.py). Нет фото у конкретного источника/актёра — падаем на инициалы.
  let cast;
  try { cast = m.actors_photos ? JSON.parse(m.actors_photos) : null; } catch (e) { cast = null; }
  if (!cast || !cast.length) {
    cast = (m.actors || "").split(",").map(a => a.trim()).filter(Boolean).map(name => ({ name, photo_url: null }));
  }
  cast = cast.slice(0, 10);

  screen.innerHTML = `
    <div class="detail-v2">
      <div class="d-sticky" id="d-sticky">
        <button class="d-ctrl" id="d-back-sticky" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button>
        <span class="t">${esc(m.title)}</span>
        <button class="d-ctrl" id="d-more-sticky" aria-label="Share">${shareSvg()}</button>
      </div>
      <div class="d-backdrop${bdUrl ? "" : " no-bd"}" id="d-backdrop">
        ${bdUrl ? `<img id="d-backdrop-img" src="${posterSrc(bdUrl)}" alt="">` : ""}
        <div class="d-scrim-t"></div><div class="d-scrim-b"></div>
        <div class="d-floatctrls">
          <button class="d-ctrl" id="d-back-top" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button>
          <button class="d-ctrl" id="d-more-top" aria-label="Share">${shareSvg()}</button>
        </div>
      </div>
      <div class="d-body">
        <div class="d-poster-wrap" id="d-poster-wrap">
          <div class="d-poster">
            <span class="fb">${esc(m.title)}</span>
            ${m.poster_url ? `<img src="${posterSrc(m.poster_url)}" alt="" onerror="this.remove()">` : ""}
          </div>
        </div>
        <h1 class="d-title">${esc(m.title)}</h1>
        ${m.title_original && m.title_original !== m.title ? `<div class="d-original">${esc(m.title_original)}</div>` : ""}
        ${metaParts.length ? `<div class="d-meta">${metaParts.map(esc).join(" · ")}</div>` : ""}
        ${genres ? `<div class="d-genres">${esc(genres)}</div>` : ""}
        ${m.directors ? `<div class="d-director">${esc(t("dir"))}<b>${esc(m.directors)}</b></div>` : ""}
        ${ratingsHTML(m)}
        ${m.plot ? `<p class="d-overview">${esc(m.plot)}</p>` : ""}
        <div id="d-actions"></div>
        <div class="d-review" id="d-review">
          <div class="d-review-h">${esc(t("my_review"))}</div>
          <div class="d-stars" id="d-stars"></div>
          <div id="d-comment-zone"></div>
        </div>
        ${cast.length ? `<div class="d-cast"><div class="d-cast-h"><h2>${esc(t("cast_title"))}</h2></div>
          <div class="d-cast-rail">${cast.map(a => `<div class="d-cast-item"><div class="d-avatar"><span class="fb">${esc(initials(a.name))}</span>${a.photo_url ? `<img loading="lazy" src="${posterSrc(a.photo_url)}" alt="" onerror="this.remove()">` : ""}</div><div class="n">${esc(a.name)}</div></div>`).join("")}</div></div>` : ""}
      </div>
    </div>`;

  renderStars(id, m);
  renderComment(id, m);
  renderActions(id, m);

  const back = () => closeDetailThen(_returnTo);
  document.getElementById("d-back-top").onclick = back;
  document.getElementById("d-back-sticky").onclick = back;
  document.getElementById("d-more-top").onclick = () => shareMovie(m);
  document.getElementById("d-more-sticky").onclick = () => shareMovie(m);

  const bdImg = document.getElementById("d-backdrop-img");
  const startScroll = () => {
    const h = document.getElementById("d-backdrop").getBoundingClientRect().height;
    wireDetailScroll(h);
    runHeroTransition();
  };
  if (bdImg && !bdImg.complete) bdImg.addEventListener("load", startScroll, { once: true });
  else requestAnimationFrame(startScroll);
}

function ratingsHTML(m) {
  const pills = [];
  if (m.kp_rating) pills.push(`<div class="d-rpill"><div class="v">${esc(m.kp_rating)}</div><div class="l">КП</div></div>`);
  if (m.imdb_rating) pills.push(`<div class="d-rpill"><div class="v">${esc(m.imdb_rating)}</div><div class="l">IMDb</div></div>`);
  if (m.community && m.community.count) pills.push(`<div class="d-rpill accent"><div class="v">${esc(m.community.avg)}</div><div class="l">${lang === "ru" ? "Комьюнити" : "Community"}</div><div class="c">${m.community.count} ${lang === "ru" ? pl(m.community.count, ["оценка", "оценки", "оценок"]) : (m.community.count === 1 ? "rating" : "ratings")}</div></div>`);
  return pills.length ? `<div class="d-ratings">${pills.join("")}</div>` : "";
}

function renderStars(id, m) {
  const el = document.getElementById("d-stars");
  if (!el) return;
  el.innerHTML = Array.from({ length: 10 }, (_, i) => i + 1)
    .map(n => `<button data-n="${n}" class="${n === m.my_rating ? "on" : ""}">${n}</button>`).join("");
  el.querySelectorAll("button").forEach(b => b.onclick = async () => {
    tg.HapticFeedback?.impactOccurred("light");
    const n = +b.dataset.n;
    if (n === m.my_rating) {
      // Повторный тап по своей же звезде — снять оценку (статус «Смотрел» не трогаем).
      await api(`/api/movie/${id}/rate`, { method: "DELETE" });
      m.my_rating = null;
    } else {
      await api(`/api/movie/${id}/rate`, { method: "POST", body: JSON.stringify({ rating: n }) });
      m.my_rating = n;
      if (m.status !== "watched") m.status = "watched";  // сервер неявно отмечает «Смотрел» при оценке
    }
    renderStars(id, m);
    renderActions(id, m);
  });
}

function renderComment(id, m) {
  const zone = document.getElementById("d-comment-zone");
  if (!zone) return;
  const has = !!(m.my_comment && m.my_comment.trim());
  zone.innerHTML = `<div class="d-comment${has ? "" : " ph"}" id="d-comment-view">${has ? esc(m.my_comment) : esc(t("comment_ph"))}</div>`;
  document.getElementById("d-comment-view").onclick = () => {
    zone.innerHTML = `<textarea class="d-comment-input" id="d-comment-input" rows="1" placeholder="${esc(t("comment_ph"))}">${esc(m.my_comment || "")}</textarea>`;
    const ta = document.getElementById("d-comment-input");
    const grow = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; };
    ta.addEventListener("input", grow); grow(); ta.focus();
    const place = ta.value.length; ta.setSelectionRange(place, place);
    ta.onblur = async () => {
      const text = ta.value.trim();
      if (text !== (m.my_comment || "").trim()) {
        await api(`/api/movie/${id}/comment`, { method: "POST", body: JSON.stringify({ text }) });
        m.my_comment = text;
      }
      renderComment(id, m);
    };
  };
}

function renderActions(id, m) {
  const el = document.getElementById("d-actions");
  if (!el) return;
  // Share живёт только в плавающем контроле (виден на любой прокрутке) — не дублируем здесь.
  if (m.status == null) {
    el.innerHTML = `<div class="d-actions"><button class="d-cta primary" id="d-primary">${esc(t("act_want"))}</button></div>
      <div class="d-status-links"><button id="d-quick-watched">${esc(t("already_watched_link"))}</button></div>`;
  } else if (m.status === "want_to_watch") {
    el.innerHTML = `<div class="d-actions"><button class="d-cta primary" id="d-primary">${esc(t("act_watched"))}</button></div>
      <div class="d-status-links"><button class="danger" id="d-remove">${esc(t("act_remove"))}</button></div>`;
  } else {
    // status === "watched": ни одной filled-кнопки — звёздный рейтинг ниже становится
    // единственным акцентным элементом (первичное взаимодействие сместилось на оценку).
    el.innerHTML = `<div class="d-status-links"><button id="d-to-want">${esc(t("act_to_want"))}</button><button class="danger" id="d-remove">${esc(t("act_remove"))}</button></div>`;
  }
  const setStatus = async (status) => {
    tg.HapticFeedback?.impactOccurred("light");
    await api(`/api/movie/${id}/status`, { method: "POST", body: JSON.stringify({ status }) });
    m.status = status;
    renderActions(id, m);
  };
  document.getElementById("d-primary")?.addEventListener("click", () => setStatus(m.status == null ? "want_to_watch" : "watched"));
  document.getElementById("d-quick-watched")?.addEventListener("click", () => setStatus("watched"));
  document.getElementById("d-to-want")?.addEventListener("click", () => setStatus("want_to_watch"));
  document.getElementById("d-remove")?.addEventListener("click", () => tg.showConfirm(t("confirm_remove", m.title), async ok => {
    if (!ok) return;
    await api(`/api/movie/${id}`, { method: "DELETE" });
    m.status = null; m.my_rating = null; m.my_comment = null;
    renderActions(id, m); renderStars(id, m); renderComment(id, m);
  }));
}

function shareMovie(m) {
  const url = "https://t.me/share/url?url=" + encodeURIComponent(m.share_link || "") + "&text=" + encodeURIComponent(t("share_text", m.title));
  if (tg.openTelegramLink) tg.openTelegramLink(url); else window.open(url, "_blank");
}

// ── Поиск ─────────────────────────────────────────────────────────────────────
function showSearch(mode = null) {
  // mode: null — обычное добавление в свой список; {type:"collection", id} — тап по
  // результату добавляет фильм в подборку id (используется showCollectionDetail).
  unwireDetailScroll();
  window.scrollTo(0, 0);
  const start = emptyState("🔍", t("search_start_t"), t("search_start_s"));
  screen.innerHTML = `<div class="search-bar">${backBtn()}<input id="si" placeholder="${esc(t("search_ph"))}" autofocus></div><div id="sr">${start}</div>`;
  wireBack(mode ? () => showCollectionDetail(mode.id) : () => { setActiveTab("home"); showHome(); });
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
          onClick: () => tg.showConfirm(
            mode ? t("coll_confirm_add", it.title) : t("confirm_add", it.title),
            async ok => {
              if (!ok) return;
              if (mode) {
                const r = await api(`/api/admin/collections/${mode.id}/films`,
                  { method: "POST", body: JSON.stringify({ src: it.src, ref: it.ref }) });
                tg.HapticFeedback?.notificationOccurred("success");
                if (!r.added) tg.showAlert(t("coll_already_in"), () => showCollectionDetail(mode.id));
                else showCollectionDetail(mode.id);
              } else {
                const r = await api("/api/add", { method: "POST", body: JSON.stringify({ src: it.src, ref: it.ref }) });
                if (r.reason === "exists") tg.showAlert(t("already_in_list"));
                else { tg.HapticFeedback?.notificationOccurred("success"); setActiveTab("want"); showList("want"); }
              }
            }),
        })));
    }, 400);
  };
  input.focus();
}

// ── Статистика (пара в приоритете сверху, личная — ниже) ──────────────────────
async function showStats() {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="page-head"><h1>${esc(t("stats_title"))}</h1></div><div id="stats"><div class="empty"><div class="empty-sub">${esc(t("calc"))}</div></div></div>`;
  const box = document.getElementById("stats");

  // 1. Пара — приоритетно, первым блоком.
  let partner = { status: "none" }, pstats = null;
  try { partner = await api("/api/partner"); } catch (e) {}
  if (partner.status === "paired") { try { pstats = await api("/api/partner/stats"); } catch (e) {} }

  // 2. Личная статистика за всё время — ниже.
  const s = await api("/api/stats");
  const personal = (!s.watched && !s.want)
    ? emptyState("📊", t("stats_empty_t"), t("stats_empty_s"))
    : personalStatsHTML(s);

  if (partner.status === "paired" && pstats) {
    // Пара: полная статистика пары (тот же формат) сверху, «Моя статистика» ниже.
    const name = esc(pstats.partner.name || t("partner_word"));
    const pairHasData = pstats.watched || pstats.want || pstats.rated_together;
    box.innerHTML =
      `<div class="sec-label">${t("partner_with")} ${name}</div>` +
      pairHeroHTML(pstats) +
      (pairHasData ? personalStatsHTML(pstats) : "") +
      `<div class="sec-label">${esc(t("my_stats"))}</div>` +
      personal;
  } else {
    box.innerHTML = partnerCardHTML(partner, null) + personal;
  }
  wirePartner(box);
}

function pairHeroHTML(ps) {
  const empty = !ps.watched && !ps.want && !ps.rated_together;
  const body = empty
    ? `<div class="partner-sub">${esc(t("pair_empty"))}</div>`
    : `${ps.agreement != null
        ? `<div class="compat"><div class="compat-num">${ps.agreement}%</div><div class="compat-lbl">${esc(t("partner_compat"))} · ${ps.rated_together} ${esc(t("count_films", ps.rated_together))}</div></div>`
        : `<div class="partner-sub">${esc(t("partner_no_common"))}</div>`}
      ${ps.matches ? `<div class="year-line">${esc(t("partner_matches"))}: <b>${ps.matches}</b></div>` : ""}
      ${ps.best ? `<div class="year-line">${esc(t("partner_best"))}: ${esc(ps.best.title)} <small>(${ps.best.avg})</small></div>` : ""}
      ${ps.controversial ? `<div class="year-line">${esc(t("partner_controversial"))}: ${esc(ps.controversial.title)} <small>(${ps.controversial.a} / ${ps.controversial.b})</small></div>` : ""}`;
  return `<div class="chart-card partner">${body}<button class="pbtn danger" id="p-unpair">${esc(t("partner_unpair_btn"))}</button></div>`;
}

function personalStatsHTML(s) {
  const y = s.year;
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
  return tiles + hist + genres + actors + directors + yearCard;
}

// ── Пара ──────────────────────────────────────────────────────────────────────
function partnerCardHTML(p, ps) {
  if (p.status === "paired") {
    const name = esc((p.partner && p.partner.name) || t("partner_word"));
    let body = "";
    if (ps) {
      body = `${ps.agreement != null
          ? `<div class="compat"><div class="compat-num">${ps.agreement}%</div><div class="compat-lbl">${esc(t("partner_compat"))} · ${ps.rated_together} ${esc(t("count_films", ps.rated_together))}</div></div>`
          : `<div class="partner-sub">${esc(t("partner_no_common"))}</div>`}
        ${ps.matches ? `<div class="year-line">${esc(t("partner_matches"))}: <b>${ps.matches}</b></div>` : ""}
        ${ps.best ? `<div class="year-line">${esc(t("partner_best"))}: ${esc(ps.best.title)} <small>(${ps.best.avg})</small></div>` : ""}
        ${ps.controversial ? `<div class="year-line">${esc(t("partner_controversial"))}: ${esc(ps.controversial.title)} <small>(${ps.controversial.a} / ${ps.controversial.b})</small></div>` : ""}
        ${ps.top_genres.length ? `<div class="year-line">${esc(t("partner_genres"))}: ${ps.top_genres.map(esc).join(", ")}</div>` : ""}`;
    }
    return `<div class="chart-card partner"><div class="chart-title">${t("partner_with")} ${name}</div>${body}
      <button class="pbtn danger" id="p-unpair">${esc(t("partner_unpair_btn"))}</button></div>`;
  }
  if (p.status === "invited") {
    return `<div class="chart-card partner"><div class="chart-title">${esc(t("partner_title"))}</div>
      <div class="partner-sub">${esc(t("partner_invited_sub"))}</div>
      <div class="code-hint">${esc(t("partner_code_hint"))}</div>
      <div class="code-box" id="p-copy" data-code="${esc(p.code || "")}">${esc(p.code || "")}</div>
      <button class="pbtn primary" id="p-share" data-link="${esc(p.link || "")}">${esc(t("partner_share_btn"))}</button>
      <button class="pbtn" id="p-enter">${esc(t("partner_code_btn"))}</button></div>`;
  }
  return `<div class="chart-card partner"><div class="chart-title">${esc(t("partner_title"))}</div>
    <div class="partner-sub">${esc(t("partner_none_sub"))}</div>
    <button class="pbtn primary" id="p-invite">${esc(t("partner_invite_btn"))}</button>
    <button class="pbtn" id="p-enter">${esc(t("partner_code_btn"))}</button></div>`;
}

function wirePartner(box) {
  const unpair = box.querySelector("#p-unpair");
  if (unpair) unpair.onclick = () => tg.showConfirm(t("partner_unpair_confirm"), async ok => {
    if (!ok) return;
    await api("/api/partner/unpair", { method: "POST" });
    showStats();
  });
  const share = box.querySelector("#p-share");
  if (share) share.onclick = () => sharePartnerLink(share.dataset.link);
  const copy = box.querySelector("#p-copy");
  if (copy) copy.onclick = () => copyText(copy.dataset.code);
  const enter = box.querySelector("#p-enter");
  if (enter) enter.onclick = () => partnerCodeForm(box.querySelector(".partner"));
  const invite = box.querySelector("#p-invite");
  if (invite) invite.onclick = async () => {
    const r = await api("/api/partner/invite", { method: "POST" });
    sharePartnerLink(r.link);
    showStats();
  };
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
  unwireDetailScroll();
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
      else if (sp.startsWith("film_")) openDetail(+sp.slice(5));  // пришли по ссылке «Поделиться» фильмом
      else showHome();
    } catch (e) {
      screen.innerHTML = emptyState("⛔", esc(e.message), t("auth_err_s"));
    }
  })();
}
