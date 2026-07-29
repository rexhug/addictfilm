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
// Состояние интерфейсных возможностей приходит с СЕРВЕРА (/api/me → features) и
// нигде не выводится из сборки: закешированный Telegram-ом фронтенд пережил бы
// откат флага и продолжил рисовать экран, которого на сервере уже нет.
const featureEnabled = (name) => me?.features?.[name] === true;
let _returnTo = () => { setActiveTab("home"); showHome(); };
let _heroSource = null;      // {rect, src} стартовой точки hero-transition, захватывается в posterTile()
let _detailScrollHandler = null;  // текущий scroll-listener страницы фильма (снимается при уходе)
let _detailLoadController = null; // отменяет устаревший detail-fetch при быстром переходе
let _tabbarScrollHandler = null;
// Навигация «экран → фильм → назад»: стек снимков предыдущего экрана (реальный
// DOM + позиция скролла), чтобы выход из фильма возвращал ровно то, что было,
// без перезагрузки и сброса скролла. _detailFilm/_detailBaseline — для точечного
// обновления карточки, если внутри фильма изменили оценку/статус.
const _navStack = [];
let _detailFilm = null;
let _detailBaseline = null;
// Короткий session cache для тяжёлых home-rails. Он живёт только пока открыт
// Mini App и сбрасывается после любого изменения списка/оценки, поэтому UI не
// показывает устаревший статус фильма.
const _readCache = new Map();
const _READ_CACHE_TTL = 30_000;
let _notificationUnread = 0;
let _notificationRefreshBound = false;
let _notificationPollTimer = null;
let _notificationRefreshGeneration = 0;
let _notificationAppliedGeneration = 0;
let _notificationFilter = "all";
let _notificationUnreadByCategory = { all: 0, pair: 0, films: 0, system: 0 };
let _viewGeneration = 0;

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
    chip_popular: "Популярное", chip_top: "Топ сообщества", chip_genres: "Жанры", chip_collections: "Подборки", chip_nav: "Разделы каталога",
    see_all: "Смотреть все",
    reco_title: "Не знаешь, что включить?", reco_sub: "Подберём фильм под твоё настроение", reco_cta: "Подобрать",
    pick_tab: "Подбор", pick_title: "Что посмотреть?", pick_sub: "Быстрый вариант или подборка по настроению.", pick_wishlist_title: "Случайный из «Хочу»", pick_wishlist_sub: "Рулетка по твоему списку — без повторов",
    pick_random_title: "Умный случайный фильм", pick_random_sub: "Одно хорошее кино из каталога, без вопросов", pick_quiz_title: "Подбор по настроению", pick_quiz_sub: "7–8 коротких вопросов — и три варианта на вечер", pick_start: "Начать", pick_loading: "Подбираю фильм…", pick_another: "Другой вариант", pick_not_suggest: "Не предлагать", pick_open: "Открыть фильм", pick_want: "В «Хочу»", pick_watched: "Уже смотрел", pick_back: "Назад", pick_restart: "Начать заново", pick_progress: (n, total) => `${n} из ${total}`, pick_best: "Лучший выбор", pick_reliable: "Надёжный вариант", pick_unexpected: "Неожиданный вариант", pick_best_sub: "Максимально совпадает с твоим запросом", pick_reliable_sub: "Высокий рейтинг и уверенный выбор", pick_unexpected_sub: "Чуть необычнее, но может приятно удивить", pick_empty: "Пока не хватает фильмов для подбора", pick_empty_sub: "Добавь несколько фильмов через поиск — каталог будет расти вместе с приложением.", pick_pair: "Смотреть с партнёром", pick_solo: "Смотреть одному", pick_pair_unavailable: "Пара сейчас не подключена",
    pick_wishlist_empty: "В списке «Хочу посмотреть» пока пусто",
    strategy_reliable: "Надёжный выбор", strategy_taste_match: "Под твой вкус", strategy_discovery: "Находка", strategy_available: "Доступный вариант",
    pick_recovery_title: "Не удалось подобрать фильм", pick_retry: "Попробовать ещё раз",
    pick_back_to_picker: "Назад к выбору", pick_change_answers: "Изменить ответы",
    pick_smart_random: "Умный случайный фильм",
    pick_partial: "Под такой запрос в каталоге нашлось меньше вариантов, чем обычно — показываем только то, что действительно подходит.",
    pick_rejected: "Больше не предложим",
    pick_version_changed: "Подбор обновился — начнём опрос заново",
    pick_v2_preview: "V2 PREVIEW",
    pick_v2_preview_hint: "Тестовая версия нового подбора, доступная только администратору",
    pick_poster_alt: "Постер фильма", pick_backdrop_alt: "Кадр из фильма",
    pick_image_unavailable: "Изображение недоступно",
    art_frame: "Кадр", art_fit: "Показ изображения",
    art_contain: "Показать полностью", art_cover: "Заполнить экран",
    art_focus_x: "Фокус по горизонтали", art_focus_y: "Фокус по вертикали",
    art_save: "Сохранить", art_reset: "Сбросить", art_close: "Закрыть",
    art_reject_poster: "Отклонить этот постер",
    art_approve_poster: "Разрешить этот постер", art_auto_poster: "Автоматически",
    art_movie_flow: "В рекомендациях", art_movie_auto: "Автоматически",
    art_movie_allow: "Разрешить", art_movie_exclude: "Исключить",
    settings_attribution: "Данные об изображениях предоставлены fanart.tv",
    reason_DARK_COMEDY_TONE: "Чёрный юмор и мрачный тон", reason_SATIRICAL_HUMOR: "Сатира на серьёзные темы",
    reason_ABSURD_DARK_HUMOR: "Абсурдная комедия с мрачной подачей", reason_HIGH_TENSION: "Держит в напряжении",
    reason_INTELLECTUAL: "Требует внимания и размышления", reason_COZY_TONE: "Спокойный и светлый тон",
    reason_EMOTIONAL_STORY: "Сильная эмоциональная история", reason_LIGHT_HUMOR: "Много юмора",
    reason_FAST_PACE: "Быстрый темп с самого начала", reason_SLOW_ATMOSPHERE: "Медленная, атмосферная подача",
    reason_UNREAL_WORLD: "Придуманный, ненастоящий мир", reason_GROUNDED_STORY: "Достоверная, приземлённая история",
    reason_MATCHES_MOOD: "Совпадает с твоим запросом", reason_MATCHES_USER_TASTE: "Похож на то, что тебе нравится",
    reason_HIGH_QUALITY: "Высокая оценка зрителей", reason_HIDDEN_GEM: "Не на слуху, но крепкий",
    reason_IN_WISHLIST: "Уже в твоём списке «Хочу посмотреть»", reason_PAIR_FRIENDLY: "Подходит вам обоим",
    reason_UNSEEN_PICK: "Из фильмов, которых ты ещё не видел",
    reason_RANDOM_RELIABLE: "Надёжный выбор", reason_RANDOM_TASTE_MATCH: "Похоже на то, что ты любишь",
    reason_RANDOM_DISCOVERY: "Находка не на слуху", reason_RANDOM_AVAILABLE: "Доступный вариант",
    notif_title: "Уведомления", notif_empty_t: "Уведомлений пока нет", notif_empty_s: "Здесь появятся новые оценки, события пары и важные сообщения", notif_filtered_empty_t: "В этой категории пока пусто", notif_filtered_empty_s: "Новые события появятся здесь автоматически", notif_mark_all: "Прочитать все", notif_load_more: "Показать ещё", notif_loading: "Загружаю уведомления…", notif_error: "Не удалось загрузить уведомления", notif_error_s: "Проверь соединение и попробуй ещё раз", notif_retry: "Повторить", notif_filter_all: "Все", notif_filter_pair: "Пара", notif_filter_films: "Фильмы", notif_filter_system: "Система", notif_now: "Только что", notif_min_ago: (n) => `${n} мин назад`, notif_hour_ago: (n) => `${n} ч назад`, notif_day_ago: (n) => `${n} дн назад`, notif_inapp: "В приложении", notif_telegram: "В Telegram", notif_telegram_hint: "События пары от бота Addict Film", notif_telegram_unavailable: "Бот сейчас недоступен", notif_browser: "В браузере", notif_browser_hint: "Локальные напоминания на этом устройстве",
    back: "Назад", settings_title: "Настройки", settings_loading: "Загружаю настройки…",
    settings_notifications: "Уведомления", settings_notifications_hint: "Важные события пары всегда видны в приложении", settings_notifications_on: "Включены", settings_notifications_off: "Выключены", settings_notifications_permission: "Нужно разрешение", settings_notifications_denied: "Разрешения отключены в Telegram или браузере", settings_notifications_unavailable: "Недоступны на этом устройстве", settings_notifications_error: "Не удалось запросить разрешение",
    settings_language: "Язык", settings_language_hint: "Изменится сразу во всём приложении", settings_language_ru: "Русский", settings_language_en: "English",
    settings_pair: "Пара", settings_pair_none: "Создайте пару, чтобы смотреть и оценивать фильмы вместе", settings_pair_create: "Создать пару", settings_pair_current: "Ваша пара", settings_pair_manage: "Управление парой", settings_pair_invited: "Приглашение ожидает принятия", settings_pair_load_error: "Не удалось загрузить статус пары", settings_pair_try_again: "Повторить",
    collections_empty_s: "Загляни позже", collections_empty_admin_s: "Создай первую подборку",
    collections_title_ph: "Название подборки", collections_create_btn: "Создать",
    admin_section: "Администрирование", admin_mode_row: "Режим администратора",
    admin_mode_hint: "Редактируйте подборки и блоки приложения прямо в интерфейсе.",
    admin_mode_active: "Режим администратора", admin_exit_mode: "Выйти",
    admin_permission_revoked: "Права администратора отозваны. Режим выключен.",
    admin_conflict: "Подборку изменил другой администратор. Откройте её заново.",
    admin_status_draft: "Черновик", admin_status_published: "Опубликовано",
    admin_status_archived: "В архиве",
    admin_publish: "Опубликовать", admin_unpublish: "Снять с публикации",
    admin_archive: "Архивировать", admin_restore: "Вернуть в черновики",
    admin_delete_forever: "Удалить навсегда", admin_save: "Сохранить",
    admin_title_label: "Название", admin_description_label: "Описание",
    admin_description_ph: "Коротко о подборке (необязательно)",
    admin_move_up: "Выше", admin_move_down: "Ниже", admin_remove: "Убрать",
    admin_move_left: "Левее", admin_move_right: "Правее", admin_edit: "Редактировать",
    admin_empty_publish: "Нельзя опубликовать пустую подборку",
    admin_published_delete: "Сначала снимите подборку с публикации",
    admin_saved: "Сохранено", admin_drafts_hidden: "Черновик виден только администраторам",
    admin_archived_hidden: "Архив скрыт от пользователей",
    admin_reorder_hint: "Стрелками меняйте порядок фильмов",
    admin_display_label: "Формат отображения",
    admin_display_standard: "Обычная", admin_display_standard_hint: "Компактная карточка в разделе «Подборки»",
    admin_display_featured: "Большая", admin_display_featured_hint: "Крупная редакционная подборка на главной",
    admin_backdrop_label: "Фоновое изображение",
    admin_backdrop_from_film: "Из фильма подборки", admin_backdrop_url: "Ссылка (https)",
    admin_backdrop_none: "Пока нет изображения — добавьте фильм с кадром или укажите ссылку",
    admin_preview_label: "Предпросмотр",
    admin_create_featured: "Создать большую подборку",
    admin_featured_image_required: "Для большой подборки нужно изображение",
    admin_url_not_allowed: "Разрешены только https-ссылки",
    coll_eyebrow: "Подборка",
    admin_save_draft: "Сохранить черновик",
    admin_new_collection: "Новая подборка", admin_new_featured: "Новая большая подборка",
    admin_unsaved: "Не сохранено", admin_new_badge: "Новая",
    admin_preview_title_ph: "Название подборки",
    admin_no_films: "Фильмов пока нет — добавьте первый",
    admin_err_title: "Введите название подборки",
    admin_err_films: "Добавьте хотя бы один фильм",
    admin_unsaved_changes: "Изменения не сохранены. Выйти без сохранения?",
    coll_confirm_add: (title) => `Добавить «${title}» в подборку?`, coll_already_in: "Уже в этой подборке",
    coll_remove_confirm: (title) => `Убрать «${title}» из подборки?`, coll_add_film_btn: "+ Добавить фильм",
    coll_edit_hint: "Тап на фильм — убрать из подборки",
    coll_delete_btn: "Удалить подборку", coll_delete_confirm: (title) => `Удалить подборку «${title}»? Фильмы останутся в каталоге.`,
    tab_home: "Главная", tab_want: "Хочу", tab_watched: "Смотрел", tab_pick: "Подбор", tab_stats: "Статистика",
    list_want: "Хочу посмотреть", list_watched: "Смотрел",
    sort_title: "Сортировка", sort_best: "Лучшие", sort_new: "Сначала новые", sort_old: "Сначала старые", sort_worst: "Худшие",
    count_films: (n) => pl(n, ["фильм", "фильма", "фильмов"]),
    rail_empty: "Пока пусто — добавь фильмы через поиск", rail_err: "Не удалось загрузить",
    genres_empty: "Каталог пока пуст",
    genre_empty_t: "Пока пусто", genre_empty_s: "В этом жанре ещё нет фильмов", load_err: "Ошибка загрузки",
    want_empty_t: "Список пуст", want_empty_s: "Добавь фильмы через поиск",
    watched_empty_t: "Пока ничего не просмотрено", watched_empty_s: "Отмечай фильмы «Смотрел»",
    load_more: "Показать ещё", loading: "Загрузка…", retry: "Повторить",
    my_rating: "Моя оценка", rate_hint: " · тап = «Смотрел(а)»", dir: "Режиссёр ",
    act_want: "Хочу посмотреть", act_watched: "Отметить как просмотрено", act_to_want: "В «Хочу»", act_remove: "Убрать из списка",
    already_watched_link: "Уже смотрел? Отметить",
    my_review: "Мой отзыв", comment_ph: "Написать отзыв…",
    cast_title: "Актёры", share_text: (title) => `Смотри «${title}» в Addict Film`,
    confirm_remove: (title) => `Убрать «${title}» из своего списка?`,
    search_start_t: "Что смотрим?", search_start_s: "Введи название — минимум 2 буквы",
    search_toomany_t: "Слишком часто", search_toomany_s: "Подожди минуту и попробуй снова",
    search_err_t: "Ошибка поиска",
    search_limited_t: "Поиск временно ограничен", search_limited_s: "Дневной лимит источника. Попробуй позже",
    search_none_t: "Ничего не найдено", search_none_s: "Попробуй год или английское название",
    confirm_add: (title) => `Добавить «${title}» в «Хочу посмотреть»?`, already_in_list: "Уже в твоём списке!",
    stats_title: "Мой кинопрофиль", my_stats: "Моя статистика", stats_empty_t: "Пока нет статистики", stats_empty_s: "Добавь фильмы и поставь оценки", calc: "Считаю…",
    stats_profile_fallback: "Киноман", stats_profile_sub: "Твоя история в кино", stats_more: "Показать ещё", stats_less: "Свернуть", stats_view_all: "Посмотреть всех",
    stats_me_tab: "Я", stats_together_tab: "Мы вместе", stats_taste: "Твой вкус", stats_together: "Ваша история",
    stats_ratings_hint: "Сколько фильмов ты поставил(а) на каждую оценку", stats_ratings_hint_pair: "Сколько общих фильмов попало в каждую оценку", stats_genres_hint: "Доля жанра среди просмотренных фильмов",
    stats_people_hint: "Чаще всего встречаются в просмотренных фильмах", stats_directors_hint: "Чаще всего среди просмотренных фильмов", stats_people_hint_pair: "Чаще всего встречаются в общих фильмах", stats_directors_hint_pair: "Чаще всего среди общих фильмов", stats_films: (n) => `${n} ${pl(n, ["фильм", "фильма", "фильмов"])}`,
    stats_favorite_actor: "Любимый актёр", stats_favorite_director: "Любимый режиссёр", stats_actors_empty: "Недостаточно данных об актёрах", stats_directors_empty: "Недостаточно данных о режиссёрах", stats_people_empty_hint: "Статистика появится после просмотра фильмов.", stats_person_films: (name) => `Фильмы с ${name}`, stats_person_films_pair: (name) => `Общие фильмы с ${name}`, stats_person_open: (name) => `Открыть фильмы с ${name}`, stats_person_empty: "Таких просмотренных фильмов пока нет",
    stats_taste_hint: (genre, rating) => genre ? `Тебе особенно нравятся ${genre}; чаще всего ты ставишь ${rating}.` : `Чаще всего ты ставишь ${rating}.`,
    tile_watched: "просмотрено", tile_want: "в «Хочу»", tile_shared_watched: "вместе посмотрено", tile_shared_want: "вместе в «Хочу»", tile_avg: "средняя", tile_hours: "часов",
    chart_ratings: "Как ты оцениваешь фильмы", chart_ratings_pair: "Общие оценки", chart_genres: "Жанры", chart_actors: "Актёры", chart_directors: "Режиссёры",
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
    chip_popular: "Popular", chip_top: "Community Top", chip_genres: "Genres", chip_collections: "Collections", chip_nav: "Catalog sections",
    see_all: "See all",
    reco_title: "Not sure what to watch?", reco_sub: "We'll find a film for your current mood", reco_cta: "Find a film",
    pick_tab: "Pick", pick_title: "What should we watch?", pick_sub: "A quick pick or a mood-based selection.", pick_wishlist_title: "Random from wishlist", pick_wishlist_sub: "A roulette over your own list — no repeats",
    pick_random_title: "Smart random film", pick_random_sub: "One good film from the catalog, no questions", pick_quiz_title: "Pick by mood", pick_quiz_sub: "7–8 quick questions, then three options for tonight", pick_start: "Start", pick_loading: "Finding a film…", pick_another: "Another option", pick_not_suggest: "Don't suggest", pick_open: "Open film", pick_want: "Add to wishlist", pick_watched: "Already watched", pick_back: "Back", pick_restart: "Start over", pick_progress: (n, total) => `${n} of ${total}`, pick_best: "Best match", pick_reliable: "Reliable choice", pick_unexpected: "Unexpected choice", pick_best_sub: "The closest match for your request", pick_reliable_sub: "A highly rated, confident pick", pick_unexpected_sub: "A little more unusual, potentially rewarding", pick_empty: "There are not enough films to recommend yet", pick_empty_sub: "Add a few films through search — the catalog grows with the app.", pick_pair: "Watch with partner", pick_solo: "Watch alone", pick_pair_unavailable: "Your pair is not connected right now",
    pick_wishlist_empty: "Your watchlist is empty for now",
    strategy_reliable: "Reliable choice", strategy_taste_match: "Matches your taste", strategy_discovery: "Discovery", strategy_available: "Available option",
    pick_recovery_title: "Couldn’t pick a movie", pick_retry: "Try again",
    pick_back_to_picker: "Back to picker", pick_change_answers: "Change answers",
    pick_smart_random: "Smart random film",
    pick_partial: "This request has fewer good matches in the catalog than usual — we only show what genuinely fits.",
    pick_rejected: "We won't suggest it again",
    pick_version_changed: "The picker was updated — let's start the quiz again",
    pick_v2_preview: "V2 PREVIEW",
    pick_v2_preview_hint: "Preview of the new recommendation engine, available only to administrators",
    pick_poster_alt: "Movie poster", pick_backdrop_alt: "Movie backdrop",
    pick_image_unavailable: "Image unavailable",
    art_frame: "Frame", art_fit: "Image fit", art_contain: "Show full image",
    art_cover: "Fill screen", art_focus_x: "Horizontal focus",
    art_focus_y: "Vertical focus", art_save: "Save", art_reset: "Reset",
    art_close: "Close", art_reject_poster: "Reject this poster",
    art_approve_poster: "Allow this poster", art_auto_poster: "Automatic",
    art_movie_flow: "In recommendations", art_movie_auto: "Automatic",
    art_movie_allow: "Allow", art_movie_exclude: "Exclude",
    settings_attribution: "Artwork data provided by fanart.tv",
    reason_DARK_COMEDY_TONE: "Dark humour with a grim tone", reason_SATIRICAL_HUMOR: "Satire about serious things",
    reason_ABSURD_DARK_HUMOR: "Absurd comedy, grim delivery", reason_HIGH_TENSION: "Keeps the tension up",
    reason_INTELLECTUAL: "Asks for attention and thought", reason_COZY_TONE: "Calm, light tone",
    reason_EMOTIONAL_STORY: "A strong emotional story", reason_LIGHT_HUMOR: "Plenty of humour",
    reason_FAST_PACE: "Fast from the first minute", reason_SLOW_ATMOSPHERE: "Slow, atmospheric pacing",
    reason_UNREAL_WORLD: "An invented, unreal world", reason_GROUNDED_STORY: "A grounded, believable story",
    reason_MATCHES_MOOD: "Matches what you asked for", reason_MATCHES_USER_TASTE: "Close to what you usually like",
    reason_HIGH_QUALITY: "Highly rated by viewers", reason_HIDDEN_GEM: "Not well known, but solid",
    reason_IN_WISHLIST: "Already on your watchlist", reason_PAIR_FRIENDLY: "Works for both of you",
    reason_UNSEEN_PICK: "From films you have not seen yet",
    reason_RANDOM_RELIABLE: "A reliable choice", reason_RANDOM_TASTE_MATCH: "Close to what you love",
    reason_RANDOM_DISCOVERY: "An off-the-radar find", reason_RANDOM_AVAILABLE: "An available choice",
    notif_title: "Notifications", notif_empty_t: "No notifications yet", notif_empty_s: "New ratings, pair activity, and important updates will appear here", notif_filtered_empty_t: "Nothing in this category yet", notif_filtered_empty_s: "New activity will appear here automatically", notif_mark_all: "Mark all read", notif_load_more: "Show more", notif_loading: "Loading notifications…", notif_error: "Couldn't load notifications", notif_error_s: "Check your connection and try again", notif_retry: "Try again", notif_filter_all: "All", notif_filter_pair: "Pair", notif_filter_films: "Films", notif_filter_system: "System", notif_now: "Just now", notif_min_ago: (n) => `${n}m ago`, notif_hour_ago: (n) => `${n}h ago`, notif_day_ago: (n) => `${n}d ago`, notif_inapp: "In app", notif_telegram: "In Telegram", notif_telegram_hint: "Pair events from the Addict Film bot", notif_telegram_unavailable: "The bot is unavailable right now", notif_browser: "In browser", notif_browser_hint: "Local reminders on this device",
    back: "Back", settings_title: "Settings", settings_loading: "Loading settings…",
    settings_notifications: "Notifications", settings_notifications_hint: "Important pair events are always shown in the app", settings_notifications_on: "On", settings_notifications_off: "Off", settings_notifications_permission: "Permission needed", settings_notifications_denied: "Notifications are blocked in Telegram or your browser", settings_notifications_unavailable: "Unavailable on this device", settings_notifications_error: "Couldn't request permission",
    settings_language: "Language", settings_language_hint: "Applies immediately across the app", settings_language_ru: "Русский", settings_language_en: "English",
    settings_pair: "Partner", settings_pair_none: "Create a pair to watch and rate films together", settings_pair_create: "Create a pair", settings_pair_current: "Your pair", settings_pair_manage: "Manage pair", settings_pair_invited: "Invite is waiting to be accepted", settings_pair_load_error: "Couldn't load pair status", settings_pair_try_again: "Try again",
    collections_empty_s: "Check back later", collections_empty_admin_s: "Create your first collection",
    collections_title_ph: "Collection name", collections_create_btn: "Create",
    admin_section: "Administration", admin_mode_row: "Admin mode",
    admin_mode_hint: "Edit collections and app blocks directly in the interface.",
    admin_mode_active: "Admin mode", admin_exit_mode: "Exit",
    admin_permission_revoked: "Admin rights were revoked. Mode disabled.",
    admin_conflict: "Another administrator changed this collection. Reopen it.",
    admin_status_draft: "Draft", admin_status_published: "Published",
    admin_status_archived: "Archived",
    admin_publish: "Publish", admin_unpublish: "Unpublish",
    admin_archive: "Archive", admin_restore: "Restore to drafts",
    admin_delete_forever: "Delete forever", admin_save: "Save",
    admin_title_label: "Title", admin_description_label: "Description",
    admin_description_ph: "A short note about the collection (optional)",
    admin_move_up: "Up", admin_move_down: "Down", admin_remove: "Remove",
    admin_move_left: "Move left", admin_move_right: "Move right", admin_edit: "Edit",
    admin_empty_publish: "An empty collection cannot be published",
    admin_published_delete: "Unpublish the collection first",
    admin_saved: "Saved", admin_drafts_hidden: "Draft is visible to admins only",
    admin_archived_hidden: "Archived items are hidden from users",
    admin_reorder_hint: "Use arrows to reorder films",
    admin_display_label: "Presentation format",
    admin_display_standard: "Standard", admin_display_standard_hint: "Compact card in the “Collections” rail",
    admin_display_featured: "Large", admin_display_featured_hint: "Large editorial block on the home screen",
    admin_backdrop_label: "Background image",
    admin_backdrop_from_film: "From a film in the collection", admin_backdrop_url: "Link (https)",
    admin_backdrop_none: "No image yet — add a film with a still or paste a link",
    admin_preview_label: "Preview",
    admin_create_featured: "Create a large collection",
    admin_featured_image_required: "A large collection needs an image",
    admin_url_not_allowed: "Only https links are allowed",
    coll_eyebrow: "Collection",
    admin_save_draft: "Save draft",
    admin_new_collection: "New collection", admin_new_featured: "New large collection",
    admin_unsaved: "Not saved", admin_new_badge: "New",
    admin_preview_title_ph: "Collection title",
    admin_no_films: "No films yet — add the first one",
    admin_err_title: "Enter a collection title",
    admin_err_films: "Add at least one film",
    admin_unsaved_changes: "Changes are not saved. Leave without saving?",
    coll_confirm_add: (title) => `Add "${title}" to the collection?`, coll_already_in: "Already in this collection",
    coll_remove_confirm: (title) => `Remove "${title}" from the collection?`, coll_add_film_btn: "+ Add film",
    coll_edit_hint: "Tap a film to remove it from the collection",
    coll_delete_btn: "Delete collection", coll_delete_confirm: (title) => `Delete collection "${title}"? Films stay in the catalog.`,
    tab_home: "Home", tab_want: "Wishlist", tab_watched: "Watched", tab_pick: "Pick", tab_stats: "Stats",
    list_want: "Wishlist", list_watched: "Watched",
    sort_title: "Sort", sort_best: "Best rated", sort_new: "Newest first", sort_old: "Oldest first", sort_worst: "Worst rated",
    count_films: (n) => (n === 1 ? "film" : "films"),
    rail_empty: "Empty — add films via search", rail_err: "Couldn't load",
    genres_empty: "Catalog is empty yet",
    genre_empty_t: "Empty", genre_empty_s: "No films in this genre yet", load_err: "Loading error",
    want_empty_t: "List is empty", want_empty_s: "Add films via search",
    watched_empty_t: "Nothing watched yet", watched_empty_s: "Mark films as Watched",
    load_more: "Show more", loading: "Loading…", retry: "Retry",
    my_rating: "My rating", rate_hint: " · tap = Watched", dir: "Director ",
    act_want: "Want to watch", act_watched: "Mark as watched", act_to_want: "To wishlist", act_remove: "Remove from list",
    already_watched_link: "Already seen it? Mark watched",
    my_review: "My review", comment_ph: "Write a review…",
    cast_title: "Cast", share_text: (title) => `Watch "${title}" on Addict Film`,
    confirm_remove: (title) => `Remove "${title}" from your list?`,
    search_start_t: "What are we watching?", search_start_s: "Type a title — at least 2 letters",
    search_toomany_t: "Too many requests", search_toomany_s: "Wait a minute and try again",
    search_err_t: "Search error",
    search_limited_t: "Search temporarily limited", search_limited_s: "Daily source limit. Try later",
    search_none_t: "Nothing found", search_none_s: "Try a year or the English title",
    confirm_add: (title) => `Add "${title}" to your wishlist?`, already_in_list: "Already in your list!",
    stats_title: "My movie profile", my_stats: "My stats", stats_empty_t: "No stats yet", stats_empty_s: "Add films and rate them", calc: "Calculating…",
    stats_profile_fallback: "Movie fan", stats_profile_sub: "Your story in movies", stats_more: "Show more", stats_less: "Show less", stats_view_all: "View all",
    stats_me_tab: "Me", stats_together_tab: "Together", stats_taste: "Your taste", stats_together: "Your story",
    stats_ratings_hint: "How many films you gave each rating", stats_ratings_hint_pair: "How many shared films received each rating", stats_genres_hint: "Genre share among watched films",
    stats_people_hint: "Most frequent in watched films", stats_directors_hint: "Most frequent among watched films", stats_people_hint_pair: "Most frequent in shared films", stats_directors_hint_pair: "Most frequent among shared films", stats_films: (n) => `${n} ${n === 1 ? "film" : "films"}`,
    stats_favorite_actor: "Favorite actor", stats_favorite_director: "Favorite director", stats_actors_empty: "Not enough actor data", stats_directors_empty: "Not enough director data", stats_people_empty_hint: "Statistics will appear after you watch films.", stats_person_films: (name) => `Films with ${name}`, stats_person_films_pair: (name) => `Shared films with ${name}`, stats_person_open: (name) => `Open films with ${name}`, stats_person_empty: "No watched films found yet",
    stats_taste_hint: (genre, rating) => genre ? `You lean toward ${genre} and most often give ${rating}.` : `You most often give ${rating}.`,
    tile_watched: "watched", tile_want: "wishlist", tile_shared_watched: "watched together", tile_shared_want: "shared wishlist", tile_avg: "average", tile_hours: "hours",
    chart_ratings: "How you rate movies", chart_ratings_pair: "Shared ratings", chart_genres: "Genres", chart_actors: "Actors", chart_directors: "Directors",
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
  const map = { home: "tab_home", want: "tab_want", watched: "tab_watched", pick: "tab_pick", stats: "tab_stats" };
  document.querySelectorAll("#tabbar .tab").forEach(b => { const s = b.querySelector("span"); if (s) s.textContent = t(map[b.dataset.tab]); });
}

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const canCache = cacheableRead(path, opts);
  const cached = canCache && _readCache.get(path);
  if (cached && cached.expiresAt > Date.now()) return cached.value;
  const res = await fetch(path, {
    ...opts,
    // Вне Telegram tg === null: без ?. падал сам вызов api(), и экран
    // «нужен Telegram» не успевал отрисоваться — вместо него было исключение.
    headers: { "Content-Type": "application/json", "X-Init-Data": tg?.initData || "", ...(opts.headers || {}) },
  });
  if (!res.ok) {
    // detail бывает строкой (обычные ошибки) и объектом {code, message} —
    // сохраняем и статус, и код, чтобы вызывающий различал 403/409 надёжно.
    const detail = (await res.json().catch(() => ({}))).detail;
    const error = new Error(typeof detail === "string" ? detail : (detail?.message || String(res.status)));
    error.status = res.status;
    error.code = detail && typeof detail === "object" ? detail.code : null;
    throw error;
  }
  const value = await res.json();
  if (canCache) _readCache.set(path, { value, expiresAt: Date.now() + _READ_CACHE_TTL });
  if (method !== "GET") _readCache.clear();
  return value;
}

// ── Утилиты ───────────────────────────────────────────────────────────────────
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
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
  "assets.fanart.tv",
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
  // Кадр не загрузился — переключаем ТОЛЬКО слой изображения на постер. Просить
  // у сервера другой фильм из-за битой картинки нельзя: человек уже увидел этот,
  // и повторный запрос записал бы второй показ.
  else if (img.hasAttribute("data-single-pick-media")) degradeSinglePickMedia(img);
  else if (img.hasAttribute("data-img-retry")) window.__imgRetry(img);
  else if (img.hasAttribute("data-img-remove-on-error")) img.remove();
}, true);

function ratingOf(m) {
  const r = m.imdb_rating || m.kp_rating;
  if (r && !isNaN(+r)) return (+r).toFixed(1);
  if (m.community && m.community.count) return m.community.avg;
  return null;
}
function skeletonRail(n = 5) { return Array.from({ length: n }, () => `<div class="poster"><div class="art sk"></div><div class="sk sk-line"></div></div>`).join(""); }
function skeletonGrid(n = 6) { return `<div class="grid">${Array.from({ length: n }, () => `<div class="poster"><div class="art sk"></div><div class="sk sk-line"></div></div>`).join("")}</div>`; }
// Эмодзи в пустых состояниях → нейтральные outline-иконки из общего реестра.
// Call-site'ы оставляем как есть (они передают эмодзи-ключ) — маппинг живёт здесь.
const EMPTY_STATE_ICONS = {
  "⏳": "clock", "⚠️": "alert", "⛔": "ban", "✅": "checkCircle",
  "🎬": "clapper", "🎭": "masks", "💙": "heart", "💬": "message",
  "📊": "chart", "🔍": "search", "🔖": "bookmark", "🤷": "help",
};
function emptyState(icon, text, sub = "") {
  const glyph = EMPTY_STATE_ICONS[icon] ? appIcon(EMPTY_STATE_ICONS[icon]) : icon;
  return `<div class="empty"><div class="empty-icon">${glyph}</div><div class="empty-text">${esc(text)}</div>${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}</div>`;
}

function posterTile(m, { onClick, badge } = {}) {
  const card = document.createElement("div");
  card.className = "poster";
  card.dataset.filmId = m.id;  // для точечного обновления карточки при возврате из фильма
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
function openDetail(id, back, preview = null) {
  if (back) _returnTo = back;
  captureScreenSnapshot();
  showDetail(id, preview);
}

// Снимаем текущий экран целиком (реальные узлы, а не innerHTML — обработчики и
// догруженные страницы сохраняются) вместе с позицией скролла.
function captureScreenSnapshot() {
  const scrollY = window.scrollY;  // ДО открепления: иначе высота схлопнется и scrollY станет 0
  const frag = document.createDocumentFragment();
  frag.append(...screen.childNodes);
  _navStack.push({ frag, scrollY, returnTo: _returnTo, empty: !frag.childNodes.length });
}

// Сброс стека при смене верхнего экрана (таб/дип-линк): иначе «назад» из фильма
// восстановил бы уже неактуальный экран.
function resetNavStack() { _navStack.length = 0; }

function returnFromDetail() {
  const snap = _navStack.pop();
  const film = _detailFilm;
  const changed = film && _detailBaseline &&
    (film.status !== _detailBaseline.status || film.my_rating !== _detailBaseline.my_rating);
  _detailFilm = null; _detailBaseline = null;
  if (!snap || snap.empty) { ((snap && snap.returnTo) || _returnTo)(); return; }
  _returnTo = snap.returnTo;
  unwireDetailScroll();
  screen.replaceChildren(snap.frag);
  // A fullscreen recommendation temporarily yields to the regular detail
  // screen.  Restore its presentation mode together with the preserved DOM
  // snapshot when the user comes back.
  if (screen.querySelector(".single-pick-screen")) singlePickMode(true);
  if (changed) reconcileFilmCard(film);
  // Форсируем layout реаттаченных узлов: иначе scrollTo клампится по ещё не
  // посчитанной (неполной) высоте документа и позиция теряется.
  void document.documentElement.scrollHeight;
  window.scrollTo(0, snap.scrollY);
  // rAF-повтор — страховка для iOS WebView, восстанавливающего скролл на след. кадре.
  requestAnimationFrame(() => window.scrollTo(0, snap.scrollY));
}

// Точечно обновляем карточку изменённого фильма в восстановленном списке, не
// перезагружая экран. Статус-фильтрованные списки («Хочу»/«Смотрел») помечены
// data-status-filter; каталог/поиск показывают общий рейтинг — их не трогаем.
function reconcileFilmCard(m) {
  screen.querySelectorAll(`.poster[data-film-id="${m.id}"]`).forEach(card => {
    const list = card.closest("[data-status-filter]");
    if (!list) return;
    if (m.status !== list.dataset.statusFilter) { card.remove(); return; }
    const row = card.querySelector(".meta-row");
    if (!row) return;
    const pill = row.querySelector(".rate-pill");
    if (m.my_rating) {
      const html = `<span class="rate-pill"><span class="s">★</span>${esc(String(m.my_rating))}</span>`;
      if (pill) pill.outerHTML = html; else row.insertAdjacentHTML("beforeend", html);
    } else if (pill) {
      pill.remove();
    }
  });
}

// ── Главная ───────────────────────────────────────────────────────────────────
// Единый набор line-иконок для категорийных чипов (в стиле нижней навигации),
// вместо разнородных эмодзи. Жанр-пилюли — чистый текст (см. genrePill).
// ── Единый реестр outline-иконок приложения ──────────────────────────────────
// Один стиль на весь продукт: 24×24, fill:none, stroke:currentColor 1.7,
// round caps/joins. Тела path'ов живут ТОЛЬКО здесь; рендер — appIcon().
// Цвет задаёт контекст через currentColor (нейтральный по умолчанию,
// фиолетовый — только активные состояния).
const ICONS = {
  flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 17a2.5 2.5 0 0 0 2.5-2.5c0-1.4-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5Z"/>',
  trophy: '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6M18 9h1.5a2.5 2.5 0 0 0 0-5H18M4 22h16M10 14.7V17c0 .6-.5 1-1 1.2C7.9 18.8 7 20.2 7 22M14 14.7V17c0 .6.5 1 1 1.2 1.1.5 2 2 2 3.8M18 2H6v7a6 6 0 0 0 12 0Z"/>',
  grid: '<rect width="7" height="7" x="3" y="3" rx="1.5"/><rect width="7" height="7" x="14" y="3" rx="1.5"/><rect width="7" height="7" x="14" y="14" rx="1.5"/><rect width="7" height="7" x="3" y="14" rx="1.5"/>',
  layers: '<path d="m12.8 2.2a2 2 0 0 0-1.6 0L2.6 6.1a1 1 0 0 0 0 1.8l8.6 3.9a2 2 0 0 0 1.6 0l8.6-3.9a1 1 0 0 0 0-1.8Z"/><path d="m22 17.6-9.2 4.2a2 2 0 0 1-1.6 0L2 17.6M22 12.6l-9.2 4.2a2 2 0 0 1-1.6 0L2 12.6"/>',
  shuffle: '<path d="M16 3h5v5"/><path d="M4 20 21 3"/><path d="M21 16v5h-5"/><path d="m15 15 6 6"/><path d="m4 4 5 5"/>',
  sliders: '<line x1="4" x2="14" y1="6" y2="6"/><line x1="18" x2="20" y1="6" y2="6"/><line x1="4" x2="8" y1="12" y2="12"/><line x1="12" x2="20" y1="12" y2="12"/><line x1="4" x2="14" y1="18" y2="18"/><line x1="18" x2="20" y1="18" y2="18"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="16" cy="18" r="2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  settings: '<circle cx="12" cy="12" r="3.1"/><path d="M19.1 14.4a1.6 1.6 0 0 0 .32 1.77l.06.06a1.9 1.9 0 1 1-2.69 2.69l-.06-.06a1.6 1.6 0 0 0-1.77-.32 1.6 1.6 0 0 0-.97 1.47v.17a1.9 1.9 0 1 1-3.8 0v-.09a1.6 1.6 0 0 0-1.05-1.47 1.6 1.6 0 0 0-1.77.32l-.06.06a1.9 1.9 0 1 1-2.69-2.69l.06-.06a1.6 1.6 0 0 0 .32-1.77 1.6 1.6 0 0 0-1.47-.97H3.4a1.9 1.9 0 1 1 0-3.8h.09a1.6 1.6 0 0 0 1.47-1.05 1.6 1.6 0 0 0-.32-1.77l-.06-.06a1.9 1.9 0 1 1 2.69-2.69l.06.06a1.6 1.6 0 0 0 1.77.32h.08a1.6 1.6 0 0 0 .97-1.47V3.4a1.9 1.9 0 1 1 3.8 0v.09a1.6 1.6 0 0 0 .97 1.47 1.6 1.6 0 0 0 1.77-.32l.06-.06a1.9 1.9 0 1 1 2.69 2.69l-.06.06a1.6 1.6 0 0 0-.32 1.77v.08a1.6 1.6 0 0 0 1.47.97h.17a1.9 1.9 0 1 1 0 3.8h-.09a1.6 1.6 0 0 0-1.47.97Z"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
  star: '<path d="m12 3.6 2.6 5.3 5.9.9-4.2 4.1 1 5.8L12 17l-5.3 2.7 1-5.8-4.2-4.1 5.9-.9z"/>',
  alert: '<path d="m10.3 3.9-8 13.8a2 2 0 0 0 1.7 3h16a2 2 0 0 0 1.7-3l-8-13.8a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8.3 12.2 2.5 2.5 4.9-5.4"/>',
  clapper: '<path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.4Z"/><path d="m6.2 5.3 3.1 3.9M12.4 3.4l3.1 4M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  masks: '<path d="M4.5 7.5 10 5l4 2.5v9L9.5 19 4.5 16.5zM14 7.5 19.5 5l.5 11.5-5 2.5z"/><path d="M7 11h.01M11 11h.01M7 14c1.1 1 2.9 1 4 0M16.5 11h.01M18.5 11h.01M16 14c.7.6 1.6.8 2.4.5"/>',
  heart: '<path d="M12 20s-7-4.4-9.2-8.6C1.3 8.3 2.6 5 5.8 5 8 5 9.3 6.5 12 9c2.7-2.5 4-4 6.2-4 3.2 0 4.5 3.3 3 6.4C19 15.6 12 20 12 20Z"/>',
  message: '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
  chart: '<path d="M5 20v-6M12 20V4M19 20v-9"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/>',
  bookmark: '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2Z"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.3 9a2.8 2.8 0 0 1 5.4 1c0 1.8-2.7 2.2-2.7 3.5M12 17h.01"/>',
  arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  arrowDown: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  arrowLeft: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
  arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  pencil: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  trash: '<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M10 11v6M14 11v6"/>',
};
const appIcon = (name, { label = null } = {}) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" ${label ? `role="img" aria-label="${esc(label)}"` : 'aria-hidden="true"'}>${ICONS[name] || ""}</svg>`;
// Тонкие обёртки для существующих call-site'ов (чипы каталога и экран «Подбор»).
const CHIP_ICONS = { pop: appIcon("flame"), top: appIcon("trophy"), gen: appIcon("grid"), coll: appIcon("layers") };
const PICK_ICONS = { shuffle: appIcon("shuffle"), sliders: appIcon("sliders"),
  heart: appIcon("heart"), check: appIcon("checkCircle") };
async function showHome() {
  // Invalidate slower async screens (notably Statistics) when a user quickly
  // switches tabs before their requests have finished.
  ++_viewGeneration;
  unwireDetailScroll();
  window.scrollTo(0, 0);
  const seeAll = (id) => `<button class="see-all" id="${id}">${esc(t("see_all"))}<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg></button>`;
  screen.innerHTML = `
    <header class="app-head rise d1">
      <div class="brand"><h1>${esc(homeGreeting())}</h1><p>${esc(t("tagline"))}</p></div>
      <div class="head-actions">
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
    <div class="chips rise d2" aria-label="${esc(t("chip_nav"))}">
      <button class="chip active" type="button" data-to="sec-pop"><span class="e" data-chip-icon="pop" aria-hidden="true">${CHIP_ICONS.pop}</span><span class="chip-label">${esc(t("chip_popular"))}</span></button>
      <button class="chip" type="button" data-to="sec-coll"><span class="e" data-chip-icon="coll" aria-hidden="true">${CHIP_ICONS.coll}</span><span class="chip-label">${esc(t("chip_collections"))}</span></button>
      <button class="chip" type="button" data-to="sec-gen"><span class="e" data-chip-icon="gen" aria-hidden="true">${CHIP_ICONS.gen}</span><span class="chip-label">${esc(t("chip_genres"))}</span></button>
      <button class="chip" type="button" data-to="sec-top"><span class="e" data-chip-icon="top" aria-hidden="true">${CHIP_ICONS.top}</span><span class="chip-label">${esc(t("chip_top"))}</span></button>
    </div>
    <section class="rise d3" id="sec-pop"><div class="head"><h2>${esc(t("chip_popular"))}</h2>${seeAll("see-pop")}</div><div class="rail" id="rail-pop">${skeletonRail(5)}</div></section>
    <section class="rise d4" id="sec-coll"><div class="head"><h2>${esc(t("chip_collections"))}</h2>${canEditCollections() ? `<button class="icon-add" id="coll-add-home" aria-label="+"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>` : ""}</div><div class="rail" id="rail-coll">${skeletonRail(5)}</div></section>
    <section class="rise d5" id="sec-gen"><div class="head"><h2>${esc(t("chip_genres"))}</h2>${seeAll("see-gen")}</div><div class="gchips" id="gen-chips"></div></section>
    ${recoCardHTML()}
    <section class="rise d5" id="sec-top"><div class="head"><h2>${esc(t("chip_top"))}</h2>${seeAll("see-top")}</div><div class="rail" id="rail-top">${skeletonRail(5)}</div></section>`;

  document.getElementById("bell-btn").onclick = () => showNotifications();
  document.getElementById("home-search").onclick = () => showSearch();
  document.getElementById("home-filter").onclick = () => showSearch();
  document.getElementById("see-pop").onclick = () => showBrowseAll("popular", t("chip_popular"));
  document.getElementById("see-top").onclick = () => showBrowseAll("top", t("chip_top"));
  document.getElementById("see-gen").onclick = () => showAllGenres();
  document.getElementById("reco-start").onclick = () => { setActiveTab("pick"); showPicker(); };
  screen.querySelectorAll(".chips .chip[data-to]").forEach(c => c.onclick = () => {
    screen.querySelectorAll(".chips .chip[data-to]").forEach(x => x.classList.toggle("active", x === c));
    document.getElementById(c.dataset.to)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  if (canEditCollections()) document.getElementById("coll-add-home").onclick = () => {
    // Никакой промежуточной формы и никакой пустой записи в БД: сразу открываем
    // полный редактор с локальной черновой сессией.
    CollectionEditor.createNew("standard");
    showCollectionEditor();
  };

  loadRail("rail-pop", "/api/browse?sort=popular&limit=20", { onItems: fillRecoArts });
  loadRail("rail-top", "/api/browse?sort=top&limit=20");
  loadGenrePills();
  loadCollectionsRail();
  refreshNotificationBadge();
}

async function refreshNotificationBadge() {
  // Home navigation, Telegram activation and the visibility listener may refresh
  // at the same time. Order the responses so a slower stale response cannot
  // remove a dot that a newer completed response just added.
  const generation = ++_notificationRefreshGeneration;
  try {
    const { unread_count } = await api("/api/notifications?limit=1");
    // Do not let a late stale response overwrite a response that was already
    // applied, but also do not block a valid response merely because a newer
    // request is still in flight (or has failed).
    if (generation < _notificationAppliedGeneration) return;
    _notificationAppliedGeneration = generation;
    _notificationUnread = Number(unread_count) || 0;
    refreshNotificationBadgeVisualOnly();
  } catch (_) { /* The home screen remains usable if the inbox is offline. */ }
}

function refreshNotificationBadgeVisualOnly() {
  const bell = document.getElementById("bell-btn");
  if (!bell) return;
  let dot = bell.querySelector(".dot");
  if (_notificationUnread && !dot) {
    dot = document.createElement("span");
    dot.className = "dot";
    dot.setAttribute("aria-hidden", "true");
    bell.appendChild(dot);
  }
  if (!_notificationUnread && dot) dot.remove();
}

function bindNotificationRefresh() {
  if (_notificationRefreshBound) return;
  _notificationRefreshBound = true;

  const refreshWhenVisible = () => {
    if (document.visibilityState === "visible") refreshNotificationBadge();
  };
  document.addEventListener("visibilitychange", refreshWhenVisible);
  // Telegram's `activated` event can arrive before WebKit updates
  // document.visibilityState, so it must not be gated by the browser state.
  try { tg?.onEvent?.("activated", refreshNotificationBadge); } catch (_) {}
  if (_notificationPollTimer === null) {
    _notificationPollTimer = window.setInterval(refreshWhenVisible, 30_000);
  }
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

// ── Adaptive recommendations ────────────────────────────────────────────────
// The quiz itself remains a thin client: the server owns the graph, branching,
// scoring and validation.  That keeps an outdated Telegram cache from being
// able to submit a hidden tag or change how a film is selected.
function pickerMode(active) {
  document.body.classList.toggle("picker-quiz-active", Boolean(active));
  const bar = document.getElementById("tabbar");
  if (bar) bar.setAttribute("aria-hidden", active ? "true" : "false");
}

function pickerHeader(back) {
  return `<header class="sub-head picker-head">${backBtn()}<h1>${esc(t("pick_title"))}</h1></header>`;
}

// Версия движка приходит ТОЛЬКО от сервера и только проверенному администратору.
// Выводить её из роли, Telegram-id или локального состояния нельзя: тогда значок
// стал бы подделываемым признаком, а не отражением того, чем реально считается
// подбор.
let _quizEngineMeta = null;

function rememberQuizEngine(data) {
  if (!data || typeof data !== "object") return;
  _quizEngineMeta = {
    version: typeof data.engine_version === "string" ? data.engine_version : null,
    preview: data.engine_preview === true,
  };
}

function quizPreviewBadge(data = null) {
  if (data) rememberQuizEngine(data);
  if (!_quizEngineMeta?.preview) return "";
  return `<span class="picker-preview-badge" aria-label="${esc(t("pick_v2_preview_hint"))}">${esc(t("pick_v2_preview"))}</span>`;
}

function pickerResultsHeader() {
  return `<header class="sub-head picker-head">${backBtn()}<div class="picker-title-meta"><h1>${esc(t("pick_title"))}</h1>${quizPreviewBadge()}</div></header>`;
}

function pickerError(message) {
  return `<div class="picker-error" role="alert">${esc(message || t("load_err"))}</div>`;
}

async function showPicker() {
  // Уходя из опроса совсем, забываем версию: иначе значок предпросмотра мог бы
  // остаться на следующем, уже обычном проходе.
  _quizEngineMeta = null;
  resetWishlistPrefetch();
  unwireDetailScroll();
  singlePickMode(false);
  pickerMode(false);
  window.scrollTo(0, 0);
  screen.innerHTML = `${pickerHeader()}<main class="picker-landing rise d1">
    <p class="picker-lead">${esc(t("pick_sub"))}</p>
    <button class="picker-mode-card" id="pick-wishlist" type="button">
      <span class="picker-mode-icon" aria-hidden="true">${PICK_ICONS.shuffle}</span>
      <span><b>${esc(t("pick_wishlist_title"))}</b><small>${esc(t("pick_wishlist_sub"))}</small></span><i aria-hidden="true">›</i>
    </button>
    <button class="picker-mode-card" id="pick-random" type="button">
      <span class="picker-mode-icon" aria-hidden="true">${PICK_ICONS.shuffle}</span>
      <span><b>${esc(t("pick_random_title"))}</b><small>${esc(t("pick_random_sub"))}</small></span><i aria-hidden="true">›</i>
    </button>
    <button class="picker-mode-card" id="pick-quiz" type="button">
      <span class="picker-mode-icon" aria-hidden="true">${PICK_ICONS.sliders}</span>
      <span><b>${esc(t("pick_quiz_title"))}</b><small>${esc(t("pick_quiz_sub"))}</small></span><i aria-hidden="true">›</i>
    </button>
  </main>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  document.getElementById("pick-wishlist").onclick = () => showWishlistRandom();
  document.getElementById("pick-random").onclick = () => showRandomRecommendation();
  document.getElementById("pick-quiz").onclick = () => startRecommendationQuiz();
}

// Причины приходят кодами и переводятся здесь. Сырые внутренние теги в интерфейс
// не попадают: незнакомый код просто пропускается, а не печатается как есть.
function recommendationReasons(item) {
  const codes = Array.isArray(item.reasons) ? item.reasons : [];
  const phrases = codes.map(code => t(`reason_${code}`)).filter(text => text && !text.startsWith("reason_"));
  return phrases.length ? phrases.join(" · ") : "";
}

function recommendationMovieCard(item, { sessionId = null, onAnother = null } = {}) {
  const poster = item.poster_url ? `<img src="${posterSrc(item.poster_url, true)}" alt="${esc(item.title)}" loading="eager" decoding="async" data-img-retry>` : `<span class="picker-poster-fallback">${esc((item.title || "?").slice(0, 1))}</span>`;
  const years = [item.year, item.runtime].filter(Boolean).join(" · ");
  const genres = String(item.genres || "").split(",").slice(0, 3).join(" · ");
  return `<article class="recommendation-film" data-recommendation-film="${item.id}">
    <button class="recommendation-poster" type="button" data-pick-open>${poster}</button>
    <div class="recommendation-film-copy"><h2>${esc(item.title || "—")}</h2>
      ${item.title_original ? `<p class="recommendation-original">${esc(item.title_original)}</p>` : ""}
      ${years ? `<p class="recommendation-meta">${esc(years)}</p>` : ""}${genres ? `<p class="recommendation-meta">${esc(genres)}</p>` : ""}
      ${item.rating ? `<span class="recommendation-rating">★ ${esc(item.rating)}</span>` : ""}
      <p class="recommendation-explanation">${esc(recommendationReasons(item))}</p>
    </div>
    <div class="recommendation-actions"><button type="button" class="picker-primary" data-pick-open>${esc(t("pick_open"))}</button><button type="button" class="picker-secondary" data-pick-want>${esc(t("pick_want"))}</button><button type="button" class="picker-secondary" data-pick-watched>${esc(t("pick_watched"))}</button>${onAnother ? `<button type="button" class="picker-text" data-pick-another>${esc(t("pick_another"))}</button>` : ""}<button type="button" class="picker-text danger" data-pick-reject>${esc(t("pick_not_suggest"))}</button></div>
  </article>`;
}

function wireRecommendationMovie(container, item, { mode, sessionId = null, role = null, onAnother = null, onReject = null, returnTo = showPicker } = {}) {
  const feedback = action => api(`/api/recommendations/${item.id}/feedback`, { method: "POST", body: JSON.stringify({ action, mode, session_id: sessionId, role: role || item.role, score: item.score }) }).catch(() => {});
  container.querySelectorAll("[data-pick-open]").forEach(button => button.onclick = () => {
    feedback("opened");
    singlePickMode(false);
    openDetail(item.id, returnTo, item);
  });
  const want = container.querySelector("[data-pick-want]");
  if (want) want.onclick = async () => {
    want.disabled = true;
    try {
      await api(`/api/movie/${item.id}/status`, { method: "POST", body: JSON.stringify({ status: "want_to_watch" }) });
      await feedback("want");
      want.textContent = "✓";
      tg?.HapticFeedback?.notificationOccurred?.("success");
    } catch (error) { want.disabled = false; tg?.showAlert?.(String(error.message || t("load_err"))); }
  };
  const watched = container.querySelector("[data-pick-watched]");
  if (watched) watched.onclick = async () => {
    watched.disabled = true;
    try {
      await api(`/api/movie/${item.id}/status`, { method: "POST", body: JSON.stringify({ status: "watched" }) });
      await feedback("watched");
      // Reuse the existing detail screen so rating, comment and list state have
      // exactly one implementation across the product.
      singlePickMode(false);
      openDetail(item.id, returnTo, item);
    } catch (error) { watched.disabled = false; tg?.showAlert?.(String(error.message || t("load_err"))); }
  };
  const reject = container.querySelector("[data-pick-reject]");
  if (reject) reject.onclick = async () => {
    reject.disabled = true;
    await feedback("rejected");
    // «Не предлагать» — это про один фильм, а не про весь подбор: раньше отсюда
    // выбрасывало на стартовый экран, и человек терял пройденную анкету.
    if (onAnother) { onAnother(); return; }
    if (onReject) { await onReject(); return; }
    showPicker();
  };
  const another = container.querySelector("[data-pick-another]");
  if (another) another.onclick = () => {
    // Pure analytics must not sit in the critical path before the next card.
    // The current show has already been committed by the picker endpoint.
    feedback("another");
    onAnother?.();
  };
}

// ── Один фильм во весь экран ────────────────────────────────────────────────
// Экран ОДИН на оба сценария (рулетка и умный случайный) — различие только в
// подписи сверху. Дублировать разметку под каждый сценарий значило бы чинить
// потом каждую правку дважды.
//
// Режим изображения выбирает СЕРВЕР и присылает в hero_type. Клиент не смотрит
// на backdrop_url и не решает сам, широкая ли картинка: именно эта догадка и
// растягивала вертикальный постер на всю ширину.

// Текущая карточка — для замены слоя изображения, когда файл не загрузился.
let _singlePickItem = null;
let _preparedWishlistPick = null;
let _wishlistPreparePromise = null;
let _wishlistPrefetchGeneration = 0;

function resetWishlistPrefetch() {
  _wishlistPrefetchGeneration += 1;
  _preparedWishlistPick = null;
  _wishlistPreparePromise = null;
}

function preloadSinglePickMedia(item) {
  const hero = singlePickHero(item);
  if (!hero.url) return Promise.resolve(false);
  const image = new Image();
  image.decoding = "async";
  image.src = singlePickSrc(hero.url, hero.type);
  return new Promise(resolve => {
    let settled = false;
    const finish = ready => {
      if (settled) return;
      settled = true;
      image.onload = null;
      image.onerror = null;
      resolve(Boolean(ready));
    };
    image.onload = () => finish(true);
    image.onerror = () => finish(false);
    if (image.complete) finish(image.naturalWidth > 0);
    else if (typeof image.decode === "function") image.decode().then(
      () => finish(true), () => { /* onload/onerror is authoritative */ });
  });
}

function installPreparedWishlistPick(prepared, generation = _wishlistPrefetchGeneration) {
  if (!prepared?.item || !prepared?.token || generation !== _wishlistPrefetchGeneration) {
    return Promise.resolve(null);
  }
  const state = { ...prepared, imageReady: false, imageSettled: false };
  _preparedWishlistPick = state;
  const promise = preloadSinglePickMedia(state.item).then(ready => {
    state.imageReady = ready;
    state.imageSettled = true;
    return generation === _wishlistPrefetchGeneration ? state : null;
  });
  const tracked = promise.finally(() => {
    if (_wishlistPreparePromise === tracked) _wishlistPreparePromise = null;
  });
  _wishlistPreparePromise = tracked;
  return tracked;
}

function prepareNextWishlistPick() {
  if (_preparedWishlistPick) return _wishlistPreparePromise || Promise.resolve(_preparedWishlistPick);
  if (_wishlistPreparePromise) return _wishlistPreparePromise;
  const generation = _wishlistPrefetchGeneration;
  const request = api("/api/wishlist/random/prepare", { method: "POST", body: "{}" })
    .then(prepared => {
      if (generation !== _wishlistPrefetchGeneration) return null;
      _wishlistPreparePromise = null;
      return installPreparedWishlistPick(prepared, generation);
    })
    .catch(error => {
      if (generation === _wishlistPrefetchGeneration) _preparedWishlistPick = null;
      if (error?.status !== 404) console.warn("Wishlist prefetch failed", error);
      return null;
    });
  _wishlistPreparePromise = request;
  return request;
}

function setSinglePickRefreshState(active) {
  const currentCard = screen.querySelector(".single-pick-card")
    || screen.querySelector(".picker-result .recommendation-film");
  currentCard?.classList.toggle("is-refreshing", Boolean(active));
  screen.querySelectorAll("[data-pick-another],[data-pick-reject]").forEach(button => {
    button.disabled = Boolean(active);
    if (active) button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
  });
  return currentCard;
}

function singlePickMode(active) {
  const enabled = Boolean(active);
  document.body.classList.toggle("single-pick-open", enabled);

  const tabbar = document.getElementById("tabbar");
  if (tabbar) {
    tabbar.setAttribute("aria-hidden", enabled ? "true" : "false");
    tabbar.inert = enabled;
  }
}

function clampUnit(value, fallback) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(0, Math.min(1, parsed));
}
function percentFromUnit(value, fallback) {
  return `${(clampUnit(value, fallback) * 100).toFixed(2)}%`;
}
// Сохранённые 1920x1080 — это ДОКАЗАТЕЛЬСТВО пригодности кадра, а не размер для
// показа: блок занимает ~1050 физических пикселей даже на плотном экране.
// Замер по каталогу: 1280x720 весит 181 КБ против 375 КБ и доступен у всех
// проверенных кадров; если какой-то редакции не окажется, сработает обычная
// деградация к постеру.
const HERO_DISPLAY_RENDITION = /^(https:\/\/avatars\.mds\.yandex\.net\/get-ott\/.+)\/\d{3,4}x\d{3,4}$/;

function singlePickSrc(url, type) {
  if (type === "backdrop") return posterSrc(String(url).replace(HERO_DISPLAY_RENDITION, "$1/1280x720"), false);
  // Постер здесь во всю карточку (до 230 CSS px, то есть ~690 физических), а
  // small=true отдал бы 300x450 — источник вдвое меньше элемента. Тот же URL
  // берут и размытый фон, и ambient-подложка: они приходят из кэша браузера.
  return posterSrc(url, false);
}

function singlePickHero(item) {
  // Presence of hero_type means the server has made a display decision.  In
  // particular, an explicit null must not be bypassed with the raw poster URL.
  const serverDecided = item && Object.prototype.hasOwnProperty.call(item, "hero_type");
  const url = item?._posterRejected
    ? null
    : (serverDecided ? item?.hero_url : (item?.hero_url || item?.poster_url || null));
  const type = item?.hero_type === "backdrop" ? "backdrop" : "poster_blur";
  const fit = type === "backdrop" && item?.hero_fit === "cover" ? "cover" : "contain";
  return {
    url, type, fit,
    focusX: percentFromUnit(item?.hero_focus_x, 0.5),
    focusY: percentFromUnit(item?.hero_focus_y, 0.36),
  };
}

function singlePickArtControlsHTML(item, hero) {
  if (!AdminMode.isCapable()) return "";
  const opener = `<button class="single-pick-art-open" type="button" data-art-open
    aria-expanded="false">${esc(t("art_frame"))}</button>`;
  const flowControls = `<div class="single-pick-flow-controls">
    <b>${esc(t("art_movie_flow"))}</b>
    <div class="single-pick-art-segments">
      <button type="button" data-movie-flow-state="auto">${esc(t("art_movie_auto"))}</button>
      <button type="button" data-movie-flow-state="allow">${esc(t("art_movie_allow"))}</button>
      <button type="button" data-movie-flow-state="exclude">${esc(t("art_movie_exclude"))}</button>
    </div>
  </div>`;
  if (hero.type === "backdrop") {
    const x = clampUnit(item?.hero_focus_x, 0.5);
    const y = clampUnit(item?.hero_focus_y, 0.36);
    return `${opener}<section class="single-pick-art-panel" data-art-panel hidden>
      <div class="single-pick-art-head"><b>${esc(t("art_fit"))}</b>
        <button type="button" data-art-close aria-label="${esc(t("art_close"))}">×</button></div>
      <div class="single-pick-art-segments">
        <button type="button" data-art-fit="contain" aria-pressed="${hero.fit === "contain"}">${esc(t("art_contain"))}</button>
        <button type="button" data-art-fit="cover" aria-pressed="${hero.fit === "cover"}">${esc(t("art_cover"))}</button>
      </div>
      <label>${esc(t("art_focus_x"))}<input type="range" min="0" max="1" step=".01"
        value="${x}" data-art-focus-x ${hero.fit === "cover" ? "" : "disabled"}></label>
      <label>${esc(t("art_focus_y"))}<input type="range" min="0" max="1" step=".01"
        value="${y}" data-art-focus-y ${hero.fit === "cover" ? "" : "disabled"}></label>
      <div class="single-pick-art-actions">
        <button type="button" data-art-save>${esc(t("art_save"))}</button>
        <button type="button" data-art-reset>${esc(t("art_reset"))}</button>
      </div>
      ${flowControls}
    </section>`;
  }
  return `${opener}<section class="single-pick-art-panel single-pick-art-poster-panel"
      data-art-panel hidden>
    <div class="single-pick-art-head"><b>${esc(t("art_frame"))}</b>
      <button type="button" data-art-close aria-label="${esc(t("art_close"))}">×</button></div>
    <button type="button" data-poster-state="rejected">${esc(t("art_reject_poster"))}</button>
    <button type="button" data-poster-state="approved">${esc(t("art_approve_poster"))}</button>
    <button type="button" data-poster-state="auto">${esc(t("art_auto_poster"))}</button>
    ${flowControls}
  </section>`;
}

function singlePickMediaHTML(item) {
  const hero = singlePickHero(item);
  if (!hero.url) {
    return `<div class="single-pick-media single-pick-media-empty"><span>${esc(t("pick_image_unavailable"))}</span>
      ${singlePickArtControlsHTML(item, hero)}</div>`;
  }
  // Резкий постер и его размытая копия — ОДИН и тот же URL: браузер берёт вторую
  // отрисовку из кэша, сети на неё не тратится.
  const src = singlePickSrc(hero.url, hero.type);
  const title = esc(item.title || "");
  if (hero.type === "backdrop") {
    const style = `--hero-focus-x:${hero.focusX};--hero-focus-y:${hero.focusY}`;
    return `<div class="single-pick-media single-pick-media-backdrop"
      data-hero-fit="${hero.fit}" style="${style}">
      <img class="single-pick-backdrop-ambient" src="${src}" alt="" aria-hidden="true"
           loading="eager" decoding="async" data-img-remove-on-error>
      <div class="single-pick-backdrop-stage">
        <img class="single-pick-backdrop" src="${src}" alt="${esc(t("pick_backdrop_alt"))}: ${title}"
             loading="eager" decoding="async" data-single-pick-media>
      </div>
      <div class="single-pick-media-shade" aria-hidden="true"></div>
      ${singlePickArtControlsHTML(item, hero)}
    </div>`;
  }
  return `<div class="single-pick-media single-pick-media-poster">
    <img class="single-pick-poster-blur" src="${src}" alt="" aria-hidden="true"
         loading="eager" decoding="async" data-img-remove-on-error>
    <div class="single-pick-poster-shade" aria-hidden="true"></div>
    <img class="single-pick-poster" src="${src}" alt="${esc(t("pick_poster_alt"))}: ${title}"
         loading="eager" decoding="async" data-single-pick-media>
    ${singlePickArtControlsHTML(item, hero)}
  </div>`;
}

// Одна деградация и ни одной петли: кадр → постер → нейтральная заглушка.
function degradeSinglePickMedia(img) {
  const container = img.closest(".single-pick-media");
  if (!container) return;
  const item = _singlePickItem;
  const canFallBack = container.classList.contains("single-pick-media-backdrop")
    && item && !item._posterRejected && item.poster_url && item.poster_url !== item.hero_url;
  if (canFallBack) {
    Object.assign(item, {
      hero_url: item.poster_url, hero_type: "poster_blur",
      hero_fit: null, hero_focus_x: null, hero_focus_y: null,
    });
  } else if (item) {
    Object.assign(item, { hero_url: null, hero_type: null });
  }
  container.outerHTML = singlePickMediaHTML(item || {});
  wireSinglePickArtControls();
}

function replaceSinglePickMedia() {
  const media = screen.querySelector(".single-pick-media");
  if (!media || !_singlePickItem) return;
  media.outerHTML = singlePickMediaHTML(_singlePickItem);
  wireSinglePickArtControls();
}

function wireSinglePickArtControls() {
  const media = screen.querySelector(".single-pick-media");
  const item = _singlePickItem;
  if (!media || !item || !AdminMode.isCapable()) return;
  const open = media.querySelector("[data-art-open]");
  const panel = media.querySelector("[data-art-panel]");
  if (!open || !panel) return;
  const closePanel = () => {
    panel.hidden = true;
    open.setAttribute("aria-expanded", "false");
  };
  open.onclick = () => {
    panel.hidden = false;
    open.setAttribute("aria-expanded", "true");
  };
  panel.querySelector("[data-art-close]")?.addEventListener("click", closePanel);

  const fitButtons = [...panel.querySelectorAll("[data-art-fit]")];
  if (fitButtons.length) {
    const focusX = panel.querySelector("[data-art-focus-x]");
    const focusY = panel.querySelector("[data-art-focus-y]");
    let fit = item.hero_fit === "cover" ? "cover" : "contain";
    const preview = () => {
      media.dataset.heroFit = fit;
      media.style.setProperty("--hero-focus-x", percentFromUnit(focusX?.value, 0.5));
      media.style.setProperty("--hero-focus-y", percentFromUnit(focusY?.value, 0.36));
      fitButtons.forEach(button => button.setAttribute(
        "aria-pressed", String(button.dataset.artFit === fit)));
      if (focusX) focusX.disabled = fit !== "cover";
      if (focusY) focusY.disabled = fit !== "cover";
    };
    fitButtons.forEach(button => button.onclick = () => {
      fit = button.dataset.artFit === "cover" ? "cover" : "contain";
      preview();
    });
    focusX?.addEventListener("input", preview);
    focusY?.addEventListener("input", preview);
    panel.querySelector("[data-art-save]")?.addEventListener("click", async event => {
      event.currentTarget.disabled = true;
      const updated = await adminCall(`/api/admin/films/${item.id}/hero-presentation`, {
        method: "PATCH",
        body: JSON.stringify({
          fit,
          focus_x: clampUnit(focusX?.value, 0.5),
          focus_y: clampUnit(focusY?.value, 0.36),
        }),
      });
      if (updated && updated !== true) Object.assign(item, updated);
      replaceSinglePickMedia();
    });
    panel.querySelector("[data-art-reset]")?.addEventListener("click", async event => {
      event.currentTarget.disabled = true;
      const updated = await adminCall(`/api/admin/films/${item.id}/hero-presentation`, {
        method: "PATCH",
        body: JSON.stringify({ fit: "contain", focus_x: 0.5, focus_y: 0.36 }),
      });
      if (updated && updated !== true) Object.assign(item, updated);
      replaceSinglePickMedia();
    });
    preview();
  }

  panel.querySelectorAll("[data-poster-state]").forEach(button => {
    button.onclick = async () => {
      button.disabled = true;
      const state = button.dataset.posterState;
      const updated = await adminCall(`/api/admin/films/${item.id}/poster-display`, {
        method: "PATCH",
        body: JSON.stringify({
          state,
          reason: state === "rejected" ? "manual_admin_rejection" : null,
        }),
      });
      if (updated && updated !== true) {
        Object.assign(item, updated);
        item._posterRejected = state === "rejected";
      }
      replaceSinglePickMedia();
    };
  });
  panel.querySelectorAll("[data-movie-flow-state]").forEach(button => {
    button.setAttribute("aria-pressed", String(
      button.dataset.movieFlowState === (item.movie_flow_state || "auto")));
    button.onclick = async () => {
      const state = button.dataset.movieFlowState;
      panel.querySelectorAll("[data-movie-flow-state]").forEach(node => { node.disabled = true; });
      try {
        const updated = await adminCall(`/api/admin/films/${item.id}/movie-flow`, {
          method: "PATCH",
          body: JSON.stringify({
            state,
            reason: state === "exclude" ? "manual_admin_exclusion" : null,
          }),
        });
        if (updated && updated !== true) Object.assign(item, updated);
        panel.querySelectorAll("[data-movie-flow-state]").forEach(node => {
          node.setAttribute("aria-pressed", String(node.dataset.movieFlowState === state));
        });
      } finally {
        panel.querySelectorAll("[data-movie-flow-state]").forEach(node => { node.disabled = false; });
      }
    };
  });
}

// В рулетке по «Хочу» фильм УЖЕ в этом списке — кнопка «В „Хочу“» там не может
// ничего изменить. Показывать действие, которое заведомо ничего не делает, хуже,
// чем не показывать его вовсе: человек жмёт, видит галочку и не понимает, что
// произошло. Когда остаётся одно действие, сетка на две колонки не нужна.
function secondaryActionsHTML(allowWant) {
  const buttons = [
    allowWant ? `<button type="button" class="single-pick-secondary" data-pick-want>${PICK_ICONS.heart}<span>${esc(t("pick_want"))}</span></button>` : "",
    `<button type="button" class="single-pick-secondary" data-pick-watched>${PICK_ICONS.check}<span>${esc(t("pick_watched"))}</span></button>`,
  ].filter(Boolean);
  return buttons.length > 1
    ? `<div class="single-pick-action-grid">${buttons.join("")}</div>`
    : buttons.join("");
}

function singlePickScreenHTML(item, { label = "", allowAnother = true, allowWant = true } = {}) {
  const chips = [item.rating ? `<span class="single-pick-chip single-pick-rating">★ ${esc(item.rating)}</span>` : ""]
    .concat([item.year, item.runtime].filter(Boolean)
      .map(value => `<span class="single-pick-chip">${esc(value)}</span>`))
    .join("");
  const genres = String(item.genres || "").split(",").map(value => value.trim())
    .filter(Boolean).slice(0, 3).join(" · ");
  const reasons = recommendationReasons(item);
  return `<main class="single-pick-screen rise d1">
    <article class="single-pick-card" data-recommendation-film="${esc(item.id)}">
      <section class="single-pick-visual">
        ${singlePickMediaHTML(item)}
        <div class="single-pick-back-slot">${backBtn()}</div>
        <div class="single-pick-hero-content">
          ${label ? `<p class="single-pick-label">${esc(label)}</p>` : ""}
          <h1 class="single-pick-title">${esc(item.title || "—")}</h1>
          ${item.title_original ? `<p class="single-pick-original">${esc(item.title_original)}</p>` : ""}
          ${chips ? `<div class="single-pick-chips">${chips}</div>` : ""}
          ${genres ? `<p class="single-pick-genres">${esc(genres)}</p>` : ""}
          ${reasons ? `<p class="single-pick-reason">${esc(reasons)}</p>` : ""}
        </div>
      </section>
    </article>
    <section class="single-pick-actions">
      <button type="button" class="single-pick-primary" data-pick-open>${esc(t("pick_open"))}</button>
      ${secondaryActionsHTML(allowWant)}
      ${allowAnother ? `<button type="button" class="single-pick-alternate" data-pick-another>${PICK_ICONS.shuffle}<span>${esc(t("pick_another"))}</span></button>` : ""}
      <button type="button" class="single-pick-danger" data-pick-reject>${esc(t("pick_not_suggest"))}</button>
    </section>
  </main>`;
}

function singlePickStateHTML(message, { error = false, recover = false } = {}) {
  return `<main class="single-pick-screen single-pick-state-screen">
    <div class="single-pick-back-slot">${backBtn()}</div>
    <div class="single-pick-state-wrap">
      ${recover ? `<section class="single-pick-recovery-copy"${error ? ' role="alert"' : ""}>
        <h1>${esc(t("pick_recovery_title"))}</h1>
        <p>${esc(message)}</p>
      </section>` : `<div class="single-pick-state-copy${error ? " is-error" : ""}"${error ? ' role="alert"' : ""}>
        ${esc(message)}
      </div>`}
      ${recover ? `<div class="single-pick-recovery">
        <button type="button" class="single-pick-primary" data-pick-retry>${esc(t("pick_retry"))}</button>
        <button type="button" class="single-pick-secondary" data-pick-back>${esc(t("pick_back_to_picker"))}</button>
      </div>` : ""}
    </div>
  </main>`;
}

function wireSinglePickRecovery(retry) {
  screen.querySelector("[data-pick-retry]")?.addEventListener("click", retry);
  screen.querySelector("[data-pick-back]")?.addEventListener("click", showPicker);
  wireBack(showPicker);
}

// Обратная связь, открытие и «другой вариант» остаются общими: полноэкранный
// экран меняет ВИД, а не поведение.
function wireSinglePickScreen(item, { label, mode, onAnother }) {
  _singlePickItem = item;
  singlePickMode(true);
  screen.innerHTML = singlePickScreenHTML(item, {
    label, allowAnother: true, allowWant: mode !== "wishlist" });
  window.scrollTo(0, 0);      // новый фильм всегда показывается с начала экрана
  wireSinglePickArtControls();
  wireBack(showPicker);
  wireRecommendationMovie(screen.querySelector(".single-pick-screen"), item,
    { mode, onAnother, returnTo: showPicker });
}

function renderLegacyPick(label, item, onAnother, mode) {
  const box = screen.querySelector(".picker-result");
  box.innerHTML = `<div class="picker-result-label">${esc(label)}</div>${recommendationMovieCard(item, { onAnother })}`;
  wireRecommendationMovie(box, item, { mode, onAnother, returnTo: showPicker });
}

function renderWishlistPick(item, fullscreen) {
  if (fullscreen) {
    wireSinglePickScreen(item, {
      label: t("pick_wishlist_title"), mode: "wishlist",
      onAnother: showNextWishlistRandom,
    });
    return;
  }
  renderLegacyPick(t("pick_wishlist_title"), item, showNextWishlistRandom, "wishlist");
}

async function consumePreparedWishlistPick(prepared, { retryStale = true } = {}) {
  try {
    return await api("/api/wishlist/random/consume", {
      method: "POST", body: JSON.stringify({ token: prepared.token }),
    });
  } catch (error) {
    if (retryStale && (error?.status === 409 || error?.status === 410)) {
      _preparedWishlistPick = null;
      _wishlistPreparePromise = null;
      const fresh = await prepareNextWishlistPick();
      if (fresh) return consumePreparedWishlistPick(fresh, { retryStale: false });
    }
    throw error;
  }
}

async function showNextWishlistRandom() {
  const fullscreen = featureEnabled("fullscreen_single_pick");
  const currentCard = setSinglePickRefreshState(true);
  try {
    const prepared = _preparedWishlistPick || await prepareNextWishlistPick();
    if (!prepared) throw Object.assign(new Error(t("pick_wishlist_empty")), { status: 404 });
    // Detach before consume so no second tap can reuse the same signed claim.
    _preparedWishlistPick = null;
    _wishlistPreparePromise = null;
    const result = await consumePreparedWishlistPick(prepared);
    renderWishlistPick(result.item, fullscreen);
    installPreparedWishlistPick(result.next);
  } catch (error) {
    const message = error?.status === 404 ? t("pick_wishlist_empty") : (error?.message || t("load_err"));
    if (currentCard?.isConnected) {
      setSinglePickRefreshState(false);
      tg?.showAlert?.(String(message));
      return;
    }
    screen.innerHTML = singlePickStateHTML(message, { error: true, recover: true });
    wireSinglePickRecovery(showWishlistRandom);
  }
}

// Рулетка по СВОЕМУ списку «Хочу»: никакого рейтинга и настроения — человек уже
// выбрал эти фильмы сам. Первый показ коммитится обычным атомарным запросом;
// следующие карточки сервер выбирает и браузер прогревает заранее.
async function showWishlistRandom() {
  resetWishlistPrefetch();
  pickerMode(false);
  _singlePickItem = null;
  const fullscreen = featureEnabled("fullscreen_single_pick");
  singlePickMode(fullscreen);
  screen.innerHTML = fullscreen
    ? singlePickStateHTML(t("pick_loading"))
    : `${pickerHeader()}<main class="picker-result"><div class="picker-loading">${esc(t("pick_loading"))}</div></main>`;
  wireBack(showPicker);
  try {
    const { item, next } = await api("/api/wishlist/random", { method: "POST", body: "{}" });
    // Свой режим: сервер подтверждает показ по учёту рулетки, а не по
    // истории рекомендаций каталога.
    renderWishlistPick(item, fullscreen);
    installPreparedWishlistPick(next);
  } catch (error) {
    const message = error.status === 404 ? t("pick_wishlist_empty") : error.message;
    if (fullscreen) {
      screen.innerHTML = singlePickStateHTML(message, { error: true });
      wireBack(showPicker);
    } else {
      screen.querySelector(".picker-result").innerHTML = `${pickerError(message)}`;
    }
  }
}

const STRATEGY_LABELS = { reliable: "strategy_reliable", taste_match: "strategy_taste_match", discovery: "strategy_discovery", available: "strategy_available" };

async function showRandomRecommendation() {
  pickerMode(false);
  const fullscreen = featureEnabled("fullscreen_single_pick");
  const currentCard = fullscreen
    ? screen.querySelector(".single-pick-card")
    : screen.querySelector(".picker-result .recommendation-film");
  const preserveCurrent = Boolean(currentCard);
  singlePickMode(fullscreen);
  if (!preserveCurrent) {
    _singlePickItem = null;
    screen.innerHTML = fullscreen
      ? singlePickStateHTML(t("pick_loading"))
      : `${pickerHeader()}<main class="picker-result"><div class="picker-loading">${esc(t("pick_loading"))}</div></main>`;
    wireBack(showPicker);
  } else {
    currentCard.classList.add("is-refreshing");
    screen.querySelectorAll("[data-pick-another],[data-pick-reject]").forEach(button => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    });
  }
  try {
    const { item } = await api("/api/recommendations/random", { method: "POST", body: JSON.stringify({ language: lang, context: "solo" }) });
    // Стратегия называется честно: «надёжный выбор» и «находка» — разные обещания.
    const strategy = STRATEGY_LABELS[item.strategy] ? t(STRATEGY_LABELS[item.strategy]) : t("pick_random_title");
    if (fullscreen) {
      wireSinglePickScreen(item, { label: strategy, mode: "random", onAnother: showRandomRecommendation });
      return;
    }
    renderLegacyPick(strategy, item, showRandomRecommendation, "random");
  } catch (error) {
    const message = error.status === 404 ? t("pick_empty") : (error.message || t("load_err"));
    if (preserveCurrent) {
      currentCard.classList.remove("is-refreshing");
      screen.querySelectorAll("[data-pick-another],[data-pick-reject]").forEach(button => {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      });
      tg?.showAlert?.(String(message));
      return;
    }
    if (fullscreen) {
      screen.innerHTML = singlePickStateHTML(message, { error: true, recover: true });
      wireSinglePickRecovery(showRandomRecommendation);
    } else {
      screen.querySelector(".picker-result").innerHTML = `${pickerError(message)}
        <p class="picker-empty-copy">${esc(t("pick_empty_sub"))}</p>
        <button class="picker-restart" data-pick-retry type="button">${esc(t("pick_retry"))}</button>`;
      screen.querySelector("[data-pick-retry]")?.addEventListener("click", showRandomRecommendation);
    }
  }
}

async function startRecommendationQuiz() {
  pickerMode(true);
  screen.innerHTML = `<main class="picker-quiz"><div class="picker-loading">${esc(t("pick_loading"))}</div></main>`;
  try {
    const data = await api("/api/recommendations/quiz/start", { method: "POST", body: JSON.stringify({ language: lang }) });
    rememberQuizEngine(data);
    showQuizQuestion(data);
  } catch (error) { pickerMode(false); showPicker(); tg?.showAlert?.(String(error.message || t("load_err"))); }
}

function showQuizQuestion(data) {
  rememberQuizEngine(data);
  if (data.state === "complete" || !data.question) { showQuizResults(data.id); return; }
  const question = data.question;
  const progress = Math.max(1, Number(data.progress) + 1);
  screen.innerHTML = `<main class="picker-quiz rise d1"><header class="picker-quiz-head"><button class="back" id="picker-back" aria-label="${esc(t("pick_back"))}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button><div class="picker-quiz-head-meta"><span>${esc(t("pick_progress", progress, data.total || 8))}</span>${quizPreviewBadge(data)}</div></header><div class="picker-progress"><i style="width:${Math.min(100, progress / (data.total || 8) * 100)}%"></i></div><section class="picker-question"><h1>${esc(question.text)}</h1><div class="picker-options">${question.options.map(option => `<button type="button" class="picker-option" data-answer="${esc(option.id)}">${esc(option.label)}<i>›</i></button>`).join("")}</div></section></main>`;
  document.getElementById("picker-back").onclick = async () => {
    if (!data.progress) { pickerMode(false); showPicker(); return; }
    try {
      const previous = await api(`/api/recommendations/quiz/${encodeURIComponent(data.id)}/back`, { method: "POST", body: JSON.stringify({ language: lang }) });
      showQuizQuestion(previous);
    } catch (error) { tg?.showAlert?.(String(error.message || t("load_err"))); }
  };
  screen.querySelectorAll(".picker-option").forEach(button => button.onclick = async () => {
    if (button.disabled) return;
    screen.querySelectorAll(".picker-option").forEach(option => { option.disabled = true; });
    button.classList.add("selected");
    try {
      const next = await api(`/api/recommendations/quiz/${encodeURIComponent(data.id)}/answer`, { method: "POST", body: JSON.stringify({ language: lang, question_id: question.id, answer_id: button.dataset.answer }) });
      if (next.state === "complete") showQuizResults(next.id); else showQuizQuestion(next);
    } catch (error) {
      screen.querySelectorAll(".picker-option").forEach(option => { option.disabled = false; option.classList.remove("selected"); });
      tg?.showAlert?.(String(error.message || t("load_err")));
    }
  });
}

const QUIZ_ROLE_LABELS = { best: ["pick_best", "pick_best_sub"], reliable: ["pick_reliable", "pick_reliable_sub"], unexpected: ["pick_unexpected", "pick_unexpected_sub"] };

function quizResultsHTML(items) {
  if (!items.length) return `<section class="picker-empty-state">
    ${pickerError(t("pick_empty"))}<p class="picker-empty-copy">${esc(t("pick_empty_sub"))}</p>
    <div class="picker-empty-actions">
      <button class="picker-secondary" data-quiz-back type="button">${esc(t("pick_change_answers"))}</button>
      <button class="picker-secondary" data-quiz-restart type="button">${esc(t("pick_restart"))}</button>
      <button class="picker-primary" data-quiz-random type="button">${esc(t("pick_smart_random"))}</button>
    </div>
  </section>`;
  // Неполная тройка — не ошибка: под узкий запрос честнее показать меньше
  // вариантов, чем добрать случайными фильмами «чтобы было три».
  const note = items.length < 3 ? `<p class="picker-empty-copy picker-partial">${esc(t("pick_partial"))}</p>` : "";
  return items.map(item => {
    const [title, subtitle] = QUIZ_ROLE_LABELS[item.role] || QUIZ_ROLE_LABELS.best;
    return `<section class="picker-role" data-role="${esc(item.role)}"><header><span>${esc(t(title))}</span><small>${esc(t(subtitle))}</small></header>${recommendationMovieCard(item)}</section>`;
  }).join("") + note;
}

async function showQuizResults(sessionId) {
  screen.innerHTML = `<main class="picker-quiz picker-results"><div class="picker-loading">${esc(t("pick_loading"))}</div></main>`;
  try {
    const data = await api(`/api/recommendations/quiz/${encodeURIComponent(sessionId)}/results?language=${encodeURIComponent(lang)}`);
    rememberQuizEngine(data);
    pickerMode(false);
    let current = data.items || [];
    const restartQuiz = async () => {
      try {
        pickerMode(true);
        // Перезапуск — новая сессия: версию берём из свежего ответа сервера.
        const restarted = await api(`/api/recommendations/quiz/${encodeURIComponent(sessionId)}/restart`, {
          method: "POST", body: JSON.stringify({ language: lang }),
        });
        showQuizQuestion(restarted);
      } catch (_) { startRecommendationQuiz(); }
    };
    const renderResults = () => {
      const box = screen.querySelector(".picker-results-list");
      box.innerHTML = quizResultsHTML(current);
      box.querySelectorAll(".picker-role").forEach((section, index) => wireRecommendationMovie(section, current[index], {
        mode: "quiz", sessionId, role: current[index].role,
        returnTo: () => showQuizResults(sessionId),
        onReject: () => replaceQuizPick(sessionId, current[index], updated => { current = updated; renderResults(); }),
      }));
      box.querySelector("[data-quiz-back]")?.addEventListener("click", async () => {
        try {
          const previous = await api(`/api/recommendations/quiz/${encodeURIComponent(sessionId)}/back`, {
            method: "POST", body: JSON.stringify({ language: lang }),
          });
          showQuizQuestion(previous);
        } catch (error) { tg?.showAlert?.(String(error.message || t("load_err"))); }
      });
      box.querySelector("[data-quiz-restart]")?.addEventListener("click", restartQuiz);
      box.querySelector("[data-quiz-random]")?.addEventListener("click", showRandomRecommendation);
    };
    const bottomRestart = current.length
      ? `<button class="picker-restart" id="picker-restart" type="button">${esc(t("pick_restart"))}</button>`
      : "";
    screen.innerHTML = `${pickerResultsHeader()}<main class="picker-results rise d1"><div class="picker-results-list"></div>${bottomRestart}</main>`;
    renderResults();
    wireBack(showPicker);
    document.getElementById("picker-restart")?.addEventListener("click", restartQuiz);
  } catch (error) {
    pickerMode(false);
    showPicker();
    tg?.showAlert?.(String(error.message || t("load_err")));
  }
}

// Заменяем только отклонённую карточку: анкета и два других варианта остаются.
async function replaceQuizPick(sessionId, item, apply) {
  try {
    const data = await api(`/api/recommendations/quiz/${encodeURIComponent(sessionId)}/replace`, {
      method: "POST", body: JSON.stringify({ language: lang, film_id: item.id, role: item.role }),
    });
    rememberQuizEngine(data);
    apply(data.items || []);
    if (!data.replacement) tg?.showAlert?.(String(t("pick_rejected")));
  } catch (error) {
    // Подбор обновился уже после начала опроса: досчитывать старую подборку
    // новой логикой нельзя, поэтому предлагаем пройти заново, а не показываем
    // человеку непонятную ошибку.
    if (error.code === "SESSION_VERSION_MISMATCH" || error.code === "SESSION_ENGINE_UNSUPPORTED") {
      tg?.showAlert?.(String(t("pick_version_changed")));
      startRecommendationQuiz();
      return;
    }
    tg?.showAlert?.(String(error.message || t("load_err")));
  }
}

async function loadCollectionsRail() {
  const el = document.getElementById("rail-coll");
  try {
    // В режиме админа лента показывает и черновики/архив (со статусом на карточке),
    // чтобы редактировать «на месте». Публичная ручка их по-прежнему не отдаёт.
    const admin = canEditCollections();
    const { items } = await api(admin ? "/api/admin/collections" : "/api/collections");
    // Одна подборка живёт ровно в одном формате: крупные уходят в featured-блок,
    // обычные остаются в этой ленте.
    const featured = items.filter(c => c.display_type === "featured");
    const standard = items.filter(c => c.display_type !== "featured");
    renderFeaturedCollections(featured);
    if (!el) return;
    if (!standard.length) {
      el.innerHTML = `<div class="rail-empty">${esc(admin ? t("collections_empty_admin_s") : t("collections_empty_s"))}</div>`;
      return;
    }
    el.replaceChildren(...standard.map(collectionCard));
  } catch (e) { if (el) el.innerHTML = `<div class="rail-empty">${esc(t("rail_err"))}</div>`; }
}

// Крупные подборки живут отдельной секцией над лентой «Подборки». Если их нет —
// секция не рендерится вовсе: ни заголовка, ни пустого места.
function renderFeaturedCollections(items) {
  document.getElementById("sec-featured")?.remove();
  if (!items.length) return;
  const anchor = document.getElementById("sec-coll");
  if (!anchor) return;
  const section = document.createElement("section");
  section.className = "rise d4";
  section.id = "sec-featured";
  if (canEditCollections()) {
    const head = document.createElement("div");
    head.className = "head";
    head.innerHTML = `<h2>${esc(t("admin_display_featured"))}</h2>
      <button class="icon-add" id="featured-add" aria-label="${esc(t("admin_create_featured"))}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>`;
    head.querySelector("#featured-add").onclick = () => {
      CollectionEditor.createNew("featured");
      showCollectionEditor();
    };
    section.appendChild(head);
  }
  const track = document.createElement("div");
  track.className = "featured-track";
  items.forEach(item => {
    const wrap = document.createElement("div");
    wrap.className = "featured-slot";
    wrap.appendChild(featuredCollectionCard(item));
    if (canEditCollections()) wrap.appendChild(featuredAdminControls(item, items));
    track.appendChild(wrap);
  });
  section.appendChild(track);
  anchor.parentNode.insertBefore(section, anchor);
}

// Управление крупной подборкой в режиме админа: статус, редактирование и
// порядок стрелками (тот же проверенный подход, что и у фильмов внутри подборки).
function featuredAdminControls(item, siblings) {
  const bar = document.createElement("div");
  bar.className = "featured-admin";
  const index = siblings.findIndex(c => c.id === item.id);
  bar.innerHTML = `
    <span class="admin-badge admin-badge-${esc(item.status || "published")}">${esc(t(COLLECTION_STATUS_LABEL[item.status] || "admin_status_published"))}</span>
    <div class="featured-admin-controls">
      <button class="admin-icon-btn" data-left ${index === 0 ? "disabled" : ""} aria-label="${esc(t("admin_move_left"))}">${appIcon("arrowLeft")}</button>
      <button class="admin-icon-btn" data-right ${index === siblings.length - 1 ? "disabled" : ""} aria-label="${esc(t("admin_move_right"))}">${appIcon("arrowRight")}</button>
      <button class="admin-icon-btn" data-edit aria-label="${esc(t("admin_edit"))}">${appIcon("pencil")}</button>
    </div>`;
  bar.querySelector("[data-edit]").onclick = () => openCollectionEditorFor(item.id);
  bar.querySelector("[data-left]").onclick = () => reorderFeatured(siblings, index, -1);
  bar.querySelector("[data-right]").onclick = () => reorderFeatured(siblings, index, 1);
  return bar;
}

async function reorderFeatured(items, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= items.length) return;
  const ordered = items.slice();
  [ordered[index], ordered[target]] = [ordered[target], ordered[index]];
  const result = await adminCall("/api/admin/collections/featured/order", {
    method: "PUT", body: JSON.stringify({ ordered_ids: ordered.map(c => c.id) }),
  });
  if (result) loadCollectionsRail();
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
// ── Фоновые изображения жанров (подготовка) ──────────────────────────────────
// Централизованный маппинг кураторского пакета WebP (frontend/assets/genres/,
// 1200×675, тёмные кинематографические кадры с уже вшитым нижним градиентом).
// Битый/отсутствующий файл тихо падает на tint-фолбэк (фон .gart).
const GENRE_BACKDROPS_READY = true;
const GENRE_BACKDROPS = {
  drama: "assets/genres/drama.webp",
  action: "assets/genres/action.webp",
  comedy: "assets/genres/comedy.webp",
  thriller: "assets/genres/thriller.webp",
  adventure: "assets/genres/adventure.webp",
  scifi: "assets/genres/scifi.webp",
  crime: "assets/genres/crime.webp",
  mystery: "assets/genres/mystery.webp",
  fantasy: "assets/genres/fantasy.webp",
  horror: "assets/genres/horror.webp",
  animation: "assets/genres/animation.webp",
  romance: "assets/genres/romance.webp",
};
function genreBackdropHTML(name) {
  if (!GENRE_BACKDROPS_READY) return "";
  const src = GENRE_BACKDROPS[genreKey(name)];
  return src ? `<img class="gart-bg" src="${src}" alt="" loading="lazy" decoding="async">` : "";
}

function genreCard(g) {
  // Карточка жанра: детерминированный тинт жанра (GENRE_VISUALS — тот же реестр,
  // что и статистика) поверх тёмной базы + маленькая outline-иконка. Фоновое
  // изображение подключится через GENRE_BACKDROPS, тинт остаётся фолбэком.
  const card = document.createElement("div");
  card.className = "genre";
  const visual = genreVisual(g.name);
  card.innerHTML = `<div class="gart" style="--gtint:${visual.glow}">
    ${genreBackdropHTML(g.name)}
    <span class="lbl"><b>${esc(cap(g.name))}</b><span>${g.count} ${esc(t("count_films", g.count))}</span></span>
  </div>`;
  // Битый/отсутствующий файл — тихо убираем картинку, остаётся tint-фолбэк.
  card.querySelector(".gart-bg")?.addEventListener("error", (e) => e.target.remove());
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

const NOTIFICATION_ICONS = Object.freeze({
  rating: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2 7.5 14 3 9.6l6.2-.9L12 3Z"/>',
  paired: '<path d="M12 20s-7-4.35-7-10a4 4 0 0 1 7-2.65A4 4 0 0 1 19 10c0 5.65-7 10-7 10Z"/><path d="m9.2 11.7 1.8 1.8 3.9-4"/>',
  invite: '<path d="M12 20s-7-4.35-7-10a4 4 0 0 1 7-2.65A4 4 0 0 1 19 10c0 5.65-7 10-7 10Z"/><path d="M12 7v6m-3-3h6"/>',
  ended: '<path d="M4 6.5h16v11H4z"/><path d="m4 7 8 6 8-6"/>',
  system: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 20h4"/>',
  empty: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 20h4"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
});
function notificationSvg(name, className = "") {
  const paths = NOTIFICATION_ICONS[name] || NOTIFICATION_ICONS.system;
  return `<svg${className ? ` class="${className}"` : ""} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}
function notificationIconName(eventType) {
  const type = String(eventType || "");
  if (type === "pair.film.rated" || type.startsWith("film.")) return "rating";
  if (type === "pair.invite.accepted") return "paired";
  if (type.includes("invite")) return "invite";
  if (type.startsWith("pair.ended")) return "ended";
  return "system";
}
function notificationCategory(eventType) {
  const type = String(eventType || "");
  if (type === "pair.film.rated" || type.startsWith("film.")) return "films";
  if (type.startsWith("pair.")) return "pair";
  return "system";
}
function relativeNotificationTime(raw) {
  const ms = Date.now() - new Date(raw || 0).getTime();
  if (!Number.isFinite(ms) || ms < 60_000) return t("notif_now");
  const min = Math.floor(ms / 60_000); if (min < 60) return t("notif_min_ago", min);
  const hour = Math.floor(min / 60); if (hour < 24) return t("notif_hour_ago", hour);
  return t("notif_day_ago", Math.floor(hour / 24));
}
function notificationCard(item) {
  const payload = item.payload || {};
  const iconName = notificationIconName(item.event_type);
  return `<article class="notification-card ${item.read ? "is-read" : "is-unread"}" data-notification-id="${Number(item.id)}" tabindex="0" role="button" aria-label="${esc(payload.title || t("notif_title"))}">
    <span class="notification-event-icon notification-event-${iconName}">${notificationSvg(iconName)}${item.read ? "" : '<i aria-hidden="true"></i>'}</span>
    <span class="notification-copy">
      <b>${esc(payload.title || t("notif_title"))}</b>
      <span>${esc(payload.body || "")}</span>
      <time datetime="${esc(item.created_at || "")}">${esc(relativeNotificationTime(item.created_at))}</time>
    </span>
    <span class="notification-chevron">${notificationSvg("chevron")}</span>
    ${item.deep_link ? `<button type="button" class="notification-action" data-notification-open>${esc(payload.action_label || t("back"))}</button>` : ""}
  </article>`;
}
function navigateFromNotification(item) {
  const link = String(item.deep_link || "");
  const movieMatch = /^movie:(\d+)$/.exec(link);
  if (movieMatch) {
    const filmId = Number(movieMatch[1]);
    if (Number.isSafeInteger(filmId) && filmId > 0) {
      setActiveTab("home");
      openDetail(filmId, showNotifications);
      return;
    }
  }
  if (link.startsWith("inv_")) { showAcceptInvite(link); return; }
  if (link === "stats") { setActiveTab("stats"); showStats("pair"); return; }
  setActiveTab("home"); showHome();
}
async function showNotifications() {
  const viewGeneration = ++_viewGeneration;
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="sub-head notification-head">${backBtn()}<h1>${esc(t("notif_title"))}</h1><button type="button" class="notification-mark-all" disabled>${esc(t("notif_mark_all"))}</button></div>
    <main class="notifications-page">
      <nav class="notification-filters" aria-label="${esc(t("notif_title"))}"></nav>
      <section class="notifications-content" aria-live="polite"></section>
    </main>`;
  wireBack(() => { setActiveTab("home"); showHome(); });
  const page = screen.querySelector(".notifications-page");
  const content = page?.querySelector(".notifications-content");
  const filters = page?.querySelector(".notification-filters");
  const markAll = screen.querySelector(".notification-mark-all");
  const itemStore = new Map();
  let nextBeforeId = null;
  let loading = false;
  const categoryKeys = ["all", "pair", "films", "system"];

  const renderFilters = () => {
    if (!filters) return;
    filters.innerHTML = categoryKeys.map(key => {
      const count = Number(_notificationUnreadByCategory[key]) || 0;
      return `<button type="button" class="notification-filter${_notificationFilter === key ? " active" : ""}" data-notification-filter="${key}" aria-pressed="${_notificationFilter === key}">
        <span>${esc(t(`notif_filter_${key}`))}</span>${count ? `<b>${count > 99 ? "99+" : count}</b>` : ""}
      </button>`;
    }).join("");
  };
  const syncUnreadState = () => {
    _notificationUnread = Number(_notificationUnreadByCategory.all) || 0;
    if (markAll) markAll.disabled = !_notificationUnread;
    refreshNotificationBadgeVisualOnly();
    renderFilters();
  };
  const skeletons = () => `<div class="notifications-list notification-skeleton-list" aria-label="${esc(t("notif_loading"))}">
    ${Array.from({ length: 4 }, () => '<div class="notification-skeleton"><i></i><span><b></b><em></em><small></small></span></div>').join("")}
  </div>`;
  const renderNotificationEmpty = (filtered = false) => `<div class="notifications-empty">
    <span>${notificationSvg("empty")}</span>
    <h2>${esc(t(filtered ? "notif_filtered_empty_t" : "notif_empty_t"))}</h2>
    <p>${esc(t(filtered ? "notif_filtered_empty_s" : "notif_empty_s"))}</p>
  </div>`;
  const errorState = () => `<div class="notifications-empty notifications-error">
    <span>${notificationSvg("system")}</span><h2>${esc(t("notif_error"))}</h2>
    <p>${esc(t("notif_error_s"))}</p><button class="notifications-more" type="button" data-notification-retry>${esc(t("notif_retry"))}</button>
  </div>`;
  const optimisticRead = (item) => {
    if (item.read) return;
    item.read = true;
    content?.querySelector(`[data-notification-id="${Number(item.id)}"]`)?.classList.replace("is-unread", "is-read");
    content?.querySelector(`[data-notification-id="${Number(item.id)}"] .notification-event-icon i`)?.remove();
    const category = notificationCategory(item.event_type);
    _notificationUnreadByCategory.all = Math.max(0, (Number(_notificationUnreadByCategory.all) || 0) - 1);
    _notificationUnreadByCategory[category] = Math.max(0, (Number(_notificationUnreadByCategory[category]) || 0) - 1);
    syncUnreadState();
  };
  const openItem = async (item) => {
    if (!item) return;
    const wasUnread = !item.read;
    if (wasUnread) {
      optimisticRead(item);
      api(`/api/notifications/${item.id}/read`, { method: "POST" }).catch(() => {});
    }
    navigateFromNotification(item);
  };
  const load = async (append = false) => {
    if (loading || !content || viewGeneration !== _viewGeneration) return;
    loading = true;
    if (!append) {
      nextBeforeId = null;
      itemStore.clear();
      content.innerHTML = skeletons();
    }
    try {
      const query = `?limit=20&category=${encodeURIComponent(_notificationFilter)}${append && nextBeforeId ? `&before_id=${encodeURIComponent(nextBeforeId)}` : ""}`;
      const result = await api(`/api/notifications${query}`);
      if (viewGeneration !== _viewGeneration) return;
      _notificationUnread = Number(result.unread_count) || 0;
      _notificationUnreadByCategory = {
        all: Number(result.unread_by_category?.all ?? result.unread_count) || 0,
        pair: Number(result.unread_by_category?.pair) || 0,
        films: Number(result.unread_by_category?.films) || 0,
        system: Number(result.unread_by_category?.system) || 0,
      };
      nextBeforeId = result.next_before_id;
      const rows = result.items || [];
      rows.forEach(item => itemStore.set(Number(item.id), item));
      const cards = rows.map(notificationCard).join("");
      if (!append) {
        content.innerHTML = rows.length
          ? `<div class="notifications-list">${cards}</div>`
          : renderNotificationEmpty(_notificationFilter !== "all");
      } else {
        content.querySelector(".notifications-list")?.insertAdjacentHTML("beforeend", cards);
      }
      content.querySelector(".notifications-more")?.remove();
      if (nextBeforeId) content.insertAdjacentHTML("beforeend", `<button class="notifications-more" type="button" data-notification-more>${esc(t("notif_load_more"))}</button>`);
      syncUnreadState();
    } catch (_) {
      if (!append && viewGeneration === _viewGeneration) content.innerHTML = errorState();
    } finally { loading = false; }
  };

  filters?.addEventListener("click", event => {
    const button = event.target.closest("[data-notification-filter]");
    if (!button || button.dataset.notificationFilter === _notificationFilter) return;
    _notificationFilter = button.dataset.notificationFilter;
    renderFilters();
    load(false);
  });
  content?.addEventListener("click", event => {
    if (event.target.closest("[data-notification-more]")) { load(true); return; }
    if (event.target.closest("[data-notification-retry]")) { load(false); return; }
    const card = event.target.closest("[data-notification-id]");
    if (card) openItem(itemStore.get(Number(card.dataset.notificationId)));
  });
  content?.addEventListener("keydown", event => {
    const card = event.target.closest("[data-notification-id]");
    if (!card || (event.key !== "Enter" && event.key !== " ")) return;
    event.preventDefault();
    openItem(itemStore.get(Number(card.dataset.notificationId)));
  });
  if (markAll) markAll.onclick = async () => {
    if (!_notificationUnread || markAll.disabled) return;
    const previousCounts = { ..._notificationUnreadByCategory };
    itemStore.forEach(optimisticRead);
    _notificationUnreadByCategory = { all: 0, pair: 0, films: 0, system: 0 };
    content?.querySelectorAll(".notification-card.is-unread").forEach(card => {
      card.classList.replace("is-unread", "is-read");
      card.querySelector(".notification-event-icon i")?.remove();
    });
    syncUnreadState();
    try {
      await api("/api/notifications/read-all", { method: "POST" });
    } catch (_) {
      _notificationUnreadByCategory = previousCounts;
      syncUnreadState();
      await load(false);
    }
  };
  renderFilters();
  await load();
}

// ── Режим администратора ─────────────────────────────────────────────────────
// Права выдаёт ТОЛЬКО сервер (/api/me/capabilities и проверка на каждой admin-
// ручке). Здесь живёт исключительно UX: показывать ли элементы редактирования.
// В localStorage хранится лишь визуальное предпочтение, никаких прав.
const ADMIN_MODE_KEY = "addictfilm.adminMode.enabled";
const AdminMode = {
  capability: null,   // ответ /api/me/capabilities, null — ещё не загружали
  enabled: false,     // тумблер в настройках (визуальное предпочтение)
  async refresh() {
    try {
      this.capability = await api("/api/me/capabilities");
    } catch (_) {
      this.capability = { is_admin: false, admin_role: null, capabilities: [] };
    }
    const wasEditing = this.enabled;
    if (!this.capability.is_admin) this.enabled = false;  // права отозвали — режим гаснет
    else this.enabled = this.preference();
    this.renderIndicator();
    // The picker may already be visible while this server-confirmed capability
    // is loading. Rebuild only its media layer; never request another film.
    if (_singlePickItem && document.body.classList.contains("single-pick-open")) {
      replaceSinglePickMedia();
    }
    // Возможности приходят асинхронно, а главная уже могла отрисоваться без
    // админских действий — перерисовываем её, когда состояние изменилось.
    if (this.enabled !== wasEditing && activeTabName() === "home"
        && document.getElementById("sec-coll")) showHome();
    return this.capability;
  },
  preference() {
    try { return localStorage.getItem(ADMIN_MODE_KEY) === "true"; } catch (_) { return false; }
  },
  isCapable() { return !!this.capability?.is_admin; },
  has(permission) { return !!this.capability?.capabilities?.includes(permission); },
  // Единственная проверка для UI: есть права И режим включён И такое разрешение.
  active(permission = "collections.write") {
    return this.isCapable() && this.enabled && this.has(permission);
  },
  setEnabled(enabled) {
    this.enabled = Boolean(enabled) && this.isCapable();
    try { localStorage.setItem(ADMIN_MODE_KEY, String(this.enabled)); } catch (_) { /* приватный режим */ }
    tg?.HapticFeedback?.impactOccurred?.("light");
    this.renderIndicator();
  },
  // Сервер ответил 403 — права отозвали прямо в сессии: гасим режим и говорим об этом.
  revoked() {
    this.capability = { is_admin: false, admin_role: null, capabilities: [] };
    this.enabled = false;
    this.renderIndicator();
    tg?.showAlert?.(t("admin_permission_revoked"));
  },
  renderIndicator() {
    document.getElementById("admin-indicator")?.remove();
    // Плашка висит поверх контента, поэтому о её высоте должна знать вёрстка:
    // иначе она накрывает последнюю карточку на экранах со списками.
    document.body.classList.toggle("admin-indicator-on", !!this.active("collections.read"));
    if (!this.active("collections.read")) return;
    const bar = document.createElement("button");
    bar.id = "admin-indicator";
    bar.type = "button";
    bar.className = "admin-indicator";
    bar.innerHTML = `<span>${esc(t("admin_mode_active"))}</span><i>${esc(t("admin_exit_mode"))}</i>`;
    bar.onclick = () => { this.setEnabled(false); route(activeTabName()); };
    document.body.appendChild(bar);
  },
};
function activeTabName() { return document.querySelector("#tabbar .tab.active")?.dataset.tab || "home"; }

// ── Сессия редактора подборки ────────────────────────────────────────────────
// Единственный источник правды для редактора. Раньше значения жили в DOM-инпутах,
// и любой уход на экран поиска фильмов перерисовывал редактор с сервера — из-за
// этого терялось введённое название. Теперь состояние переживает навигацию, а
// запись в БД создаётся только по явному «Сохранить»/«Опубликовать».
const EDITOR_STORAGE_VERSION = 1;
const EDITOR_STORAGE_PREFIX = "addictfilm.collectionEditor.v1";

const CollectionEditor = {
  session: null,
  _persistTimer: null,

  createNew(displayType = "standard") {
    this.session = {
      schemaVersion: EDITOR_STORAGE_VERSION,
      sessionId: uuid(),
      createRequestId: uuid(),   // не меняется между повторами — защита от дублей
      mode: "create",
      collectionId: null,
      version: null,
      serverStatus: null,
      fields: { title: "", description: "", displayType, coverUrl: "", backdropUrl: "" },
      films: [],
      dirty: false,
    };
    this.persistNow();
    return this.session;
  },

  fromCollection(collection) {
    this.session = {
      schemaVersion: EDITOR_STORAGE_VERSION,
      sessionId: uuid(),
      createRequestId: uuid(),
      mode: "edit",
      collectionId: collection.id,
      version: collection.version,
      serverStatus: collection.status,
      fields: {
        title: collection.title || "",
        description: collection.description || "",
        displayType: collection.display_type || "standard",
        coverUrl: collection.cover_url || "",
        backdropUrl: collection.backdrop_url || "",
      },
      films: (collection.items || []).map(normalizeEditorFilm),
      dirty: false,
    };
    this.persistNow();
    return this.session;
  },

  get() { return this.session; },

  updateField(field, value) {
    if (!this.session || this.session.fields[field] === value) return;
    this.session.fields[field] = value;
    this.session.dirty = true;
    this.schedulePersist();
  },

  setFilms(films) {
    if (!this.session) return;
    this.session.films = films;
    this.session.dirty = true;
    this.schedulePersist();
  },

  // Первое сохранение превращает локальную черновую сессию в серверную запись —
  // без перемонтирования экрана и без потери введённого.
  becomeExisting({ id, version, status }) {
    if (!this.session) return;
    Object.assign(this.session, {
      mode: "edit", collectionId: id, version, serverStatus: status, dirty: false,
    });
    this.persistNow();
  },

  applyServer(collection) {
    if (!this.session || !collection) return;
    this.session.version = collection.version;
    this.session.serverStatus = collection.status;
    this.session.dirty = false;
    this.persistNow();
  },

  schedulePersist() {
    clearTimeout(this._persistTimer);
    this._persistTimer = setTimeout(() => this.persistNow(), 150);
  },

  persistNow() {
    if (!this.session) return;
    try {
      sessionStorage.setItem(`${EDITOR_STORAGE_PREFIX}:${this.session.sessionId}`,
        JSON.stringify(this.session));
    } catch (_) { /* приватный режим/переполнение — в памяти состояние всё равно есть */ }
  },

  restore(sessionId) {
    if (this.session?.sessionId === sessionId) return this.session;
    try {
      const raw = sessionStorage.getItem(`${EDITOR_STORAGE_PREFIX}:${sessionId}`);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (parsed?.schemaVersion !== EDITOR_STORAGE_VERSION) return null;
      this.session = parsed;
      return parsed;
    } catch (_) { return null; }
  },

  discard() {
    if (!this.session) return;
    try { sessionStorage.removeItem(`${EDITOR_STORAGE_PREFIX}:${this.session.sessionId}`); }
    catch (_) { /* нечего чистить */ }
    this.session = null;
  },
};

function uuid() {
  try { return crypto.randomUUID(); }
  catch (_) { return `s${Date.now()}-${Math.random().toString(36).slice(2)}`; }
}

// В сессии держим только то, что рисует редактор: без сырых ответов API и картинок.
function normalizeEditorFilm(film) {
  return {
    id: film.id,
    title: film.title,
    year: film.year ?? null,
    poster_url: film.poster_url ?? null,
    backdrop_url: film.backdrop_url ?? null,
  };
}

function mergeUniqueFilms(current, added) {
  const result = current.slice();
  const seen = new Set(current.map(f => f.id));
  for (const film of added) {
    if (seen.has(film.id)) continue;
    result.push(normalizeEditorFilm(film));
    seen.add(film.id);
  }
  return result;
}

// Состояние успевает записаться, даже если WebView сворачивают или перезагружают.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") CollectionEditor.persistNow();
});
window.addEventListener("pagehide", () => CollectionEditor.persistNow());

// ── Подборки (публичный просмотр + in-app редактирование в режиме админа) ─────
function canEditCollections() { return AdminMode.active("collections.write"); }

// Крупная редакционная подборка. ОДИН рендерер и для главной, и для
// предпросмотра в редакторе — превью не может разойтись с продакшеном.
function featuredCollectionCard(c, { preview = false } = {}) {
  const card = document.createElement("article");
  card.className = "featured-card";
  card.dataset.collectionId = c.id;
  const image = c.backdrop || c.cover || "";
  const count = `${c.film_count || 0} ${t("count_films", c.film_count || 0)}`;
  card.innerHTML = `
    ${image ? `<img class="featured-card-img" src="${posterSrc(image, true)}" alt="" loading="lazy" decoding="async" data-img-retry>` : ""}
    <div class="featured-card-scrim"></div>
    <div class="featured-card-body">
      <span class="featured-card-eyebrow">${esc(t("coll_eyebrow"))}</span>
      <h3 class="featured-card-title">${esc(c.title || "")}</h3>
      ${c.description ? `<p class="featured-card-desc">${esc(c.description)}</p>` : ""}
      <div class="featured-card-meta"><span>${esc(count)}</span><i aria-hidden="true">›</i></div>
    </div>`;
  if (!preview) {
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    card.onclick = () => showCollectionDetail(c.id);
    card.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showCollectionDetail(c.id); }
    };
  }
  return card;
}

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
      <div class="meta-row"><span class="y">${c.film_count} ${esc(t("count_films", c.film_count))}</span>${
        c.status && c.status !== "published"
          ? `<span class="admin-badge admin-badge-${esc(c.status)}">${esc(t(COLLECTION_STATUS_LABEL[c.status] || "admin_status_draft"))}</span>`
          : ""}</div>
    </div>`;
  card.onclick = () => showCollectionDetail(c.id);
  return card;
}

const COLLECTION_STATUS_LABEL = {
  draft: "admin_status_draft", published: "admin_status_published", archived: "admin_status_archived",
};

// Действия статуса зависят от текущего состояния — язык действий однозначный
// («Снять с публикации» ≠ «Архивировать» ≠ «Удалить навсегда»).
function collectionStatusActions(status) {
  if (status === "draft") return [["publish", "admin_publish", "primary"], ["archive", "admin_archive", ""]];
  if (status === "published") return [["unpublish", "admin_unpublish", ""], ["archive", "admin_archive", ""]];
  return [["restore", "admin_restore", "primary"]];
}

// ── Единый полноэкранный редактор подборки ───────────────────────────────────
// Один экран для создания и редактирования, обычной и крупной подборки.
// Структура строится один раз; при вводе обновляются только затронутые узлы —
// поэтому фокус и позиция курсора не прыгают.
function showCollectionEditor({ sessionId = null } = {}) {
  unwireDetailScroll();
  const session = sessionId ? CollectionEditor.restore(sessionId) : CollectionEditor.get();
  if (!session) { setActiveTab("home"); showHome(); return; }
  document.body.classList.add("collection-editor-open");
  window.scrollTo(0, 0);

  const isNew = session.collectionId == null;
  screen.innerHTML = `
    <div class="editor-head">
      <button class="back" id="ed-back" aria-label="${esc(t("back"))}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button>
      <h1 id="ed-head-title"></h1>
      <span class="admin-badge" id="ed-badge"></span>
    </div>
    <main class="collection-editor">
      <label class="admin-field"><span>${esc(t("admin_title_label"))}</span>
        <input id="ed-title" class="code-input" maxlength="80" autocomplete="off"
               placeholder="${esc(t("collections_title_ph"))}">
        <em class="editor-error" id="ed-err-title" hidden></em></label>
      <label class="admin-field"><span>${esc(t("admin_description_label"))}</span>
        <textarea id="ed-desc" class="admin-textarea" rows="2" maxlength="1000"
                  placeholder="${esc(t("admin_description_ph"))}"></textarea></label>
      <div class="admin-field"><span>${esc(t("admin_display_label"))}</span>
        <div class="admin-segments" role="radiogroup" aria-label="${esc(t("admin_display_label"))}" id="ed-segments">
          ${["standard", "featured"].map(type => `
            <button type="button" class="admin-segment" data-display="${type}" role="radio">
              <b>${esc(t(type === "standard" ? "admin_display_standard" : "admin_display_featured"))}</b>
              <small>${esc(t(type === "standard" ? "admin_display_standard_hint" : "admin_display_featured_hint"))}</small>
            </button>`).join("")}
        </div></div>
      <div class="admin-field" id="ed-backdrop-field" hidden>
        <span>${esc(t("admin_backdrop_label"))}</span>
        <div id="ed-backdrops"></div>
        <button type="button" class="editor-link" id="ed-url-toggle">${esc(t("admin_backdrop_url"))}</button>
        <input id="ed-backdrop-url" class="code-input" maxlength="2048" autocomplete="off"
               placeholder="https://…" hidden>
      </div>
      <div class="admin-field" id="ed-preview-field" hidden>
        <span>${esc(t("admin_preview_label"))}</span><div id="ed-preview"></div></div>
      <div class="admin-field">
        <div class="editor-films-head"><span>${esc(t("chip_collections"))}</span>
          <button type="button" class="editor-link" id="ed-add-film">${esc(t("coll_add_film_btn"))}</button></div>
        <em class="editor-error" id="ed-err-films" hidden></em>
        <div id="ed-films"></div></div>
      <div class="editor-actions">
        <button class="pbtn" id="ed-save">${esc(t("admin_save_draft"))}</button>
        <button class="pbtn primary" id="ed-publish">${esc(t("admin_publish"))}</button>
      </div>
      ${isNew ? "" : `<div class="editor-actions secondary" id="ed-lifecycle"></div>`}
    </main>`;

  const titleInput = document.getElementById("ed-title");
  const descInput = document.getElementById("ed-desc");
  const urlInput = document.getElementById("ed-backdrop-url");
  titleInput.value = session.fields.title;
  descInput.value = session.fields.description;
  urlInput.value = session.fields.backdropUrl;

  // Точечные апдейты: перерисовываем только то, что зависит от изменившегося поля.
  const paintHeader = () => {
    const state = CollectionEditor.get();
    document.getElementById("ed-head-title").textContent =
      state.fields.title.trim() ||
      t(state.fields.displayType === "featured" ? "admin_new_featured" : "admin_new_collection");
    const badge = document.getElementById("ed-badge");
    const status = state.collectionId == null ? "unsaved" : (state.serverStatus || "draft");
    badge.className = `admin-badge admin-badge-${status === "unsaved" ? "draft" : status}`;
    badge.textContent = status === "unsaved"
      ? t(state.dirty ? "admin_unsaved" : "admin_new_badge")
      : t(COLLECTION_STATUS_LABEL[status] || "admin_status_draft");
  };

  const paintPreview = () => {
    const state = CollectionEditor.get();
    const featured = state.fields.displayType === "featured";
    document.getElementById("ed-preview-field").hidden = !featured;
    document.getElementById("ed-backdrop-field").hidden = !featured;
    if (!featured) return;
    const host = document.getElementById("ed-preview");
    host.replaceChildren(featuredCollectionCard({
      id: state.collectionId,
      title: state.fields.title.trim() || t("admin_preview_title_ph"),
      description: state.fields.description,
      backdrop: resolveEditorBackdrop(state),
      film_count: state.films.length,
    }, { preview: true }));
  };

  const paintBackdrops = () => {
    const state = CollectionEditor.get();
    const host = document.getElementById("ed-backdrops");
    const options = state.films.map(f => ({ url: f.backdrop_url || f.poster_url, title: f.title }))
      .filter(art => art.url).slice(0, 12);
    if (!options.length) {
      host.className = "";
      host.innerHTML = `<p class="admin-hint" style="margin:0 0 8px;">${esc(t("admin_backdrop_none"))}</p>`;
      return;
    }
    host.className = "admin-backdrops";
    host.replaceChildren(...options.map(art => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `admin-backdrop${state.fields.backdropUrl === art.url ? " on" : ""}`;
      button.setAttribute("aria-label", art.title);
      button.innerHTML = `<img src="${posterSrc(art.url, true)}" alt="" loading="lazy" data-img-retry>`;
      button.onclick = () => {
        CollectionEditor.updateField("backdropUrl", art.url);
        urlInput.value = art.url;
        paintBackdrops(); paintPreview(); paintHeader();
      };
      return button;
    }));
  };

  const paintSegments = () => {
    const state = CollectionEditor.get();
    document.querySelectorAll("#ed-segments [data-display]").forEach(button => {
      const on = button.dataset.display === state.fields.displayType;
      button.classList.toggle("on", on);
      button.setAttribute("aria-checked", String(on));
    });
  };

  const paintFilms = () => {
    const state = CollectionEditor.get();
    const host = document.getElementById("ed-films");
    if (!state.films.length) {
      host.className = "";
      host.innerHTML = `<p class="admin-hint" style="margin:6px 0 0;">${esc(t("admin_no_films"))}</p>`;
      return;
    }
    host.className = "admin-item-list";
    host.replaceChildren(...state.films.map((movie, index) => {
      const row = document.createElement("div");
      row.className = "admin-item";
      row.innerHTML = `
        <span class="admin-item-art">${movie.poster_url ? `<img loading="lazy" src="${posterSrc(movie.poster_url, true)}" alt="" data-img-retry>` : ""}</span>
        <span class="admin-item-copy"><b>${esc(movie.title)}</b><small>${esc(movie.year || "")}</small></span>
        <div class="admin-item-controls">
          <button class="admin-icon-btn" data-up ${index === 0 ? "disabled" : ""} aria-label="${esc(t("admin_move_up"))}">${appIcon("arrowUp")}</button>
          <button class="admin-icon-btn" data-down ${index === state.films.length - 1 ? "disabled" : ""} aria-label="${esc(t("admin_move_down"))}">${appIcon("arrowDown")}</button>
          <button class="admin-icon-btn danger" data-remove aria-label="${esc(t("admin_remove"))}">${appIcon("trash")}</button>
        </div>`;
      const move = (delta) => {
        const films = CollectionEditor.get().films.slice();
        const target = index + delta;
        if (target < 0 || target >= films.length) return;
        [films[index], films[target]] = [films[target], films[index]];
        CollectionEditor.setFilms(films);
        paintFilms(); paintPreview(); paintHeader();
      };
      row.querySelector("[data-up]").onclick = () => move(-1);
      row.querySelector("[data-down]").onclick = () => move(1);
      row.querySelector("[data-remove]").onclick = () => {
        CollectionEditor.setFilms(CollectionEditor.get().films.filter(f => f.id !== movie.id));
        paintFilms(); paintBackdrops(); paintPreview(); paintHeader();
      };
      return row;
    }));
  };

  const paintLifecycle = () => {
    const host = document.getElementById("ed-lifecycle");
    if (!host) return;
    const state = CollectionEditor.get();
    // Архив и «удалить навсегда» появляются только у существующей записи.
    if (state.collectionId == null) { host.innerHTML = ""; return; }
    const status = state.serverStatus || "draft";
    host.innerHTML = collectionStatusActions(status)
      .filter(([action]) => action !== "publish")
      .map(([action, key]) => `<button class="pbtn" data-life="${action}">${esc(t(key))}</button>`).join("")
      + `<button class="pbtn danger" data-life="delete">${esc(t("admin_delete_forever"))}</button>`;
    host.querySelectorAll("[data-life]").forEach(button => button.onclick = async () => {
      const action = button.dataset.life;
      button.disabled = true;
      if (action === "delete") {
        tg.showConfirm(t("coll_delete_confirm", state.fields.title), async ok => {
          if (!ok) { button.disabled = false; return; }
          if (await adminCall(`/api/admin/collections/${state.collectionId}`, { method: "DELETE" })) {
            CollectionEditor.discard(); closeCollectionEditor();
          } else button.disabled = false;
        });
        return;
      }
      const updated = await adminCall(
        `/api/admin/collections/${state.collectionId}/${action}`,
        { method: "POST", body: JSON.stringify({ version: state.version }) });
      button.disabled = false;
      if (!updated) return;
      CollectionEditor.applyServer(updated);
      paintHeader(); paintLifecycle();
    });
  };

  const repaintAll = () => { paintHeader(); paintSegments(); paintBackdrops(); paintPreview(); paintFilms(); paintLifecycle(); };

  // Каждое нажатие клавиши уходит в состояние, но НЕ перестраивает поле ввода.
  titleInput.addEventListener("input", () => {
    CollectionEditor.updateField("title", titleInput.value);
    document.getElementById("ed-err-title").hidden = true;
    paintHeader(); paintPreview();
  });
  descInput.addEventListener("input", () => {
    CollectionEditor.updateField("description", descInput.value);
    paintPreview();
  });
  urlInput.addEventListener("input", () => {
    CollectionEditor.updateField("backdropUrl", urlInput.value.trim());
    paintBackdrops(); paintPreview();
  });
  document.getElementById("ed-url-toggle").onclick = () => {
    urlInput.hidden = !urlInput.hidden;
    if (!urlInput.hidden) urlInput.focus();
  };
  document.querySelectorAll("#ed-segments [data-display]").forEach(button => button.onclick = () => {
    CollectionEditor.updateField("displayType", button.dataset.display);
    tg?.HapticFeedback?.selectionChanged?.();
    paintSegments(); paintBackdrops(); paintPreview(); paintHeader();
  });
  document.getElementById("ed-add-film").onclick = () => {
    CollectionEditor.persistNow();     // уходим на пикер — состояние уже сохранено
    showSearch({ type: "collection-editor", sessionId: CollectionEditor.get().sessionId });
  };
  document.getElementById("ed-save").onclick = () => saveCollectionEditor({ publish: false });
  document.getElementById("ed-publish").onclick = () => saveCollectionEditor({ publish: true });
  document.getElementById("ed-back").onclick = () => confirmLeaveEditor();

  repaintAll();
}

// Открыть редактор для существующей подборки: тянем актуальные данные и версию,
// заводим из них сессию — дальше экран не ходит на сервер за состоянием.
async function openCollectionEditorFor(collectionId) {
  const collection = await adminCall(`/api/admin/collections/${collectionId}`);
  if (!collection) return;
  CollectionEditor.fromCollection(collection);
  showCollectionEditor();
}

function resolveEditorBackdrop(state) {
  // Тот же порядок падения, что и на сервере, — превью не расходится с итогом.
  if (state.fields.backdropUrl) return state.fields.backdropUrl;
  const withBackdrop = state.films.find(f => f.backdrop_url);
  if (withBackdrop) return withBackdrop.backdrop_url;
  if (state.fields.coverUrl) return state.fields.coverUrl;
  return state.films.find(f => f.poster_url)?.poster_url || null;
}

function closeCollectionEditor() {
  document.body.classList.remove("collection-editor-open");
  setActiveTab("home");
  showHome();
}

function confirmLeaveEditor() {
  const state = CollectionEditor.get();
  if (!state || !state.dirty) { CollectionEditor.discard(); closeCollectionEditor(); return; }
  tg.showConfirm(t("admin_unsaved_changes"), ok => {
    if (!ok) return;                       // «Отмена» — остаёмся в редакторе
    CollectionEditor.discard();            // выход без сохранения: записи в БД не появится
    closeCollectionEditor();
  });
}

// Один контроллер сохранения: знает, создаём мы подборку или обновляем.
async function saveCollectionEditor({ publish = false } = {}) {
  const state = CollectionEditor.get();
  if (!state) return;
  const saveButton = document.getElementById("ed-save");
  const publishButton = document.getElementById("ed-publish");
  if (saveButton.disabled || publishButton.disabled) return;   // защита от двойного тапа
  const title = state.fields.title.trim();
  if (!title) {
    const error = document.getElementById("ed-err-title");
    error.textContent = t("admin_err_title"); error.hidden = false;
    const input = document.getElementById("ed-title");
    input.scrollIntoView({ block: "center" });
    input.focus();
    return;
  }
  if (publish && !state.films.length) {
    const error = document.getElementById("ed-err-films");
    error.textContent = t("admin_err_films"); error.hidden = false;
    error.scrollIntoView({ block: "center" });
    return;
  }
  saveButton.disabled = true; publishButton.disabled = true;
  try {
    let collectionId = state.collectionId;
    if (collectionId == null) {
      // Ключ идемпотентности живёт в сессии: повтор после ошибки не создаст дубль.
      const created = await adminCall("/api/admin/collections", {
        method: "POST",
        headers: { "Idempotency-Key": state.createRequestId },
        body: JSON.stringify({
          title, description: state.fields.description || null,
          display_type: state.fields.displayType,
          backdrop_url: state.fields.backdropUrl || null,
          ordered_film_ids: state.films.map(f => f.id),
        }),
      });
      if (!created) return;
      collectionId = created.id;
      const fresh = await adminCall(`/api/admin/collections/${collectionId}`);
      CollectionEditor.becomeExisting({
        id: collectionId, version: fresh?.version || 1, status: fresh?.status || "draft" });
    } else {
      const updated = await adminCall(`/api/admin/collections/${collectionId}`, {
        method: "PATCH",
        body: JSON.stringify({
          version: state.version, title,
          description: state.fields.description || null,
          display_type: state.fields.displayType,
          backdrop_url: state.fields.backdropUrl || null,
        }),
      });
      if (!updated) return;
      CollectionEditor.applyServer(updated);
      await syncEditorFilms(collectionId);
    }
    if (publish) {
      const current = CollectionEditor.get();
      const published = await adminCall(`/api/admin/collections/${collectionId}/publish`, {
        method: "POST", body: JSON.stringify({ version: current.version }) });
      // Черновик уже создан: даже если публикация не прошла, введённое не теряется.
      if (published) CollectionEditor.applyServer(published);
    }
    tg?.HapticFeedback?.notificationOccurred?.("success");
    showCollectionEditor({ sessionId: CollectionEditor.get().sessionId });  // остаёмся в редакторе
  } finally {
    const save = document.getElementById("ed-save");
    const pub = document.getElementById("ed-publish");
    if (save) save.disabled = false;
    if (pub) pub.disabled = false;
  }
}

// Для существующей подборки состав правим точечно: добавляем новое, убираем
// снятое и один раз фиксируем порядок.
async function syncEditorFilms(collectionId) {
  const state = CollectionEditor.get();
  const server = await adminCall(`/api/admin/collections/${collectionId}`);
  if (!server) return;
  const serverIds = (server.items || []).map(f => f.id);
  const localIds = state.films.map(f => f.id);
  for (const id of serverIds.filter(existing => !localIds.includes(existing))) {
    await adminCall(`/api/admin/collections/${collectionId}/films/${id}`, { method: "DELETE" });
  }
  for (const id of localIds.filter(local => !serverIds.includes(local))) {
    await adminCall(`/api/admin/collections/${collectionId}/films`,
      { method: "POST", body: JSON.stringify({ src: "id", ref: String(id) }) });
  }
  if (localIds.length > 1) {
    const fresh = await adminCall(`/api/admin/collections/${collectionId}`);
    if (fresh) {
      const reordered = await adminCall(`/api/admin/collections/${collectionId}/items/order`, {
        method: "PUT",
        body: JSON.stringify({ version: fresh.version, ordered_film_ids: localIds }) });
      if (reordered) CollectionEditor.applyServer(reordered);
    }
  }
}

async function showCollectionDetail(id) {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  const canEdit = canEditCollections();
  screen.innerHTML = `<div class="sub-head">${backBtn()}<h1 id="cd-title">…</h1></div>
    <div id="cd-editor"></div>
    <div id="cdg">${skeletonGrid(6)}</div>
    <div id="cd-actions"></div>`;
  wireBack(() => { setActiveTab("home"); showHome(); });

  let collection = null;
  try {
    // Админ читает через admin-ручку: только она отдаёт черновики и версию для
    // оптимистичной блокировки. Обычный пользователь — через публичную.
    collection = await api(canEdit ? `/api/admin/collections/${id}` : `/api/collections/${id}`);
  } catch (error) {
    if (canEdit && error.status === 403) AdminMode.revoked();
    document.getElementById("cdg").innerHTML = emptyState("⚠️", t("load_err"), "");
    return;
  }
  document.getElementById("cd-title").textContent = collection.title;

  const renderItems = () => {
    const grid = document.getElementById("cdg");
    if (!collection.items.length) {
      grid.innerHTML = emptyState("🎬", t("genre_empty_t"), t("genre_empty_s"));
      return;
    }
    const back = () => showCollectionDetail(id);
    if (!canEdit) {
      grid.replaceChildren(gridOf(collection.items,
        m => posterTile(m, { onClick: () => openDetail(m.id, back, m) })));
      return;
    }
    // В режиме админа тайл остаётся тайлом (тап открывает фильм), а
    // редактирование живёт в отдельных кнопках с целью ≥44px — случайный тап
    // по карточке не должен ничего переставлять или удалять.
    const list = document.createElement("div");
    list.className = "admin-item-list";
    collection.items.forEach((movie, index) => {
      const row = document.createElement("div");
      row.className = "admin-item";
      row.innerHTML = `
        <button class="admin-item-open" type="button" aria-label="${esc(movie.title)}">
          <span class="admin-item-art">${movie.poster_url ? `<img loading="lazy" src="${posterSrc(movie.poster_url, true)}" alt="" data-img-retry>` : ""}</span>
          <span class="admin-item-copy"><b>${esc(movie.title)}</b><small>${esc(movie.year || "")}</small></span>
        </button>
        <div class="admin-item-controls">
          <button class="admin-icon-btn" data-up ${index === 0 ? "disabled" : ""} aria-label="${esc(t("admin_move_up"))}">${appIcon("arrowUp")}</button>
          <button class="admin-icon-btn" data-down ${index === collection.items.length - 1 ? "disabled" : ""} aria-label="${esc(t("admin_move_down"))}">${appIcon("arrowDown")}</button>
          <button class="admin-icon-btn danger" data-remove aria-label="${esc(t("admin_remove"))}">${appIcon("trash")}</button>
        </div>`;
      row.querySelector(".admin-item-open").onclick = () => openDetail(movie.id, back, movie);
      row.querySelector("[data-up]").onclick = () => moveItem(index, -1);
      row.querySelector("[data-down]").onclick = () => moveItem(index, 1);
      row.querySelector("[data-remove]").onclick = () => tg.showConfirm(t("coll_remove_confirm", movie.title), async ok => {
        if (!ok) return;
        await adminCall(`/api/admin/collections/${id}/films/${movie.id}`, { method: "DELETE" });
        showCollectionDetail(id);
      });
      list.appendChild(row);
    });
    grid.replaceChildren(list);
  };

  // Порядок сохраняется одним запросом после перестановки (не на каждое касание),
  // с текущей версией — параллельная правка вернёт 409, а не тихо перезапишется.
  const moveItem = async (index, delta) => {
    const target = index + delta;
    if (target < 0 || target >= collection.items.length) return;
    const items = collection.items.slice();
    [items[index], items[target]] = [items[target], items[index]];
    collection.items = items;
    renderItems();
    const updated = await adminCall(`/api/admin/collections/${id}/items/order`, {
      method: "PUT",
      body: JSON.stringify({ version: collection.version, ordered_film_ids: items.map(m => m.id) }),
    });
    if (updated) collection.version = updated.version;
    else showCollectionDetail(id);  // конфликт/ошибка — перечитываем правду с сервера
  };

  // Черновик формы: незасейвленный выбор формата/фона живёт здесь, чтобы
  // предпросмотр обновлялся мгновенно, а на сервер уходил один явный PATCH.
  const draft = {
    display_type: collection.display_type || "standard",
    backdrop_url: collection.backdrop_url || "",
  };

  const renderEditor = () => {
    const editor = document.getElementById("cd-editor");
    const actions = document.getElementById("cd-actions");
    if (!canEdit) { editor.innerHTML = ""; actions.innerHTML = ""; return; }
    const status = collection.status || "draft";
    // Кадры из фильмов подборки — первый и самый безопасный источник фона.
    const filmBackdrops = (collection.items || [])
      .map(m => ({ url: m.backdrop_url || m.poster_url, title: m.title }))
      .filter(art => art.url).slice(0, 12);
    const note = status === "draft" ? t("admin_drafts_hidden") : status === "archived" ? t("admin_archived_hidden") : "";
    editor.innerHTML = `
      <div class="admin-panel">
        <div class="admin-panel-head">
          <span class="admin-badge admin-badge-${esc(status)}">${esc(t(COLLECTION_STATUS_LABEL[status]))}</span>
          ${note ? `<small>${esc(note)}</small>` : ""}
        </div>
        <label class="admin-field"><span>${esc(t("admin_title_label"))}</span>
          <input id="cd-f-title" class="code-input" value="${esc(collection.title)}" maxlength="80" autocomplete="off"></label>
        <label class="admin-field"><span>${esc(t("admin_description_label"))}</span>
          <textarea id="cd-f-desc" class="admin-textarea" rows="2" maxlength="1000" placeholder="${esc(t("admin_description_ph"))}">${esc(collection.description || "")}</textarea></label>
        <div class="admin-field"><span>${esc(t("admin_display_label"))}</span>
          <div class="admin-segments" role="radiogroup" aria-label="${esc(t("admin_display_label"))}">
            ${["standard", "featured"].map(type => `
              <button type="button" class="admin-segment${draft.display_type === type ? " on" : ""}"
                      data-display="${type}" role="radio" aria-checked="${draft.display_type === type}">
                <b>${esc(t(type === "standard" ? "admin_display_standard" : "admin_display_featured"))}</b>
                <small>${esc(t(type === "standard" ? "admin_display_standard_hint" : "admin_display_featured_hint"))}</small>
              </button>`).join("")}
          </div></div>
        ${draft.display_type === "featured" ? `
        <div class="admin-field"><span>${esc(t("admin_backdrop_label"))}</span>
          ${filmBackdrops.length ? `<div class="admin-backdrops">${filmBackdrops.map(art => `
            <button type="button" class="admin-backdrop${draft.backdrop_url === art.url ? " on" : ""}" data-backdrop="${esc(art.url)}" aria-label="${esc(art.title)}">
              <img src="${posterSrc(art.url, true)}" alt="" loading="lazy" data-img-retry></button>`).join("")}</div>`
            : `<p class="admin-hint" style="margin:0 0 8px;">${esc(t("admin_backdrop_none"))}</p>`}
          <input id="cd-f-backdrop" class="code-input" value="${esc(draft.backdrop_url || "")}"
                 placeholder="${esc(t("admin_backdrop_url"))}" maxlength="2048" autocomplete="off"></div>
        <div class="admin-field"><span>${esc(t("admin_preview_label"))}</span>
          <div id="cd-preview" class="admin-preview"></div></div>` : ""}
        <button class="pbtn primary" id="cd-save">${esc(t("admin_save"))}</button>
      </div>`;
    actions.innerHTML = `<div class="admin-actions">
      <button class="pbtn" id="cd-add">${esc(t("coll_add_film_btn"))}</button>
      ${collectionStatusActions(status).map(([action, key, cls]) =>
        `<button class="pbtn ${cls}" data-status-action="${action}">${esc(t(key))}</button>`).join("")}
      <button class="pbtn danger" id="cd-delete">${esc(t("admin_delete_forever"))}</button>
    </div>${esc(t("admin_reorder_hint")) ? `<p class="admin-hint">${esc(t("admin_reorder_hint"))}</p>` : ""}`;

    // Предпросмотр использует ТОТ ЖЕ рендерер, что и главная, — расхождение
    // между превью и продакшеном невозможно по построению.
    const renderPreview = () => {
      const host = document.getElementById("cd-preview");
      if (!host) return;
      host.replaceChildren(featuredCollectionCard({
        id: collection.id,
        title: document.getElementById("cd-f-title")?.value || collection.title,
        description: document.getElementById("cd-f-desc")?.value || "",
        backdrop: draft.backdrop_url || collection.backdrop,
        cover: collection.cover,
        film_count: (collection.items || []).length,
      }, { preview: true }));
    };
    renderPreview();

    editor.querySelectorAll("[data-display]").forEach(button => button.onclick = () => {
      draft.display_type = button.dataset.display;
      tg?.HapticFeedback?.selectionChanged?.();
      renderEditor();   // перерисовываем: у форматов разные поля
    });
    editor.querySelectorAll("[data-backdrop]").forEach(button => button.onclick = () => {
      draft.backdrop_url = button.dataset.backdrop;
      const input = document.getElementById("cd-f-backdrop");
      if (input) input.value = draft.backdrop_url;
      editor.querySelectorAll("[data-backdrop]").forEach(other =>
        other.classList.toggle("on", other === button));
      renderPreview();
    });
    const backdropInput = document.getElementById("cd-f-backdrop");
    if (backdropInput) backdropInput.oninput = () => {
      draft.backdrop_url = backdropInput.value.trim();
      renderPreview();
    };
    document.getElementById("cd-f-title")?.addEventListener("input", renderPreview);
    document.getElementById("cd-f-desc")?.addEventListener("input", renderPreview);

    document.getElementById("cd-add").onclick = () => showSearch({ type: "collection", id });
    document.getElementById("cd-save").onclick = async (event) => {
      const button = event.currentTarget;
      button.disabled = true;  // защита от двойной отправки
      const updated = await adminCall(`/api/admin/collections/${id}`, {
        method: "PATCH",
        body: JSON.stringify({
          version: collection.version,
          title: document.getElementById("cd-f-title").value,
          description: document.getElementById("cd-f-desc").value,
          display_type: draft.display_type,
          // Пустая строка — это «фона нет», а не «не трогать»: шлём null.
          backdrop_url: draft.backdrop_url || null,
        }),
      });
      button.disabled = false;
      if (!updated) return;
      collection = { ...collection, ...updated };
      document.getElementById("cd-title").textContent = collection.title;
      tg?.HapticFeedback?.notificationOccurred?.("success");
      renderEditor();
    };
    actions.querySelectorAll("[data-status-action]").forEach(button => button.onclick = async () => {
      button.disabled = true;
      const updated = await adminCall(
        `/api/admin/collections/${id}/${button.dataset.statusAction}`,
        { method: "POST", body: JSON.stringify({ version: collection.version }) });
      button.disabled = false;
      if (!updated) return;
      collection = { ...collection, ...updated };
      tg?.HapticFeedback?.notificationOccurred?.("success");
      renderEditor();
    });
    document.getElementById("cd-delete").onclick = () => {
      tg.showConfirm(t("coll_delete_confirm", collection.title), async ok => {
        if (!ok) return;
        if (await adminCall(`/api/admin/collections/${id}`, { method: "DELETE" })) {
          setActiveTab("home"); showHome();
        }
      });
    };
  };

  renderEditor();
  renderItems();
}

// Единая обработка админских мутаций: 403 гасит режим, 409 объясняет конфликт,
// остальное показывает текст ошибки. Возвращает данные или null.
async function adminCall(path, options) {
  try {
    return await api(path, options) || true;
  } catch (error) {
    if (error.status === 403) { AdminMode.revoked(); return null; }
    if (error.status === 409) { tg?.showAlert?.(t("admin_conflict")); return null; }
    tg?.showAlert?.(String(error.message || t("load_err")));
    return null;
  }
}

// ── Личные списки ─────────────────────────────────────────────────────────────
const STATUS_MAP = { want: "want_to_watch", watched: "watched" };
// Сортировка экрана «Смотрел». Ключи совпадают с бэкендом (get_user_films).
// Значение хранится локально, чтобы выбор жил, пока пользователь в приложении.
let _watchedSort = null;
function watchedSort() {
  if (_watchedSort == null) {
    try { _watchedSort = localStorage.getItem("watchedSort") || "new"; } catch (e) { _watchedSort = "new"; }
  }
  return _watchedSort;
}
const _SORT_STAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5l2.6 5.3 5.9.9-4.2 4.1 1 5.8L12 16.9 6.7 19.6l1-5.8-4.2-4.1 5.9-.9Z"/></svg>';
const _SORT_CLOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
const _SORT_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4.5 4.5L19 7"/></svg>';
function watchedSortOptions() {
  return [
    { k: "best", label: t("sort_best"), icon: _SORT_STAR },
    { k: "new", label: t("sort_new"), icon: _SORT_CLOCK },
    { k: "old", label: t("sort_old"), icon: _SORT_CLOCK },
    { k: "worst", label: t("sort_worst"), icon: _SORT_STAR },
  ];
}
function openSortMenu() {
  const active = watchedSort();
  const sheet = document.createElement("div");
  sheet.className = "sort-sheet";
  sheet.innerHTML = `<div class="sort-backdrop"></div><div class="sort-card" role="menu">${
    watchedSortOptions().map(o => `<button class="sort-item${o.k === active ? " on" : ""}" role="menuitemradio" aria-checked="${o.k === active}" data-k="${o.k}">
      <span class="sort-ic">${o.icon}</span><span class="sort-lbl">${esc(o.label)}</span>
      <span class="sort-check">${o.k === active ? _SORT_CHECK : ""}</span></button>`).join("")}</div>`;
  document.body.appendChild(sheet);
  requestAnimationFrame(() => sheet.classList.add("in"));
  const close = () => { sheet.classList.remove("in"); setTimeout(() => sheet.remove(), 180); };
  sheet.querySelector(".sort-backdrop").onclick = close;
  sheet.querySelectorAll(".sort-item").forEach(b => b.onclick = () => {
    const k = b.dataset.k;
    if (k !== _watchedSort) {
      _watchedSort = k;
      try { localStorage.setItem("watchedSort", k); } catch (e) {}
      tg?.HapticFeedback?.selectionChanged?.();
      loadList("watched");  // обновляем только сетку, без перезагрузки экрана
    }
    close();
  });
}

async function showList(tab) {
  unwireDetailScroll();
  window.scrollTo(0, 0);
  const title = tab === "want" ? t("list_want") : t("list_watched");
  const action = tab === "watched"
    ? `<button class="page-head-action" id="sort-btn" aria-label="${esc(t("sort_title"))}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M7 12h10M10 18h4"/></svg></button>`
    : "";
  screen.innerHTML = `<div class="page-head"><h1>${esc(title)}</h1>${action}</div><div id="list">${skeletonGrid(6)}</div>`;
  if (tab === "watched") document.getElementById("sort-btn").onclick = () => openSortMenu();
  await loadList(tab);
}

async function loadList(tab) {
  const el = document.getElementById("list");
  if (!el) return;
  el.dataset.statusFilter = STATUS_MAP[tab];  // метка для точечного апдейта карточек при возврате
  el.innerHTML = skeletonGrid(6);
  const pageSize = 30;
  const sortQ = tab === "watched" ? `&sort=${watchedSort()}` : "";
  try {
    const { items, total } = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=${pageSize}${sortQ}`);
    if (!items.length) {
      el.innerHTML = tab === "want" ? emptyState("🔖", t("want_empty_t"), t("want_empty_s"))
        : emptyState("✅", t("watched_empty_t"), t("watched_empty_s"));
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
          const next = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=${pageSize}&offset=${offset}${sortQ}`);
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
  document.getElementById("d-back-preview").onclick = () => closeDetailThen(returnFromDetail);
}

async function showDetail(id, preview = null) {
  unwireDetailScroll();
  _detailFilm = null; _detailBaseline = null;  // актуальные ставит renderDetail после загрузки
  if (preview) renderDetailPreview(preview);
  else {
    screen.innerHTML = `<div class="detail-v2">
      <div class="d-backdrop sk"></div>
      <div class="d-body"><div class="d-poster-wrap"><div class="d-poster sk"></div></div>
        <div class="sk sk-line wide"></div><div class="sk sk-line"></div></div>
      <div class="d-floatctrls" style="position:fixed;top:0;left:0;right:0;padding:calc(10px + env(safe-area-inset-top)) 14px 0;z-index:41;">${backBtn()}</div>
    </div>`;
    wireBack(() => closeDetailThen(returnFromDetail));
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
  // Запоминаем фильм и его исходное состояние — при возврате точечно обновим карточку,
  // если оценка/статус изменились.
  _detailFilm = m;
  _detailBaseline = { status: m.status, my_rating: m.my_rating };
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

  const back = () => closeDetailThen(returnFromDetail);
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
  // Возврат зависит от того, откуда пришли: редактор восстанавливаем по сессии
  // (введённое название и остальные поля переживают этот переход).
  wireBack(() => {
    if (mode?.type === "collection-editor") showCollectionEditor({ sessionId: mode.sessionId });
    else if (mode) showCollectionDetail(mode.id);
    else { setActiveTab("home"); showHome(); }
  });
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
              if (mode?.type === "collection-editor") {
                // Возвращаем фильм В СЕССИЮ редактора, а не в базу: подборка
                // может ещё не существовать, а введённые поля должны уцелеть.
                const restored = CollectionEditor.restore(mode.sessionId);
                if (!restored) { tg.showAlert(t("load_err")); return; }
                // Резолвим фильм в общий каталог, НЕ добавляя его в личный
                // список куратора.
                const film = await adminCall("/api/admin/films/resolve", { method: "POST",
                  body: JSON.stringify({ src: it.src, ref: it.ref }) });
                if (!film?.id) return;
                const merged = mergeUniqueFilms(restored.films, [{
                  id: film.id, title: film.title || it.title, year: film.year || it.year,
                  poster_url: film.poster_url || it.poster || it.poster_url,
                  backdrop_url: film.backdrop_url || null,
                }]);
                if (merged.length === restored.films.length) {
                  tg.showAlert(t("coll_already_in"), () => showCollectionEditor({ sessionId: mode.sessionId }));
                  return;
                }
                CollectionEditor.setFilms(merged);
                CollectionEditor.persistNow();
                tg.HapticFeedback?.notificationOccurred("success");
                showCollectionEditor({ sessionId: mode.sessionId });
              } else if (mode) {
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
  const viewGeneration = ++_viewGeneration;
  unwireDetailScroll();
  window.scrollTo(0, 0);
  screen.innerHTML = `<div class="page-head"><h1>${esc(t("stats_title"))}</h1><button class="page-head-action" data-stats-settings type="button" aria-label="${esc(t("settings_title"))}">${settingsSvg()}</button></div><div id="stats"><div class="empty"><div class="empty-sub">${esc(t("calc"))}</div></div></div>`;
  const box = document.getElementById("stats");

  // 1. Пара — приоритетно, первым блоком.
  let partner = { status: "none" }, pstats = null;
  try { partner = await api("/api/partner"); } catch (e) {}
  if (viewGeneration !== _viewGeneration) return;
  let pairStatsFailed = false;
  if (partner.status === "paired") {
    try { pstats = await api("/api/partner/stats"); }
    catch (e) { pairStatsFailed = true; }
    if (viewGeneration !== _viewGeneration) return;
  }

  // 2. Личная статистика за всё время.
  const s = await api("/api/stats");
  if (viewGeneration !== _viewGeneration) return;
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

// Шестерёнка берётся из общего реестра — тот же stroke и цвет, что у иконок
// статистических плиток и нижней навигации.
function settingsSvg() { return appIcon("settings"); }

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
      <section class="settings-section" aria-labelledby="settings-pair-title"><h2 id="settings-pair-title">${esc(t("settings_pair"))}</h2><div class="settings-card settings-pair-card">${settingsPairHTML(partner, partnerFailed)}</div></section>
      ${AdminMode.isCapable() ? `<section class="settings-section" aria-labelledby="settings-admin-title"><h2 id="settings-admin-title">${esc(t("admin_section"))}</h2><div class="settings-card">
        ${settingsRow({ title: t("admin_mode_row"), subtitle: t("admin_mode_hint"), action: `<button class="settings-toggle" data-settings-admin type="button" role="switch" aria-checked="${AdminMode.enabled}" aria-label="${esc(t("admin_mode_row"))}"></button>` })}
      </div></section>` : ""}
      <p class="settings-attribution">${esc(t("settings_attribution"))}</p>`;

    const telegramToggle = page.querySelector("[data-settings-telegram]");
    if (telegramToggle) telegramToggle.onclick = async () => {
      telegramToggle.disabled = true;
      try { serverSettings = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ telegram_notifications: !serverSettings.telegram_enabled }) }); }
      catch (_) { tg?.showAlert?.(t("settings_pair_load_error")); }
      render();
    };
    const adminToggle = page.querySelector("[data-settings-admin]");
    if (adminToggle) adminToggle.onclick = async () => {
      // Перед включением перепроверяем права на сервере: если роль отозвали,
      // тумблер не включится и раздел исчезнет при следующем рендере.
      if (!AdminMode.enabled) await AdminMode.refresh();
      if (!AdminMode.isCapable()) { render(); return; }
      AdminMode.setEnabled(!AdminMode.enabled);
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
    ${favoriteCards ? `<section class="pair-showcase pair-favorites-showcase"><header class="pair-showcase-head"><div><h2>${esc(t("pair_common_favorites"))}</h2><p>${esc(t("pair_common_favorites_hint"))}</p></div></header><div class="pair-favorites-rail" aria-label="${esc(t("pair_common_favorites"))}">${favoriteCards}</div></section>` : ""}
    ${differenceCards ? `<section class="pair-showcase pair-differences-showcase"><header class="pair-showcase-head"><div><h2>${esc(t("pair_disagreements"))}</h2><p>${esc(t("pair_disagreements_hint"))}</p></div></header><div class="pair-differences-rail" aria-label="${esc(t("pair_disagreements"))}">${differenceCards}</div></section>` : ""}
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
  const avatar = userAvatarHTML({ photo_url: photo }, name);
  const topGenre = cap(s.top_genres_pct?.[0]?.[0] || "");
  const topRating = (s.rating_dist || []).reduce((best, count, index, values) => count > values[best] ? index : best, 0) + 1;
  const taste = s.rating_dist?.some(v => v > 0) ? t("stats_taste_hint", topGenre, topRating) : t("stats_profile_sub");
  return `<section class="profile-hero">
    <div class="profile-main">${avatar}<div class="profile-copy"><div class="profile-name">${esc(name)}</div>
      <div class="profile-handle">${username ? `@${esc(username)}` : esc(t("stats_profile_sub"))}</div><p class="profile-taste">${esc(taste)}</p></div></div>
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
  thriller: { color: "#a855f7", glow: "rgba(168,85,247,.22)", icon: '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.5"/><path d="M12 2.5V5M12 19v2.5M2.5 12H5M19 12h2.5"/>' },
  mystery: { color: "#14b8a6", glow: "rgba(20,184,166,.22)", icon: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/>' },
  romance: { color: "#ec4899", glow: "rgba(236,72,153,.22)", icon: '<path d="M20 8.5C20 5.5 16.7 4 14.5 6.2L12 8.7 9.5 6.2C7.3 4 4 5.5 4 8.5c0 4.7 8 9.5 8 9.5s8-4.8 8-9.5Z"/>' },
  comedy: { color: "#fbbf24", glow: "rgba(251,191,36,.22)", icon: '<circle cx="12" cy="12" r="8"/><path d="M8 10h.01M16 10h.01M8.5 14c2 2 5 2 7 0"/>' },
  scifi: { color: "#38bdf8", glow: "rgba(56,189,248,.22)", icon: '<circle cx="12" cy="12" r="5.5"/><path d="M4.5 9.5C2.6 10.6 1.7 11.7 2 12.6c.5 1.5 4.9 1.6 10 .2 5-1.4 8.7-3.7 8.2-5.2-.3-.9-1.8-1.2-4-1"/>' },
  fantasy: { color: "#4ade80", glow: "rgba(74,222,128,.22)", icon: '<path d="M5 17c1-6 3-9 7-9 3 0 4.5 1.6 5.5 4.5M7 11 5 7l4 1 1-4 2 4M10 17c2-1 4.5-1 7 .5M17 6l1.5-2M19 9l2-1"/>' },
  adventure: { color: "#fb923c", glow: "rgba(251,146,60,.22)", icon: '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2.2 5-4.8 2 2.2-5z"/>' },
  horror: { color: "#fb5a62", glow: "rgba(251,90,98,.22)", icon: '<path d="M7 19v-3l-2-2 2-2V8l2-3h6l2 3v4l2 2-2 2v3l-2 2H9z"/><path d="M9 11h.01M15 11h.01M10 15h4"/>' },
  crime: { color: "#22c7be", glow: "rgba(34,199,190,.22)", icon: '<path d="M6 17v-3a6 6 0 0 1 12 0v3"/><rect x="4" y="17" width="16" height="3.5" rx="1"/><path d="M12 3.5V6M5.8 5.8 7.4 7.4M18.2 5.8 16.6 7.4"/>' },
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

function genreRow([genre, pct, count]) {
  const percentage = Math.max(0, Math.min(100, Number(pct) || 0));
  const countValue = Number.isFinite(Number(count)) ? Number(count) : null;
  const visual = genreVisual(genre);
  return `<div class="genre-stat-row" style="--genre-color:${visual.color};--genre-glow:${visual.glow}">
    <div class="genre-stat-name">${esc(cap(genre))}</div>
    <div class="genre-stat-track"><i style="width:${Math.max(percentage ? 7 : 0, percentage)}%"></i></div>
    <div class="genre-stat-value"><b>${percentage}%</b>${countValue != null ? `<small>${esc(t("stats_films", countValue))}</small>` : ""}</div>
  </div>`;
}

function genreStatsCard(items, expanded) {
  return `<section class="genre-stats-card">
    <div class="genre-stats-head"><div><h2>${esc(t("chart_genres"))}</h2><p>${esc(t("stats_genres_hint"))}</p></div></div>
    <div class="genre-stat-list">${statsList("genres", items, genreRow, expanded)}</div>
  </section>`;
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
  return `<section class="people-stats-section people-stats-${type}"><header class="people-stats-head"><div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div></header>${body}</section>`;
}

function personalStatsHTML(s, scope = "me", expanded = { genres: false }) {
  const hours = Math.floor(s.total_runtime_min / 60);
  const intro = "";
  const tiles = `<div class="stats-grid">
    ${statTile("eye", s.watched, t(scope === "pair" ? "tile_shared_watched" : "tile_watched"))}${statTile("heart", s.want, t(scope === "pair" ? "tile_shared_want" : "tile_want"))}
    ${statTile("star", s.avg_rating ?? "—", t("tile_avg"))}${statTile("clock", hours, t("tile_hours"))}</div>`;
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
  return intro + tiles + hist + genres + actors + directors;
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

// icon — ключ общего реестра ICONS (не сырой SVG), чтобы у всех плиток был один
// stroke и один цвет; иконка вторична по отношению к числу.
function statTile(icon, value, label) { return `<div class="tile">${icon ? `<span class="tile-icon" aria-hidden="true">${appIcon(icon)}</span>` : ""}<div class="tile-val">${esc(value)}</div><div class="tile-label">${esc(label)}</div></div>`; }
function chartCard(title, inner) { return `<div class="chart-card"><div class="chart-title">${esc(title)}</div>${inner}</div>`; }

// ── Навигация ─────────────────────────────────────────────────────────────────
function backBtn() { return `<button class="back" aria-label="${esc(t("back"))}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 6-6 6 6 6"/></svg></button>`; }
function wireBack(fn) { const b = screen.querySelector(".back"); if (b) b.onclick = fn; }
function setActiveTab(tab) {
  document.querySelectorAll("#tabbar .tab").forEach(b => {
    const active = b.dataset.tab === tab;
    b.classList.toggle("active", active);
    b.setAttribute("aria-current", active ? "page" : "false");
  });
}
function route(tab) {
  // Every tab switch invalidates unfinished async work from the previous tab.
  ++_viewGeneration;
  resetNavStack();
  if (tab === "home") showHome();
  else if (tab === "stats") showStats();
  else if (tab === "pick") showPicker();
  else showList(tab);
}
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
      // iOS dispatches a synthetic click after pointerup. Suppress exactly that
      // click, rather than blocking this tab for 250 ms: a user may legitimately
      // switch away and immediately return.
      btn.dataset.suppressNextClick = "1";
      activateTab(btn);
    });
    // Keyboard, desktop and WebViews without Pointer Events still work through click.
    btn.addEventListener("click", () => {
      if (btn.dataset.suppressNextClick === "1") {
        delete btn.dataset.suppressNextClick;
        return;
      }
      activateTab(btn);
    });
  });
  wireTabbarAutoHide();
  applyTabLabels();
  (async () => {
    try {
      me = await api("/api/me");
      bindNotificationRefresh();
      // Возможности спрашиваем у сервера отдельно и не блокируем ими старт:
      // обычный пользователь получит пустой набор и ничего админского не увидит.
      AdminMode.refresh().catch(() => {});
      const sp = tg?.initDataUnsafe?.start_param || "";
      if (sp.startsWith("inv_")) showAcceptInvite(sp);  // пришли по инвайт-ссылке
      else if (sp.startsWith("film_")) openDetail(+sp.slice(5));  // пришли по ссылке «Поделиться» фильмом
      else showHome();
    } catch (e) {
      screen.innerHTML = emptyState("⛔", esc(e.message), t("auth_err_s"));
    }
  })();
}
