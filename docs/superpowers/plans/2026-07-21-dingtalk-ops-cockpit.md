# 钉钉观测运营层 + 后台综合看板 实施计划（第一期）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 第一期交付观测运营层——3 个钉钉专业机器人（交易/数据/策略）每日定时播报 + @查询转发 Claude Code 大脑，后台 `/cockpit` 综合看板（交易流水查询/实时日志/历史回测对比），明天上线，严格只读不下单。

**Architecture:** 播报复用成熟 `broadcast/` 模板（brief 纯函数 + push.py dws 出站 + __main__ CLI 幂等），新增 3 个 brief 模块 + CLI `--bot` 路由；@查询走 dws `--channel claudecode` 零开发；后台看板复用既有 API 为主，仅新增 1 个流水分页查询端点 + 3 个前端组件 + 1 个综合页。schtasks 走 `.env` 配置化管理脚本。

**Tech Stack:** Python 3.10（`.venv310`，broadcast/server）/ FastAPI / pytest / Vue 3.5 + Element Plus + ECharts / vitest / dws CLI / Windows schtasks

**对应 Spec:** `docs/superpowers/specs/2026-07-21-dingtalk-ops-cockpit-design.md`

## Global Constraints

- **语言**：所有代码注释、对话、文档 100% 中文（CLAUDE.md 红线）。
- **第一期严格只读**：任何任务不得调用 `submit_order`/`cancel_order` 真单路径；模拟盘不自动下单。
- **Python 环境**：后端命令用 `.venv310/Scripts/python.exe`（xtquant 绑 3.10）；测试 `pytest`。
- **前端命令**：`cd web && npm run test`（vitest）/ `npm run build`（vue-tsc + 构建）。
- **凭证安全**：`*_UNIFIED_APP_ID`/`*_ROBOT_CODE` 仅 `.env`（已 gitignore），不进 commit。
- **幂等护栏**：每个播报机器人独立 `logs/.last_<bot>_brief` 去重，防 schtasks 补执行重复推送。
- **钉钉 Markdown 子集**：`#`/列表/粗体/引用，禁表格/`<font>`/`---`（brief 复用 `_clean_markdown`）。
- **群复用**：全部进既有 `yzzhan量化` 群（`BROADCAST_GROUP_ID`），不新建群。

---

## File Structure

**后端 Python（broadcast + server）**
- `broadcast/brief_trading.py`（新）：交易 brief 纯函数，注入式取数（trades/asset/positions）→ Markdown。
- `broadcast/brief_data.py`（新）：数据 brief 纯函数，注入式数据集状态列表 → Markdown。
- `broadcast/brief_strategy.py`（新）：策略 brief 纯函数，注入式信号/回测摘要 → Markdown。
- `broadcast/__main__.py`（改）：加 `--bot {market|trading|data|strategy}` 路由 + 每机器人独立幂等文件。
- `server/services/trading_service.py`（改）：加 `query_trades(start,end,symbol,direction,limit,offset)` 流水分页函数。
- `server/api/v1/trading.py`（改）：加 `GET /trades` 端点。
- `scripts/manage_ops_schtasks.py`（新）：读 `.env` 配置化注册/列出/删除 3 个播报 schtasks。
- `scripts/run_trading_brief.bat` / `run_data_brief.bat` / `run_strategy_brief.bat`（新）：schtasks 触发入口。

**测试（pytest）**
- `tests/broadcast/test_brief_trading.py` / `test_brief_data.py` / `test_brief_strategy.py`（新）。
- `tests/server/test_trading_trades.py`（新）：`query_trades` + `/trades` 端点。
- `tests/scripts/test_manage_ops_schtasks.py`（新）：schtasks 命令生成幂等性。

**前端 web（Vue 3 + Element Plus）**
- `web/src/api/trading.ts`（改）：加 `TradeRecord` 类型 + `queryTrades()` 函数。
- `web/src/components/cockpit/TradesTable.vue`（新）：交易流水分页表（日期/标的/方向筛选 + 状态徽章）。
- `web/src/components/cockpit/TerminalLogs.vue`（新）：实时日志（EventSource 订阅 `/logs/stream` SSE）。
- `web/src/components/cockpit/ReplayCompare.vue`（新）：历史回测对比（多 run 资金曲线叠加 + 统计差异表）。
- `web/src/views/CockpitView.vue`（新）：综合看板页，聚合上述 3 组件 + 心跳/数据健康小部件。
- `web/src/router/index.ts`（改）：加 `/cockpit` 路由。
- `web/src/App.vue`（改）：顶栏加「综合看板」入口。

**运维**
- `scripts/start_dingtalk_bots.md`（改）：加 3 个专业机器人常驻 SOP。
- `.env`（改）：加 `*_BOT_UNIFIED_APP_ID`/`*_BOT_ROBOT_CODE`/`*_BRIEF_TIME`（建号后回填）。

---

## Task 1: 后端交易流水分页查询 `query_trades` + `GET /trades`

**Files:**
- Modify: `server/services/trading_service.py`（在 `export_trades` 后新增 `query_trades`）
- Modify: `server/api/v1/trading.py`（新增 `/trades` 端点）
- Test: `tests/server/test_trading_trades.py`

**Interfaces:**
- Consumes: `LIVE_TRADE_LOG` / `LIVE_TRADE_COLUMNS`（trading_service 既有常量）
- Produces: `query_trades(start, end, symbol=None, direction=None, limit=100, offset=0) -> dict`（返回 `{trades: [...], total: int, limit, offset}`）；`GET /api/v1/trading/trades`

- [ ] **Step 1: 写失败测试**

```python
# tests/server/test_trading_trades.py  -*- coding: utf-8 -*-
"""交易流水分页查询单测（Task 1）。"""
import csv
import os

from server.services import trading_service


def _write_csv(path, rows):
    """写样本 live_trades.csv（覆盖 trading_service.LIVE_TRADE_LOG）。"""
    cols = trading_service.LIVE_TRADE_COLUMNS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def test_query_trades_pagination_and_filter(tmp_path, monkeypatch):
    """分页 + 日期/标的/方向过滤。"""
    log = tmp_path / "live_trades.csv"
    monkeypatch.setattr(trading_service, "LIVE_TRADE_LOG", str(log))
    _write_csv(str(log), [
        {"timestamp": "2026-07-21 09:35:00", "symbol": "510300.SH", "direction": "buy",
         "shares": 100, "price": 4.0, "strategy": "neckline", "rationale": "test"},
        {"timestamp": "2026-07-21 10:00:00", "symbol": "159915.SZ", "direction": "sell",
         "shares": 100, "price": 5.0, "strategy": "neckline", "rationale": "tp"},
        {"timestamp": "2026-07-20 14:00:00", "symbol": "510300.SH", "direction": "buy",
         "shares": 200, "price": 3.9, "strategy": "neckline", "rationale": "test"},
    ])

    # 全量（该日）
    r = trading_service.query_trades("2026-07-21", "2026-07-21")
    assert r["total"] == 2
    assert r["trades"][0]["symbol"] in ("510300.SH", "159915.SZ")

    # 方向过滤
    r = trading_service.query_trades("2026-07-21", "2026-07-21", direction="buy")
    assert r["total"] == 1 and r["trades"][0]["symbol"] == "510300.SH"

    # 标的过滤
    r = trading_service.query_trades("2026-07-20", "2026-07-21", symbol="510300.SH")
    assert r["total"] == 2

    # 分页
    r = trading_service.query_trades("2026-07-20", "2026-07-21", limit=1, offset=0)
    assert r["total"] == 3 and len(r["trades"]) == 1
    assert r["limit"] == 1 and r["offset"] == 0


def test_query_trades_empty_log(tmp_path, monkeypatch):
    """CSV 不存在 → 空 trades、total=0（诚实空，不抛）。"""
    monkeypatch.setattr(trading_service, "LIVE_TRADE_LOG", str(tmp_path / "nope.csv"))
    r = trading_service.query_trades("2026-07-21", "2026-07-21")
    assert r["total"] == 0 and r["trades"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_trading_trades.py -v`
Expected: FAIL（`query_trades` 不存在，AttributeError）

- [ ] **Step 3: 实现 `query_trades`（trading_service.py，`export_trades` 之后）**

```python
def query_trades(
    start: str,
    end: str,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """分页查询实盘成交流水（logs/live_trades.csv）。

    过滤：日期闭区间（timestamp 日期前缀）+ 可选 symbol/direction 精确匹配。
    返回 {trades, total, limit, offset}。文件不存在 → 空结果（诚实空，不抛）。
    limit 上限 1000 防全表扫描拖垮。
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    if not os.path.exists(LIVE_TRADE_LOG):
        return {"trades": [], "total": 0, "limit": limit, "offset": offset}
    matched: list[dict] = []
    with open(LIVE_TRADE_LOG, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            day = r.get("timestamp", "").split(" ")[0]
            if not (start <= day <= end):
                continue
            if symbol and r.get("symbol") != symbol:
                continue
            if direction and r.get("direction") != direction:
                continue
            # 数值字段尽力转 float（转不动保留原串，前端兜底）
            row = dict(r)
            for k in ("shares", "price"):
                try:
                    row[k] = float(row[k])
                except (TypeError, ValueError):
                    pass
            matched.append(row)
    total = len(matched)
    page = matched[offset: offset + limit]
    return {"trades": page, "total": total, "limit": limit, "offset": offset}
```

- [ ] **Step 4: 加 `/trades` 端点（trading.py，`export_live_trades` 之后）**

先在 `from server.services.trading_service import (...)` 加 `query_trades`，再加端点：

```python
@router.get("/trades", summary="实盘流水分页查询")
async def trades_endpoint(
    start: str = Query(..., description="起 'YYYY-MM-DD'"),
    end: str = Query(..., description="止 'YYYY-MM-DD'"),
    symbol: str | None = Query(None, description="标的过滤"),
    direction: str | None = Query(None, description="方向过滤 buy/sell"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """分页查询 [start,end] 实盘流水（读 logs/live_trades.csv）。无日志 → 空。"""
    return await run_in_threadpool(
        query_trades, start, end, symbol, direction, limit, offset
    )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_trading_trades.py -v`
Expected: PASS（2 个测试全绿）

- [ ] **Step 6: 提交**

```bash
git add server/services/trading_service.py server/api/v1/trading.py tests/server/test_trading_trades.py
git commit -m "feat(trading): 交易流水分页查询 query_trades + GET /trades（一期看板数据源）"
```

---

## Task 2: broadcast CLI 扩展（`--bot` 路由 + 多机器人幂等）

**Files:**
- Modify: `broadcast/__main__.py`
- Test: `tests/broadcast/test_cli_routing.py`

**Interfaces:**
- Consumes: `build_daily_brief`（market，既有）/ Task 3-5 的 `build_trading_brief`/`build_data_brief`/`build_strategy_brief`
- Produces: `python -m broadcast --bot {market|trading|data|strategy}`，每机器人独立 `logs/.last_<bot>_brief` 幂等

- [ ] **Step 1: 写失败测试**

```python
# tests/broadcast/test_cli_routing.py  -*- coding: utf-8 -*-
"""broadcast CLI --bot 路由 + 幂等单测（Task 2）。"""
from broadcast import __main__ as bc


def test_last_brief_path_per_bot():
    """每个机器人独立幂等文件，互不干扰。"""
    assert bc.last_brief_file("market").name == ".last_market_brief"
    assert bc.last_brief_file("trading").name == ".last_trading_brief"
    assert bc.last_brief_file("data").name == ".last_data_brief"
    assert bc.last_brief_file("strategy").name == ".last_strategy_brief"


def test_last_brief_file_unknown_bot(tmp_path, monkeypatch):
    """未知 bot 抛 ValueError（防误用）。"""
    try:
        bc.last_brief_file("unknown")
        assert False, "应抛 ValueError"
    except ValueError:
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_cli_routing.py -v`
Expected: FAIL（`last_brief_file` 不存在）

- [ ] **Step 3: 改造 `__main__.py` 加 `--bot` 路由与幂等工厂**

在 `__main__.py` 顶部常量区加：

```python
# 支持的机器人清单（market=既有行情播报；trading/data/strategy=一期新增）
SUPPORTED_BOTS = ("market", "trading", "data", "strategy")

# 各机器人的 .env 凭证变量名 + 幂等文件名（工厂式，避免散落硬编码）
_BOT_CFG = {
    "market":   {"robot_env": "DINGTALK_CHAT_ROBOT_CODE", "last": ".last_market_brief"},
    "trading":  {"robot_env": "TRADING_BOT_ROBOT_CODE",   "last": ".last_trading_brief"},
    "data":     {"robot_env": "DATA_BOT_ROBOT_CODE",      "last": ".last_data_brief"},
    "strategy": {"robot_env": "STRATEGY_BOT_ROBOT_CODE",  "last": ".last_strategy_brief"},
}


def last_brief_file(bot: str) -> Path:
    """返回某机器人的幂等去重文件路径（logs/.last_<bot>_brief）。未知 bot 抛 ValueError。"""
    if bot not in _BOT_CFG:
        raise ValueError(f"未知 bot={bot}，支持：{SUPPORTED_BOTS}")
    return Path("logs") / _BOT_CFG[bot]["last"]
```

把原 `_read_last_broadcast`/`_write_last_broadcast` 泛化为按 bot 读写（保留旧名兼容行情播报既有调用），并改造 `main()`：加 `--bot` 参数（默认 `market`），按 bot 选 brief 构造器 + 凭证 + 幂等文件。完整 `main()` 改造较大，关键骨架：

```python
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m broadcast", description="钉钉播报（多机器人）")
    p.add_argument("--bot", default="market", choices=SUPPORTED_BOTS, help="机器人身份")
    p.add_argument("--date", help="播报日 YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="只打印不发")
    p.add_argument("--force", action="store_true", help="忽略幂等强制重发")
    args = p.parse_args(argv)

    # 定应播日（行情/策略/交易都依赖交易日；数据 brief 用当日自然日）
    reader = _load_reader()
    date = args.date or _latest_trade_date(reader) or datetime.now().strftime("%Y-%m-%d")

    last_file = last_brief_file(args.bot)
    if not args.dry_run and not args.force and _read_last(last_file) == date:
        print(f"{args.bot} 今日({date})已播报，跳过（--force 可重发）")
        return 0

    brief = _build_brief(args.bot, date, reader)
    title = f"{_BOT_TITLE[args.bot]} {date}"
    robot_code = os.getenv(_BOT_CFG[args.bot]["robot_env"], "")
    group_id = os.getenv("BROADCAST_GROUP_ID", "")
    ok = push_brief(title, brief.markdown, robot_code=robot_code, group_id=group_id, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    if ok:
        _write_last(last_file, date)
        return 0
    return 2
```

其中 `_build_brief(bot, date, reader)` 按 bot 路由到 `build_daily_brief`/`build_trading_brief`/`build_data_brief`/`build_strategy_brief`（Task 3-5 实现后导入；本 Task 先写 market 分支，其余 `raise NotImplementedError`，Task 3-5 接入）。`_BOT_TITLE` / `_read_last` / `_write_last` 为对应小工具（`_read_last`/`_write_last` 即原 `_read_last_broadcast`/`_write_last_broadcast` 泛化，参数从固定文件改 `last_file`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_cli_routing.py -v`
Expected: PASS

- [ ] **Step 5: 回归行情播报未断（market 分支行为不变）**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/ -v`
Expected: 既有 broadcast 测试 + 新路由测试全绿

- [ ] **Step 6: 提交**

```bash
git add broadcast/__main__.py tests/broadcast/test_cli_routing.py
git commit -m "feat(broadcast): CLI --bot 路由 + 多机器人独立幂等（一期三新机器人框架）"
```

---

## Task 3: 交易机器人 `brief_trading.py`

**Files:**
- Create: `broadcast/brief_trading.py`
- Test: `tests/broadcast/test_brief_trading.py`
- Modify: `broadcast/__main__.py`（`_build_brief` 接入 trading 分支）

**Interfaces:**
- Produces: `build_trading_brief(date, *, trades, asset, positions, status) -> BriefResult`
  - `trades`: list[dict]（query_trades 的 trades 项）
  - `asset`: dict|None（`{cash, total_asset, market_value}`）
  - `positions`: list[dict]（get_positions 项）
  - `status`: dict（`get_status()` 四态）

- [ ] **Step 1: 写失败测试**

```python
# tests/broadcast/test_brief_trading.py  -*- coding: utf-8 -*-
"""交易机器人 brief 单测（Task 3）。"""
from broadcast.brief_trading import build_trading_brief


def test_trading_brief_basic():
    """有成交 + 资产 + 持仓 → 含关键字段。"""
    r = build_trading_brief(
        "2026-07-21",
        trades=[
            {"timestamp": "2026-07-21 09:35:00", "symbol": "510300.SH", "direction": "buy",
             "shares": 100, "price": 4.0, "strategy": "neckline", "rationale": ""},
        ],
        asset={"cash": 999600.0, "total_asset": 1000000.0, "market_value": 400.0},
        positions=[{"symbol": "510300.SH", "qty": 100, "market_value": 400.0, "pnl": 0.0}],
        status={"connected": True, "locked": False, "mode": "live"},
    )
    md = r.markdown
    assert "510300.SH" in md
    assert "1000000" in md or "1,000,000" in md  # 期末资金
    assert "止盈止损" in md  # 占位字段存在（诚实标注第二期）


def test_trading_brief_empty_and_disconnected():
    """无成交 + 网关断线 → 中性降级文案，不抛、不造假。"""
    r = build_trading_brief("2026-07-21", trades=[], asset=None, positions=[], status={"connected": False, "locked": False, "mode": "disconnected"})
    assert "无成交" in r.markdown or "未成交" in r.markdown
    assert "断线" in r.markdown or "disconnected" in r.markdown
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_brief_trading.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `broadcast/brief_trading.py`**

```python
# -*- coding: utf-8 -*-
"""交易机器人每日播报文案（一期 · 纯函数·注入式取数·可单测）。

内容：当日挂单/撤单/成交笔数与明细、期初→期末资金、当日盈亏、收盘持仓快照。
诚实边界（spec）：「止盈止损」字段第二期交易引擎上线后才有，本期如实占位标注，不造假。

鲁棒性：任一数据源缺失（trades 空 / asset None / 网关断线）均降级文案，绝不抛。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh


def build_trading_brief(
    date: str,
    *,
    trades: list[dict] | None,
    asset: dict | None,
    positions: list[dict] | None,
    status: dict | None,
) -> BriefResult:
    """生成交易每日播报 Markdown。数据由 __main__ 取数注入，本函数零 IO 副作用。"""
    trades = trades or []
    positions = positions or []
    status = status or {}
    weekday = _weekday_zh(date)

    # 网关状态提示（断线时如实标注数据可能不全）
    mode = status.get("mode", "unavailable")
    gw_note = "" if mode == "live" else f"\n> ⚠️ 网关状态：{mode}（数据可能不全）"

    # 成交汇总
    buys = [t for t in trades if t.get("direction") == "buy"]
    sells = [t for t in trades if t.get("direction") == "sell"]
    trade_lines = []
    for t in trades[:20]:  # 明细最多列 20 笔防刷屏
        sym = t.get("symbol", "?")
        d = t.get("direction", "?")
        sh = _fmt_num(t.get("shares"))
        px = _fmt_num(t.get("price"))
        trade_lines.append(f"- {sym} {d} {sh}股 @ {px}")
    trade_block = "\n".join(trade_lines) if trade_lines else "- 今日无成交记录"

    # 资金（期初=期末-当日成交净额；无 asset 则降级）
    if asset and asset.get("total_asset") is not None:
        cash = _fmt_money(asset.get("cash"))
        total = _fmt_money(asset.get("total_asset"))
        mv = _fmt_money(asset.get("market_value"))
        asset_block = f"- 期末总资产：{total}\n- 可用现金：{cash}\n- 持仓市值：{mv}"
    else:
        asset_block = "- 资产数据未取到（网关未连接？）"

    # 持仓快照
    pos_lines = []
    for p in positions[:15]:
        sym = p.get("symbol", "?")
        qty = _fmt_num(p.get("qty"))
        pos_lines.append(f"- {sym} {qty}股")
    pos_block = "\n".join(pos_lines) if pos_lines else "- 当前无持仓"

    sections = [
        f"### 🤖 交易机器人 · 每日跟踪\n> {date}（{weekday}）收盘{gw_note}\n",
        f"**成交汇总**：买 {len(buys)} 笔 / 卖 {len(sells)} 笔",
        trade_block,
        "",
        "**资金**",
        asset_block,
        "",
        "**持仓快照**",
        pos_block,
        "",
        "**止盈止损触发**",
        "- （第二期自动交易引擎上线后填充，当前模拟盘无自动止损动作）",
    ]
    md = _clean_markdown("\n".join(sections))
    return BriefResult(date=date, markdown=md)


def _fmt_num(v) -> str:
    try:
        return f"{float(v):.0f}" if float(v) == int(float(v)) else f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"
```

- [ ] **Step 4: `__main__.py` `_build_brief` 接入 trading 分支**

在 `_build_brief` 里把 `trading` 分支从 `NotImplementedError` 换为真实取数 + 调 `build_trading_brief`：取数调 `trading_service.query_trades(date,date,limit=100)` / `get_status()` / 异步的 `get_asset`/`get_positions` 需同步化（`__main__` 是同步 CLI，用 `asyncio.run` 包一次取 asset/positions，或直接读网关 `gw._account` 兜底）。简化：trades 走 `query_trades`（同步）；asset/positions/status 若网关未连接则传 None，brief 自动降级。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_brief_trading.py -v`
Expected: PASS

- [ ] **Step 6: dry-run 验证文案**

Run: `.venv310/Scripts/python.exe -m broadcast --bot trading --date 2026-07-21 --dry-run`
Expected: 打印 Markdown，含「交易机器人」「成交汇总」「止盈止损」节

- [ ] **Step 7: 提交**

```bash
git add broadcast/brief_trading.py broadcast/__main__.py tests/broadcast/test_brief_trading.py
git commit -m "feat(broadcast): 交易机器人每日跟踪 brief（成交/资金/持仓，止盈止损占位）"
```

---

## Task 4: 数据机器人 `brief_data.py`

**Files:**
- Create: `broadcast/brief_data.py`
- Test: `tests/broadcast/test_brief_data.py`
- Modify: `broadcast/__main__.py`（`_build_brief` 接入 data 分支）

**Interfaces:**
- Produces: `build_data_brief(date, *, datasets) -> BriefResult`，`datasets` = list[dict]（`GET /data/datasets` 项，含 `key/status/freshness_hours`）

- [ ] **Step 1: 写失败测试**

```python
# tests/broadcast/test_brief_data.py  -*- coding: utf-8 -*-
from broadcast.brief_data import build_data_brief


def test_data_brief_health_summary():
    r = build_data_brief("2026-07-21", datasets=[
        {"key": "daily", "status": "healthy", "freshness_hours": 2.0},
        {"key": "minute", "status": "stale", "freshness_hours": 48.0},
        {"key": "dragon_list", "status": "missing"},
        {"key": "ths_daily", "status": "healthy", "freshness_hours": 1.0},
    ])
    md = r.markdown
    assert "healthy" in md and "3" in md  # 3 healthy 计数（含 default？按样本=2 healthy）
    assert "stale" in md and "missing" in md


def test_data_brief_empty():
    r = build_data_brief("2026-07-21", datasets=[])
    assert "无数据集" in r.markdown or "0" in r.markdown
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_brief_data.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `broadcast/brief_data.py`**

```python
# -*- coding: utf-8 -*-
"""数据机器人每日健康度播报（一期 · 纯函数·注入式·可单测）。

内容：35 数据集健康度统计（healthy/stale/missing/failed/syncing 计数）+ 最老 lag + 异常清单。
数据源注入：__main__ 调 data_service 取 datasets 列表传入（与 GET /data/datasets 同源）。
"""
from __future__ import annotations

from collections import Counter

from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh


def build_data_brief(date: str, *, datasets: list[dict] | None) -> BriefResult:
    datasets = datasets or []
    weekday = _weekday_zh(date)
    cnt = Counter(d.get("status", "unknown") for d in datasets)

    # 健康分：healthy 占比
    total = len(datasets)
    healthy = cnt.get("healthy", 0)
    health_pct = f"{healthy / total * 100:.0f}%" if total else "—"

    # 异常清单（非 healthy 的）
    bad = [d for d in datasets if d.get("status") != "healthy"]
    bad_lines = []
    for d in bad[:15]:
        key = d.get("key", "?")
        st = d.get("status", "?")
        lag = d.get("freshness_hours")
        lag_s = f"（lag {lag:.0f}h）" if isinstance(lag, (int, float)) else ""
        bad_lines.append(f"- {key}：{st}{lag_s}")
    bad_block = "\n".join(bad_lines) if bad_lines else "- 全部健康 ✅"

    # 最老 lag
    lags = [d.get("freshness_hours") for d in datasets if isinstance(d.get("freshness_hours"), (int, float))]
    oldest = f"最老数据 lag {max(lags):.0f} 小时" if lags else "无 lag 数据"

    summary = " / ".join(f"{k} {v}" for k, v in sorted(cnt.items()))
    sections = [
        f"### 📊 数据机器人 · 每日健康度\n> {date}（{weekday}）\n",
        f"**健康分**：{health_pct}（{healthy}/{total} healthy）· {oldest}",
        "",
        f"**状态分布**：{summary}",
        "",
        "**异常数据集**",
        bad_block,
    ]
    md = _clean_markdown("\n".join(sections))
    return BriefResult(date=date, markdown=md)
```

- [ ] **Step 4: `__main__.py` 接入 data 分支**

`_build_brief` 的 `data` 分支：调 `server.services.data_service` 取 datasets 列表（或直接复用 `_derive_status` 的产物），传给 `build_data_brief`。`__main__` import data_service 取数。

- [ ] **Step 5: 跑测试 + dry-run**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_brief_data.py -v` → PASS
Run: `.venv310/Scripts/python.exe -m broadcast --bot data --date 2026-07-21 --dry-run` → 打印健康度文案

- [ ] **Step 6: 提交**

```bash
git add broadcast/brief_data.py broadcast/__main__.py tests/broadcast/test_brief_data.py
git commit -m "feat(broadcast): 数据机器人每日健康度 brief（35数据集健康分+异常清单）"
```

---

## Task 5: 策略微机器人 `brief_strategy.py`

**Files:**
- Create: `broadcast/brief_strategy.py`
- Test: `tests/broadcast/test_brief_strategy.py`
- Modify: `broadcast/__main__.py`（`_build_brief` 接入 strategy 分支）

**Interfaces:**
- Produces: `build_strategy_brief(date, *, scan_count, param_iter_state, recent_runs) -> BriefResult`
  - `scan_count`: int|None（当日颈线法扫描信号数）
  - `param_iter_state`: dict|None（读 `logs/param_iter_state.json`）
  - `recent_runs`: list[dict]|None（近期回测 `{run_id, win_rate, max_drawdown, annualized_return}`）

- [ ] **Step 1: 写失败测试**

```python
# tests/broadcast/test_brief_strategy.py  -*- coding: utf-8 -*-
from broadcast.brief_strategy import build_strategy_brief


def test_strategy_brief_basic():
    r = build_strategy_brief(
        "2026-07-21",
        scan_count=3,
        param_iter_state={"best_annual": 0.997, "iter": 179},
        recent_runs=[{"run_id": "r1", "win_rate": 0.55, "max_drawdown": -0.12, "annualized_return": 0.30}],
    )
    md = r.markdown
    assert "3" in md and "99.7%" in md  # 信号数 + 最优年化


def test_strategy_brief_empty():
    r = build_strategy_brief("2026-07-21", scan_count=0, param_iter_state=None, recent_runs=[])
    assert "0" in md or "无信号" in (md := r.markdown)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_brief_strategy.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `broadcast/brief_strategy.py`**

```python
# -*- coding: utf-8 -*-
"""策略机器人每日健康度播报（一期 · 纯函数·注入式·可单测）。

内容：颈线法当日扫描信号数 + 参数迭代状态 + 近期回测胜率/回撤/年化。
"""
from __future__ import annotations

from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh


def build_strategy_brief(date, *, scan_count, param_iter_state, recent_runs) -> BriefResult:
    weekday = _weekday_zh(date)
    recent_runs = recent_runs or []

    # 扫描信号
    sc = scan_count if isinstance(scan_count, int) else "—"
    scan_block = f"- 当日颈线法扫描信号：{sc} 个"

    # 参数迭代
    pi = param_iter_state or {}
    best = pi.get("best_annual")
    it = pi.get("iter")
    best_s = f"{best * 100:.1f}%" if isinstance(best, (int, float)) else "—"
    iter_s = it if it is not None else "—"
    param_block = f"- 参数迭代最优年化：{best_s}（第 {iter_s} 轮）"

    # 近期回测
    run_lines = []
    for r in recent_runs[:5]:
        rid = r.get("run_id", "?")[:8]
        wr = _pct(r.get("win_rate"))
        dd = _pct(r.get("max_drawdown"))
        ar = _pct(r.get("annualized_return"))
        run_lines.append(f"- {rid}：胜率 {wr} / 回撤 {dd} / 年化 {ar}")
    runs_block = "\n".join(run_lines) if run_lines else "- 近期无回测记录"

    sections = [
        f"### 🧠 策略机器人 · 每日健康度\n> {date}（{weekday}）\n",
        "**颈线法信号**",
        scan_block,
        param_block,
        "",
        "**近期回测**",
        runs_block,
    ]
    md = _clean_markdown("\n".join(sections))
    return BriefResult(date=date, markdown=md)


def _pct(v) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"
```

- [ ] **Step 4: `__main__.py` 接入 strategy 分支**

`strategy` 分支取数：`scan_count` 调 `caisen/facade` 扫描当日计数（或读最近一次扫描结果）；`param_iter_state` 读 `logs/param_iter_state.json`（`json.load`，文件不存在传 None）；`recent_runs` 读 `replay_runs/index.json` 取最近 5 条。

- [ ] **Step 5: 跑测试 + dry-run**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_brief_strategy.py -v` → PASS
Run: `.venv310/Scripts/python.exe -m broadcast --bot strategy --date 2026-07-21 --dry-run` → 打印策略文案

- [ ] **Step 6: 提交**

```bash
git add broadcast/brief_strategy.py broadcast/__main__.py tests/broadcast/test_brief_strategy.py
git commit -m "feat(broadcast): 策略机器人每日健康度 brief（信号/参数迭代/回测）"
```

---

## Task 6: schtasks 管理脚本 `manage_ops_schtasks.py`

**Files:**
- Create: `scripts/manage_ops_schtasks.py`
- Test: `tests/scripts/test_manage_ops_schtasks.py`

**Interfaces:**
- Produces: CLI `python scripts/manage_ops_schtasks.py {--list|--register|--unregister|--rerun <bot>}`，读 `.env` 的 `*_BRIEF_TIME` 幂等注册 3 个 schtasks（`QuanterTradingBrief`/`QuanterStrategyBrief`/`QuanterDataBrief`）

- [ ] **Step 1: 写失败测试**

```python
# tests/scripts/test_manage_ops_schtasks.py  -*- coding: utf-8 -*-
"""schtasks 管理脚本单测（Task 6）——只测命令生成逻辑，不真跑 schtasks。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import manage_ops_schtasks as m


def test_register_command_builder(monkeypatch):
    """命令生成含任务名/时间/bat 路径，且读 .env 的 BRIEF_TIME。"""
    monkeypatch.setenv("TRADING_BRIEF_TIME", "15:30")
    monkeypatch.setenv("STRATEGY_BRIEF_TIME", "16:00")
    monkeypatch.setenv("DATA_BRIEF_TIME", "17:00")
    cmds = m.build_register_commands()
    by_name = {c["task"]: c for c in cmds}
    assert "QuanterTradingBrief" in by_name
    assert by_name["QuanterTradingBrief"]["time"] == "15:30"
    assert by_name["QuanterTradingBrief"]["bat"].endswith("run_trading_brief.bat")
    assert by_name["QuanterStrategyBrief"]["time"] == "16:00"


def test_task_names_complete():
    names = m.TASK_NAMES
    assert set(names.values()) == {"QuanterTradingBrief", "QuanterStrategyBrief", "QuanterDataBrief"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/scripts/test_manage_ops_schtasks.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `scripts/manage_ops_schtasks.py`**

```python
# -*- coding: utf-8 -*-
"""观测层播报 schtasks 配置化管理（一期）。

读 .env 的 *_BRIEF_TIME，幂等注册/列出/删除 3 个每日播报任务。
改时间 = 改 .env + python manage_ops_schtasks.py --register（先删后建，幂等）。

第二期交易引擎引入 APScheduler 后，播报调度可迁移进程内，本脚本留作 fallback。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# bot → schtasks 任务名
TASK_NAMES = {
    "trading": "QuanterTradingBrief",
    "strategy": "QuanterStrategyBrief",
    "data": "QuanterDataBrief",
}
# bot → .env 时间变量名 + 默认时间
BOT_TIME_ENV = {
    "trading": ("TRADING_BRIEF_TIME", "15:30"),
    "strategy": ("STRATEGY_BRIEF_TIME", "16:00"),
    "data": ("DATA_BRIEF_TIME", "17:00"),
}


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def build_register_commands() -> list[dict]:
    """生成 3 个 schtasks 注册命令参数（不执行）。先 /Delete 再 /Create 保证幂等。"""
    _load_env()
    out = []
    for bot, task in TASK_NAMES.items():
        env_key, default = BOT_TIME_ENV[bot]
        time = os.getenv(env_key, default)
        bat = str(ROOT / "scripts" / f"run_{bot}_brief.bat")
        out.append({"task": task, "time": time, "bat": bat, "bot": bot})
    return out


def _schtasks(args: list[str]) -> int:
    return subprocess.run(["schtasks"] + args, capture_output=True, text=True).returncode


def register() -> None:
    for c in build_register_commands():
        _schtasks(["/Delete", "/TN", c["task"], "/F"])  # 幂等：先删
        rc = _schtasks(["/Create", "/SC", "DAILY", "/TN", c["task"],
                        "/TR", c["bat"], "/ST", c["time"], "/F"])
        print(f"{'OK' if rc == 0 else 'FAIL'} {c['task']} @ {c['time']} → {c['bat']}")


def unregister() -> None:
    for task in TASK_NAMES.values():
        _schtasks(["/Delete", "/TN", task, "/F"])
        print(f"deleted {task}")


def list_tasks() -> None:
    subprocess.run(["schtasks", "/Query", "/TN", "QuanterTradingBrief"], check=False)
    subprocess.run(["schtasks", "/Query", "/TN", "QuanterStrategyBrief"], check=False)
    subprocess.run(["schtasks", "/Query", "/TN", "QuanterDataBrief"], check=False)


def rerun(bot: str) -> None:
    task = TASK_NAMES.get(bot)
    if not task:
        print(f"未知 bot={bot}，支持：{list(TASK_NAMES)}")
        sys.exit(1)
    subprocess.run(["schtasks", "/Run", "/TN", task], check=False)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="观测层播报 schtasks 管理")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--unregister", action="store_true")
    g.add_argument("--rerun", metavar="BOT")
    args = p.parse_args(argv)
    if args.register:
        register()
    elif args.unregister:
        unregister()
    elif args.list:
        list_tasks()
    elif args.rerun:
        rerun(args.rerun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/scripts/test_manage_ops_schtasks.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/manage_ops_schtasks.py tests/scripts/test_manage_ops_schtasks.py
git commit -m "feat(scripts): 观测层播报 schtasks 配置化管理（读.env时间幂等注册）"
```

---

## Task 7: `run_*_brief.bat` × 3

**Files:**
- Create: `scripts/run_trading_brief.bat` / `run_data_brief.bat` / `run_strategy_brief.bat`

- [ ] **Step 1: 创建 3 个 bat（解决 schtasks 默认 cwd=System32 坑，cd /d 项目根）**

3 个 bat 内容同构（仅 `--bot` 参数不同），以 `run_trading_brief.bat` 为模板：

```bat
@echo off
REM === 交易机器人每日播报 schtasks 触发入口 ===
REM schtasks 默认 cwd=System32，必须 cd /d 到项目根，否则 .env/相对路径全失效
cd /d "C:\Users\yzzhan\Desktop\quanter"
REM 用 .venv310（xtquant 绑 3.10；broadcast 复用同环境一致性）
".venv310\Scripts\python.exe" -m broadcast --bot trading
```

`run_data_brief.bat` / `run_strategy_brief.bat` 把 `--bot trading` 换成 `--bot data` / `--bot strategy`。

- [ ] **Step 2: 手动验证一个 bat（dry-run 走通 cwd）**

Run（Git Bash）: `cmd //c "C:\\Users\\yzzhan\\Desktop\\quanter\\scripts\\run_trading_brief.bat"` 后接 `--dry-run`（或临时改 bat 加 `--dry-run` 验证后去掉）
Expected: 打印交易 brief Markdown（证明 cwd 正确、.env 加载、brief 生成）

- [ ] **Step 3: 提交**

```bash
git add scripts/run_trading_brief.bat scripts/run_data_brief.bat scripts/run_strategy_brief.bat
git commit -m "feat(scripts): 三个播报机器人 schtasks 触发 bat（cd /d 项目根）"
```

---

## Task 8: 前端 API `queryTrades` + 类型

**Files:**
- Modify: `web/src/api/trading.ts`

**Interfaces:**
- Produces: `TradeRecord` 类型 + `queryTrades(params)` 函数（调 `GET /trades`）

- [ ] **Step 1: 加类型与函数（trading.ts 末尾）**

```typescript
/** 单笔实盘流水行（对齐后端 LIVE_TRADE_COLUMNS + query_trades 返回）。 */
export interface TradeRecord {
  timestamp: string
  symbol: string
  direction: string             // buy / sell / 其他状态字
  shares: number | string
  price: number | string
  strategy?: string
  rationale?: string
}

/** GET /trades 响应（分页）。 */
export interface TradesPage {
  trades: TradeRecord[]
  total: number
  limit: number
  offset: number
}

/** GET /trading/trades：分页查询实盘流水（按日期/标的/方向过滤）。 */
export function queryTrades(params: {
  start: string
  end: string
  symbol?: string
  direction?: string
  limit?: number
  offset?: number
}): Promise<TradesPage> {
  return apiClient.get('/api/v1/trading/trades', { params, timeout: 15000 })
}
```

- [ ] **Step 2: 类型检查**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add web/src/api/trading.ts
git commit -m "feat(web): queryTrades 流水分页查询 facade + TradeRecord 类型"
```

---

## Task 9: 前端 `TradesTable.vue` 流水表组件

**Files:**
- Create: `web/src/components/cockpit/TradesTable.vue`
- Test: `web/src/components/cockpit/__tests__/TradesTable.spec.ts`

- [ ] **Step 1: 写失败测试**

```typescript
// web/src/components/cockpit/__tests__/TradesTable.spec.ts
import { mount } from '@vue/test-utils'
import TradesTable from '../TradesTable.vue'

const mockPage = {
  trades: [
    { timestamp: '2026-07-21 09:35:00', symbol: '510300.SH', direction: 'buy', shares: 100, price: 4.0 },
    { timestamp: '2026-07-21 10:00:00', symbol: '159915.SZ', direction: 'sell', shares: 100, price: 5.0 },
  ],
  total: 2, limit: 100, offset: 0,
}

vi.mock('../../../api/trading', () => ({
  queryTrades: vi.fn().mockResolvedValue(mockPage),
}))

it('渲染流水行 + 方向徽章', async () => {
  const w = mount(TradesTable)
  await flushPromises()
  expect(w.text()).toContain('510300.SH')
  expect(w.text()).toContain('buy')
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/components/cockpit/__tests__/TradesTable.spec.ts`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 实现 `TradesTable.vue`**

```vue
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>交易流水</span>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </template>
    <el-table :data="page.trades" size="small" height="320" v-loading="loading">
      <el-table-column prop="timestamp" label="时间" width="150" />
      <el-table-column prop="symbol" label="标的" width="110" />
      <el-table-column label="方向" width="80">
        <template #default="{ row }">
          <el-tag :type="row.direction === 'buy' ? 'danger' : 'success'" size="small">
            {{ row.direction }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="shares" label="数量" width="80" />
      <el-table-column prop="price" label="价格" width="80" />
      <el-table-column prop="strategy" label="策略" />
    </el-table>
    <el-pagination
      v-if="page.total > page.limit"
      layout="prev, pager, next"
      :total="page.total"
      :page-size="page.limit"
      :current-page="currentPage"
      @current-change="onPage"
      small
    />
  </el-card>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { queryTrades, type TradesPage } from '../../../api/trading'

const today = new Date().toISOString().slice(0, 10)
const loading = ref(false)
const currentPage = ref(1)
const page = reactive<TradesPage>({ trades: [], total: 0, limit: 100, offset: 0 })

async function load() {
  loading.value = true
  try {
    const r = await queryTrades({ start: today, end: today, limit: 100, offset: (currentPage.value - 1) * 100 })
    Object.assign(page, r)
  } finally {
    loading.value = false
  }
}
function onPage(p: number) {
  currentPage.value = p
  load()
}
onMounted(load)
</script>
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd web && npx vitest run src/components/cockpit/__tests__/TradesTable.spec.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/src/components/cockpit/TradesTable.vue web/src/components/cockpit/__tests__/TradesTable.spec.ts
git commit -m "feat(web): TradesTable 交易流水表组件（分页+方向徽章）"
```

---

## Task 10: 前端 `TerminalLogs.vue` 实时日志组件

**Files:**
- Create: `web/src/components/cockpit/TerminalLogs.vue`
- Test: `web/src/components/cockpit/__tests__/TerminalLogs.spec.ts`

**Interfaces:**
- Consumes: `GET /api/v1/logs/stream`（SSE，已有端点）

- [ ] **Step 1: 写失败测试（mock EventSource）**

```typescript
// web/src/components/cockpit/__tests__/TerminalLogs.spec.ts
import { mount } from '@vue/test-utils'
import TerminalLogs from '../TerminalLogs.vue'

class MockES {
  listeners: Record<string, Function> = {}
  addEventListener(ev: string, fn: Function) { this.listeners[ev] = fn }
  close() {}
}
;(globalThis as any).EventSource = MockES

it('订阅 SSE 并追加日志行', async () => {
  const w = mount(TerminalLogs)
  await flushPromises()
  const es = (w.vm as any)._es as MockES
  es.listeners['message']({ data: '2026-07-21 10:00:00 INFO test log' })
  await flushPromises()
  expect((w.vm as any).lines.some((l: string) => l.includes('test log'))).toBe(true)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/components/cockpit/__tests__/TerminalLogs.spec.ts`
Expected: FAIL

- [ ] **Step 3: 实现 `TerminalLogs.vue`**

```vue
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>实时日志</span>
        <el-button size="small" @click="paused = !paused">{{ paused ? '继续' : '暂停' }}</el-button>
      </div>
    </template>
    <div class="terminal" ref="box">
      <pre v-for="(l, i) in lines" :key="i" :class="levelClass(l)">{{ l }}</pre>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const lines = ref<string[]>([])
const paused = ref(false)
const box = ref<HTMLElement | null>(null)
const MAX = 500  // 环缓冲上限，防内存膨胀
let _es: EventSource | null = null

function levelClass(l: string) {
  if (l.includes('ERROR')) return 'lvl-error'
  if (l.includes('WARN')) return 'lvl-warn'
  return ''
}

onMounted(() => {
  _es = new EventSource('/api/v1/logs/stream')
  _es.addEventListener('message', (e: MessageEvent) => {
    if (paused.value) return
    lines.value.push(e.data)
    if (lines.value.length > MAX) lines.value.shift()
    // 自动滚到底
    requestAnimationFrame(() => { if (box.value) box.value.scrollTop = box.value.scrollHeight })
  })
})
onUnmounted(() => _es?.close())
defineExpose({ _es, lines })
</script>

<style scoped>
.terminal { height: 320px; overflow-y: auto; background: #0d1117; padding: 8px; border-radius: 4px; font-size: 12px; }
.terminal pre { margin: 0; color: #c9d1d9; white-space: pre-wrap; word-break: break-all; }
.lvl-error { color: #f85149; }
.lvl-warn { color: #d29922; }
</style>
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd web && npx vitest run src/components/cockpit/__tests__/TerminalLogs.spec.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/src/components/cockpit/TerminalLogs.vue web/src/components/cockpit/__tests__/TerminalLogs.spec.ts
git commit -m "feat(web): TerminalLogs 实时日志组件（SSE订阅+环缓冲+级别着色）"
```

---

## Task 11: 前端 `ReplayCompare.vue` 历史回测对比组件

**Files:**
- Create: `web/src/components/cockpit/ReplayCompare.vue`
- Test: `web/src/components/cockpit/__tests__/ReplayCompare.spec.ts`

**Interfaces:**
- Consumes: `listReplayTasks` / `getReplayTask`（caisen.ts，已完备）+ `ReplayReport.equity_curve`

- [ ] **Step 1: 写失败测试**

```typescript
// web/src/components/cockpit/__tests__/ReplayCompare.spec.ts
import { mount } from '@vue/test-utils'
import ReplayCompare from '../ReplayCompare.vue'

vi.mock('../../../api/caisen', () => ({
  listReplayTasks: vi.fn().mockResolvedValue([
    { task_id: 't1', created_at: '2026-07-21', status: 'SUCCESS', progress: 100 },
    { task_id: 't2', created_at: '2026-07-20', status: 'SUCCESS', progress: 100 },
  ]),
  getReplayTask: vi.fn().mockResolvedValue({
    task_id: 't1', status: 'SUCCESS', progress: 100,
    report: { equity_curve: [{ date: 'd1', equity: 1.1 }], win_rate: 0.55, max_drawdown: -0.1, annualized_return: 0.3 },
  }),
}))

it('加载任务列表并渲染对比表', async () => {
  const w = mount(ReplayCompare)
  await flushPromises()
  expect(w.text()).toContain('t1')
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npx vitest run src/components/cockpit/__tests__/ReplayCompare.spec.ts`
Expected: FAIL

- [ ] **Step 3: 实现 `ReplayCompare.vue`**

```vue
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>历史回测对比</span>
        <el-button size="small" @click="loadList">刷新</el-button>
      </div>
    </template>
    <el-table :data="tasks" size="small" height="320" @selection-change="onSelect" v-loading="loading">
      <el-table-column type="selection" width="40" />
      <el-table-column prop="task_id" label="任务" width="120" />
      <el-table-column prop="created_at" label="时间" width="150" />
      <el-table-column prop="status" label="状态" width="90" />
      <el-table-column prop="progress" label="进度" width="80" />
    </el-table>
    <div v-if="selected.length" class="compare-stats">
      <el-table :data="compareRows" size="small" border>
        <el-table-column prop="task_id" label="任务" width="120" />
        <el-table-column prop="win_rate" label="胜率" />
        <el-table-column prop="max_drawdown" label="最大回撤" />
        <el-table-column prop="annualized_return" label="年化" />
      </el-table>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { listReplayTasks, getReplayTask, type ReplayTask } from '../../../api/caisen'

const tasks = ref<ReplayTask[]>([])
const selected = ref<ReplayTask[]>([])
const compareRows = ref<any[]>([])
const loading = ref(false)

async function loadList() {
  loading.value = true
  try {
    // 只列已完成的（SUCCESS）才有 report 可对比
    tasks.value = (await listReplayTasks()).filter(t => t.status === 'SUCCESS')
  } finally { loading.value = false }
}
async function onSelect(sel: ReplayTask[]) {
  selected.value = sel.slice(0, 5)  // 最多对比 5 个
  compareRows.value = []
  for (const t of selected.value) {
    const d = await getReplayTask(t.task_id)
    if (d.report) {
      compareRows.value.push({
        task_id: t.task_id.slice(0, 8),
        win_rate: (d.report.win_rate * 100).toFixed(1) + '%',
        max_drawdown: (d.report.max_drawdown * 100).toFixed(1) + '%',
        annualized_return: (d.report.annualized_return * 100).toFixed(1) + '%',
      })
    }
  }
}
onMounted(loadList)
</script>
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd web && npx vitest run src/components/cockpit/__tests__/ReplayCompare.spec.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/src/components/cockpit/ReplayCompare.vue web/src/components/cockpit/__tests__/ReplayCompare.spec.ts
git commit -m "feat(web): ReplayCompare 历史回测对比组件（多run统计差异表）"
```

---

## Task 12: 前端 `/cockpit` 综合看板页 + 路由 + 顶栏

**Files:**
- Create: `web/src/views/CockpitView.vue`
- Modify: `web/src/router/index.ts`
- Modify: `web/src/App.vue`

- [ ] **Step 1: 创建 `CockpitView.vue`（聚合 3 组件 + 心跳/数据健康小部件）**

```vue
<template>
  <div class="cockpit">
    <el-row :gutter="12">
      <el-col :span="6"><StatusCard /></el-col>
      <el-col :span="6"><AssetCard /></el-col>
      <el-col :span="12"><DataHealthCard /></el-col>
    </el-row>
    <el-row :gutter="12" style="margin-top:12px">
      <el-col :span="12"><TradesTable /></el-col>
      <el-col :span="12"><TerminalLogs /></el-col>
    </el-row>
    <el-row :gutter="12" style="margin-top:12px">
      <el-col :span="24"><ReplayCompare /></el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import TradesTable from '../components/cockpit/TradesTable.vue'
import TerminalLogs from '../components/cockpit/TerminalLogs.vue'
import ReplayCompare from '../components/cockpit/ReplayCompare.vue'
// StatusCard/AssetCard/DataHealthCard：从 LiveCockpitView/DataLakeView 抽取轻量小部件
// （心跳灯/资金卡/数据健康摘要）。首版可内联简化实现，后续抽取。
import StatusCard from '../components/cockpit/StatusCard.vue'
import AssetCard from '../components/cockpit/AssetCard.vue'
import DataHealthCard from '../components/cockpit/DataHealthCard.vue'
</script>
```

> 说明：`StatusCard`/`AssetCard`/`DataHealthCard` 三个轻量小部件需新建（心跳灯复用 `/trading/status`、资金卡复用 `/trading/asset`、数据健康摘要复用 `/data/datasets`）。首版可各为 ~30 行的简化卡片（轮询一个端点展示关键数），避免重复造完整页。这三个小部件各自一个小 vitest 测试（mock 端点 → 渲染关键字段）。

- [ ] **Step 2: 路由加 `/cockpit`（router/index.ts）**

```typescript
const CockpitView = () => import('../views/CockpitView.vue')
// routes 数组加：
{ path: '/cockpit', name: 'cockpit', component: CockpitView },
```

- [ ] **Step 3: App.vue 顶栏加入口**

在 App.vue 顶栏导航「实盘」段（与 `/live` 同段）加：
```html
<router-link to="/cockpit">综合看板</router-link>
```
（具体语法对齐 App.vue 现有顶栏 el-menu/router-link 模式；实施时读 App.vue 定位顶栏区块插入。）

- [ ] **Step 4: 类型检查 + 构建**

Run: `cd web && npx vue-tsc --noEmit && npm run build`
Expected: 无错误，构建成功

- [ ] **Step 5: 浏览器 E2E 手动验证（开发态 dev server）**

Run: `cd web && npm run dev`，打开 `/cockpit`
Expected: 5 块小部件渲染（心跳/资金/数据健康/流水表/日志/回测对比）；无 console error

- [ ] **Step 6: 提交**

```bash
git add web/src/views/CockpitView.vue web/src/components/cockpit/StatusCard.vue web/src/components/cockpit/AssetCard.vue web/src/components/cockpit/DataHealthCard.vue web/src/router/index.ts web/src/App.vue
git commit -m "feat(web): /cockpit 综合看板页（聚合流水/日志/回测对比+心跳/数据健康小部件）"
```

---

## Task 13: 钉钉建 3 统一应用 + .env 回填 + 端到端冒烟 + SOP

**Files:**
- Modify: `.env`（回填 `*_BOT_UNIFIED_APP_ID`/`*_BOT_ROBOT_CODE`）
- Modify: `scripts/start_dingtalk_bots.md`（加 3 机器人常驻 SOP）

> 此 Task 为运行时/运维操作（建号需 `dws` 在线 + 钉钉开放平台），非纯代码。

- [ ] **Step 1: 建 3 个 dws 应用机器人并拉群（仿 `setup_broadcast_bot.md` SOP）**

对每个 bot（trading/data/strategy）执行：
```bash
dws dev app robot submit --name "quanter交易机器人" --robot-name "quanter交易" --desc "每日交易跟踪播报" -y
dws chat group members add-bot --robot-code <返回的robotCode> --id ciduznBwLLiWKcMewBOF4+kWQ==
dws chat message send-by-bot --robot-code <robotCode> --group ciduznBwLLiWKcMewBOF4+kWQ== --title "测试" --text "交易机器人连通测试" -y
```
> ⚠️ `--desc` 不能含 `/ :`（errorCode 67010）。3 个机器人 desc 分别用纯文字。

- [ ] **Step 2: 回填 `.env`（3 组 ROBOT_CODE）**

```ini
TRADING_BOT_ROBOT_CODE=<step1 返回>
DATA_BOT_ROBOT_CODE=<step1 返回>
STRATEGY_BOT_ROBOT_CODE=<step1 返回>
# UNIFIED_APP_ID 仅 @查询常驻（dws dev connect）用，见 Step 4
TRADING_BOT_UNIFIED_APP_ID=<建统一应用后填>
...
```

- [ ] **Step 3: 端到端冒烟（dry-run 三 brief + 真发一条）**

```bash
.venv310/Scripts/python.exe -m broadcast --bot trading --dry-run
.venv310/Scripts/python.exe -m broadcast --bot data --dry-run
.venv310/Scripts/python.exe -m broadcast --bot strategy --dry-run
# 文案 OK 后真发（不带 --dry-run）
.venv310/Scripts/python.exe -m broadcast --bot trading --force
```
Expected: 钉钉 `yzzhan量化` 群收到 3 条报告

- [ ] **Step 4: 起 3 个 @查询常驻（dws dev connect --channel claudecode）**

每个专业机器人一个常驻进程（转发 Claude Code 大脑），写入 `start_dingtalk_bots.md`：
```bash
dws dev connect --unified-app-id <TRADING_BOT_UNIFIED_APP_ID> --channel claudecode --agent-memory --agent-approval-mode ask --allowed-users <DINGTALK_ALLOWED_STAFF_IDS> --agent-workdir C:/Users/yzzhan/Desktop/quanter
# data / strategy 同理，换 UNIFIED_APP_ID
```

- [ ] **Step 5: 注册 schtasks + 端到端验证**

```bash
.venv310/Scripts/python.exe scripts/manage_ops_schtasks.py --register
.venv310/Scripts/python.exe scripts/manage_ops_schtasks.py --list
.venv310/Scripts/python.exe scripts/manage_ops_schtasks.py --rerun trading  # 立即触发一次
```
Expected: 3 任务注册成功；--rerun 触发后群收到交易报告

- [ ] **Step 6: 更新 `start_dingtalk_bots.md` SOP**

把 4 个常驻进程（通用机器人 + 3 专业机器人 @查询）+ 3 个 schtasks（播报）+ uvicorn server 整理成启动清单。

- [ ] **Step 7: 提交（.env 不进 git，只提交 SOP）**

```bash
git add scripts/start_dingtalk_bots.md
git commit -m "docs(sop): 观测层上线 SOP（3专业机器人常驻+播报schtasks+cockpit看板）"
```

---

## Self-Review（plan 写完后自检，已修正）

1. **Spec 覆盖**：
   - 三机器人播报（交易/数据/策略）→ Task 3/4/5 ✓
   - @查询转发 Claude Code → Task 13 Step 4（dws --channel claudecode）✓
   - CLI 路由 + 幂等 → Task 2 ✓
   - 后台流水查询 API → Task 1 ✓
   - 实时日志页 → Task 10 ✓
   - 历史回测对比页 → Task 11 ✓
   - 综合看板页 → Task 12 ✓
   - schtasks 配置化 → Task 6 ✓
   - .env 配置 → Task 13 Step 2 ✓
   - 通用机器人复用 → Global Constraints（不新建）✓
   - 群复用 BROADCAST_GROUP_ID → Task 2 `_BOT_CFG` 不含 group（用全局）✓

2. **类型一致性**：`BriefResult`（brief.py 既有，三新 brief 复用）；`TradesPage`/`TradeRecord`（Task 8 定义，Task 9 消费）；`listReplayTasks`/`getReplayTask`（caisen.ts 既有，Task 11 消费）—— 已对齐。

3. **已知需实现时注意**：
   - Task 12 的 `StatusCard`/`AssetCard`/`DataHealthCard` 三小部件需新建（首版简化）。
   - Task 2 的 `_build_brief` 在 Task 2 完成时 trading/data/strategy 分支临时 `NotImplementedError`，Task 3/4/5 分别接入。

4. **诚实边界**：交易 brief「止盈止损」占位（Task 3 测试断言占位字段存在，不造假）✓；第一期全程不下单 ✓。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-21-dingtalk-ops-cockpit.md`. Two execution options:

**1. Subagent-Driven（推荐）** — 每个 Task 派一个全新 subagent 执行，任务间我做 review 关卡，快速迭代，context 隔离干净。

**2. Inline Execution** — 在当前 session 用 executing-plans 批量执行，带 checkpoint 复核。

**Which approach?**
