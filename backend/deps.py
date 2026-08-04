"""Request-scoped dependencies shared by every router.

Extracted from main.py so that a router module can guard its endpoints without
importing the application object: main imports routers, routers import deps,
and nothing imports main.  That is the whole reason this module exists.
"""
import secrets

import database as db
import ratelimit
import user_touch
from auth import extract_start_param, validate_init_data
from config import ADMIN_TOKEN, ADMIN_USER_IDS, BOT_TOKEN
from fastapi import Depends, Header, HTTPException


# ── Авторизация: каждый запрос несёт initData в заголовке ────────────────────
async def current_user(x_init_data: str = Header(default="")) -> dict:
    """Проверяем подпись Telegram и регистрируем/обновляем пользователя.
    Белого списка нет — публичный продукт: пускаем любого с валидной подписью."""
    user = validate_init_data(x_init_data, BOT_TOKEN)
    user_id = user.get("id") if user else None
    if isinstance(user_id, bool):
        user_id = None
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        user_id = None
    if not user or not user_id or user_id < 0:
        raise HTTPException(status_code=401, detail="Не авторизован")
    user["id"] = user_id
    # Параметр берём из той же строки, подпись которой только что проверена, и
    # передаём отдельным аргументом: в профиль он не подмешивается.
    if user_touch.should_persist(user):
        await db.upsert_user(user, start_param=extract_start_param(x_init_data))
    return user



async def _effective_role(user_id: int) -> str | None:
    """"admin" — если id в ADMIN_USER_IDS (bootstrap-секрет, всегда есть, не зависит
    от БД); иначе — роль из users.role ("editor"/"admin", назначается вручную)."""
    if user_id in ADMIN_USER_IDS:
        return "admin"
    return await db.get_user_role(user_id)


async def throttled_mutation(user: dict = Depends(current_user)) -> dict:
    """429 instead of an unbounded write path.  Applied at the boundary, not
    inside handlers, so a new write endpoint is guarded by adding one Depends.
    """
    if not ratelimit.allow_mutation(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто, попробуйте через минуту")
    return user


async def throttled_quiz(user: dict = Depends(current_user)) -> dict:
    """Quiz calls INSERT a session row each, so they get their own budget."""
    if not ratelimit.allow_quiz(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто, попробуйте через минуту")
    return user


async def require_editor(user: dict = Depends(current_user)) -> dict:
    """Гейт для in-app админки подборок — по самому Telegram-юзеру (не по токену,
    как require_admin ниже — тот для curl/скриптов обслуживания)."""
    role = await _effective_role(user["id"])
    if role not in ("editor", "admin"):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


async def require_admin_user(user: dict = Depends(current_user)) -> dict:
    """Строже require_editor: только роль "admin".

    Редактор ведёт подборки, и знать размер и источники аудитории ему для этого
    не нужно. Отдельный гейт, а не проверка внутри обработчика: иначе следующий
    аналитический эндпоинт легко повесят на editor по невнимательности.
    """
    if await _effective_role(user["id"]) != "admin":
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user



# ── Обслуживание (админ по ADMIN_TOKEN) ──────────────────────────────────────
def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Гейт для служебных эндпоинтов. Без заданного ADMIN_TOKEN — выключены (404)."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")
    if not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Не авторизован")
