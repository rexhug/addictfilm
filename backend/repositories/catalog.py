"""Users, the shared film catalogue and its genre taxonomy.

One module because it is one write path: get_or_create_film dedupes against the
catalogue while upsert_user records who asked for it, and a split would put a
transaction boundary through the middle of a single insert.

list_genres lives here rather than with discovery despite the section marker it
sat under. It reads film_genres and owns the cache that get_or_create_film
invalidates; leaving it behind would mean one module rebinding another module's
cache through `global`, which stops working the moment they are separate files.
"""
import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta

import aiosqlite
import cast as cast_model
import db_session
from db_helpers import (  # noqa: F401
    _GENRE_CANON,
    _GENRE_SHOWCASE,
    _MEDIA_EPISODE,
    _MEDIA_MOVIE,
    _MEDIA_SERIES,
    _MEDIA_SHORT,
    _canon_genre,
    _catalog_search_text,
    _genre_query_aliases,
    _now,
    _person_key,
    _portrait_entries,
    _portrait_urls,
    _split_genres,
    _split_people,
)

logger = logging.getLogger(__name__)


_GENRES_CACHE_TTL_SECONDS = max(30, int(os.getenv("GENRES_CACHE_TTL_SEC", "300")))
_genres_cache: tuple[float, list[dict]] | None = None


# Telegram и так ограничивает startapp этим набором; обрезаем на своей стороне,
# чтобы в аналитику не попало ничего, кроме метки кампании.
_ACQUISITION_PARAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


ACQUISITION_DIRECT = "direct"
ACQUISITION_PAIR_INVITE = "pair_invite"
ACQUISITION_SHARED_FILM = "shared_film"
ACQUISITION_CAMPAIGN = "campaign"
ACQUISITION_UNKNOWN = "unknown"


def normalize_acquisition(start_param: object) -> tuple[str, str | None]:
    """Разложить startapp на бакет и (только для кампаний) сырое значение.

    Токен приглашения в пару НЕ сохраняем: `inv_<token>` — действующий секрет,
    и аналитике достаточно знать, что человек пришёл по приглашению. Ссылка на
    фильм тоже ничего не добавляет как строка: это идентификатор, а не канал.
    """
    value = str(start_param or "").strip()
    if not value:
        return ACQUISITION_DIRECT, None
    if value.startswith("inv_"):
        return ACQUISITION_PAIR_INVITE, None
    if value.startswith("film_"):
        return ACQUISITION_SHARED_FILM, None
    if _ACQUISITION_PARAM_RE.match(value):
        return ACQUISITION_CAMPAIGN, value
    # Формат, которого Telegram выдать не может: считаем каналом неизвестного
    # происхождения и не храним само значение.
    return ACQUISITION_CAMPAIGN, None


# ── Пользователи ─────────────────────────────────────────────────────────────
async def get_user(user_id: int) -> dict | None:
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, first_name, username, photo_url FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_role(user_id: int) -> str | None:
    """Роль из БД (назначается вручную админом) — отдельно от ADMIN_USER_IDS (main.py)."""
    async with db_session.connect() as db:
        cur = await db.execute("SELECT role FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def upsert_user(user: dict, start_param: object = None) -> None:
    """Регистрация/обновление любого пользователя Telegram без write-amplification.

    InitData приходит с каждым API запросом. Профиль обновляем сразу при реальном
    изменении, а last_seen — максимум раз в 15 минут, поэтому обычная навигация
    больше не создаёт коммит в Postgres на каждый GET.
    """
    now = _now()
    last_seen_cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    source, param = normalize_acquisition(start_param)
    async with db_session.connect() as db:
        # Источник пишется ТОЛЬКО в INSERT: ветка обновления его не трогает.
        # Атрибуция по первому касанию — иначе достаточно один раз открыть
        # приложение по ссылке «Поделиться», чтобы задним числом переписать
        # канал, по которому человек пришёл в продукт.
        await db.execute(
            """
            INSERT INTO users (id, first_name, username, photo_url, created_at, last_seen,
                               acquisition_source, acquisition_param)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                username   = excluded.username,
                photo_url  = COALESCE(excluded.photo_url, users.photo_url),
                last_seen  = excluded.last_seen
            WHERE users.first_name IS DISTINCT FROM excluded.first_name
               OR users.username IS DISTINCT FROM excluded.username
               OR (excluded.photo_url IS NOT NULL AND users.photo_url IS DISTINCT FROM excluded.photo_url)
               OR users.last_seen IS NULL OR users.last_seen < ?
            """,
            (user.get("id"), user.get("first_name"), user.get("username"), user.get("photo_url"), now, now,
             source, param, last_seen_cutoff),
        )
        await db.commit()


async def user_analytics(now: datetime | None = None) -> dict:
    """Сводка по аудитории. Границы считаются в UTC.

    Сутки — календарные UTC, а не «последние 24 часа»: это разные числа, и
    отчёт обязан говорить, какое из них показывает. Часовой пояс уходит в
    ответ, чтобы цифру «сегодня» нельзя было прочитать неоднозначно.

    Сравнение дат — строковое: все метки пишутся `datetime.now(UTC).isoformat()`
    в одном формате, поэтому лексикографический порядок совпадает с временным
    и работает одинаково в SQLite и Postgres.
    """
    moment = now or datetime.now(UTC)
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_ago = (moment - timedelta(days=7)).isoformat()
    month_ago = (moment - timedelta(days=30)).isoformat()
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        totals = dict(await (await db.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS today,
                   COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS week,
                   COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS month,
                   COALESCE(SUM(CASE WHEN last_seen  >= ? THEN 1 ELSE 0 END), 0) AS active_week
            FROM users
            """,
            (day_start, week_ago, month_ago, week_ago),
        )).fetchone())
        # Группируем по голой колонке, без единого параметра в ключе.
        # COALESCE(acquisition_source, ?) в GROUP BY ломался на Postgres дважды:
        # сначала GroupingError (драйвер делает из каждого ? свой параметр, и
        # выражения в SELECT и GROUP BY перестают совпадать), потом
        # IndeterminateDatatypeError (тип параметра внутри ключа группировки
        # вывести не из чего). SQLite прощал оба варианта. Подстановка «unknown»
        # — задача Python, а не SQL.
        sources = [dict(row) for row in await (await db.execute(
            "SELECT acquisition_source AS source, COUNT(*) AS users "
            "FROM users GROUP BY acquisition_source"
        )).fetchall()]
    return {
        "total_users": int(totals["total"] or 0),
        "new_users": {
            "today": int(totals["today"] or 0),
            "7d": int(totals["week"] or 0),
            "30d": int(totals["month"] or 0),
        },
        "active_users_7d": int(totals["active_week"] or 0),
        # NULL — пользователи, зарегистрированные до появления атрибуции. Они
        # «unknown», а не «direct»: приписать им прямой заход значило бы
        # выдумать данные. Порядок задаём здесь же, чтобы он не зависел от того,
        # как конкретный движок вернул группы.
        "acquisition": sorted(
            (
                {
                    "source": str(row["source"] or ACQUISITION_UNKNOWN),
                    "users": int(row["users"]),
                }
                for row in sources
            ),
            key=lambda item: (-item["users"], item["source"]),
        ),
        "timezone": "UTC",
        "generated_at": moment.isoformat(),
    }


# ── Каталог фильмов ──────────────────────────────────────────────────────────
def _prefer_catalog_value(current: str | None, incoming: str | None) -> str | None:
    """Fill a genuinely missing catalog field without overwriting good data."""
    return current if str(current or "").strip() else incoming


def _prefer_richer_catalog_list(current: str | None, incoming: str | None,
                                splitter) -> str | None:
    """Keep the richer credit/genre list while avoiding provider churn."""
    current_items = splitter(current)
    incoming_items = splitter(incoming)
    if len(incoming_items) > len(current_items):
        return incoming
    return current if current_items else incoming


def _film_identity_key(imdb_id: str, kp_id: str | None, title: str,
                       year: str | None, poster_url: str | None) -> str:
    """Stable lock key shared by IMDb and Kinopoisk representations.

    The exact provider poster is the safest bridge when one provider lookup is
    temporarily missing the other provider's id.  The digest keeps lock rows
    compact even for long signed image URLs.
    """
    if poster_url and title:
        raw = "|".join((
            "poster",
            " ".join(title.split()).casefold(),
            str(year or "").strip(),
            str(poster_url).strip(),
        ))
    elif kp_id:
        raw = f"kp|{kp_id}"
    else:
        raw = f"imdb|{imdb_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _backfill_catalog_search_text() -> None:
    """Give legacy catalog entries the same local-search behaviour as new ones."""
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id, title, title_original, actors, directors, imdb_id, kp_id FROM films "
            "WHERE search_text IS NULL OR search_text = ''")).fetchall()
        for row in rows:
            await db.execute(
                "UPDATE films SET search_text = ? WHERE id = ?",
                (_catalog_search_text(row["title"], row["title_original"], row["actors"],
                                      row["directors"], row["imdb_id"], row["kp_id"]), row["id"]))
        await db.commit()


async def _backfill_film_genres() -> None:
    """Populate the indexable genre projection for legacy catalog rows."""
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT id, genres FROM films")).fetchall()
        for row in rows:
            for genre in _split_genres(row["genres"]):
                await db.execute(
                    "INSERT INTO film_genres (film_id, genre) VALUES (?,?) ON CONFLICT DO NOTHING",
                    (row["id"], genre),
                )
        await db.commit()


async def _set_film_genres(db, film_id: int, genres: str | None) -> None:
    await db.execute("DELETE FROM film_genres WHERE film_id = ?", (film_id,))
    for genre in _split_genres(genres):
        await db.execute("INSERT INTO film_genres (film_id, genre) VALUES (?,?)", (film_id, genre))


def _invalidate_genres_cache() -> None:
    global _genres_cache
    _genres_cache = None


def _catalog_item(row) -> dict | None:
    """Catalog row in the normalized shape expected by the search UI."""
    imdb_id = str(row["imdb_id"] or "")
    kp_id = str(row["kp_id"] or "")
    if imdb_id.startswith("tt"):
        src, ref = "i", imdb_id
    elif kp_id:
        src, ref = "k", kp_id
    elif imdb_id.startswith("kp_"):
        src, ref = "k", imdb_id[3:]
    else:
        return None
    rating = row["imdb_rating"] or row["kp_rating"]
    return {
        "catalog_id": row["id"],
        "src": src,
        "ref": ref,
        "imdb_id": row["imdb_id"],
        "kp_id": row["kp_id"],
        "title": row["title"],
        "title_original": row["title_original"],
        "year": row["year"] or "?",
        "poster": row["poster_url"],
        "poster_url": row["poster_url"],
        "rating": rating,
        "imdb_rating": row["imdb_rating"],
        "kp_rating": row["kp_rating"],
        "imdb_votes": row["imdb_votes"],
        "actors": row["actors"],
        "directors": row["directors"],
        "genres": row["genres"],
        "type": row["media_type"] or "movie",
    }


async def search_catalog(query: str, limit: int = 8) -> list[dict]:
    """Search the permanent catalog before spending an external API request."""
    terms = [term.casefold() for term in query.split() if term]
    # Years are ranking signals, not part of the indexed search_text.
    if len(terms) > 1 and re.fullmatch(r"(?:19|20)\d{2}", terms[-1]):
        terms.pop()
    if not terms:
        return []

    def like(term: str) -> str:
        return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    clauses = ["search_text LIKE ? ESCAPE '\\'" for _ in terms]
    params = [like(term) for term in terms]
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        safe_prefix = terms[0] if re.fullmatch(r"[\w-]+", terms[0], flags=re.UNICODE) else None
        rows = []
        # Most title searches start with the movie title. GLOB prefix can use
        # SQLite's idx_films_search_text; PostgreSQL uses LIKE + text_pattern_ops.
        # Fallback below preserves actor/infix discovery.
        if safe_prefix:
            prefix_clause = "search_text LIKE ? ESCAPE '\\'" if db_session.uses_postgres() else "search_text GLOB ?"
            prefix_value = safe_prefix + ("%" if db_session.uses_postgres() else "*")
            cur = await db.execute(
                "SELECT * FROM films WHERE " + prefix_clause + " AND " + " AND ".join(clauses) + " "
                "ORDER BY id DESC LIMIT ?",
                (prefix_value, *params, max(1, min(limit, 60))),
            )
            rows = await cur.fetchall()
        candidate_limit = max(1, min(limit, 60))
        if len(rows) < candidate_limit:
            existing_ids = [row["id"] for row in rows]
            exclusion = "" if not existing_ids else " AND id NOT IN (" + ",".join("?" for _ in existing_ids) + ")"
            cur = await db.execute(
                "SELECT * FROM films WHERE " + " AND ".join(clauses) + exclusion + " "
                "ORDER BY id DESC LIMIT ?",
                (*params, *existing_ids, candidate_limit - len(rows)),
            )
            rows += await cur.fetchall()
    return [item for row in rows if (item := _catalog_item(row)) is not None]


async def get_film_id_by_source(src: str, ref: str) -> int | None:
    """Find a cataloged film by the identifier sent back to the search UI."""
    async with db_session.connect() as db:
        if src == "i":
            cur = await db.execute("SELECT id FROM films WHERE imdb_id = ?", (ref,))
        else:
            cur = await db.execute(
                "SELECT id FROM films WHERE kp_id = ? OR imdb_id = ? LIMIT 1",
                (ref, f"kp_{ref}"))
        row = await cur.fetchone()
        return row[0] if row else None


async def get_catalog_item_by_source(src: str, ref: str) -> dict | None:
    """Exact catalog lookup for a direct IMDb/Kinopoisk search."""
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        if src == "i":
            cur = await db.execute("SELECT * FROM films WHERE imdb_id = ?", (ref,))
        else:
            cur = await db.execute(
                "SELECT * FROM films WHERE kp_id = ? OR imdb_id = ? LIMIT 1",
                (ref, f"kp_{ref}"))
        row = await cur.fetchone()
    return _catalog_item(row) if row else None


async def _enqueue_enrichment(db, film_id: int, *, priority: int) -> None:
    """Поставить фильм в очередь обогащения, не роняя сохранение каталога.

    Импорт локальный: enrichment зависит от database, и модульный импорт наверху
    замкнул бы цикл. Отказ очереди здесь намеренно проглатывается — фильм должен
    сохраниться в любом случае, а пропущенное задание доберёт согласование
    (enrichment/reconciliation.py).
    """
    try:
        from enrichment import queue as enrichment_queue
        from enrichment.taxonomy import MOVIE_FEATURE_VERSION, MOVIE_TAXONOMY_VERSION
        await enrichment_queue.enqueue(
            film_id, feature_version=MOVIE_FEATURE_VERSION,
            taxonomy_version=MOVIE_TAXONOMY_VERSION, priority=priority, connection=db)
    except Exception:
        logger.warning("не удалось поставить задание обогащения для film_id=%s", film_id,
                       exc_info=True)


async def get_or_create_film(
    imdb_id: str, title: str, year: str | None = None, genres: str | None = None,
    runtime: str | None = None, imdb_rating: str | None = None, imdb_votes: str | None = None,
    plot: str | None = None, poster_url: str | None = None, title_original: str | None = None,
    kp_rating: str | None = None, directors: str | None = None, actors: str | None = None,
    backdrop_url: str | None = None, age_rating: str | None = None, actors_photos: str | None = None,
    directors_photos: str | None = None, kp_id: str | None = None,
    media_type: str | None = None, cast_json: str | None = None,
) -> int:
    """Возвращает id фильма в общем каталоге, создавая запись при первом появлении
    (dedup по imdb_id/kp_id). Идемпотентно — один фильм на всех пользователей."""
    imdb_id = str(imdb_id or "").strip()
    if imdb_id.lower().startswith(("tt", "kp_")):
        imdb_id = imdb_id.lower()
    kp_id = str(kp_id).strip() if kp_id else None
    if not kp_id and imdb_id.startswith("kp_"):
        kp_id = imdb_id[3:]
    # Тип записи от провайдера: 'unknown' не сохраняем, чтобы бэкфил потом мог
    # доопределить его, а не считал колонку уже заполненной.
    media_type = media_type if media_type in (_MEDIA_MOVIE, _MEDIA_SERIES, _MEDIA_EPISODE, _MEDIA_SHORT) else None
    search_text = _catalog_search_text(title, title_original, actors, directors, imdb_id, kp_id)
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        # INSERT + no-op UPDATE is a portable transaction-scoped mutex.  It
        # prevents two web machines from inserting IMDb and kp aliases of the
        # same poster at the same time.
        identity_key = _film_identity_key(imdb_id, kp_id, title, year, poster_url)
        await db.execute(
            "INSERT INTO film_identity_locks (identity_key, created_at) VALUES (?, ?) "
            "ON CONFLICT(identity_key) DO NOTHING",
            (identity_key, _now()),
        )
        await db.execute(
            "UPDATE film_identity_locks SET created_at = created_at WHERE identity_key = ?",
            (identity_key,),
        )
        cur = await db.execute(
            "SELECT id, imdb_id, title, title_original, year, genres, directors, actors, runtime, "
            "imdb_rating, kp_rating, imdb_votes, plot, kp_id FROM films "
            "WHERE imdb_id = ? OR (kp_id IS NOT NULL AND kp_id = ?) LIMIT 1",
            (imdb_id, kp_id))
        row = await cur.fetchone()
        # Conservative bridge for provider outages: the same exact non-empty
        # poster + title + year may join a synthetic kp alias to one IMDb row.
        # Two candidates are treated as ambiguous and never auto-merged.
        if row is None and poster_url and title:
            fingerprint_cur = await db.execute(
                "SELECT id, imdb_id, title, title_original, year, genres, directors, actors, runtime, "
                "imdb_rating, kp_rating, imdb_votes, plot, kp_id FROM films "
                "WHERE LOWER(TRIM(title)) = LOWER(TRIM(?)) "
                "AND COALESCE(year, '') = COALESCE(?, '') AND poster_url = ? "
                "AND (imdb_id LIKE 'kp_%' OR ? LIKE 'kp_%') ORDER BY id LIMIT 2",
                (title, year, poster_url, imdb_id),
            )
            candidates = await fingerprint_cur.fetchall()
            row = candidates[0] if len(candidates) == 1 else None
        if row:
            current_imdb_id = str(row["imdb_id"] or "")
            merged_imdb_id = (
                imdb_id
                if imdb_id.startswith("tt") and current_imdb_id.startswith("kp_")
                else current_imdb_id
            )
            merged_title = _prefer_catalog_value(row["title"], title)
            merged_original = _prefer_catalog_value(row["title_original"], title_original)
            merged_year = _prefer_catalog_value(row["year"], year)
            merged_genres = _prefer_richer_catalog_list(row["genres"], genres, _split_genres)
            merged_directors = _prefer_richer_catalog_list(row["directors"], directors, _split_people)
            merged_actors = _prefer_richer_catalog_list(row["actors"], actors, _split_people)
            merged_runtime = _prefer_catalog_value(row["runtime"], runtime)
            merged_imdb_rating = _prefer_catalog_value(row["imdb_rating"], imdb_rating)
            merged_kp_rating = _prefer_catalog_value(row["kp_rating"], kp_rating)
            merged_imdb_votes = _prefer_catalog_value(row["imdb_votes"], imdb_votes)
            merged_plot = _prefer_catalog_value(row["plot"], plot)
            merged_kp_id = _prefer_catalog_value(row["kp_id"], kp_id)
            merged_search_text = _catalog_search_text(
                merged_title, merged_original, merged_actors, merged_directors,
                merged_imdb_id, merged_kp_id,
            )
            await db.execute(
                "UPDATE films SET imdb_id = ?, title = ?, title_original = ?, year = ?, genres = ?, directors = ?, actors = ?, "
                "runtime = ?, imdb_rating = ?, kp_rating = ?, imdb_votes = ?, plot = ?, kp_id = ?, search_text = ?, "
                "poster_url = COALESCE(NULLIF(poster_url, ''), ?), "
                "backdrop_url = COALESCE(NULLIF(backdrop_url, ''), ?), "
                "age_rating = COALESCE(age_rating, ?), "
                "actors_photos = COALESCE(NULLIF(actors_photos, ''), ?), "
                "cast_json = COALESCE(NULLIF(cast_json, ''), ?), "
                "directors_photos = COALESCE(NULLIF(directors_photos, ''), ?), "
                "media_type = COALESCE(NULLIF(media_type, ''), ?), "
                "media_type_source = CASE WHEN COALESCE(NULLIF(media_type, ''), ?) IS NULL "
                "THEN media_type_source ELSE COALESCE(media_type_source, 'provider') END "
                "WHERE id = ?",
                (merged_imdb_id, merged_title, merged_original, merged_year, merged_genres, merged_directors, merged_actors,
                 merged_runtime, merged_imdb_rating, merged_kp_rating, merged_imdb_votes, merged_plot,
                 merged_kp_id, merged_search_text, poster_url, backdrop_url, age_rating, actors_photos,
                 cast_json,
                 directors_photos, media_type, media_type, row["id"]))
            if merged_genres != row["genres"]:
                await _set_film_genres(db, row["id"], merged_genres)
            # Задание на пересчёт признаков ставится в ТОЙ ЖЕ транзакции: иначе
            # запись обновится, а обогащение потеряется на перезапуске процесса.
            await _enqueue_enrichment(db, row["id"], priority=0)
            await db.commit()
            if merged_genres != row["genres"]:
                _invalidate_genres_cache()
            return row["id"]
        # RETURNING id вместо lastrowid — портируемо (SQLite 3.35+ и Postgres одинаково).
        cur = await db.execute(
            """
            INSERT INTO films
                (imdb_id, kp_id, title, title_original, year, genres, directors, actors, runtime,
                 imdb_rating, kp_rating, imdb_votes, plot, poster_url, backdrop_url, age_rating,
                 actors_photos, cast_json, directors_photos, search_text, media_type, media_type_source, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (imdb_id, kp_id, title, title_original, year, genres, directors, actors, runtime,
             imdb_rating, kp_rating, imdb_votes, plot, poster_url, backdrop_url, age_rating,
             actors_photos, cast_json, directors_photos, search_text, media_type,
             "provider" if media_type else None, _now()),
        )
        inserted = await cur.fetchone()
        if inserted:
            await _set_film_genres(db, inserted["id"], genres)
            await _enqueue_enrichment(db, inserted["id"], priority=10)
        await db.commit()
        if inserted:
            _invalidate_genres_cache()
            return inserted["id"]
        # Гонка: кто-то вставил параллельно — перечитываем.
        cur = await db.execute(
            "SELECT id FROM films WHERE imdb_id = ? OR (kp_id IS NOT NULL AND kp_id = ?) LIMIT 1",
            (imdb_id, kp_id))
        winner = await cur.fetchone()
        if winner is None and poster_url and title:
            cur = await db.execute(
                "SELECT id FROM films WHERE LOWER(TRIM(title)) = LOWER(TRIM(?)) "
                "AND COALESCE(year, '') = COALESCE(?, '') AND poster_url = ? "
                "AND (imdb_id LIKE 'kp_%' OR ? LIKE 'kp_%') ORDER BY id LIMIT 1",
                (title, year, poster_url, imdb_id),
            )
            winner = await cur.fetchone()
        if winner is None:
            raise RuntimeError(
                f"catalog insert lost without an identity winner: imdb={imdb_id!r}, kp={kp_id!r}"
            )
        return winner["id"]


async def get_film(film_id: int) -> dict | None:
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM films WHERE id = ?", (film_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


def decode_film_cast(value: str | None) -> list[dict]:
    return cast_model.decode_cast(value)


async def update_film_cast(
    film_id: int,
    canonical_cast: list[dict],
    *,
    checked_at: str | None = None,
    kp_id: str | None = None,
) -> bool:
    """Atomically keep canonical and legacy cast representations in one order."""
    normalized = cast_model.normalize_cast(canonical_cast)
    cast_json = (
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        if normalized else None
    )
    actor_names = ", ".join(member["name"] for member in normalized[:8]) or None
    legacy_photos = (
        json.dumps(
            cast_model.legacy_actor_photos(normalized),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if normalized else None
    )
    timestamp = checked_at or _now()
    async with db_session.connect() as db:
        cursor = await db.execute(
            """
            UPDATE films
            SET cast_json = ?, cast_checked_at = ?, actors = ?,
                actors_photos = ?, actor_photos_checked_at = ?,
                kp_id = COALESCE(NULLIF(kp_id, ''), NULLIF(?, ''))
            WHERE id = ?
            """,
            (
                cast_json, timestamp, actor_names, legacy_photos, timestamp,
                str(kp_id or "").strip(), film_id,
            ),
        )
        await db.commit()
        return cursor.rowcount > 0


async def films_for_cast_backfill(
    *, film_id: int | None = None, limit: int = 20,
) -> list[dict]:
    """Bounded catalog candidates for the maintenance command."""
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        if film_id is not None:
            cur = await db.execute("SELECT * FROM films WHERE id = ?", (film_id,))
        else:
            cur = await db.execute(
                "SELECT * FROM films ORDER BY "
                "CASE WHEN cast_checked_at IS NULL THEN 0 ELSE 1 END, id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )
        return [dict(row) for row in await cur.fetchall()]


async def set_film_artwork(imdb_id: str, backdrop_url: str | None,
                           age_rating: str | None = None) -> bool:
    """Записать найденный backdrop для уже существующего фильма.

    Не затираем ранее сохранённые данные: обогащение вызывается лениво при
    открытии карточки фильма и должно быть безопасным при повторных запросах.
    """
    if not imdb_id or not backdrop_url:
        return False
    async with db_session.connect() as db:
        cur = await db.execute(
            "UPDATE films SET backdrop_url = ?, age_rating = COALESCE(age_rating, ?) "
            "WHERE imdb_id = ? AND (backdrop_url IS NULL OR backdrop_url = '')",
            (backdrop_url, age_rating, imdb_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def mark_film_artwork_checked(imdb_id: str, backdrop_url: str | None,
                                    age_rating: str | None = None) -> bool:
    """Persist a completed artwork lookup, including a legitimate empty result.

    Without this marker, films that simply have no backdrop would hit Kinopoisk
    every time someone opens the detail page.
    """
    async with db_session.connect() as db:
        cur = await db.execute(
            "UPDATE films SET backdrop_url = COALESCE(NULLIF(backdrop_url, ''), ?), "
            "age_rating = COALESCE(age_rating, ?), artwork_checked_at = ? "
            "WHERE imdb_id = ?",
            (backdrop_url, age_rating, _now(), imdb_id))
        await db.commit()
        return cur.rowcount > 0


async def mark_film_visuals_checked(
    imdb_id: str, poster_url: str | None, backdrop_url: str | None,
    age_rating: str | None = None,
) -> bool:
    """Persist one completed visual-asset lookup, including empty fields.

    A provider result without an image is valid data. Both timestamps prevent a
    film with no poster/backdrop from making the same provider call on every
    detail-page visit.
    """
    now = _now()
    async with db_session.connect() as db:
        cur = await db.execute(
            "UPDATE films SET poster_url = COALESCE(NULLIF(poster_url, ''), ?), "
            "backdrop_url = COALESCE(NULLIF(backdrop_url, ''), ?), "
            "age_rating = COALESCE(age_rating, ?), poster_checked_at = ?, artwork_checked_at = ? "
            "WHERE imdb_id = ?",
            (poster_url, backdrop_url, age_rating, now, now, imdb_id))
        await db.commit()
        return cur.rowcount > 0


async def get_film_id_by_imdb(imdb_id: str) -> int | None:
    """id фильма в каталоге по imdb_id, если уже есть (без вставки). Нужно, чтобы
    /api/add не ходил в внешние API за фильмом, который уже в каталоге."""
    async with db_session.connect() as db:
        cur = await db.execute("SELECT id FROM films WHERE imdb_id = ?", (imdb_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def films_missing_poster(limit: int = 200) -> list[dict]:
    """Фильмы каталога без постера (poster_url NULL/пусто) — для бекфила.
    Возвращает [{id, imdb_id, title, title_original, year}] (последние два нужны
    для добора по названию). Свежие сверху (чаще всего нужны первыми)."""
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, imdb_id, title, title_original, year FROM films "
            "WHERE poster_url IS NULL OR poster_url = '' "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def set_film_poster(imdb_id: str, poster_url: str) -> bool:
    """Проставить постер фильму по imdb_id. Только если его ещё нет (бекфил не
    затирает уже найденное). True = запись обновлена."""
    async with db_session.connect() as db:
        cur = await db.execute(
            "UPDATE films SET poster_url = ? "
            "WHERE imdb_id = ? AND (poster_url IS NULL OR poster_url = '')",
            (poster_url, imdb_id))
        await db.commit()
        return cur.rowcount > 0


async def films_with_omdb_poster(limit: int = 200) -> list[dict]:
    """Фильмы с постером Amazon/OMDb (заметно хуже качеством, чем кинопоиск) —
    кандидаты на апгрейд. Возвращает [{id, imdb_id, title, title_original, year}]."""
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, imdb_id, title, title_original, year FROM films "
            "WHERE poster_url LIKE '%media-amazon.com%' "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def upgrade_film_poster(imdb_id: str, poster_url: str) -> bool:
    """Заменить постер фильма на лучший (используется только когда нашли
    kinopoisk-версию взамен OMDb) — в отличие от set_film_poster, ЗАТИРАЕТ
    существующее значение. True = запись обновлена."""
    async with db_session.connect() as db:
        cur = await db.execute(
            "UPDATE films SET poster_url = ? WHERE imdb_id = ?", (poster_url, imdb_id))
        await db.commit()
        return cur.rowcount > 0


async def list_genres() -> list[dict]:
    """Жанры, присутствующие в каталоге, по убыванию частоты: [{name, count}]."""
    global _genres_cache
    now = datetime.now(UTC).timestamp()
    if _genres_cache and _genres_cache[0] > now:
        return [dict(item) for item in _genres_cache[1]]
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT genre AS name, COUNT(*) AS count FROM film_genres GROUP BY genre"
        )
        rows = await cur.fetchall()
    # Мёржим языковые дубли одного жанра под русский канон, суммируем счётчики
    # и показываем только кураторскую витрину (см. _GENRE_SHOWCASE).
    merged: dict[str, int] = {}
    for row in rows:
        merged[_canon_genre(row["name"])] = merged.get(_canon_genre(row["name"]), 0) + row["count"]
    items = [{"name": name, "count": count} for name, count in
             sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
             if name in _GENRE_SHOWCASE]
    _genres_cache = (now + _GENRES_CACHE_TTL_SECONDS, items)
    return [dict(item) for item in items]
