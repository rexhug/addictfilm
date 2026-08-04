"""The moderation audit trail.

Two functions that sat at the end of the editorial-collections section while
belonging to neither collections nor discovery: they write and read one table,
admin_audit_log, and their only caller is routers/admin.py. Keeping them with
the collections would mean the admin router imports a collections repository to
read an audit log.
"""
import json
import logging

import aiosqlite
import db_session
from db_helpers import _now

logger = logging.getLogger(__name__)


# ── Журнал действий администратора ────────────────────────────────────────────
async def write_audit(actor_id: int, actor_role: str, action: str, entity_type: str,
                      entity_id: str | int, details: dict | None = None) -> None:
    """Append-only журнал админских мутаций. Секреты/initData сюда не попадают —
    вызывающий передаёт только редактируемые поля."""
    async with db_session.connect() as db:
        await db.execute(
            "INSERT INTO admin_audit_log (actor_id, actor_role, action, entity_type, entity_id, "
            "details, created_at) VALUES (?,?,?,?,?,?,?)",
            (actor_id, actor_role, action, entity_type, str(entity_id),
             json.dumps(details, ensure_ascii=False) if details else None, _now()))
        await db.commit()


async def list_audit_log(limit: int = 50) -> list[dict]:
    async with db_session.connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT actor_id, actor_role, action, entity_type, entity_id, details, created_at "
            "FROM admin_audit_log ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]
