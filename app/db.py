import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

SEED_BIZ_DATE = "2026-11-14"
SEED_POLICY = "拒写"


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_db(db_path: str | None = None, write_lock: bool = False):
    """逐次连接。write_lock=True 时立刻 BEGIN IMMEDIATE，跨进程写串行化。

    跨日登记靠这把库级写锁保证：两名灌数员同时登记时，后到者只能在前者
    提交后再判断一次命运，不会双双把对象写进昨日批。
    """
    conn = connect(db_path)
    if write_lock:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if conn.in_transaction:
            if write_lock:
                conn.execute("COMMIT")
            else:
                conn.commit()
    except Exception:
        # 服务层可能已显式 COMMIT 过命运标记（拒写/挪批失败），那时不再回滚
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_code TEXT NOT NULL,
  zone_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('收集中','已闭合','已作废')),
  closed_object_count INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(zone_code, biz_date)
);

CREATE TABLE IF NOT EXISTS landing_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  biz_date TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('已登记','已闭合','已作废')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(batch_id, object_key),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS close_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL UNIQUE,
  object_count INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS visibility_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  closed_batch_count INTEGER NOT NULL,
  open_batch_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  orphan_object_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  mixed_object_count INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS day_cut_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  current_biz_date TEXT NOT NULL,
  policy TEXT NOT NULL CHECK (policy IN ('拒写','挪批')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS day_cut_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  biz_date TEXT NOT NULL,
  source_batch_id INTEGER NOT NULL,
  target_batch_id INTEGER,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL DEFAULT '',
  fate TEXT NOT NULL CHECK (fate IN ('拒写','挪批')),
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(source_batch_id) REFERENCES landing_batches(id),
  FOREIGN KEY(target_batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS day_cut_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  biz_date TEXT NOT NULL,
  policy TEXT NOT NULL,
  created_batch_count INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS day_cut_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  mixed_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(landing_objects)")}
    if "biz_date" not in cols:
        conn.execute(
            "ALTER TABLE landing_objects ADD COLUMN biz_date TEXT NOT NULL DEFAULT ''"
        )
    # 旧库对象按其所属批次业务日回填，边界核对才能看见混日
    conn.execute(
        "UPDATE landing_objects SET biz_date=("
        "  SELECT biz_date FROM landing_batches WHERE id=landing_objects.batch_id"
        ") WHERE biz_date IS NULL OR biz_date=''"
    )
    report_cols = {r["name"] for r in conn.execute("PRAGMA table_info(visibility_reports)")}
    if "mixed_object_count" not in report_cols:
        conn.execute(
            "ALTER TABLE visibility_reports ADD COLUMN mixed_object_count INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "INSERT OR IGNORE INTO day_cut_settings(id, current_biz_date, policy) "
        "VALUES (1, ?, ?)",
        (SEED_BIZ_DATE, SEED_POLICY),
    )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for zone, name, biz, status in SEED_BATCHES:
            conn.execute(
                "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status, closed_object_count) "
                "VALUES (?,?,?,?,?)",
                (zone, name, biz, status, 2 if status == "已闭合" else None),
            )
        closed = conn.execute(
            "SELECT id, biz_date FROM landing_batches WHERE status='已闭合'"
        ).fetchone()
        if closed:
            bid = closed["id"]
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, biz_date, status) "
                "VALUES (?,?,?,?,?,?)",
                (bid, "part-000.parquet", "hash-silver-a", 1024, closed["biz_date"], "已闭合"),
            )
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, biz_date, status) "
                "VALUES (?,?,?,?,?,?)",
                (bid, "part-001.parquet", "hash-silver-b", 2048, closed["biz_date"], "已闭合"),
            )
            conn.execute(
                "INSERT INTO close_runs(batch_id, object_count, ok, detail) VALUES (?,?,1,?)",
                (bid, 2, "种子闭合"),
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('seeded','1')"
        )
