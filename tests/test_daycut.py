import threading
import time

from app.db import get_db, init_db
from app.services import register_object
from app.services.errors import ServiceError


def _settings(client):
    return client.get("/api/day-cut").json()["settings"]


def _batches(client):
    return client.get("/api/batches").json()["items"]


def _objects(client, bid):
    return client.get(f"/api/batches/{bid}").json()["objects"]


def _batch(client, zone_code, biz_date):
    return next(
        b for b in _batches(client) if b["zone_code"] == zone_code and b["biz_date"] == biz_date
    )


def _advance(client, day):
    r = client.post("/api/day-cut/advance", json={"biz_date": day})
    assert r.status_code == 200, r.json()
    return r.json()


def _register(client, bid, key, digest="h1", size=1):
    return client.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": key, "content_hash": digest, "byte_size": size},
    )


def _set_policy(client, policy, day=None):
    body = {"policy": policy}
    if day:
        body["current_biz_date"] = day
    r = client.post("/api/day-cut/settings", json=body)
    assert r.status_code == 200, r.json()
    return r.json()["setting"]


def test_settings_default_and_policy_validation(authed):
    s = _settings(authed)
    assert s["current_biz_date"] == "2026-11-14"
    assert s["policy"] in ("拒写", "挪批")
    bad = authed.post(
        "/api/day-cut/settings", json={"current_biz_date": "2026-11-15", "policy": "丢弃"}
    )
    assert bad.status_code == 400
    assert "拒写" in bad.json()["error"] and "挪批" in bad.json()["error"]
    back = authed.post("/api/day-cut/settings", json={"policy": "拒写", "current_biz_date": "2026-11-01"})
    assert back.status_code == 400


def test_reject_policy_drops_object_and_leaves_marker(authed):
    _set_policy(authed, "拒写")
    _advance(authed, "2026-11-15")
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")
    before = len(_objects(authed, yesterday["id"]))

    r = _register(authed, yesterday["id"], "late/a.parquet")
    assert r.status_code == 409
    body = r.json()
    assert body["fate"] == "拒写"
    assert len(body["error"]) < 40
    assert "跨日" in body["error"]

    assert len(_objects(authed, yesterday["id"])) == before
    cut = authed.get("/api/day-cut").json()
    assert cut["events"][0]["fate"] == "拒写"
    assert cut["events"][0]["object_key"] == "late/a.parquet"
    # 拒写不能凭空生出一笔今日批对象
    today = _batch(authed, "bronze_orders", "2026-11-15")
    assert _objects(authed, today["id"]) == []


def test_move_policy_atomically_moves_to_today(authed):
    _set_policy(authed, "挪批")
    _advance(authed, "2026-11-15")
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")
    before = len(_objects(authed, yesterday["id"]))

    r = _register(authed, yesterday["id"], "late/b.parquet", "hb", 7)
    assert r.status_code == 200
    obj = r.json()["object"]
    assert obj["fate"] == "挪批"
    assert obj["biz_date"] == "2026-11-15"
    assert obj["batch_id"] != yesterday["id"]

    # 昨档不膨胀，今日批多一条
    assert len(_objects(authed, yesterday["id"])) == before
    today = _batch(authed, "bronze_orders", "2026-11-15")
    today_objs = _objects(authed, today["id"])
    assert [o["object_key"] for o in today_objs] == ["late/b.parquet"]
    assert obj["batch_id"] == today["id"]

    cut = authed.get("/api/day-cut").json()
    assert cut["events"][0]["fate"] == "挪批"
    assert cut["events"][0]["target_batch_id"] == today["id"]


def test_two_fates_never_coexist(authed):
    """同一笔跨日请求只落一边：挪批成功时不留拒写标记，反之亦然。"""
    _set_policy(authed, "挪批")
    _advance(authed, "2026-11-15")
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")
    _register(authed, yesterday["id"], "late/c.parquet")
    events = authed.get("/api/day-cut").json()["events"]
    assert [e["fate"] for e in events] == ["挪批"]

    _set_policy(authed, "拒写")
    r = _register(authed, yesterday["id"], "late/d.parquet")
    assert r.status_code == 409
    events = authed.get("/api/day-cut").json()["events"]
    assert [e["fate"] for e in events] == ["拒写", "挪批"]
    # 拒写的对象绝不出现在任何批里
    all_keys = []
    for b in _batches(authed):
        all_keys += [o["object_key"] for o in _objects(authed, b["id"])]
    assert "late/d.parquet" not in all_keys


def test_move_policy_reuses_existing_target_batch(authed):
    _set_policy(authed, "挪批")
    _advance(authed, "2026-11-15")
    yesterday = _batch(authed, "bronze_payments", "2026-11-14")
    r1 = _register(authed, yesterday["id"], "late/e1.parquet")
    r2 = _register(authed, yesterday["id"], "late/e2.parquet")
    assert r1.json()["object"]["batch_id"] == r2.json()["object"]["batch_id"]
    zones = [
        b for b in _batches(authed)
        if b["zone_code"] == "bronze_payments" and b["biz_date"] == "2026-11-15"
    ]
    assert len(zones) == 1


def test_move_policy_rejects_duplicate_in_target(authed):
    _set_policy(authed, "挪批")
    _advance(authed, "2026-11-15")
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")
    today = _batch(authed, "bronze_orders", "2026-11-15")
    assert _register(authed, today["id"], "dup.parquet").status_code == 200
    r = _register(authed, yesterday["id"], "dup.parquet")
    assert r.status_code == 409
    assert r.json()["fate"] == "挪批"
    assert len(_objects(authed, yesterday["id"])) == 0


def test_close_blocks_on_mixed_day(authed):
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")
    _register(authed, yesterday["id"], "ok/a.parquet")
    _set_policy(authed, "拒写")
    _advance(authed, "2026-11-15")
    # 人为制造一条混日对象：归属日写成当前业务日
    with get_db() as conn:
        conn.execute(
            "UPDATE landing_objects SET biz_date='2026-11-15' WHERE batch_id=?",
            (yesterday["id"],),
        )
    r = authed.post(f"/api/batches/{yesterday['id']}/close", json={})
    assert r.status_code == 409
    assert "混日" in r.json()["error"]


def test_check_and_visibility_report_count_mixed(authed):
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")
    _register(authed, yesterday["id"], "ok/b.parquet")
    _set_policy(authed, "拒写")
    _advance(authed, "2026-11-15")
    chk = authed.post("/api/day-cut/check", json={}).json()["check"]
    assert chk["mixed_count"] == 0
    with get_db() as conn:
        conn.execute(
            "UPDATE landing_objects SET biz_date='2026-11-15' WHERE batch_id=?",
            (yesterday["id"],),
        )
    chk = authed.post("/api/day-cut/check", json={}).json()["check"]
    assert chk["mixed_count"] == 1
    assert chk["mixed_objects"][0]["object_key"] == "ok/b.parquet"

    rep = authed.post(
        "/api/visibility-reports", json={"as_of_date": "2026-11-15"}
    ).json()["report"]
    assert rep["mixed_object_count"] == 1
    assert "混日 1" in rep["summary"]


def test_advance_is_idempotent_and_forward_only(authed):
    first = _advance(authed, "2026-11-15")
    assert first["setting"]["current_biz_date"] == "2026-11-15"
    again = authed.post("/api/day-cut/advance", json={"biz_date": "2026-11-15"})
    assert again.status_code == 400
    back = authed.post("/api/day-cut/advance", json={"biz_date": "2026-11-14"})
    assert back.status_code == 400
    n = len(_batches(authed))
    second = _advance(authed, "2026-11-16")
    assert len(_batches(authed)) == n + 4  # 四个湖区各开一空批
    assert second["run"]["created_batch_count"] == 4
    # 新增的都是收集中空批
    for b in _batches(authed):
        if b["biz_date"] == "2026-11-16":
            assert b["status"] == "收集中"
            assert _objects(authed, b["id"]) == []


def test_two_writers_across_day_cut_serialize(authed):
    """两名灌数员跨日抢写：库级写锁下不能双成功把昨档写漂。"""
    _set_policy(authed, "挪批")
    _advance(authed, "2026-11-15")
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")
    before = len(_objects(authed, yesterday["id"]))
    today = _batch(authed, "bronze_orders", "2026-11-15")

    def worker(key):
        register_object(yesterday["id"], key, "hz", 3)

    results = []
    errors = []

    def run(key):
        try:
            results.append(worker(key))
        except ServiceError as exc:
            errors.append(exc)
        except Exception as exc:  # noqa: BLE001 - 只关心是否双写成功
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"race/{i}.parquet",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 2
    assert len(_objects(authed, yesterday["id"])) == before
    keys = sorted(o["object_key"] for o in _objects(authed, today["id"]))
    assert keys == ["race/0.parquet", "race/1.parquet"]


def test_move_policy_fails_when_today_batch_closed(authed):
    _set_policy(authed, "挪批")
    old = _batch(authed, "bronze_events", "2026-11-13")
    _register(authed, old["id"], "seed/one.parquet")
    _advance(authed, "2026-11-15")
    target = _batch(authed, "bronze_events", "2026-11-15")
    with get_db() as conn:
        conn.execute(
            "UPDATE landing_batches SET status='已闭合', closed_object_count=1 WHERE id=?",
            (target["id"],),
        )
    r = _register(authed, old["id"], "late/f.parquet")
    assert r.status_code == 409
    assert r.json()["fate"] == "挪批"
    assert "闭合" in r.json()["error"]
    # 挪不过去也不能偷偷留在昨档
    assert "late/f.parquet" not in [o["object_key"] for o in _objects(authed, old["id"])]
    assert _objects(authed, target["id"]) == []


def test_current_day_register_unaffected(authed):
    today = _batch(authed, "bronze_orders", "2026-11-14")
    r = _register(authed, today["id"], "same/a.parquet")
    assert r.status_code == 200
    obj = r.json()["object"]
    assert "fate" not in obj
    assert obj["biz_date"] == "2026-11-14"
    assert authed.get("/api/day-cut").json()["events"] == []


def test_write_lock_serializes_connections(db_path):
    """写锁是真锁：持锁者未提交时，后来者必须等，不能并发进事务。"""
    init_db(db_path)
    started = threading.Event()
    release = threading.Event()
    order = []

    def holder():
        with get_db(db_path, write_lock=True) as conn:
            conn.execute("SELECT COUNT(*) FROM landing_batches").fetchone()
            started.set()
            release.wait(5)

    def waiter():
        started.wait(5)
        t0 = time.monotonic()
        with get_db(db_path, write_lock=True) as conn:
            conn.execute("SELECT COUNT(*) FROM landing_batches").fetchone()
        order.append((time.monotonic() - t0,))

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    started.wait(5)
    time.sleep(0.6)
    release.set()
    t1.join()
    t2.join()
    assert order and order[0][0] >= 0.4, order


def test_cut_and_writers_race_leaves_no_mixed_day(authed, db_path):
    """日切与两名灌数员同时进行：不混日、不丢写、不错写。"""
    _set_policy(authed, "拒写")
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")

    def cut():
        try:
            _advance(authed, "2026-11-15")
        except AssertionError:
            pass

    def writer(i):
        try:
            register_object(yesterday["id"], f"race/{i}.parquet", "hr", 2, db_path)
        except ServiceError:
            pass

    threads = [threading.Thread(target=cut)] + [
        threading.Thread(target=writer, args=(i,)) for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_objs = []
    for b in _batches(authed):
        for o in _objects(authed, b["id"]):
            all_objs.append(o)
    assert all(o["biz_date"] == next(
        b["biz_date"] for b in _batches(authed) if b["id"] == o["batch_id"]
    ) for o in all_objs)
    # 三条写入只可能：成功落昨档、或成功后挪批、或被拒；不存在既没落也没标记
    events = authed.get("/api/day-cut").json()["events"]
    landed = [o for o in all_objs if o["object_key"].startswith("race/")]
    assert len(landed) + len(events) == 3


def test_advance_blocked_when_mixed_day(authed):
    old = _batch(authed, "bronze_orders", "2026-11-14")
    _register(authed, old["id"], "ok/x.parquet")
    with get_db() as conn:
        conn.execute(
            "UPDATE landing_objects SET biz_date='2026-11-15' WHERE batch_id=?",
            (old["id"],),
        )
    r = authed.post("/api/day-cut/advance", json={"biz_date": "2026-11-15"})
    assert r.status_code == 409
    assert "混日" in r.json()["error"]
    assert _settings(authed)["current_biz_date"] == "2026-11-14"


def test_reject_policy_two_writers_still_no_writes(authed):
    _set_policy(authed, "拒写")
    _advance(authed, "2026-11-15")
    yesterday = _batch(authed, "bronze_orders", "2026-11-14")
    before = len(_objects(authed, yesterday["id"]))
    for i in range(2):
        r = _register(authed, yesterday["id"], f"late/{i}.parquet")
        assert r.status_code == 409
    assert len(_objects(authed, yesterday["id"])) == before
    assert len(authed.get("/api/day-cut").json()["events"]) == 2
