// Addict Film — премиум-редизайн + локализация RU/EN.
// Фиксированная high-end тёмная тема (не зависит от темы Telegram).

const tg = window.Telegram && window.Telegram.WebApp;  // вне Telegram — null, не падаем
if (tg) {
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor("#050505"); tg.setBackgroundColor("#050505"); } catch (e) {}
  // Telegram otherwise treats a fast vertical feed swipe as a gesture to collapse
  // the Mini App. CSS can prevent browser overscroll, but only this native API
  // prevents the Telegram container itself from handling the gesture.
  try { tg.disableVerticalSwipes?.(); } catch (e) {}

  // Полноэкранный режим: контент уходит под системную область iPhone и под
  // «шапку» Telegram (кнопки Закрыть/⋯). Пробрасываем их высоту в CSS-переменную
  // --tg-inset-top; заголовки экранов начинаются ниже неё (см. --safe-top в CSS).
  // safeAreaInset — вырез устройства, contentSafeAreaInset — панель самого Telegram.
  const applyTgInsets = () => {
    const sa = tg.safeAreaInset || {}, ca = tg.contentSafeAreaInset || {};
    const top = Math.max(0, (sa.top || 0) + (ca.top || 0));
    const bottom = Math.max(0, (sa.bottom || 0) + (ca.bottom || 0));
    const root = document.documentElement.style;
    root.setProperty("--tg-inset-top", top + "px");
    root.setProperty("--tg-inset-bottom", bottom + "px");
  };
  applyTgInsets();
  ["safeAreaChanged", "contentSafeAreaChanged", "fullscreenChanged"].forEach(
    ev => { try { tg.onEvent?.(ev, applyTgInsets); } catch (e) {} });
  // Инсеты иногда приходят чуть позже готовности — добираем повторной попыткой.
  setTimeout(applyTgInsets, 300);
}

const screen = document.getElementById("screen");
let me = null;
let _returnTo = () => { setActiveTab("home"); showHome(); };
let _heroSource = null;      // {rect, src} стартовой точки hero-transition, захватывается в posterTile()
let _detailScrollHandler = null;  // текущий scroll-listener страницы фильма (снимается при уходе)
let _detailLoadController = null; // отменяет устаревший detail-fetch при быстром переходе
let _tabbarScrollHandler = null;
// Короткий session cache для тяжёлых home-rails. Он живёт только пока открыт
// Mini App и сбрасывается после любого изменения списка/оценки, поэтому UI не
// показывает устаревший статус фильма.
const _readCache = new Map();
const _READ_CACHE_TTL = 30_000;
let _notificationUnread = 0;

function cacheableRead(path, opts) {
  const method = (opts.method || "GET").toUpperCase();
  return method === "GET" && (path.startsWith("/api/browse") || path === "/api/genres" || path === "/api/collections");
}

// ── Локализация ───────────────────────────────────────────────────────────────
function pl(n, f) { const a = Math.abs(n) % 100, b = a % 10; if (a > 10 && a < 20) return f[2]; if (b > 1 && b < 5) return f[1]; if (b === 1) return f[0]; return f[2]; }
const DICT = {
  ru: {
    tagline: "Кино, которое ты любишь",
    greeting: (n) => `Привет, ${n}`,
    search_ph: "Поиск фильмов, сериалов, актёров…",
    chip_popular: "Популярное", chip_top: "Топ сообщества", chip_genres: "Жанры", chip_collections: "Подборки",
    see_all: "Смотреть все",
    reco_title: "Оценивай и получай рекомендации", reco_sub: "Персональные подборки на основе твоих оценок", reco_cta: "Начать",
    notif_title: "Уведомления", notif_empty_t: "Уведомлений пока нет", notif_empty_s: "Здесь появятся важные события вашей пары", notif_mark_all: "Прочитать все", notif_load_more: "Показать ещё", notif_loading: "Загружаю уведомления…", notif_error: "Не удалось загрузить уведомления", notif_retry: "Повторить", notif_now: "только что", notif_min_ago: (n) => `${n} мин назад`, notif_hour_ago: (n) => `${n} ч назад`, notif_day_ago: (n) => `${n} дн назад`, notif_inapp: "В приложении", notif_telegram: "В Telegram", notif_telegram_hint: "События пары от бота Addict Film", notif_telegram_unavailable: "Бот сейчас недоступен", notif_browser: "В браузере", notif_browser_hint: "Локальные напоминания на этом устройстве",
    back: "Назад", settings_title: "Настройки", settings_loading: "Загружаю настройки…",
    settings_notifications: "Уведомления", settings_notifications_hint: "Важные события пары всегда видны в приложении", settings_notifications_on: "Включены", settings_notifications_off: "Выключены", settings_notifications_permission: "Нужно разрешение", settings_notifications_denied: "Разрешения отключены в Telegram или браузере", settings_notifications_unavailable: "Недоступны на этом устройстве", settings_notifications_error: "Не удалось запросить разрешение",
    settings_language: "Язык", settings_language_hint: "Изменится сразу во всём приложении", settings_language_ru: "Русский", settings_language_en: "English",
    settings_pair: "Пара", settings_pair_none: "Создайте пару, чтобы смотреть и оценивать фильмы вместе", settings_pair_create: "Создать пару", settings_pair_current: "Ваша пара", settings_pair_manage: "Управление парой", settings_pair_invited: "Приглашение ожидает принятия", settings_pair_load_error: "Не удалось загрузить статус пары", settings_pair_try_again: "Повторить",
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
    load_more: "Показать ещё", loading: "Загрузка…", retry: "Повторить",
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
    stats_title: "Мой кинопрофиль", my_stats: "Моя статистика", stats_empty_t: "Пока нет статистики", stats_empty_s: "Добавь фильмы и поставь оценки", calc: "Считаю…",
    stats_profile_fallback: "Киноман", stats_profile_sub: "Твоя история в кино", stats_more: "Показать ещё", stats_less: "Свернуть", stats_view_all: "Посмотреть всех",
    stats_me_tab: "Я", stats_together_tab: "Мы вместе", stats_taste: "Твой вкус", stats_together: "Ваша история",
    stats_ratings_hint: "Сколько фильмов ты поставил(а) на каждую оценку", stats_ratings_hint_pair: "Сколько общих фильмов попало в каждую оценку", stats_genres_hint: "Доля жанра среди просмотренных фильмов",
    stats_people_hint: "Чаще всего встречаются в просмотренных фильмах", stats_directors_hint: "Чаще всего среди просмотренных фильмов", stats_people_hint_pair: "Чаще всего встречаются в общих фильмах", stats_directors_hint_pair: "Чаще всего среди общих фильмов", stats_films: (n) => `${n} ${pl(n, ["фильм", "фильма", "фильмов"])}`,
    stats_favorite_actor: "Любимый актёр", stats_favorite_director: "Любимый режиссёр", stats_actors_empty: "Недостаточно данных об актёрах", stats_directors_empty: "Недостаточно данных о режиссёрах", stats_people_empty_hint: "Статистика появится после просмотра фильмов.", stats_person_films: (name) => `Фильмы с ${name}`, stats_person_films_pair: (name) => `Общие фильмы с ${name}`, stats_person_open: (name) => `Открыть фильмы с ${name}`, stats_person_empty: "Таких просмотренных фильмов пока нет",
    stats_taste_hint: (genre, rating) => genre ? `Тебе особенно нравятся ${genre}; чаще всего ты ставишь ${rating}.` : `Чаще всего ты ставишь ${rating}.`,
    tile_watched: "просмотрено", tile_want: "в «Хочу»", tile_shared_watched: "вместе посмотрено", tile_shared_want: "вместе в «Хочу»", tile_avg: "средняя", tile_hours: "часов",
    chart_ratings: "Как ты оцениваешь фильмы", chart_ratings_pair: "Общие оценки", chart_genres: "Жанры", chart_actors: "Актёры", chart_directors: "Режиссёры",
    year_title: (y) => `Итоги ${y}`, year_avg: "средняя", year_fav_genre: "Любимый жанр — ", year_actor: "Актёр года — ", year_best: "Лучшее",
    auth_err_s: "Открой через кнопку меню бота в Telegram",
    partner_title: "Пара", partner_none_sub: "Добавь партнёра — считайте совместимость вкусов вместе",
    partner_invite_btn: "Добавить партнёра", partner_invited_sub: "Приглашение готово. Отправь ссылку партнёру в Telegram.",
    partner_share_btn: "Поделиться ссылкой", partner_share_text: "Давай смотреть фильмы вместе ❤️",
    partner_with: "Пара с", partner_word: "партнёром", partner_compat: "совместимость",
    pair_title: "Мы вместе", pair_subtitle: "Общие фильмы и ваш вкус", pair_compat_title: "Совместимость вкусов",
    pair_common_favorites: "Общие любимчики", pair_common_favorites_hint: "Фильмы, которые понравились вам обоим",
    pair_disagreements: "Наши расхождения", pair_disagreements_hint: "Где ваши оценки расходятся сильнее всего",
    pair_loved_by_both: "Понравилось вам обоим", pair_rating_you: "Вы", pair_rating_partner: "Партнёр", pair_difference: "Разница",
    pair_more: "Показать все",
    partner_explainer: (n) => `Считаем по разнице ваших оценок у ${n} ${pl(n, ["общего фильма", "общих фильмов", "общих фильмов"])}. Чем ближе к 100%, тем чаще вы согласны.`,
    partner_settings: "Настройки пары", partner_exact_hint: "Одинаковые оценки", partner_shared_best: "Ваш фаворит", partner_shared_dispute: "Самое большое расхождение",
    partner_no_common: "Пока нет фильмов, которые оценили оба",
    partner_matches: "Точных совпадений", partner_best: "Лучший общий", partner_controversial: "Самый спорный", partner_genres: "Общие жанры",
    partner_unpair_btn: "Разорвать пару", partner_unpair_confirm: "Разорвать пару? Личные списки останутся у каждого.", partner_unpair_success: "Пара завершена.",
    partner_code_btn: "У меня есть код", partner_code_ph: "Код партнёра", partner_connect: "Подключить",
    partner_code_hint: "Или отправь партнёру этот код:",
    pair_empty: "Добавляйте фильмы вместе — здесь появится ваша совместная статистика",
    accept_title: "Приглашение в пару", accept_title_from: (name) => `Вас зовёт ${name} в пару`, accept_sub: "Отмечайте и оценивайте фильмы вместе. У вас будет общая статистика и совместимость.",
    accept_yes: "Принять", accept_no: "Не сейчас",
    accept_ok: (name) => `Готово! Теперь вы в паре${name ? ` с ${name}` : ""}.`,
    accept_fail_invalid: "Приглашение недействительно или уже использовано.",
    accept_fail_self: "Нельзя принять собственное приглашение 🙂",
    accept_fail_inviter_taken: "У пригласившего уже есть пара.",
    accept_fail_already_paired: "У вас уже есть пара. Сначала разорвите текущую.",
  },
  en: {
    tagline: "Movies you'll love",
    greeting: (n) => `Hi, ${n}`,
    search_ph: "Search movies, TV shows, actors…",
    chip_popular: "Popular", chip_top: "Community Top", chip_genres: "Genres", chip_collections: "Collections",
    see_all: "See all",
    reco_title: "Rate films, get recommendations", reco_sub: "Personal picks based on your ratings", reco_cta: "Start",
    notif_title: "Notifications", notif_empty_t: "No notifications yet", notif_empty_s: "Important pair events will appear here", notif_mark_all: "Mark all read", notif_load_more: "Show more", notif_loading: "Loading notifications…", notif_error: "Couldn't load notifications", notif_retry: "Try again", notif_now: "just now", notif_min_ago: (n) => `${n}m ago`, notif_hour_ago: (n) => `${n}h ago`, notif_day_ago: (n) => `${n}d ago`, notif_inapp: "In app", notif_telegram: "In Telegram", notif_telegram_hint: "Pair events from the Addict Film bot", notif_telegram_unavailable: "The bot is unavailable right now", notif_browser: "In browser", notif_browser_hint: "Local reminders on this device",
    back: "Back", settings_title: "Settings", settings_loading: "Loading settings…",
    settings_notifications: "Notifications", settings_notifications_hint: "Important pair events are always shown in the app", settings_notifications_on: "On", settings_notifications_off: "Off", settings_notifications_permission: "Permission needed", settings_notifications_denied: "Notifications are blocked in Telegram or your browser", settings_notifications_unavailable: "Unavailable on this device", settings_notifications_error: "Couldn't request permission",
    settings_language: "Language", settings_language_hint: "Applies immediately across the app", settings_language_ru: "Русский", settings_language_en: "English",
    settings_pair: "Partner", settings_pair_none: "Create a pair to watch and rate films together", settings_pair_create: "Create a pair", settings_pair_current: "Your pair", settings_pair_manage: "Manage pair", settings_pair_invited: "Invite is waiting to be accepted", settings_pair_load_error: "Couldn't load pair status", settings_pair_try_again: "Try again",
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
    load_more: "Show more", loading: "Loading…", retry: "Retry",
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
    stats_title: "My movie profile", my_stats: "My stats", stats_empty_t: "No stats yet", stats_empty_s: "Add films and rate them", calc: "Calculating…",
    stats_profile_fallback: "Movie fan", stats_profile_sub: "Your story in movies", stats_more: "Show more", stats_less: "Show less", stats_view_all: "View all",
    stats_me_tab: "Me", stats_together_tab: "Together", stats_taste: "Your taste", stats_together: "Your story",
    stats_ratings_hint: "How many films you gave each rating", stats_ratings_hint_pair: "How many shared films received each rating", stats_genres_hint: "Genre share among watched films",
    stats_people_hint: "Most frequent in watched films", stats_directors_hint: "Most frequent among watched films", stats_people_hint_pair: "Most frequent in shared films", stats_directors_hint_pair: "Most frequent among shared films", stats_films: (n) => `${n} ${n === 1 ? "film" : "films"}`,
    stats_favorite_actor: "Favorite actor", stats_favorite_director: "Favorite director", stats_actors_empty: "Not enough actor data", stats_directors_empty: "Not enough director data", stats_people_empty_hint: "Statistics will appear after you watch films.", stats_person_films: (name) => `Films with ${name}`, stats_person_films_pair: (name) => `Shared films with ${name}`, stats_person_open: (name) => `Open films with ${name}`, stats_person_empty: "No watched films found yet",
    stats_taste_hint: (genre, rating) => genre ? `You lean toward ${genre} and most often give ${rating}.` : `You most often give ${rating}.`,
    tile_watched: "watched", tile_want: "wishlist", tile_shared_watched: "watched together", tile_shared_want: "shared wishlist", tile_avg: "average", tile_hours: "hours",
    chart_ratings: "How you rate movies", chart_ratings_pair: "Shared ratings", chart_genres: "Genres", chart_actors: "Actors", chart_directors: "Directors",
    year_title: (y) => `${y} in review`, year_avg: "average", year_fav_genre: "Favorite genre — ", year_actor: "Actor of the year — ", year_best: "Best",
    auth_err_s: "Open via the bot's menu button in Telegram",
    partner_title: "Partner", partner_none_sub: "Add a partner — see how your movie tastes match",
    partner_invite_btn: "Add partner", partner_invited_sub: "Invite ready. Send the link to your partner in Telegram.",
    partner_share_btn: "Share link", partner_share_text: "Let's watch movies together ❤️",
    partner_with: "Paired with", partner_word: "partner", partner_compat: "compatibility",
    pair_title: "Together", pair_subtitle: "Shared films and your taste", pair_compat_title: "Taste compatibility",
    pair_common_favorites: "Shared favorites", pair_common_favorites_hint: "Films you both enjoyed",
    pair_disagreements: "Where you differ", pair_disagreements_hint: "Films with the biggest rating gaps",
    pair_loved_by_both: "Loved by both", pair_rating_you: "You", pair_rating_partner: "Partner", pair_difference: "Difference",
    pair_more: "Show all",
    partner_explainer: (n) => `Based on the gap between your ratings across ${n} shared ${n === 1 ? "film" : "films"}. Closer to 100% means you agree more often.`,
    partner_settings: "Pair settings", partner_exact_hint: "Same ratings", partner_shared_best: "Your shared favorite", partner_shared_dispute: "Biggest difference",
    partner_no_common: "No films you both rated yet",
    partner_matches: "Exact matches", partner_best: "Best shared", partner_controversial: "Most divisive", partner_genres: "Shared genres",
    partner_unpair_btn: "Unpair", partner_unpair_confirm: "Unpair? Each keeps their personal lists.", partner_unpair_success: "Pair ended.",
    partner_code_btn: "I have a code", partner_code_ph: "Partner code", partner_connect: "Connect",
    partner_code_hint: "Or send your partner this code:",
    pair_empty: "Add films together — your shared stats will show here",
    accept_title: "Pairing invite", accept_title_from: (name) => `${name} invited you to pair up`, accept_sub: "Track and rate films together, with shared stats and compatibility.",
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
function setLang(l, onApplied = null) {
  if (!DICT[l]) return;
  lang = l;
  try { localStorage.setItem("lang", l); } catch (e) {}
  // The backend uses this preference for a localized Telegram bot message.
  api("/api/settings", { method: "PATCH", body: JSON.stringify({ language: l }) }).catch(() => {});
  applyTabLabels();
  if (typeof onApplied === "function") onApplied();
  else showHome();
}
function applyTabLabels() {
  const map = { home: "tab_home", want: "tab_want", watched: "tab_watched", top: "tab_top", stats: "tab_stats" };
  document.querySelectorAll("#tabbar .tab").forEach(b => { const s = b.querySelector("span"); if (s) s.textContent = t(map[b.dataset.tab]); });
}

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const canCache = cacheableRead(path, opts);
  const cached = canCache && _readCache.get(path);
  if (cached && cached.expiresAt > Date.now()) return cached.value;
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-Init-Data": tg.initData, ...(opts.headers || {}) },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
  const value = await res.json();
  if (canCache) _readCache.set(path, { value, expiresAt: Date.now() + _READ_CACHE_TTL });
  if (method !== "GET") _readCache.clear();
  return value;
}

// ── Утилиты ───────────────────────────────────────────────────────────────────
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function hash(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h; }
function cap(s) { s = String(s || ""); return s ? s[0].toUpperCase() + s.slice(1) : s; }
// В Telegram-шапке уже есть «Addict Film» — на самом экране не дублируем название,
// а здороваемся по имени (реальные данные), иначе мягкий фолбэк на бренд.
function homeGreeting() {
  const name = (me && me.label) || (tg?.initDataUnsafe?.user?.first_name) || "";
  return name ? t("greeting", name) : "Addict Film";
}
// Постеры грузим через наш прокси /img — работает даже если CDN блокируется у клиента.
// small=true — кинопоисковские постеры в 300x450 вместо 600x900 (вчетверо меньше
// байт). Используется ВЕЗДЕ, где постер мелкий: тайлы, и карточка фильма тоже
// (там постер 128px — 300x450 достаточно, а главное URL совпадает с тайлом →
// постер на карточке встаёт мгновенно из браузерного кэша, без сети).
// Бекдропы get-ott жмём 1344x756 (до 1.9МБ!) → 672x378 — обрывы уходят.
function posterSrc(u, small) {
  if (!u) return "";
  if (small) u = u.replace(/^(https:\/\/avatars\.mds\.yandex\.net\/get-kinopoisk-image\/.+)\/600x900$/, "$1/300x450");
  u = u.replace(/^(https:\/\/avatars\.mds\.yandex\.net\/get-ott\/.+)\/1344x756$/, "$1/672x378");
  return "/img?u=" + encodeURIComponent(u);
}
// Узнаём исходный CDN у URL нашего /img-прокси. Если источник вообще не входит
// в разрешённый набор, повторять запрос бессмысленно: прокси гарантированно
// вернёт 400, а два ретрая лишь замедлят экран в Telegram WebView.
const RETRYABLE_IMAGE_HOSTS = new Set([
  "m.media-amazon.com", "images-na.ssl-images-amazon.com", "ia.media-imdb.com",
  "avatars.mds.yandex.net", "st.kp.yandex.net", "image.openmoviedb.com",
  "image.tmdb.org", "kinopoiskapiunofficial.tech", "commons.wikimedia.org", "upload.wikimedia.org",
]);
function isRetryableImage(img) {
  try {
    const proxy = new URL(img.src, window.location.origin);
    const source = new URL(proxy.searchParams.get("u"));
    return RETRYABLE_IMAGE_HOSTS.has(source.hostname.toLowerCase());
  } catch (_) {
    return false;
  }
}
// Ретрай оборванной картинки. Важно: НЕ трогаем видимый <img> — свежую копию
// грузим в отдельном Image() и подменяем src только когда она ПОЛНОСТЬЮ доехала.
// Частично показанный постер никогда не «моргает» и не исчезает (раньше retry
// менял src на живом теге — постер появлялся и тут же пропадал). Кэш-баст &r=
// обходит застрявшую в WebView-кэше битую копию. Убираем тег только если не
// отрисовалось ВООБЩЕ ничего и все попытки провалились.
window.__imgRetry = function (img) {
  if (!isRetryableImage(img)) {
    if (!img.naturalWidth) img.remove();
    return;
  }
  const n = +(img.dataset.r || 0);
  if (n >= 2) {
    if (!img.naturalWidth) img.remove();  // совсем пусто → плейсхолдер с названием
    return;                               // частично видно → оставляем как есть
  }
  img.dataset.r = n + 1;
  const fresh = img.src.replace(/&r=\d+$/, "") + "&r=" + Date.now();
  setTimeout(() => {
    const probe = new Image();
    probe.onload = () => { img.src = fresh; };   // подменяем только готовую
    probe.onerror = () => { window.__imgRetry(img); };
    probe.src = fresh;
  }, 700 * (n + 1));
};

// Kinopoisk sometimes returns its monochrome "K" card as a person's `photo`.
// It is a successful image request, so the usual error/retry path cannot catch
// it. Person photos are served through our same-origin proxy, which lets us
// inspect a tiny downscaled copy before revealing it. Real portraits have
// either colour variation or a substantially richer greyscale range; the
// provider placeholder is almost entirely neutral and has only a few shades.
function isKinopoiskPortraitPlaceholder(img) {
  if (!img.naturalWidth || !img.naturalHeight) return false;
  try {
    const side = 40;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = side;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, side, side);
    const pixels = ctx.getImageData(0, 0, side, side).data;
    let neutral = 0;
    const shades = new Set();
    for (let i = 0; i < pixels.length; i += 4) {
      const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
      const high = Math.max(r, g, b), low = Math.min(r, g, b);
      if (high - low <= 10) {
        neutral += 1;
        shades.add(Math.floor((r + g + b) / 48));
      }
    }
    const sampleCount = pixels.length / 4;
    return neutral / sampleCount >= 0.99 && shades.size <= 6;
  } catch (_) {
    // If a browser ever disallows canvas inspection, favour showing a real
    // photo rather than hiding it on an inconclusive check.
    return false;
  }
}

function revealPersonPhoto(img) {
  if (img.dataset.personPhotoChecked) return;
  img.dataset.personPhotoChecked = "1";
  if (isKinopoiskPortraitPlaceholder(img)) {
    showNextPersonPhoto(img);
    return;
  }
  // Most provider images are real 2:3-ish headshots and look best filling the
  // compact portrait card.  A few sources are extremely tall or landscape;
  // keep those intact instead of cutting through a face.
  const ratio = img.naturalWidth / img.naturalHeight;
  img.classList.toggle("person-photo-safe-fit", ratio < .62 || ratio > 1.38);
  img.classList.add("ready");
}

function personPhotoCandidates(img) {
  if (img._personPhotoCandidates) return img._personPhotoCandidates;
  let fallbacks = [];
  try { fallbacks = JSON.parse(img.dataset.personPhotoFallbacks || "[]"); } catch (_) {}
  const urls = [img.dataset.personPhotoSrc, ...fallbacks]
    .filter(value => typeof value === "string" && value)
    .map(value => posterSrc(value));
  img._personPhotoCandidates = [...new Set(urls)];
  return img._personPhotoCandidates;
}

function showNextPersonPhoto(img) {
  const candidates = personPhotoCandidates(img);
  const next = +(img.dataset.personPhotoIndex || 0) + 1;
  if (next >= candidates.length) {
    img.remove(); // The styled initials/icon below remain visible; never a blank hole.
    return;
  }
  img.dataset.personPhotoIndex = String(next);
  img.dataset.personPhotoChecked = "";
  img.classList.remove("person-photo-safe-fit");
  // Do not swap the currently visible portrait before its replacement is
  // complete. This avoids a flash on a weak Telegram connection.
  const probe = new Image();
  probe.onload = () => { if (img.isConnected) img.src = candidates[next]; };
  probe.onerror = () => { if (img.isConnected) showNextPersonPhoto(img); };
  probe.src = candidates[next];
}

function loadPersonPhoto(img) {
  if (img.dataset.personPhotoLoaded) return;
  const candidates = personPhotoCandidates(img);
  if (!candidates.length) {
    img.remove();
    return;
  }
  img.dataset.personPhotoLoaded = "1";
  img.dataset.personPhotoIndex = "0";
  img.src = candidates[0];
}

let _personPhotoObserver = null;
function lazyLoadPersonPhotos(root = document) {
  const images = [...(root.querySelectorAll?.("img[data-person-photo-src]") || [])]
    .filter(img => !img.dataset.personPhotoLoaded);
  if (!images.length) return;
  if (!("IntersectionObserver" in window)) {
    images.forEach(loadPersonPhoto);
    return;
  }
  if (!_personPhotoObserver) {
    _personPhotoObserver = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        _personPhotoObserver.unobserve(entry.target);
        loadPersonPhoto(entry.target);
      });
    }, { rootMargin: "180px 120px" });
  }
  images.forEach(img => _personPhotoObserver.observe(img));
}

function revealLoadedPersonPhotos(root = document) {
  lazyLoadPersonPhotos(root);
  root.querySelectorAll?.("img[data-person-photo]").forEach(img => {
    if (img.complete && img.naturalWidth) revealPersonPhoto(img);
  });
}

// HTML is assembled from API data, so keep recovery behaviour out of inline
// attributes. This allows a strict CSP without `script-src 'unsafe-inline'`.
document.addEventListener("load", event => {
  const img = event.target;
  if (img instanceof HTMLImageElement && img.hasAttribute("data-person-photo")) revealPersonPhoto(img);
}, true);
document.addEventListener("error", event => {
  const img = event.target;
  if (!(img instanceof HTMLImageElement)) return;
  if (img.hasAttribute("data-person-photo-src")) showNextPersonPhoto(img);
  else if (img.hasAttribute("data-img-retry")) window.__imgRetry(img);
  else if (img.hasAttribute("data-img-remove-on-error")) img.remove();
}, true);

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
  // Рейтинг живёт в мета-строке под постером (год слева, оценка справа), а не поверх арта.
  let rv = badge !== undefined ? badge : (ratingOf(m) || "");
  rv = String(rv).replace(/^★\s*/, "").trim();
  const year = m.year ? `<span class="y">${esc(m.year)}</span>` : "";
  const rating = rv ? `<span class="rate-pill"><span class="s">★</span>${esc(rv)}</span>` : "";
  card.innerHTML = `
    <div class="art">
      <div class="noposter">${esc(m.title)}</div>
      ${m.poster_url ? `<img loading="lazy" decoding="async" src="${posterSrc(m.poster_url, true)}" alt="" data-img-retry>` : ""}
    </div>
    <div class="meta">
      <div class="t">${esc(m.title)}</div>
      ${year || rating ? `<div class="meta-row">${year}${rating}</div>` : ""}
    </div>`;
  if (onClick) card.onclick = () => {
    // Захватываем стартовую точку для hero-transition ДО того, как экран будет уничтожен.
    const img = card.querySelector(".art img");
    _heroSource = img && img.currentSrc ? { rect: card.querySelector(".art").getBoundingClientRect(), src: img.currentSrc } : null;
    onClick();
  };
  return card;
}
function gridOf(items, toCard) { const g = document.createElement("div"); g.className = "grid"; for (const it of items) g.appendChild(toCard(it)); return g; }
function openDetail(id, back, preview = null) { if (back) _returnTo = back; showDetail(id, preview); }

// ── Главная ───────────────────────────────────────────────────────────────────
// Единый набор line-иконок для категорийных чипов (в стиле нижней навигации),
// вместо разнородных эмодзи. Жанр-пилюли — чистый текст (см. genrePill).
const _svg = (p) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const CHIP_ICONS = {
  pop: _svg('<path d="M8.5 14.5A2.5 2.5 0 0 0 11 17a2.5 2.5 0 0 0 2.5-2.5c0-1.4-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5Z"/>'),
  top: _svg('<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6M18 9h1.5a2.5 2.5 0 0 0 0-5H18M4 22h16M10 14.7V17c0 .6-.5 1-1 1.2C7.9 18.8 7 20.2 7 22M14 14.7V17c0 .6.5 1 1 1.2 1.1.5 2 2 2 3.8M18 2H6v7a6 6 0 0 0 12 0Z"/>'),
  gen: _svg('<rect width="7" height="7" x="3" y="3" rx="1.5"/><rect width="7" height="7" x="14" y="3" rx="1.5"/><rect width="7" height="7" x="14" y="14" rx="1.5"/><rect width="7" height="7" x="3" y="14" rx="1.5"/>'),
  coll: _svg('<path d="m12.8 2.2a2 2 0 0 0-1.6 0L2.6 6.1a1 1 0 0 0 0 1.8l8.6 3.9a2 2 0 0 0 1.6 0l8.6-3.9a1 1 0 0 0 0-1.8Z"/><path d="m22 17.6-9.2 4.2a2 2 0 0 1-1.6 0L2 17.6M22 12.6l-9.2 4.2a2 2 0 0 1-1.6 0L2 12.6"/>'),
};
async function showHome() {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  const seeAll = (id) => `<button class="see-all" id="${id}">${esc(t("see_all"))}<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg></button>`;
  screen.innerHTML = `
    <header class="app-head rise d1">
      <div class="brand"><h1>${esc(homeGreeting())}</h1><p>${esc(t("tagline"))}</p></div>
      <div class="head-actions">
        <button class="lang" id="lang-btn" aria-label="Language">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.6 2.4 4 5.6 4 9s-1.4 6.6-4 9c-2.6-2.4-4-5.6-4-9s1.4-6.6 4-9Z"/></svg>
          <b>${lang.toUpperCase()}</b>
        </button>
        <button class="bell" id="bell-btn" aria-label="${esc(t("notif_title"))}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>
          ${_notificationUnread ? `<span class="dot" aria-hidden="true"></span>` : ""}
        </button>
      </div>
    </header>
    <div class="search rise d1">
      <div class="search-tap" id="home-search">
        <span class="icn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg></span>
        <span class="q">${esc(t("search_ph"))}</span>
      </div>
      <button class="filter" id="home-filter" aria-label="${esc(t("chip_genres"))}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M7 12h10M10 18h4"/></svg>
      </button>
    </div>
    <div class="chips rise d2">
      <span class="chip active" data-to="sec-pop"><span class="e">${CHIP_ICONS.pop}</span>${esc(t("chip_popular"))}</span>
      <span class="chip" data-to="sec-top"><span class="e">${CHIP_ICONS.top}</span>${esc(t("chip_top"))}</span>
      <span class="chip" data-to="sec-gen"><span class="e">${CHIP_ICONS.gen}</span>${esc(t("chip_genres"))}</span>
      <span class="chip" data-to="sec-coll"><span class="e">${CHIP_ICONS.coll}</span>${esc(t("chip_collections"))}</span>
    </div>
    <section class="rise d3" id="sec-pop"><div class="head"><h2>${esc(t("chip_popular"))}</h2>${seeAll("see-pop")}</div><div class="rail" id="rail-pop">${skeletonRail(5)}</div></section>
    <section class="rise d4" id="sec-top"><div class="head"><h2>${esc(t("chip_top"))}</h2>${seeAll("see-top")}</div><div class="rail" id="rail-top">${skeletonRail(5)}</div></section>
    <section class="rise d5" id="sec-gen"><div class="head"><h2>${esc(t("chip_genres"))}</h2>${seeAll("see-gen")}</div><div class="gchips" id="gen-chips"></div></section>
    ${recoCardHTML()}
    <section class="rise d5" id="sec-coll"><div class="head"><h2>${esc(t("chip_collections"))}</h2>${canEditCollections() ? `<button class="icon-add" id="coll-add-home" aria-label="+"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>` : ""}</div><div class="rail" id="rail-coll">${skeletonRail(5)}</div></section>`;

  document.getElementById("lang-btn").onclick = () => setLang(lang === "ru" ? "en" : "ru");
  document.getElementById("bell-btn").onclick = () => showNotifications();
  document.getElementById("home-search").onclick = () => showSearch();
  document.getElementById("home-filter").onclick = () => showSearch();
  document.getElementById("see-pop").onclick = () => showBrowseAll("popular", t("chip_popular"));
  document.getElementById("see-top").onclick = () => showBrowseAll("top", t("chip_top"));
  document.getElementById("see-gen").onclick = () => showAllGenres();
  document.getElementById("reco-start").onclick = () => showSearch();
  screen.querySelectorAll(".chips .chip[data-to]").forEach(c => c.onclick = () => {
    screen.querySelectorAll(".chips .chip[data-to]").forEach(x => x.classList.toggle("active", x === c));
    document.getElementById(c.dataset.to)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  if (canEditCollections()) document.getElementById("coll-add-home").onclick = () => createCollectionFlow();

  loadRail("rail-pop", "/api/browse?sort=popular&limit=20", { onItems: fillRecoArts });
  loadRail("rail-top", "/api/browse?sort=top&limit=20");
  loadGenrePills();
  loadCollectionsRail();
  refreshNotificationBadge();
}

async function refreshNotificationBadge() {
  try {
    const { unread_count } = await api("/api/notifications?limit=1");
    _notificationUnread = Number(unread_count) || 0;
    const bell = document.getElementById("bell-btn");
    if (bell) {
      let dot = bell.querySelector(".dot");
      if (_notificationUnread && !dot) { dot = document.createElement("span"); dot.className = "dot"; dot.setAttribute("aria-hidden", "true"); bell.appendChild(dot); }
      if (!_notificationUnread && dot) dot.remove();
    }
  } catch (_) { /* The home screen remains usable if the inbox is offline. */ }
}

function recoCardHTML() {
  return `<div class="reco rise d5">
    <div class="reco-icon"><svg viewBox="0 0 24 24" fill="currentColor"><path d="m12 3 2.6 5.6 6.1.7-4.5 4.1 1.2 6-5.4-3-5.4 3 1.2-6L3 9.3l6.1-.7Z"/></svg></div>
    <div class="reco-copy"><h3>${esc(t("reco_title"))}</h3><p>${esc(t("reco_sub"))}</p><button class="reco-btn" id="reco-start">${esc(t("reco_cta"))}</button></div>
    <div class="reco-arts" id="reco-arts"></div>
  </div>`;
}
function fillRecoArts(items) {
  const box = document.getElementById("reco-arts");
  if (!box) return;
  const arts = (items || []).filter(m => m.poster_url).slice(0, 3);
  if (!arts.length) { box.remove(); return; }
  box.innerHTML = arts.map(m => `<div class="reco-art"><img loading="lazy" src="${posterSrc(m.poster_url, true)}" alt="" data-img-retry></div>`).join("");
}

async function loadCollectionsRail() {
  const el = document.getElementById("rail-coll");
  try {
    const { items } = await api("/api/collections");
    if (!el) return;
    if (!items.length) {
      el.innerHTML = `<div class="rail-empty">${esc(canEditCollections() ? t("collections_empty_admin_s") : t("collections_empty_s"))}</div>`;
      return;
    }
    el.replaceChildren(...items.map(collectionCard));
  } catch (e) { if (el) el.innerHTML = `<div class="rail-empty">${esc(t("rail_err"))}</div>`; }
}

async function loadRail(id, path, { onItems = null } = {}) {
  const el = document.getElementById(id);
  try {
    const { items } = await api(path);
    if (onItems) onItems(items);
    if (!el) return;
    if (!items.length) { el.innerHTML = `<div class="rail-empty">${esc(t("rail_empty"))}</div>`; return; }
    const back = () => { setActiveTab("home"); showHome(); };
    el.replaceChildren(...items.map(m => posterTile(m, { onClick: () => openDetail(m.id, back, m) })));
  } catch (e) { if (el) el.innerHTML = `<div class="rail-empty">${esc(t("rail_err"))}</div>`; }
}

// Жанры на Главной — компактные текстовые пилюли (полный список — «Смотреть все»).
async function loadGenrePills() {
  const el = document.getElementById("gen-chips");
  try {
    const { items } = await api("/api/genres");
    if (!el) return;
    if (!items.length) { el.innerHTML = `<div class="rail-empty">${esc(t("genres_empty"))}</div>`; return; }
    el.replaceChildren(...items.map(genrePill));
  } catch (e) { if (el) el.innerHTML = `<div class="rail-empty">—</div>`; }
}
function genrePill(g) {
  const pill = document.createElement("button");
  pill.className = "gchip";
  pill.textContent = cap(g.name);
  pill.onclick = () => showGenre(g.name);
  return pill;
}
function genreCard(g) {
  const card = document.createElement("div");
  card.className = "genre";
  const grad = GENRE_GRAD[hash(g.name) % GENRE_GRAD.length];
  card.innerHTML = `<div class="gart" style="background:${grad}"><span class="lbl"><b>${esc(cap(g.name))}</b><span>${g.count} ${esc(t("count_films", g.count))}</span></span></div>`;
  card.onclick = () => showGenre(g.name);
  return card;
}
async function showGenre(name) {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="sub-head">${backBtn()}<h1>${esc(cap(name))}</h1></div><div id="gg">${skeletonGrid(6)}</div>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  try {
    const { items } = await api(`/api/browse?sort=genre&genre=${encodeURIComponent(name)}`);
    const el = document.getElementById("gg");
    if (!items.length) { el.innerHTML = emptyState("🎭", t("genre_empty_t"), t("genre_empty_s")); return; }
    const back = () => showGenre(name);
    el.replaceChildren(gridOf(items, m => posterTile(m, { onClick: () => openDetail(m.id, back, m) })));
  } catch (e) { document.getElementById("gg").innerHTML = emptyState("⚠️", t("load_err"), ""); }
}

// «Смотреть все» для секции каталога — сетка с догрузкой (тот же /api/browse).
async function showBrowseAll(sort, title) {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="sub-head">${backBtn()}<h1>${esc(title)}</h1></div><div id="ba">${skeletonGrid(9)}</div>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  const LIMIT = 30;
  let offset = 0, done = false, loading = false;
  const el = document.getElementById("ba");
  const back = () => showBrowseAll(sort, title);
  let grid = null;
  const loadPage = async () => {
    if (done || loading) return;
    loading = true;
    try {
      const { items } = await api(`/api/browse?sort=${encodeURIComponent(sort)}&limit=${LIMIT}&offset=${offset}`);
      if (offset === 0 && !items.length) { el.innerHTML = emptyState("🎬", t("rail_empty"), ""); done = true; return; }
      if (!grid) { grid = gridOf([], () => {}); el.innerHTML = ""; el.appendChild(grid); }
      for (const m of items) grid.appendChild(posterTile(m, { onClick: () => openDetail(m.id, back, m) }));
      offset += items.length;
      if (items.length < LIMIT) { done = true; moreBtn.remove(); }
    } catch (e) { if (offset === 0) el.innerHTML = emptyState("⚠️", t("load_err"), ""); }
    finally { loading = false; }
  };
  const moreBtn = document.createElement("button");
  moreBtn.className = "load-more";
  moreBtn.textContent = t("load_more") || "Показать ещё";
  moreBtn.onclick = () => loadPage().then(() => { if (!done) screen.appendChild(moreBtn); });
  await loadPage();
  if (!done) screen.appendChild(moreBtn);
}

// «Смотреть все» для жанров — крупные карточки-жанры (та же genreCard).
async function showAllGenres() {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="sub-head">${backBtn()}<h1>${esc(t("chip_genres"))}</h1></div><div id="ag" class="genre-grid">${skeletonGrid(6)}</div>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  try {
    const { items } = await api("/api/genres");
    const el = document.getElementById("ag");
    if (!items.length) { el.innerHTML = emptyState("🎭", t("genres_empty"), ""); return; }
    el.innerHTML = "";
    el.replaceChildren(...items.map(genreCard));
  } catch (e) { document.getElementById("ag").innerHTML = emptyState("⚠️", t("load_err"), ""); }
}

function notificationGlyph(eventType) {
  const paths = eventType === "pair.ended"
    ? '<path d="m7 7 10 10M17 7 7 17"/><path d="M5 5h14v14H5z"/>'
    : eventType.includes("invite")
      ? '<path d="M12 20s-7-4.35-7-10a4 4 0 0 1 7-2.65A4 4 0 0 1 19 10c0 5.65-7 10-7 10Z"/><path d="M12 7v6m-3-3h6"/>'
      : '<path d="M12 20s-7-4.35-7-10a4 4 0 0 1 7-2.65A4 4 0 0 1 19 10c0 5.65-7 10-7 10Z"/>';
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}
function relativeNotificationTime(raw) {
  const ms = Date.now() - new Date(raw || 0).getTime();
  if (!Number.isFinite(ms) || ms < 60_000) return t("notif_now");
  const min = Math.floor(ms / 60_000); if (min < 60) return t("notif_min_ago", min);
  const hour = Math.floor(min / 60); if (hour < 24) return t("notif_hour_ago", hour);
  return t("notif_day_ago", Math.floor(hour / 24));
}
function notificationRow(item) {
  const payload = item.payload || {};
  const avatar = item.actor?.photo_url ? `<img src="${posterSrc(item.actor.photo_url)}" alt="" data-img-retry>` : "";
  return `<article class="notification-row ${item.read ? "" : "unread"}" data-notification-id="${Number(item.id)}" data-notification-link="${esc(item.deep_link || "")}" tabindex="0" role="button">
    <span class="notification-event-icon">${notificationGlyph(item.event_type || "")}</span>
    <span class="notification-copy"><b>${esc(payload.title || t("notif_title"))}</b><span>${esc(payload.body || "")}</span><time datetime="${esc(item.created_at || "")}">${esc(relativeNotificationTime(item.created_at))}</time></span>
    ${item.actor ? `<span class="notification-avatar">${avatar || `<span>${esc(initials(item.actor.name || ""))}</span>`}</span>` : ""}
    ${item.deep_link ? `<button type="button" class="notification-action" data-notification-action>${esc(payload.action_label || t("back"))}</button>` : ""}
  </article>`;
}
async function openNotification(item, page) {
  try { await api(`/api/notifications/${item.id}/read`, { method: "POST" }); } catch (_) {}
  _notificationUnread = Math.max(0, _notificationUnread - (item.read ? 0 : 1));
  const link = item.deep_link || "";
  if (link.startsWith("inv_")) { showAcceptInvite(link); return; }
  if (link === "stats") { setActiveTab("stats"); showStats("pair"); return; }
  setActiveTab("home"); showHome();
}
async function showNotifications() {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="sub-head notification-head">${backBtn()}<h1>${esc(t("notif_title"))}</h1><button type="button" class="notification-mark-all" disabled>${esc(t("notif_mark_all"))}</button></div><main class="notifications-page"><div class="notifications-loading">${esc(t("notif_loading"))}</div></main>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  const page = screen.querySelector(".notifications-page");
  let nextBeforeId = null;
  let loading = false;
  const load = async (append = false) => {
    if (loading || !page) return;
    loading = true;
    try {
      const query = `?limit=20${append && nextBeforeId ? `&before_id=${encodeURIComponent(nextBeforeId)}` : ""}`;
      const result = await api(`/api/notifications${query}`);
      _notificationUnread = Number(result.unread_count) || 0;
      nextBeforeId = result.next_before_id;
      const rows = result.items || [];
      if (!append) page.innerHTML = rows.length ? `<div class="notifications-list">${rows.map(notificationRow).join("")}</div>` : `<div class="notifications-empty"><span>${notificationGlyph("")}</span><h2>${esc(t("notif_empty_t"))}</h2><p>${esc(t("notif_empty_s"))}</p></div>`;
      else page.querySelector(".notifications-list")?.insertAdjacentHTML("beforeend", rows.map(notificationRow).join(""));
      const existing = page.querySelector(".notifications-more"); if (existing) existing.remove();
      if (nextBeforeId) page.insertAdjacentHTML("beforeend", `<button class="notifications-more" type="button">${esc(t("notif_load_more"))}</button>`);
      const markAll = screen.querySelector(".notification-mark-all");
      if (markAll) { markAll.disabled = !_notificationUnread; markAll.onclick = async () => { markAll.disabled = true; await api("/api/notifications/read-all", { method: "POST" }); _notificationUnread = 0; page.querySelectorAll(".notification-row.unread").forEach(row => row.classList.remove("unread")); refreshNotificationBadge(); }; }
      page.querySelector(".notifications-more")?.addEventListener("click", () => load(true));
      page.querySelectorAll(".notification-row").forEach(row => {
        const id = Number(row.dataset.notificationId);
        const item = rows.find(entry => Number(entry.id) === id) || { id, deep_link: row.dataset.notificationLink, read: !row.classList.contains("unread") };
        const open = () => openNotification(item, page);
        row.addEventListener("click", event => { if (event.target.closest("button")) return; open(); });
        row.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
        row.querySelector("[data-notification-action]")?.addEventListener("click", open);
      });
    } catch (_) {
      if (!append) page.innerHTML = `<div class="notifications-empty"><h2>${esc(t("notif_error"))}</h2><button class="notifications-more" type="button">${esc(t("notif_retry"))}</button></div>`;
      page.querySelector(".notifications-more")?.addEventListener("click", () => load(false));
    } finally { loading = false; }
  };
  await load();
}

// ── Подборки (публичный просмотр + in-app админка для editor/admin) ───────────
function canEditCollections() { return !!(me && (me.role === "admin" || me.role === "editor")); }

function collectionCard(c) {
  // Тот же формат карточки, что у фильмов: обложка + мета-строка (кол-во фильмов
  // справа, как рейтинг у постеров) — единый визуальный ритм секций.
  const card = document.createElement("div");
  card.className = "poster";
  card.innerHTML = `
    <div class="art">
      <div class="noposter">${esc(c.title)}</div>
      ${c.cover ? `<img loading="lazy" src="${posterSrc(c.cover, true)}" alt="" data-img-retry>` : ""}
    </div>
    <div class="meta">
      <div class="t">${esc(c.title)}</div>
      <div class="meta-row"><span class="y">${c.film_count} ${esc(t("count_films", c.film_count))}</span></div>
    </div>`;
  card.onclick = () => showCollectionDetail(c.id);
  return card;
}

function createCollectionFlow() {
  // Вызывается с Главной («+» в заголовке секции «Подборки») — временно подменяет
  // содержимое секции формой; после создания уходим в showCollectionDetail, а сама
  // секция пересоберётся из API при следующем возврате на Главную (showHome).
  const el = document.getElementById("sec-coll");
  if (!el) return;
  el.innerHTML = `<div class="chart-card" style="margin:0 20px;">
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
  wireBack(() => { setActiveTab("home"); showHome(); });
  if (canEdit) {
    document.getElementById("cd-add").onclick = () => showSearch({ type: "collection", id });
    document.getElementById("cd-delete").onclick = () => {
      const title = document.getElementById("cd-title").textContent;
      tg.showConfirm(t("coll_delete_confirm", title), async ok => {
        if (!ok) return;
        await api(`/api/admin/collections/${id}`, { method: "DELETE" });
        setActiveTab("home"); showHome();
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
      : (m) => openDetail(m.id, back, m);
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
  const el = document.getElementById("list");
  try {
    const pageSize = 30;
    const data = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=${pageSize}`);
    const { items, total } = data;
    if (!items.length) {
      el.innerHTML = tab === "want" ? emptyState("🔖", t("want_empty_t"), t("want_empty_s"))
        : tab === "watched" ? emptyState("✅", t("watched_empty_t"), t("watched_empty_s"))
        : emptyState("⭐", t("top_empty_t"), t("top_empty_s"));
      return;
    }
    const back = () => showList(tab);
    const renderCards = (list) => list.map(m => posterTile(m, { onClick: () => openDetail(m.id, back, m), badge: m.my_rating ? `★ ${m.my_rating}` : "" }));
    const grid = gridOf(items, m => posterTile(m, { onClick: () => openDetail(m.id, back, m), badge: m.my_rating ? `★ ${m.my_rating}` : "" }));
    el.replaceChildren(grid);
    let offset = items.length;
    if (offset < total) {
      const more = document.createElement("button");
      more.className = "load-more";
      more.textContent = t("load_more");
      more.onclick = async () => {
        more.disabled = true;
        more.textContent = t("loading");
        try {
          const next = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=${pageSize}&offset=${offset}`);
          grid.append(...renderCards(next.items));
          offset += next.items.length;
          if (!next.items.length || offset >= total) more.remove();
          else { more.disabled = false; more.textContent = t("load_more"); }
        } catch (e) { more.disabled = false; more.textContent = t("retry"); }
      };
      el.appendChild(more);
    }
  } catch (e) { el.innerHTML = emptyState("⚠️", t("load_err"), String(e.message)); }
}

// ── Карточка фильма ───────────────────────────────────────────────────────────
function initials(name) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
}
function shareSvg() { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v14"/></svg>`; }

function unwireDetailScroll() {
  if (_detailScrollHandler) { window.removeEventListener("scroll", _detailScrollHandler); _detailScrollHandler = null; }
  if (_detailLoadController) { _detailLoadController.abort(); _detailLoadController = null; }
}
function resetDetailViewport() {
  // iOS WebView occasionally restores the previous document scroll position
  // while a detail template is being swapped in. Reset both possible scroll
  // roots now and on the following frame, after the new layout exists.
  const reset = () => {
    window.scrollTo(0, 0);
    if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  };
  reset();
  requestAnimationFrame(reset);
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

function renderDetailPreview(preview) {
  const title = preview.title || "…";
  const poster = preview.poster_url || preview.poster || "";
  const meta = [preview.year, preview.age_rating, preview.runtime].filter(Boolean).join(" · ");
  screen.innerHTML = `
    <div class="detail-v2 detail-preview">
      <div class="d-backdrop no-bd">
        ${poster ? `<img src="${posterSrc(poster, true)}" alt="" data-img-retry>` : ""}
        <div class="d-scrim-t"></div><div class="d-scrim-b"></div>
        <div class="d-floatctrls">
          <button class="d-ctrl" id="d-back-preview" aria-label="Back"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button>
        </div>
      </div>
      <div class="d-body">
        <div class="d-poster-wrap"><div class="d-poster"><span class="fb">${esc(title)}</span>${poster ? `<img src="${posterSrc(poster, true)}" alt="" data-img-retry>` : ""}</div></div>
        <h1 class="d-title">${esc(title)}</h1>
        ${preview.title_original && preview.title_original !== title ? `<div class="d-original">${esc(preview.title_original)}</div>` : ""}
        ${meta ? `<div class="d-meta">${esc(meta)}</div>` : ""}
        <div class="d-preview-lines"><div class="sk sk-line wide"></div><div class="sk sk-line"></div></div>
      </div>
    </div>`;
  document.getElementById("d-back-preview").onclick = () => closeDetailThen(_returnTo);
}

async function showDetail(id, preview = null) {
  unwireDetailScroll();
  if (preview) renderDetailPreview(preview);
  else {
    screen.innerHTML = `<div class="detail-v2">
      <div class="d-backdrop sk"></div>
      <div class="d-body"><div class="d-poster-wrap"><div class="d-poster sk"></div></div>
        <div class="sk sk-line wide"></div><div class="sk sk-line"></div></div>
      <div class="d-floatctrls" style="position:fixed;top:0;left:0;right:0;padding:calc(10px + env(safe-area-inset-top)) 14px 0;z-index:41;">${backBtn()}</div>
    </div>`;
    wireBack(() => closeDetailThen(_returnTo));
  }
  resetDetailViewport();
  const controller = new AbortController();
  _detailLoadController = controller;
  try {
    const m = await api(`/api/movie/${id}`, { signal: controller.signal });
    if (!controller.signal.aborted) renderDetail(id, m);
  } catch (e) {
    if (!controller.signal.aborted) {
      const body = screen.querySelector(".detail-v2 .d-body");
      if (body) body.insertAdjacentHTML("beforeend", emptyState("⚠️", t("load_err"), String(e.message)));
    }
  } finally {
    if (_detailLoadController === controller) _detailLoadController = null;
  }
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
      <div class="d-backdrop${m.backdrop_url ? "" : " no-bd"}" id="d-backdrop">
        ${bdUrl ? `<img id="d-backdrop-img" src="${posterSrc(bdUrl, !m.backdrop_url)}" alt="">` : ""}
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
            ${m.poster_url ? `<img src="${posterSrc(m.poster_url, true)}" alt="" data-img-retry>` : ""}
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
          <div class="d-cast-rail">${cast.map(a => `<div class="d-cast-item"><div class="d-avatar"><span class="fb">${esc(initials(a.name))}</span>${a.photo_url ? `<img loading="lazy" decoding="async" src="${posterSrc(a.photo_url)}" alt="" data-img-retry data-person-photo>` : ""}</div><div class="n">${esc(a.name)}</div></div>`).join("")}</div></div>` : ""}
      </div>
    </div>`;

  renderStars(id, m);
  renderComment(id, m);
  renderActions(id, m);
  revealLoadedPersonPhotos(screen);

  const back = () => closeDetailThen(_returnTo);
  document.getElementById("d-back-top").onclick = back;
  document.getElementById("d-back-sticky").onclick = back;
  document.getElementById("d-more-top").onclick = () => shareMovie(m);
  document.getElementById("d-more-sticky").onclick = () => shareMovie(m);

  const backdropShell = document.getElementById("d-backdrop");
  const bdImg = document.getElementById("d-backdrop-img");
  // A portrait is a valid fallback, but a failed wide backdrop must never leave
  // a tall black hole above the poster. The proxy already retries the source;
  // after that, switch once to the known poster or collapse to a compact gradient.
  const usePosterBackdropFallback = () => {
    backdropShell?.classList.add("no-bd");
    if (!bdImg) return;
    if (bdImg.dataset.heroFallback === "poster") {
      bdImg.remove();
      return;
    }
    if (!m.poster_url) {
      bdImg.remove();
      return;
    }
    bdImg.dataset.heroFallback = "poster";
    bdImg.addEventListener("error", usePosterBackdropFallback, { once: true });
    bdImg.src = posterSrc(m.poster_url, true);
  };
  if (bdImg && m.backdrop_url) {
    bdImg.addEventListener("error", usePosterBackdropFallback, { once: true });
  }
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
  let requestVersion = 0;
  let searchController = null;
  input.oninput = () => {
    const version = ++requestVersion;
    clearTimeout(timer);
    searchController?.abort();
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (q.length < 2) { results.innerHTML = start; return; }
      results.innerHTML = skeletonGrid(6);
      searchController = new AbortController();
      let data;
      try { data = await api(`/api/search?q=${encodeURIComponent(q)}`, { signal: searchController.signal }); }
      catch (e) {
        if (e.name === "AbortError" || version !== requestVersion || !input.isConnected) return;
        results.innerHTML = String(e.message) === "429"
          ? emptyState("⏳", t("search_toomany_t"), t("search_toomany_s"))
          : emptyState("⚠️", t("search_err_t"), String(e.message));
        return;
      }
      if (version !== requestVersion || !input.isConnected) return;
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

// ── Статистика: личный вкус и совместная история — отдельные режимы ───────────
async function showStats(initialMode = "me") {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="page-head"><h1>${esc(t("stats_title"))}</h1><button class="page-head-action" data-stats-settings type="button" aria-label="${esc(t("settings_title"))}">${settingsSvg()}</button></div><div id="stats"><div class="empty"><div class="empty-sub">${esc(t("calc"))}</div></div></div>`;
  const box = document.getElementById("stats");

  // 1. Пара — приоритетно, первым блоком.
  let partner = { status: "none" }, pstats = null;
  try { partner = await api("/api/partner"); } catch (e) {}
  let pairStatsFailed = false;
  if (partner.status === "paired") {
    try { pstats = await api("/api/partner/stats"); }
    catch (e) { pairStatsFailed = true; }
  }

  // 2. Личная статистика за всё время.
  const s = await api("/api/stats");
  // Only genres use a compact/expanded state. People cards always stay in one
  // horizontal rail, so the whole list is reachable with a normal swipe.
  const expanded = { genres: false };
  // Membership and statistics availability are separate states. A transient
  // stats error must never hide the pair tab or alter the personal profile.
  const paired = partner.status === "paired";
  let mode = paired && initialMode === "pair" ? "pair" : "me";

  const render = () => {
    const tabs = paired ? `<div class="stats-switch" role="tablist">
      <button class="stats-switch-btn ${mode === "me" ? "active" : ""}" data-stats-mode="me" role="tab">${esc(t("stats_me_tab"))}</button>
      <button class="stats-switch-btn ${mode === "pair" ? "active" : ""}" data-stats-mode="pair" role="tab">${esc(t("stats_together_tab"))}</button>
    </div>` : "";
    let content;
    if (mode === "pair" && paired) {
      if (pstats) {
        const hasData = pstats.watched || pstats.want || pstats.rated_together;
        content = pairHeroHTML(pstats) +
          (hasData ? personalStatsHTML(pstats, "pair", expanded) + pairHighlightsHTML(pstats) : `<div class="chart-card">${emptyState("💙", t("stats_together"), t("pair_empty"))}</div>`);
      } else {
        const retry = pairStatsFailed ? `<button class="pbtn primary" data-pair-stats-retry type="button">${esc(t("retry"))}</button>` : "";
        content = `<div class="chart-card">${emptyState("⚠️", t("load_err"), "")}${retry}</div>`;
      }
    } else {
      // Build this at render time: expanded genres are UI state, so reusing a
      // prebuilt string would make the “Show more” button look inert.
      const personal = statsProfileHTML(s) + ((!s.watched && !s.want)
        ? emptyState("📊", t("stats_empty_t"), t("stats_empty_s"))
        : personalStatsHTML(s, "me", expanded));
      content = partner.status === "invited" ? partnerCardHTML(partner, null) + personal : personal;
    }
    box.innerHTML = tabs + content;
    const heading = screen.querySelector(".page-head h1");
    if (heading) heading.textContent = t(mode === "pair" ? "pair_title" : "stats_title");
    box.querySelectorAll("[data-stats-mode]").forEach(button => button.onclick = () => {
      mode = button.dataset.statsMode;
      window.scrollTo(0, 0);
      render();
    });
    box.querySelectorAll("[data-stats-expand]").forEach(button => button.onclick = () => {
      const section = button.dataset.statsExpand;
      expanded[section] = !expanded[section];
      render();
    });
    box.querySelector("[data-pair-stats-retry]")?.addEventListener("click", () => showStats("pair"));
    box.querySelectorAll("[data-film-id]").forEach(card => card.onclick = () => openDetail(+card.dataset.filmId, showStats));
    box.querySelectorAll("[data-stats-person-name]").forEach(card => card.onclick = () => {
      showPersonFilms({
        name: card.dataset.statsPersonName,
        role: card.dataset.statsPersonRole,
        scope: mode,
      });
    });
    const settingsButton = screen.querySelector("[data-stats-settings]");
    if (settingsButton) settingsButton.onclick = () => showStatsSettings(mode);
    wirePartner(box);
    revealLoadedPersonPhotos(box);
  };
  render();
}

function settingsSvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.12 2.12-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.04 1.56v.08h-3v-.08A1.7 1.7 0 0 0 10.66 18.7a1.7 1.7 0 0 0-1.88.34l-.06.06-2.12-2.12.06-.06A1.7 1.7 0 0 0 7 15.04a1.7 1.7 0 0 0-1.56-1.04h-.08v-3h.08A1.7 1.7 0 0 0 7 9.96a1.7 1.7 0 0 0-.34-1.88L6.6 8.02 8.72 5.9l.06.06A1.7 1.7 0 0 0 10.66 6.3a1.7 1.7 0 0 0 1.04-1.56v-.08h3v.08A1.7 1.7 0 0 0 15.74 6.3a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.12 2.12-.06.06a1.7 1.7 0 0 0-.34 1.88 1.7 1.7 0 0 0 1.56 1.04h.08v3h-.08A1.7 1.7 0 0 0 19.4 15Z"/></svg>`;
}

function telegramNotificationStatus(settings) {
  if (!settings?.telegram_available) return t("notif_telegram_unavailable");
  return settings.telegram_enabled ? t("settings_notifications_on") : t("settings_notifications_off");
}

function settingsRow({ title, subtitle = "", action = "", className = "", attrs = "" }) {
  return `<div class="settings-row ${className}" ${attrs}><span class="settings-row-copy"><b>${esc(title)}</b>${subtitle ? `<small>${esc(subtitle)}</small>` : ""}</span>${action}</div>`;
}

function settingsPairHTML(partner, failed) {
  if (failed) return settingsRow({ title: t("settings_pair_load_error"), action: `<button class="settings-retry" data-settings-pair-retry type="button">${esc(t("settings_pair_try_again"))}</button>` });
  if (partner.status === "paired") {
    const name = partner.partner?.name || t("partner_word");
    return settingsRow({ title: t("settings_pair_current"), subtitle: name, action: `<button class="settings-chevron-button" data-settings-pair-manage type="button" aria-label="${esc(t("settings_pair_manage"))}">${settingsChevron()}</button>` });
  }
  if (partner.status === "invited") {
    return settingsRow({ title: t("settings_pair_invited"), subtitle: partner.code || "", action: `<button class="settings-chevron-button" data-settings-pair-invited type="button" aria-label="${esc(t("partner_share_btn"))}">${settingsChevron()}</button>` });
  }
  return settingsRow({ title: t("settings_pair_none"), action: `<button class="settings-primary-action" data-settings-pair-create type="button">${esc(t("settings_pair_create"))}</button>` });
}

function settingsPairManagementHTML(partner) {
  const name = partner.partner?.name || t("partner_word");
  const username = partner.partner?.username ? `@${partner.partner.username}` : "";
  return `<div class="settings-pair-management">
    <div class="settings-pair-management-head">
      <button class="settings-pair-management-back" data-settings-pair-close type="button" aria-label="${esc(t("back"))}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 6-6 6 6 6"/></svg>
      </button>
      <span><b>${esc(t("settings_pair_current"))}</b><small>${esc(name)}${username ? ` · ${esc(username)}` : ""}</small></span>
    </div>
    <button class="settings-danger-action" data-settings-pair-unpair type="button">${esc(t("partner_unpair_btn"))}</button>
  </div>`;
}

function settingsChevron() { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>`; }

async function showStatsSettings(returnMode = "me", managePair = false) {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="sub-head"><button class="back" aria-label="${esc(t("back"))}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button><h1>${esc(t("settings_title"))}</h1></div><main class="settings-page"><div class="settings-loading">${esc(t("settings_loading"))}</div></main>`;
  wireBack(() => showStats(returnMode));
  const page = screen.querySelector(".settings-page");
  let partner = { status: "none" };
  let serverSettings = { language: lang, telegram_enabled: false, telegram_available: false };
  let partnerFailed = false;
  try { [partner, serverSettings] = await Promise.all([api("/api/partner"), api("/api/settings")]); } catch (_) { partnerFailed = true; }
  if (!page) return;

  const openPairManagement = () => {
    const card = page.querySelector(".settings-pair-card");
    if (!card || partner.status !== "paired") return;
    card.innerHTML = settingsPairManagementHTML(partner);
    card.querySelector("[data-settings-pair-close]")?.addEventListener("click", () => {
      card.innerHTML = settingsPairHTML(partner, false);
      card.querySelector("[data-settings-pair-manage]")?.addEventListener("click", openPairManagement);
    });
    card.querySelector("[data-settings-pair-unpair]")?.addEventListener("click", () => confirmPairUnlink(async () => {
      try { await api("/api/partner/unpair", { method: "POST" }); tg?.showAlert?.(t("partner_unpair_success")); showStatsSettings(returnMode); }
      catch (_) { tg?.showAlert?.(t("settings_pair_load_error")); }
    }));
  };

  const render = () => {
    page.innerHTML = `
      <section class="settings-section" aria-labelledby="settings-notifications-title"><h2 id="settings-notifications-title">${esc(t("settings_notifications"))}</h2><div class="settings-card">
        ${settingsRow({ title: t("notif_inapp"), subtitle: t("settings_notifications_hint"), action: `<span class="settings-fixed-status">${esc(t("settings_notifications_on"))}</span>` })}
        ${settingsRow({ title: t("settings_notifications"), subtitle: `${t("notif_telegram_hint")} · ${telegramNotificationStatus(serverSettings)}`, action: `<button class="settings-toggle" data-settings-telegram type="button" role="switch" aria-checked="${!!serverSettings.telegram_enabled}" aria-label="${esc(t("settings_notifications"))}" ${serverSettings.telegram_available ? "" : "disabled"}></button>` })}
      </div></section>
      <section class="settings-section" aria-labelledby="settings-language-title"><h2 id="settings-language-title">${esc(t("settings_language"))}</h2><div class="settings-card settings-language-card">
        <p>${esc(t("settings_language_hint"))}</p><div class="settings-language-options" role="group" aria-label="${esc(t("settings_language"))}">
          <button data-settings-language="ru" type="button" class="${lang === "ru" ? "active" : ""}" aria-pressed="${lang === "ru"}">${esc(t("settings_language_ru"))}</button>
          <button data-settings-language="en" type="button" class="${lang === "en" ? "active" : ""}" aria-pressed="${lang === "en"}">${esc(t("settings_language_en"))}</button>
        </div></div></section>
      <section class="settings-section" aria-labelledby="settings-pair-title"><h2 id="settings-pair-title">${esc(t("settings_pair"))}</h2><div class="settings-card settings-pair-card">${settingsPairHTML(partner, partnerFailed)}</div></section>`;

    const telegramToggle = page.querySelector("[data-settings-telegram]");
    if (telegramToggle) telegramToggle.onclick = async () => {
      telegramToggle.disabled = true;
      try { serverSettings = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ telegram_notifications: !serverSettings.telegram_enabled }) }); }
      catch (_) { tg?.showAlert?.(t("settings_pair_load_error")); }
      render();
    };
    page.querySelectorAll("[data-settings-language]").forEach(button => button.onclick = () => setLang(button.dataset.settingsLanguage, () => showStatsSettings(returnMode, managePair)));
    const retry = page.querySelector("[data-settings-pair-retry]");
    if (retry) retry.onclick = () => showStatsSettings(returnMode, managePair);
    const create = page.querySelector("[data-settings-pair-create]");
    if (create) create.onclick = async () => {
      create.disabled = true;
      try { const invite = await api("/api/partner/invite", { method: "POST" }); sharePartnerLink(invite.link); } catch (_) { tg?.showAlert?.(t("settings_pair_load_error")); create.disabled = false; return; }
      showStatsSettings(returnMode);
    };
    const invited = page.querySelector("[data-settings-pair-invited]");
    if (invited) invited.onclick = () => settingsInvitePanel(page, partner, returnMode);
    const manage = page.querySelector("[data-settings-pair-manage]");
    if (manage) manage.onclick = openPairManagement;
  };
  render();
}

function settingsInvitePanel(page, partner, returnMode) {
  const card = page.querySelector(".settings-pair-card");
  if (!card) return;
  card.innerHTML = `<div class="settings-invite"><b>${esc(t("settings_pair_invited"))}</b><div class="settings-invite-code" data-settings-pair-code>${esc(partner.code || "")}</div><button class="settings-primary-full" data-settings-pair-share type="button">${esc(t("partner_share_btn"))}</button><button class="settings-text-action" data-settings-pair-code-enter type="button">${esc(t("partner_code_btn"))}</button></div>`;
  card.querySelector("[data-settings-pair-code]")?.addEventListener("click", () => copyText(partner.code || ""));
  card.querySelector("[data-settings-pair-share]")?.addEventListener("click", () => sharePartnerLink(partner.link));
  card.querySelector("[data-settings-pair-code-enter]")?.addEventListener("click", () => settingsPartnerCodeForm(card, returnMode));
}

function settingsPartnerCodeForm(card, returnMode) {
  card.innerHTML = `<div class="settings-invite"><b>${esc(t("partner_code_btn"))}</b><input class="code-input" data-settings-pair-input placeholder="${esc(t("partner_code_ph"))}" autocomplete="off" autocapitalize="off"><button class="settings-primary-full" data-settings-pair-connect type="button">${esc(t("partner_connect"))}</button></div>`;
  const input = card.querySelector("[data-settings-pair-input]");
  input?.focus();
  card.querySelector("[data-settings-pair-connect]")?.addEventListener("click", async event => {
    let code = input?.value.trim() || "";
    const match = code.match(/inv_[A-Za-z0-9_-]+/);
    if (match) code = match[0];
    if (!code) return;
    event.currentTarget.disabled = true;
    try {
      const result = await api("/api/partner/accept", { method: "POST", body: JSON.stringify({ token: code }) });
      if (result.ok) { tg?.HapticFeedback?.notificationOccurred("success"); tg?.showAlert?.(t("accept_ok", result.partner?.name), () => showStatsSettings(returnMode)); }
      else { tg?.showAlert?.(t("accept_fail_" + result.reason) || t("accept_fail_invalid")); event.currentTarget.disabled = false; }
    } catch (_) { event.currentTarget.disabled = false; }
  });
}

function confirmPairUnlink(onConfirm) {
  if (tg?.showConfirm) { tg.showConfirm(t("partner_unpair_confirm"), ok => { if (ok) onConfirm(); }); return; }
  if (window.confirm(t("partner_unpair_confirm"))) onConfirm();
}

async function showPersonFilms({ name, role, scope }) {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  const title = t(scope === "pair" ? "stats_person_films_pair" : "stats_person_films", name);
  screen.innerHTML = `<div class="sub-head">${backBtn()}<h1>${esc(title)}</h1></div>
    <div class="person-films-intro"><span>${esc(role === "director" ? t("chart_directors") : t("chart_actors"))}</span><b>${esc(name)}</b></div>
    <div id="person-films-grid">${skeletonGrid(6)}</div>`;
  wireBack(() => showStats(scope));
  try {
    const path = `/api/stats/person?role=${encodeURIComponent(role)}&name=${encodeURIComponent(name)}&scope=${encodeURIComponent(scope)}`;
    const { items } = await api(path);
    const grid = document.getElementById("person-films-grid");
    if (!grid) return;
    if (!items.length) {
      grid.innerHTML = emptyState("🎬", t("stats_person_empty"));
      return;
    }
    const back = () => showPersonFilms({ name, role, scope });
    grid.replaceChildren(gridOf(items, movie => {
      const rating = scope === "pair" && movie.partner_rating != null
        ? `★ ${movie.my_rating ?? "—"} · ${movie.partner_rating}`
        : (movie.my_rating != null ? `★ ${movie.my_rating}` : undefined);
      return posterTile(movie, { badge: rating, onClick: () => openDetail(movie.id, back, movie) });
    }));
  } catch (_) {
    const grid = document.getElementById("person-films-grid");
    if (grid) grid.innerHTML = emptyState("⚠️", t("load_err"));
  }
}

function pairHeroHTML(ps) {
  const meUser = tg?.initDataUnsafe?.user || {};
  const myName = me?.label || meUser.first_name || t("stats_profile_fallback");
  const partner = ps.partner || {};
  const partnerName = partner.name || t("partner_word");
  const myAvatar = userAvatarHTML({ photo_url: meUser.photo_url }, myName, "pair-avatar pair-avatar-me");
  const partnerAvatar = userAvatarHTML(partner, partnerName, "pair-avatar pair-avatar-partner");
  const partnerHandle = partner.username ? `@${partner.username}` : "";
  const myHandle = me?.username || meUser.username ? `@${me?.username || meUser.username}` : "";
  const empty = !ps.watched && !ps.want && !ps.rated_together;
  const compatibility = ps.agreement != null ? `${ps.agreement}%` : "—";
  const explainer = ps.agreement != null ? t("partner_explainer", ps.rated_together) : t("pair_empty");
  return `<section class="pair-hero">
    <div class="pair-heading"><div class="pair-avatar-stack">${myAvatar}<span class="pair-heart">♥</span>${partnerAvatar}</div>
      <div class="pair-names"><div class="pair-people"><span class="pair-person"><b>${esc(myName)}</b>${myHandle ? `<small>${esc(myHandle)}</small>` : ""}</span><i>×</i><span class="pair-person"><b>${esc(partnerName)}</b>${partnerHandle ? `<small>${esc(partnerHandle)}</small>` : ""}</span></div><p>${esc(t("pair_subtitle"))}</p></div></div>
    <div class="pair-compat"><div class="pair-compat-copy"><span>♥ ${esc(t("pair_compat_title"))}</span><strong>${compatibility}</strong><p>${esc(explainer)}</p></div><div class="pair-gauge"><div class="pair-gauge-track"><i style="--pair-score:${ps.agreement ?? 0}%"></i></div><b>${compatibility}</b></div></div>
    ${ps.matches != null ? `<div class="pair-fact"><span>${esc(t("partner_exact_hint"))}</span><b>${ps.matches}</b></div>` : ""}
  </section>`;
}

function pairHighlightsHTML(ps) {
  const favorites = ps.common_favorites || (ps.best ? [ps.best] : []);
  const disagreements = ps.disagreements || (ps.controversial ? [ps.controversial] : []);
  const poster = (item, alt) => item.poster_url
    ? `<img loading="lazy" decoding="async" src="${esc(posterSrc(item.poster_url, true))}" alt="${esc(alt)}" data-img-remove-on-error>`
    : `<span class="pair-poster-fallback" aria-hidden="true">✦</span>`;
  const favoriteCards = favorites.map((item, index) => `<button class="pair-favorite-card" type="button" data-film-id="${item.film_id}" aria-label="${esc(item.title)}">
    <span class="pair-favorite-rank">${index + 1}</span><span class="pair-favorite-poster">${poster(item, item.title)}</span>
    <span class="pair-favorite-copy"><b>${esc(item.title)}</b><small>♥ ${esc(t("pair_loved_by_both"))}</small><strong>★ ${item.avg ?? "—"}/10</strong></span>
  </button>`).join("");
  const differenceCards = disagreements.map(item => {
    const a = Number(item.a) || 0;
    const b = Number(item.b) || 0;
    const diff = item.diff ?? Math.abs(a - b);
    return `<button class="pair-difference-card" type="button" data-film-id="${item.film_id}" aria-label="${esc(item.title)}">
      <span class="pair-difference-heading"><span class="pair-difference-poster">${poster(item, item.title)}</span><span class="pair-difference-copy"><b>${esc(item.title)}</b><small>${esc(t("pair_difference"))} <strong>+${diff}</strong></small></span></span>
      <span class="pair-rating-compare">
        <span class="pair-rating-row pair-rating-me"><span>${esc(t("pair_rating_you"))} <b>★ ${a}</b></span><i><em style="width:${Math.min(100, Math.max(0, a * 10))}%"></em></i></span>
        <span class="pair-rating-row pair-rating-partner"><span>${esc(t("pair_rating_partner"))} <b>★ ${b}</b></span><i><em style="width:${Math.min(100, Math.max(0, b * 10))}%"></em></i></span>
      </span>
    </button>`;
  }).join("");
  return `<div class="pair-discovery">
    ${favoriteCards ? `<section class="pair-showcase pair-favorites-showcase"><header class="pair-showcase-head"><span class="pair-showcase-icon pair-showcase-icon-favorite" aria-hidden="true">♡</span><div><h2>${esc(t("pair_common_favorites"))}</h2><p>${esc(t("pair_common_favorites_hint"))}</p></div></header><div class="pair-favorites-rail" aria-label="${esc(t("pair_common_favorites"))}">${favoriteCards}</div></section>` : ""}
    ${differenceCards ? `<section class="pair-showcase pair-differences-showcase"><header class="pair-showcase-head"><span class="pair-showcase-icon pair-showcase-icon-difference" aria-hidden="true">↔</span><div><h2>${esc(t("pair_disagreements"))}</h2><p>${esc(t("pair_disagreements_hint"))}</p></div></header><div class="pair-differences-rail" aria-label="${esc(t("pair_disagreements"))}">${differenceCards}</div></section>` : ""}
  </div>`;
}

function userAvatarHTML(user, name, className = "profile-avatar") {
  const photo = user?.photo_url || user?.avatar_url;
  return `<div class="${className}"><span>${esc(initials(name))}</span>${photo ? `<img src="${esc(photo)}" alt="" loading="eager" data-img-remove-on-error>` : ""}</div>`;
}

function statsProfileHTML(s) {
  const telegramUser = tg?.initDataUnsafe?.user || {};
  const name = me?.label || telegramUser.first_name || t("stats_profile_fallback");
  const username = me?.username || telegramUser.username;
  const photo = telegramUser.photo_url;
  const hours = Math.floor((s.total_runtime_min || 0) / 60);
  const avatar = userAvatarHTML({ photo_url: photo }, name);
  const topGenre = cap(s.top_genres_pct?.[0]?.[0] || "");
  const topRating = (s.rating_dist || []).reduce((best, count, index, values) => count > values[best] ? index : best, 0) + 1;
  const taste = s.rating_dist?.some(v => v > 0) ? t("stats_taste_hint", topGenre, topRating) : t("stats_profile_sub");
  return `<section class="profile-hero">
    <div class="profile-main">${avatar}<div class="profile-copy"><div class="profile-name">${esc(name)}</div>
      <div class="profile-handle">${username ? `@${esc(username)}` : esc(t("stats_profile_sub"))}</div><p class="profile-taste">${esc(taste)}</p></div></div>
    <div class="profile-facts"><span><b>${s.watched || 0}</b> ${esc(t("tile_watched"))}</span><span><b>${s.avg_rating ?? "—"}</b> ${esc(t("tile_avg"))}</span><span><b>${hours}</b> ${esc(t("tile_hours"))}</span></div>
  </section>`;
}

function statsList(section, items, renderItem, expanded) {
  const visible = expanded[section] ? items : items.slice(0, 3);
  const rows = visible.map(renderItem).join("");
  if (items.length <= 3) return rows;
  return rows + `<button class="stats-more" data-stats-expand="${section}">${esc(t(expanded[section] ? "stats_less" : "stats_more"))}<span>${expanded[section] ? "↑" : "↓"}</span></button>`;
}

// Genre visuals are intentionally small inline SVGs: they load instantly in the
// Telegram WebView, inherit the app's colour system, and never depend on an
// external icon CDN.  The aliases cover both catalogue languages and common API
// spellings; unknown genres get a neutral film icon instead of a broken image.
const GENRE_VISUALS = {
  drama: { color: "#3b82f6", glow: "rgba(59,130,246,.22)", icon: '<path d="M4.5 7.5 10 5l4 2.5v9L9.5 19 4.5 16.5zM14 7.5 19.5 5l.5 11.5-5 2.5z"/><path d="M7 11h.01M11 11h.01M7 14c1.1 1 2.9 1 4 0M16.5 11h.01M18.5 11h.01M16 14c.7.6 1.6.8 2.4.5"/>' },
  thriller: { color: "#a855f7", glow: "rgba(168,85,247,.22)", icon: '<path d="m5 4 15 15M8 7l-3 8 8-3M16 14l3-8-8 3M4 20l3-3"/>' },
  mystery: { color: "#14b8a6", glow: "rgba(20,184,166,.22)", icon: '<path d="M5 11c2.5-3 11.5-3 14 0M8 10l1-4h6l1 4M4 14h16M7 15a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm10 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z"/>' },
  romance: { color: "#ec4899", glow: "rgba(236,72,153,.22)", icon: '<path d="M20 8.5C20 5.5 16.7 4 14.5 6.2L12 8.7 9.5 6.2C7.3 4 4 5.5 4 8.5c0 4.7 8 9.5 8 9.5s8-4.8 8-9.5Z"/>' },
  comedy: { color: "#fbbf24", glow: "rgba(251,191,36,.22)", icon: '<circle cx="12" cy="12" r="8"/><path d="M8 10h.01M16 10h.01M8.5 14c2 2 5 2 7 0"/>' },
  scifi: { color: "#38bdf8", glow: "rgba(56,189,248,.22)", icon: '<path d="M6 14c0-5 2.4-9 6-9s6 4 6 9c0 3-2.7 5-6 5s-6-2-6-5Z"/><path d="M8.5 12h.01M15.5 12h.01M9 16c1.8 1 4.2 1 6 0"/>' },
  fantasy: { color: "#4ade80", glow: "rgba(74,222,128,.22)", icon: '<path d="M5 17c1-6 3-9 7-9 3 0 4.5 1.6 5.5 4.5M7 11 5 7l4 1 1-4 2 4M10 17c2-1 4.5-1 7 .5M17 6l1.5-2M19 9l2-1"/>' },
  adventure: { color: "#fb923c", glow: "rgba(251,146,60,.22)", icon: '<path d="m5 19 14-14M8 16l-3 3 3-1M16 8l3-3-1 3M7 6l4 4M14 13l4 4"/>' },
  horror: { color: "#fb5a62", glow: "rgba(251,90,98,.22)", icon: '<path d="M7 19v-3l-2-2 2-2V8l2-3h6l2 3v4l2 2-2 2v3l-2 2H9z"/><path d="M9 11h.01M15 11h.01M10 15h4"/>' },
  crime: { color: "#22c7be", glow: "rgba(34,199,190,.22)", icon: '<path d="M7 20v-5a5 5 0 0 1 10 0v5M5 9c0-3 14-3 14 0M8 9l1-5h6l1 5M4 20h16M9 15h.01M15 15h.01"/>' },
  courtroom: { color: "#8b5cf6", glow: "rgba(139,92,246,.22)", icon: '<path d="m7 7 6 6M9 5l-4 4 5 5 4-4zM15 13l4 4M4 20h16"/>' },
  biography: { color: "#fbbf24", glow: "rgba(251,191,36,.22)", icon: '<path d="m12 4 2.2 4.5L19 9.2l-3.5 3.4.8 4.8-4.3-2.3-4.3 2.3.8-4.8L5 9.2l4.8-.7z"/>' },
  space: { color: "#38bdf8", glow: "rgba(56,189,248,.22)", icon: '<path d="M14 4c3 2 4 5 3 9l2 2-3 1-1 3-2-2c-4 1-7 0-9-3 1-4 5-8 10-10Z"/><path d="M12 9h.01M5 19l3-3"/>' },
  nature: { color: "#68d46b", glow: "rgba(104,212,107,.22)", icon: '<path d="M19 5C10 5 5 9 5 16c0 2 1 3 3 3 7 0 11-5 11-14Z"/><path d="M5 19c3-4 6-6 11-9"/>' },
  action: { color: "#f97316", glow: "rgba(249,115,22,.22)", icon: '<circle cx="15" cy="5" r="2"/><path d="m11 10 4-2 2 3 3 1M11 10l-3 4 3 2 1 4M14 12l-1 4 4 3"/>' },
  war: { color: "#3297f6", glow: "rgba(50,151,246,.22)", icon: '<path d="M12 4c2 2 4.5 2 7 2v5c0 5-3 8-7 9-4-1-7-4-7-9V6c2.5 0 5-.5 7-2Z"/><path d="m12 9 1 2 2 .3-1.5 1.4.4 2-1.9-1-1.9 1 .4-2L9 11.3l2-.3z"/>' },
  music: { color: "#9b5cf5", glow: "rgba(155,92,245,.22)", icon: '<path d="M14 5v10.5a3 3 0 1 1-2-2.8V7l7-2v8.5a3 3 0 1 1-2-2.8V4z"/>' },
  family: { color: "#4297f7", glow: "rgba(66,151,247,.22)", icon: '<circle cx="9" cy="8" r="2.5"/><circle cx="16" cy="9" r="2"/><path d="M4 19v-2.5C4 14 6.3 13 9 13s5 1 5 3.5V19M14 19v-2c0-1.8 1.5-3 3.5-3S21 15.2 21 17v2"/>' },
  animation: { color: "#4ade80", glow: "rgba(74,222,128,.22)", icon: '<circle cx="12" cy="12" r="6"/><circle cx="8" cy="7" r="2"/><circle cx="16" cy="7" r="2"/><path d="M9 13h.01M15 13h.01M9.5 16c1.5 1 3.5 1 5 0"/>' },
  documentary: { color: "#3b82f6", glow: "rgba(59,130,246,.22)", icon: '<circle cx="12" cy="12" r="8"/><path d="M4 12h16M12 4c2 2 3 5 3 8s-1 6-3 8c-2-2-3-5-3-8s1-6 3-8Z"/>' },
  history: { color: "#ec4899", glow: "rgba(236,72,153,.22)", icon: '<circle cx="12" cy="10" r="4"/><path d="m9 14-2 6 5-2 5 2-2-6M10 10l1.3 1.3L14 8.8"/>' },
  default: { color: "#6f88aa", glow: "rgba(111,136,170,.20)", icon: '<path d="M4 7.5 8 5h8l4 2.5v9L16 19H8l-4-2.5zM8 5l4 4 4-4M4 7.5l4 4-4 4M20 7.5l-4 4 4 4"/>' },
};

function genreKey(genre) {
  const key = String(genre || "").trim().toLowerCase().replace(/[–—-]/g, " ").replace(/\s+/g, " ");
  const aliases = {
    "драма": "drama", "триллер": "thriller", "детектив": "mystery", "детективный": "mystery", "detective": "mystery", "містерія": "mystery", "мелодрама": "romance", "романтика": "romance",
    "комедия": "comedy", "комедія": "comedy", "фантастика": "scifi", "научная фантастика": "scifi", "наукова фантастика": "scifi", "sci fi": "scifi", "science fiction": "scifi",
    "фэнтези": "fantasy", "фентезі": "fantasy", "приключения": "adventure", "пригоди": "adventure", "ужасы": "horror", "жахи": "horror",
    "криминал": "crime", "кримінал": "crime", "судебный": "courtroom", "биография": "biography", "біографія": "biography", "космос": "space", "природа": "nature",
    "боевик": "action", "екшн": "action", "война": "war", "війна": "war", "музыка": "music", "музика": "music", "мюзикл": "music",
    "семейный": "family", "сімейний": "family", "kids": "family", "children": "family", "мультфильм": "animation", "анимация": "animation", "анімація": "animation", "animation": "animation", "musical": "music", "dance": "music", "документальный": "documentary", "документальний": "documentary",
    "talk show": "comedy", "fairy tale": "fantasy", "educational": "documentary", "history": "history", "история": "history", "історія": "history", "historical": "history", "period": "history", "politics": "history", "news": "documentary", "business": "documentary",
    "time travel": "scifi", "puzzle": "mystery", "psychological": "mystery", "mind bending": "mystery", "surreal": "mystery", "suspense": "thriller", "spy": "thriller", "post apocalyptic": "scifi", "vampire": "horror", "werewolf": "horror", "zombie": "horror", "alien": "scifi", "cyberpunk": "scifi", "techno": "scifi", "road movie": "adventure", "western": "adventure",
  };
  return aliases[key] || key.replace(/[^a-z]/g, "") || "default";
}

function genreVisual(genre) { return GENRE_VISUALS[genreKey(genre)] || GENRE_VISUALS.default; }
function genreIcon(genre, className = "") {
  const visual = genreVisual(genre);
  return `<span class="${className}" style="--genre-color:${visual.color};--genre-glow:${visual.glow}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${visual.icon}</svg></span>`;
}

function genreRow([genre, pct, count]) {
  const percentage = Math.max(0, Math.min(100, Number(pct) || 0));
  const countValue = Number.isFinite(Number(count)) ? Number(count) : null;
  const visual = genreVisual(genre);
  return `<div class="genre-stat-row" style="--genre-color:${visual.color};--genre-glow:${visual.glow}">
    ${genreIcon(genre, "genre-stat-icon")}<div class="genre-stat-name">${esc(cap(genre))}</div>
    <div class="genre-stat-track"><i style="width:${Math.max(percentage ? 7 : 0, percentage)}%"></i></div>
    <div class="genre-stat-value"><b>${percentage}%</b>${countValue != null ? `<small>${esc(t("stats_films", countValue))}</small>` : ""}</div>
  </div>`;
}

function genreStatsCard(items, expanded) {
  return `<section class="genre-stats-card">
    <div class="genre-stats-head">${genreIcon("drama", "genre-title-icon")}<div><h2>${esc(t("chart_genres"))}</h2><p>${esc(t("stats_genres_hint"))}</p></div></div>
    <div class="genre-stat-list">${statsList("genres", items, genreRow, expanded)}</div>
  </section>`;
}

function peopleSectionIcon(type) {
  const icon = type === "actors"
    ? '<path d="M15 19v-1.4c0-2-1.8-3.6-4-3.6s-4 1.6-4 3.6V19"/><circle cx="11" cy="8" r="3"/><path d="m18 12 .9 1.8 2 .3-1.4 1.4.3 2-1.8-1-1.8 1 .4-2-1.5-1.4 2-.3z"/>'
    : '<path d="M5 8h14M7 8V5h10v3M7 12h10v7H7zM4 12h16M9 16h.01M15 16h.01"/><path d="M4 20h16"/>';
  return `<span class="people-section-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${icon}</svg></span>`;
}

function personStatCard(item, index, type) {
  const [name, count, photoUrl, ...photoFallbacks] = item;
  const favorite = index === 0;
  const photo = photoUrl ? `<img loading="lazy" decoding="async" alt="${esc(name)}" data-person-photo data-person-photo-src="${esc(photoUrl)}" data-person-photo-fallbacks="${esc(JSON.stringify(photoFallbacks.filter(Boolean)))}">` : "";
  const favoriteLabel = type === "actors" ? t("stats_favorite_actor") : t("stats_favorite_director");
  return `<button class="person-stat-card${favorite ? " is-favorite" : ""}" type="button" data-stats-person-name="${esc(name)}" data-stats-person-role="${type === "actors" ? "actor" : "director"}" aria-label="${esc(t("stats_person_open", name))}">
    <span class="person-stat-rank" aria-label="${index + 1}">${index + 1}</span>
    <span class="person-stat-avatar person-stat-avatar-${type}"><span class="fb">${esc(initials(name))}</span>${photo}</span>
    ${favorite ? `<span class="person-stat-favorite"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="m12 3.8 2.5 5 5.5.8-4 3.9.9 5.5-4.9-2.6-4.9 2.6.9-5.5-4-3.9 5.5-.8z"/></svg>${esc(favoriteLabel)}</span>` : ""}
    <div class="person-stat-copy"><b title="${esc(name)}">${esc(name)}</b><small>${esc(t("stats_films", count))}</small></div>
  </button>`;
}

function peopleStatsSection({ type, title, subtitle, items }) {
  const list = Array.isArray(items) ? items : [];
  const emptyKey = type === "actors" ? "stats_actors_empty" : "stats_directors_empty";
  const body = list.length
    ? `<div class="people-stats-rail" aria-label="${esc(title)}">${list.map((item, index) => personStatCard(item, index, type)).join("")}</div>`
    : `<div class="people-stats-empty"><b>${esc(t(emptyKey))}</b><span>${esc(t("stats_people_empty_hint"))}</span></div>`;
  return `<section class="people-stats-section people-stats-${type}"><header class="people-stats-head">${peopleSectionIcon(type)}<div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div></header>${body}</section>`;
}

function personalStatsHTML(s, scope = "me", expanded = { genres: false }) {
  const y = s.year;
  const hours = Math.floor(s.total_runtime_min / 60);
  const topGenre = cap(s.top_genres_pct?.[0]?.[0] || "");
  const topRating = (s.rating_dist || []).reduce((best, count, index, values) => count > values[best] ? index : best, 0) + 1;
  const intro = "";
  const tiles = `<div class="stats-grid">
    ${statTile("", s.watched, t(scope === "pair" ? "tile_shared_watched" : "tile_watched"))}${statTile("", s.want, t(scope === "pair" ? "tile_shared_want" : "tile_want"))}
    ${statTile("", s.avg_rating ?? "—", t("tile_avg"))}${statTile("", hours, t("tile_hours"))}</div>`;
  const dist = s.rating_dist || [];
  const maxD = Math.max(1, ...dist);
  const rankedRatings = dist.map((count, index) => ({ rating: index + 1, count })).filter(x => x.count).sort((a, b) => b.count - a.count).slice(0, 2).map(x => x.rating);
  const ratingFooter = rankedRatings.length ? `<div class="chart-footer">${esc(scope === "pair" ? "Больше всего ставим: " : "Больше всего ставишь: ")}<b>${rankedRatings.join(" и ")}</b></div>` : "";
  const hist = dist.some(v => v > 0) ? chartCard(t(scope === "pair" ? "chart_ratings_pair" : "chart_ratings"), `<div class="chart-badge">${esc(t("tile_avg"))} <b>${s.avg_rating ?? "—"}</b></div><div class="stats-hint">${esc(t(scope === "pair" ? "stats_ratings_hint_pair" : "stats_ratings_hint"))}</div><div class="hist">${
    dist.map((c, i) => `<div class="hist-col"><div class="hist-bar-area">${c ? `<div class="hist-val">${c}</div>` : ""}<div class="hist-bar" style="height:${c ? Math.max(6, Math.round(c / maxD * 100)) : 0}%"></div></div><div class="hist-x">${i + 1}</div></div>`).join("")}</div>${ratingFooter}`) : "";
  const genres = s.top_genres_pct.length ? genreStatsCard(s.top_genres_pct, expanded) : "";
  const actorHint = scope === "pair" ? "stats_people_hint_pair" : "stats_people_hint";
  const directorHint = scope === "pair" ? "stats_directors_hint_pair" : "stats_directors_hint";
  const actors = peopleStatsSection({ type: "actors", title: t("chart_actors"), subtitle: t(actorHint), items: s.top_actors });
  const directors = peopleStatsSection({ type: "directors", title: t("chart_directors"), subtitle: t(directorHint), items: s.top_directors });
  const yearCard = scope !== "pair" && y.count ? `<section class="year-card">
    <div class="year-card-title">🏆 ${esc(t("year_title", y.year))}</div>
    <div class="year-line"><b>${y.count}</b> ${esc(t("count_films", y.count))}${y.avg_rating ? ` · ${esc(t("year_avg"))} <b>${y.avg_rating}</b>` : ""}</div>
    ${y.top_genre ? `<div class="year-line">${esc(t("year_fav_genre"))}${esc(y.top_genre)}</div>` : ""}
    ${y.top_actor ? `<div class="year-line">${esc(t("year_actor"))}${esc(y.top_actor[0])} <small>(${y.top_actor[1]})</small></div>` : ""}
    ${y.best_films && y.best_films.length ? `<div class="year-line">${esc(t("year_best"))} <small>(${y.best_avg})</small>: ${y.best_films.map(item => `<button class="stats-film-link" data-film-id="${item.film_id}" type="button">${esc(item.title)}</button>`).join(", ")}</div>` : y.best_titles && y.best_titles.length ? `<div class="year-line">${esc(t("year_best"))} <small>(${y.best_avg})</small>: ${y.best_titles.map(esc).join(", ")}</div>` : ""}</section>` : "";
  return intro + tiles + hist + genres + actors + directors + yearCard;
}

// ── Пара ──────────────────────────────────────────────────────────────────────
function partnerCardHTML(p, ps) {
  if (p.status === "paired") {
    // The active pair has a dedicated statistics tab. Pair management is only
    // available through Settings, so a stale fallback cannot expose unlinking
    // on the personal profile again.
    return "";
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
  screen.innerHTML = `<main class="accept-screen"><section class="accept" aria-labelledby="accept-title">
    <div class="accept-illustration" aria-hidden="true">
      <img class="accept-illustration-img" src="assets/pair-hearts.webp" width="560" height="422" alt="" decoding="async"
        onerror="this.hidden=true;this.nextElementSibling.hidden=false">
      <div class="accept-illustration-fallback" hidden></div>
    </div>
    <h1 class="accept-title" id="accept-title">${esc(t("accept_title"))}</h1>
    <p class="accept-sub">${esc(t("accept_sub"))}</p>
    <div class="accept-actions">
      <button class="accept-btn accept-btn-primary" id="acc-yes" type="button">${esc(t("accept_yes"))}</button>
      <button class="accept-btn" id="acc-no" type="button">${esc(t("accept_no"))}</button>
    </div></section></main>`;
  try {
    const preview = await api(`/api/partner/invite/${encodeURIComponent(param)}`);
    const inviter = preview?.inviter || {};
    const name = inviter.name || inviter.username;
    const title = document.getElementById("accept-title");
    if (title && name) {
      const marker = "\uFFF0";
      const [before, after = ""] = String(t("accept_title_from", marker)).split(marker);
      title.innerHTML = `${esc(before)}<span class="accept-inviter-name">${esc(name)}</span>${esc(after)}`;
    }
  } catch (_) { /* A stale link still keeps its normal accept/reject flow. */ }
  document.getElementById("acc-no").onclick = async () => {
    const button = document.getElementById("acc-no");
    if (button) button.disabled = true;
    try { await api("/api/partner/decline", { method: "POST", body: JSON.stringify({ token: param }) }); } catch (_) {}
    setActiveTab("home"); showHome();
  };
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

function statTile(icon, value, label) { return `<div class="tile">${icon ? `<div class="tile-icon">${icon}</div>` : ""}<div class="tile-val">${esc(value)}</div><div class="tile-label">${esc(label)}</div></div>`; }
function chartCard(title, inner) { return `<div class="chart-card"><div class="chart-title">${esc(title)}</div>${inner}</div>`; }
function hbar(label, valueText, pct) { return `<div class="hbar-row"><div class="hbar-label">${esc(label)}</div><div class="hbar-track"><div class="hbar-fill" style="width:${Math.max(4, pct)}%"></div></div><div class="hbar-val">${esc(valueText)}</div></div>`; }
function personPill(name, count, photoUrl = null) { return `<div class="person-pill"><span class="person-avatar"><span class="fb">${esc(initials(name))}</span>${photoUrl ? `<img loading="lazy" decoding="async" src="${posterSrc(photoUrl)}" alt="" data-img-retry data-person-photo>` : ""}</span><span><b>${esc(name)}</b><small>${esc(t("stats_films", count))}</small></span></div>`; }

// ── Навигация ─────────────────────────────────────────────────────────────────
function backBtn() { return `<button class="back" aria-label="${esc(t("back"))}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button>`; }
function wireBack(fn) { const b = screen.querySelector(".back"); if (b) b.onclick = fn; }
function setActiveTab(t) {
  document.querySelectorAll("#tabbar .tab").forEach(b => {
    const active = b.dataset.tab === t;
    b.classList.toggle("active", active);
    b.setAttribute("aria-current", active ? "page" : "false");
  });
}
function route(tab) { if (tab === "home") showHome(); else if (tab === "stats") showStats(); else showList(tab); }
function wireTabbarAutoHide() {
  if (_tabbarScrollHandler) window.removeEventListener("scroll", _tabbarScrollHandler);
  let lastY = window.scrollY;
  let ticking = false;
  const bar = document.getElementById("tabbar");
  const scrim = document.getElementById("top-scrim");
  const onScroll = () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      const y = window.scrollY;
      if (scrim) scrim.classList.toggle("show", y > 8);
      if (bar) {
        // У самого низа страницы всегда показываем панель: иначе, докрутив вниз,
        // её нельзя вернуть (наверх скроллить уже некуда) и по ней не нажать.
        const atBottom = window.innerHeight + y >= document.documentElement.scrollHeight - 4;
        if (atBottom) bar.classList.remove("tabbar-hidden");
        else if (y > 96 && y > lastY + 8) bar.classList.add("tabbar-hidden");
        else if (y < 96 || y < lastY - 8) bar.classList.remove("tabbar-hidden");
      }
      lastY = y;
      ticking = false;
    });
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  _tabbarScrollHandler = onScroll;
}
// Вне Telegram (нет window.Telegram.WebApp) — не падаем, а объясняем.
if (!tg) {
  screen.innerHTML = emptyState("💬", "Откройте в Telegram", "Это мини-приложение работает внутри Telegram");
} else {
  const activateTab = btn => {
    if (btn.dataset.navBusy === "1") return;
    btn.dataset.navBusy = "1";  // pointerup + click are both delivered on iOS
    window.setTimeout(() => { delete btn.dataset.navBusy; }, 250);
    const tab = btn.dataset.tab;
    if (!tab) return;
    tg.HapticFeedback?.impactOccurred("light");
    setActiveTab(tab);
    route(tab);
  };
  document.querySelectorAll("#tabbar .tab").forEach(btn => {
    btn.addEventListener("pointerup", event => {
      if (!event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
      event.preventDefault();
      activateTab(btn);
    });
    // Keyboard, desktop and WebViews without Pointer Events still work through click.
    btn.addEventListener("click", () => activateTab(btn));
  });
  wireTabbarAutoHide();
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
