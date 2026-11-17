"""日切：业务日推进、跨日登记的两种命运、昨档/今日批对象边界核对。

命运只能落一边，绝不并存：
- 拒写：整笔不落库，只留一条拒写标记；
- 挪批：同一事务里把对象原子挪进今日空批再登记，昨档不增对象。
"""

from datetime import datetime

from app.db import get_db
from app.services.errors import ServiceError

POLICIES = ("拒写", "挪批")


def _settings_row(conn) -> dict:
    row = conn.execute("SELECT * FROM day_cut_settings WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO day_cut_settings(id, current_biz_date, policy) "
            "SELECT 1, COALESCE(MIN(biz_date), '2026-11-14'), '拒写' FROM landing_batches"
        )
        row = conn.execute("SELECT * FROM day_cut_settings WHERE id=1").fetchone()
    return dict(row)


def current_biz_date(conn) -> str:
    return _settings_row(conn)["current_biz_date"]


def get_cut_status(db_path: str | None = None) -> dict:
    """只读当前日切主体设置，给详情页用，不拖全量标记。"""
    with get_db(db_path) as conn:
        return _settings_row(conn)


def get_settings(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        settings = _settings_row(conn)
        events = conn.execute(
            "SELECT * FROM day_cut_events ORDER BY id DESC"
        ).fetchall()
        runs = conn.execute(
            "SELECT * FROM day_cut_runs ORDER BY id DESC"
        ).fetchall()
        checks = conn.execute(
            "SELECT * FROM day_cut_checks ORDER BY id DESC"
        ).fetchall()
        stale = conn.execute(
            "SELECT id, zone_code, zone_name, biz_date, status FROM landing_batches "
            "WHERE biz_date < ? ORDER BY id",
            (settings["current_biz_date"],),
        ).fetchall()
        return {
            "settings": settings,
            "events": [dict(r) for r in events],
            "runs": [dict(r) for r in runs],
            "checks": [dict(r) for r in checks],
            "stale_batches": [dict(r) for r in stale],
        }


def update_settings(
    biz_date: str, policy: str, db_path: str | None = None
) -> dict:
    day = (biz_date or "").strip()
    fate = (policy or "").strip()
    if fate not in POLICIES:
        raise ServiceError("日切策略只能是拒写或挪批")
    with get_db(db_path, write_lock=True) as conn:
        settings = _settings_row(conn)
        if not day:
            day = settings["current_biz_date"]
        if not _looks_like_date(day):
            raise ServiceError("业务日格式应为 YYYY-MM-DD")
        if day < settings["current_biz_date"]:
            raise ServiceError("业务日只能前进，不能回拨")
        conn.execute(
            "UPDATE day_cut_settings SET current_biz_date=?, policy=?, "
            "updated_at=datetime('now') WHERE id=1",
            (day, fate),
        )
        return _settings_row(conn)


def advance_day(biz_date: str, db_path: str | None = None) -> dict:
    """增量任务：推进业务日，并为上一批业务日覆盖的湖区开新日空批。

    同一目标日重复推进会被拒，可反复验证而不会重复开批。
    """
    day = (biz_date or "").strip()
    if not _looks_like_date(day):
        raise ServiceError("业务日格式应为 YYYY-MM-DD")
    with get_db(db_path, write_lock=True) as conn:
        settings = _settings_row(conn)
        if day <= settings["current_biz_date"]:
            raise ServiceError("业务日只能前进")
        # 混日即停：边界不齐时不许再往前切，免得把错事带进新日
        assert_day_boundary(conn)
        zones = conn.execute(
            "SELECT zone_code, zone_name, MAX(biz_date) AS latest "
            "FROM landing_batches GROUP BY zone_code, zone_name"
        ).fetchall()
        created = []
        for zone in zones:
            if zone["latest"] > settings["current_biz_date"]:
                continue
            exists = conn.execute(
                "SELECT id FROM landing_batches WHERE zone_code=? AND biz_date=?",
                (zone["zone_code"], day),
            ).fetchone()
            if exists:
                continue
            cur = conn.execute(
                "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status) "
                "VALUES (?,?,?, '收集中')",
                (zone["zone_code"], zone["zone_name"], day),
            )
            created.append(cur.lastrowid)
        detail = (
            f"日切 {settings['current_biz_date']}→{day}，新开 {len(created)} 批，"
            f"策略 {settings['policy']}"
        )
        conn.execute(
            "UPDATE day_cut_settings SET current_biz_date=?, updated_at=datetime('now') "
            "WHERE id=1",
            (day,),
        )
        cur = conn.execute(
            "INSERT INTO day_cut_runs(biz_date, policy, created_batch_count, detail) "
            "VALUES (?,?,?,?)",
            (day, settings["policy"], len(created), detail),
        )
        run = conn.execute(
            "SELECT * FROM day_cut_runs WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return {"setting": _settings_row(conn), "run": dict(run), "created": created}


def boundary_mismatches(conn, batch_id: int | None = None) -> list[dict]:
    """对象归属日与所属批次业务日不一致即为混日。"""
    sql = (
        "SELECT o.id, o.batch_id, o.object_key, o.biz_date AS object_biz_date, "
        "       b.biz_date AS batch_biz_date, b.zone_code "
        "FROM landing_objects o JOIN landing_batches b ON b.id=o.batch_id "
        "WHERE o.biz_date != b.biz_date"
    )
    params: tuple = ()
    if batch_id is not None:
        sql += " AND o.batch_id=?"
        params = (batch_id,)
    rows = conn.execute(sql + " ORDER BY o.id", params).fetchall()
    return [dict(r) for r in rows]


def assert_day_boundary(conn, batch_id: int | None = None) -> None:
    mixed = boundary_mismatches(conn, batch_id)
    if mixed:
        raise ServiceError(
            "混日拦截：昨档与今日批对象边界不一致", 409, {"mixed_count": len(mixed)}
        )


def run_check(as_of_date: str, db_path: str | None = None) -> dict:
    day = (as_of_date or "").strip()
    with get_db(db_path, write_lock=True) as conn:
        settings = _settings_row(conn)
        if not day:
            day = settings["current_biz_date"]
        mixed = boundary_mismatches(conn)
        total = conn.execute("SELECT COUNT(*) AS c FROM landing_objects").fetchone()["c"]
        detail = (
            f"截至 {day}：主体业务日 {settings['current_biz_date']}，"
            f"对象 {total}，混日 {len(mixed)}"
        )
        cur = conn.execute(
            "INSERT INTO day_cut_checks(as_of_date, mixed_count, object_count, detail) "
            "VALUES (?,?,?,?)",
            (day, len(mixed), total, detail),
        )
        row = conn.execute(
            "SELECT * FROM day_cut_checks WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return {**dict(row), "mixed_objects": mixed}


def record_event(
    conn,
    *,
    biz_date: str,
    source_batch_id: int,
    object_key: str,
    content_hash: str,
    fate: str,
    target_batch_id: int | None = None,
    detail: str = "",
) -> dict:
    cur = conn.execute(
        "INSERT INTO day_cut_events("
        "biz_date, source_batch_id, target_batch_id, object_key, content_hash, fate, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            biz_date,
            source_batch_id,
            target_batch_id,
            object_key,
            content_hash,
            fate,
            detail,
        ),
    )
    row = conn.execute(
        "SELECT * FROM day_cut_events WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def find_or_create_target(conn, source_batch: dict, biz_date: str) -> dict:
    """同事务内找今日空批，没有就建；已闭合/已作废不算可用。"""
    row = conn.execute(
        "SELECT * FROM landing_batches WHERE zone_code=? AND biz_date=?",
        (source_batch["zone_code"], biz_date),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status) "
            "VALUES (?,?,?, '收集中')",
            (source_batch["zone_code"], source_batch["zone_name"], biz_date),
        )
        row = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (cur.lastrowid,)
        ).fetchone()
    target = dict(row)
    if target["status"] != "收集中":
        raise ServiceError(
            f"跨日挪批失败：今日批已{target['status'][1:]}", 409
        )
    return target


def _looks_like_date(value: str) -> bool:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True
