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
| 接口 | http://127.0.0.1:10813 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 超限处置（写路径）

超限源同时亮「冻结」与「降级只读」两牌、目录态与策略页对不上，属缺陷。本仓处置口径：

- **乙路径**：先写处置意图影子表，再条件更新输出态与处置册；中断可续推（`POST /api/dispositions/resume`）。
  甲路径（同笔事务）不启用，甲乙不得混用。
- 处置只能是「冻结」或「降级只读」之一，须与超限要求同态；偏离即拒绝并记「处置偏了」。
- 两名值班抢同一超限源：先到占位，后到返回 409 并只记「超限处置争用」。
- 落定即钉死，超限当天不许改口处置册；采集入库只登记分级，不改输出态。
- 源目录、到达详情、策略附页、补采队列、水位卡片共用 `GET /api/board` 一份读数；
  `GET /api/dispositions/integrity` 必须 `ok=true`。

口径全文见 [`docs/超限处置说明.md`](docs/超限处置说明.md)（首次初始化写入库并 SHA 钉死）。
复现脚本：`.venv/bin/python scripts/repro_overlimit_mismatch.py`。

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

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

浏览器：http://127.0.0.1:10813/  
健康检查：GET http://127.0.0.1:10813/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
