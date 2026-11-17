# cc-manifest-lab

CC_Codex 二期（26.11）**数据工程**产品线：**湖区落地清单台**（Litestar + Preact CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**湖区落地批次 → 对象指纹登记 → 清单闭合 → 可见性对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（落地清单 / 对象登记闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、Litestar、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，侧栏），靛青竖纹 + 亮青强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 湖区落地批次列表（收集中 / 已闭合）
2. 向批次登记对象键与内容指纹
3. 闭合清单（对象数固化，拒绝空批）
4. 可见性对账报告（闭合批 vs 开放批 vs 孤儿对象）

## 日切与跨日登记（本版新增）

统一业务日（主体日）存在 `day_cut_settings`，每条批次与每笔对象都带 `biz_date`。
批次业务日早于主体日即“昨档”，往昨档登记只能落一种命运，不并存：

| 策略 | 行为 | 昨档对象数 |
|------|------|-----------|
| **拒写** | 整笔请求拒写、不落库，仅留一条拒写标记；返回 409 短句 | 不增 |
| **挪批** | 同一事务里找/建今日空批 → 原子挪入并登记 → 提交 | 不增 |

- 登记入口按当下策略**一次判定**：`拒写` 分支提交标记后抛错；`挪批` 分支失败（今日批已闭合 / 今日批同键）同样只留挪批标记，对象绝不偷偷留在昨档。
- 所有写路径均在 `BEGIN IMMEDIATE` 库级写锁内，两名灌数员跨日抢写被串行化，不会双成功把昨档写漂。
- **边界核对**：对象 `biz_date` 必须等于所属批次 `biz_date`，否则判混日。闭合清单与日切推进都先核对，混日即停（409）。
- 日切设置页可见拒写/挪批标记、日切记录、混日核对结果；收集列表标“昨档”，详情页标“混日”，登记卡片提示该批当前命运。

接口：`GET /api/day-cut`、`POST /api/day-cut/settings`、`POST /api/day-cut/advance`、`POST /api/day-cut/check`。

本机库核对与增量日切：

```bash
python -m app.cli status
python -m app.cli check
python -m app.cli advance 2026-11-16
```

预留切口（未做，供后续功能题）：清单换版生效窗、闭合作废回开放、批量失败闭互斥、跨区依赖冻结、清单回灌等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-manifest-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6900/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6900**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
