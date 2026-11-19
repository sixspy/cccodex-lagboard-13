# 源到达SLA

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 源到达SLA |
| 目录 | `06-lagboard` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10823 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

## 水位与到达绑定（本仓口径：甲径）

> 定稿卡：`docs/水位绑定卡.json`，封签 `docs/水位绑定卡.seal`。卡面或封签不符、口径非甲、非当日专册，一律拒写并在对账账册记「指针偏了」。指针当天不得更换绑定专册。

- **甲径（已采用）**：推进水位与挂到达**同库一次提交**，任一步失败则两侧一并撤销（`_bind_arrival`，`BEGIN IMMEDIATE` … `COMMIT`/`ROLLBACK`）。
- 甲径与乙径（先写草稿再原子晋升）不得混用。
- **严禁**只改水位数字样式，也禁止回滚后残留到达；`POST /api/watermarks/promote` 一律 400 拒写。
- **指针锁**：调度与对账同推同一源水位须先占 `BEGIN IMMEDIATE`；后交只记「水位绑定争用」，不推进。
- **五处对齐**：水位卡片、到达列表、源目录、补采附页、对账账册。`GET /api/watermarks/verify` 无空窗才为 `ok`。
- 水位看板挂在**现有时限台**的第 4 个页签，不另起指针台；登录沿用同一会话（`lagboard_token`）。

### 测试（先红后改）

- `tests/test_watermark_binding_failure.py`：到达失败 / 水位失败两侧一并撤销；只改数字拒写；改卡拒写；换册拒写；正常晋升五处对齐。
- `tests/test_watermark_race.py`：双路抢推只成一路，后交留「争用」痕。
- 空窗复现脚本：`python3 -I ops/repro/repro_watermark_gap.py`（水位已走到达未挂、回滚后残留到达）。

## 避开

别做成纯监控大盘；须有写路径与状态机

## 互斥切口候选

- 超SLA冻结 vs 降级只读
- 补齐整源解冻 vs 按业务日窗口解冻
- 只认源时钟 vs 双时钟取严

## 启动（一键）

`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10823/  
健康检查：GET http://127.0.0.1:10823/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
