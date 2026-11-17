from sqlite3 import IntegrityError

from app.db import get_db
from app.services.daycut import (
    current_biz_date,
    find_or_create_target,
    record_event,
)
from app.services.errors import ServiceError

__all__ = ["ServiceError", "list_objects", "register_object"]


def list_objects(batch_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM landing_objects WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _insert_object(conn, batch_id: int, biz_date: str, key: str, digest: str, size: int) -> dict:
    try:
        cur = conn.execute(
            "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, biz_date, status) "
            "VALUES (?,?,?,?,?, '已登记')",
            (batch_id, key, digest, size, biz_date),
        )
    except IntegrityError as exc:
        raise ServiceError("同批对象键已存在", 409) from exc
    row = conn.execute(
        "SELECT * FROM landing_objects WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def register_object(
    batch_id: int,
    object_key: str,
    content_hash: str,
    byte_size: int,
    db_path: str | None = None,
) -> dict:
    key = (object_key or "").strip()
    digest = (content_hash or "").strip()
    if not key or not digest:
        raise ServiceError("对象键与内容指纹不能为空")
    if byte_size < 0:
        raise ServiceError("字节数不能为负")
    # 库级写锁：跨日抢写只能串行判断命运
    with get_db(db_path, write_lock=True) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        batch = dict(batch)
        if batch["status"] != "收集中":
            raise ServiceError("仅收集中批次可登记对象", 409)

        today = current_biz_date(conn)
        if batch["biz_date"] >= today:
            _assert_key_free(conn, batch_id, key)
            return _insert_object(
                conn, batch_id, batch["biz_date"], key, digest, int(byte_size)
            )

        policy = conn.execute(
            "SELECT policy FROM day_cut_settings WHERE id=1"
        ).fetchone()["policy"]
        if policy == "拒写":
            _reject(conn, batch, today, key, digest)
        return _move(conn, batch, today, key, digest, int(byte_size))


def _assert_key_free(conn, batch_id: int, key: str) -> None:
    exists = conn.execute(
        "SELECT id FROM landing_objects WHERE batch_id=? AND object_key=?",
        (batch_id, key),
    ).fetchone()
    if exists:
        raise ServiceError("同批对象键已存在", 409)


def _reject(conn, batch: dict, today: str, key: str, digest: str) -> None:
    """拒写：整笔不落库，只落一条拒写标记。标记先提交，再抛短句。"""
    event = record_event(
        conn,
        biz_date=today,
        source_batch_id=batch["id"],
        object_key=key,
        content_hash=digest,
        fate="拒写",
        detail=f"跨日登记拒绝：批次业务日 {batch['biz_date']} < 当前 {today}",
    )
    conn.execute("COMMIT")
    raise ServiceError(
        "跨日拒写：批次业务日已过，对象未落库",
        409,
        {"fate": "拒写", "event": event, "current_biz_date": today},
    )


def _move(conn, batch: dict, today: str, key: str, digest: str, size: int) -> dict:
    """挪批：同事务新建/复用今日批并登记，昨档对象数不增。"""
    try:
        target = find_or_create_target(conn, batch, today)
    except ServiceError as exc:
        # 挪不过去也是挪批这一边的失败：落标记、不写对象
        event = record_event(
            conn,
            biz_date=today,
            source_batch_id=batch["id"],
            object_key=key,
            content_hash=digest,
            fate="挪批",
            detail=f"跨日挪批失败：{exc.message}",
        )
        conn.execute("COMMIT")
        raise ServiceError(
            exc.message, exc.status_code, {"fate": "挪批", "event": event}
        ) from exc
    exists = conn.execute(
        "SELECT id FROM landing_objects WHERE batch_id=? AND object_key=?",
        (target["id"], key),
    ).fetchone()
    if exists:
        event = record_event(
            conn,
            biz_date=today,
            source_batch_id=batch["id"],
            target_batch_id=target["id"],
            object_key=key,
            content_hash=digest,
            fate="挪批",
            detail=f"跨日挪批失败：今日批 #{target['id']} 已有同键对象",
        )
        conn.execute("COMMIT")
        raise ServiceError(
            "跨日挪批失败：今日批已有同键对象",
            409,
            {"fate": "挪批", "event": event},
        )
    obj = _insert_object(conn, target["id"], today, key, digest, size)
    event = record_event(
        conn,
        biz_date=today,
        source_batch_id=batch["id"],
        target_batch_id=target["id"],
        object_key=key,
        content_hash=digest,
        fate="挪批",
        detail=f"跨日挪批：#{batch['id']} → #{target['id']}",
    )
    return {**obj, "fate": "挪批", "event": event, "target_batch_id": target["id"]}
