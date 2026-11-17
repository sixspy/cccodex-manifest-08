"""本机库核对与增量日切的小命令行入口。

    python -m app.cli status          # 看当前业务日 / 策略 / 混日数
    python -m app.cli check           # 落一条边界核对记录
    python -m app.cli advance 2026-11-16   # 增量日切，为各湖区开新日空批
"""

import json
import sys

from app.db import init_db
from app.services import advance_day, get_settings, run_check
from app.services.errors import ServiceError


def main(argv: list[str]) -> int:
    init_db()
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        if cmd == "status":
            cut = get_settings()
            print(json.dumps({k: cut[k] for k in ("settings", "stale_batches")}, ensure_ascii=False, indent=2))
            print(f"跨日标记 {len(cut['events'])} 条")
            return 0
        if cmd == "check":
            result = run_check(argv[2] if len(argv) > 2 else "")
            print(result["detail"])
            for row in result["mixed_objects"]:
                print(
                    f"  混日 对象#{row['id']} {row['object_key']} 归属 {row['object_biz_date']} "
                    f"批次#{row['batch_id']} 业务日 {row['batch_biz_date']}"
                )
            return 0
        if cmd == "advance":
            if len(argv) < 3:
                print("用法：python -m app.cli advance YYYY-MM-DD")
                return 2
            result = advance_day(argv[2])
            print(result["run"]["detail"])
            print("新开批次 " + ", ".join("#%d" % i for i in result["created"]))
            return 0
        print(f"未知命令：{cmd}")
        return 2
    except ServiceError as exc:
        print(f"失败：{exc.message}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
