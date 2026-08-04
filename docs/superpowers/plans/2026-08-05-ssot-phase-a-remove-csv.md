# ssot-final-hardening Phase A 实施计划 · 彻底移除 CSV（精修版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 彻底移除 `logs/live_trades.csv` 写/读路径——submit 审计平移 `trade_event`、成交真相归 `fill` 表、消费端全切 `state_store.query_fills`、CSV 归档、测试去 mock + 契约改 DB、静态护栏锁零引用。SQLite 成为成交流水唯一真相源。

**Architecture:** 删 `record_live_trade` 写口 + 3 读函数 CSV 回退；submit 的 BLOCKED/ORDERED/REJECTED/DRY_RUN 平移 `trade_event`（`UNIQUE(account_id,trade_id,action)` 幂等双写安全）；成交真相由 `fill` 表承担；digest/review 切 `query_fills`；CSV 归档；测试去 mock；静态护栏锁生产代码零引用。

**Tech Stack:** Python 3.10 / SQLite (state_store) / pytest / ripgrep / `.venv310` venv

**关联 spec:** `docs/superpowers/specs/2026-08-05-ssot-final-hardening-design.md` §4 Phase A（spec §A3「保留函数签名」一句已按本 plan 修订：`load_live_fills` 签名 `csv_path→db_path` 不可避免）

## 全局约束

- 全中文注释（CLAUDE.md）；测试 `.venv310/Scripts/python.exe -m pytest <path> -q`。
- **每 task 提交前相关 pytest 全绿**（A1 含既有 5 用例同步改，不留中间红）。
- 不引入新依赖；commit message 中文 + `Co-Authored-By: Claude <noreply@anthropic.com>`。
- **TDD 真红纪律**：每个测试先确认在旧代码下 FAIL（非假红），再实现。DB 异常类测试必须写真实 CSV 行 + `LIVE_TRADE_READ_SOURCE=csv` 证真红。
- **接口事实（已核验，非假设）**：白名单 env = `QMT_SYMBOL_WHITELIST`（dynamic_whitelist.py:54）；`logs/live_trades.csv` 被 .gitignore 忽略（`git check-ignore` 命中）用 `mv`，`scripts/*` 被跟踪用 `git mv`；`finish_run` 只 UPDATE（须先 `begin_run`）；trade_event `timestamp`=写入时间（非计划日）。

## 文件结构

| 文件 | 改动 |
|---|---|
| `presentation/server/services/data_service.py` | A0 承接工作区路径修复（_PROJECT_ROOT 四级）提交 |
| `tests/conftest.py` | A0 加 `tmp_db` fixture（顶层共享） |
| `trading/state_store.py` | A1 `build_trade_id` 单点 + fill 表加 strategy 列 + `insert_fill` 接 strategy 参数 |
| `trading/engine.py` | A1 删 record_live_trade 调用 + `insert_fill` 传 strategy |
| `presentation/server/services/trading_service.py` | A1 submit 平移 + `_resolve_account_id`；A2 三函数删 CSV 分支 + `_EXPORT_COLUMNS`；A4 删 record_live_trade/LIVE_TRADE_LOG/COLUMNS |
| `research/digest.py` | A3 `load_live_fills` 切 query_fills 保 strategy 过滤 |
| 文案清理（13 文件） | A3：review_service/schemas/review/api v1 trading+review/broadcast __main__/digest docstring/engine qmt_smoke 注释/state_store docstring/http config 注释 |
| `logs/live_trades.csv` + `scripts/{migrate,backfill}_*.py` | A4 归档 |
| `tests/`（10+ 文件） | A4 去 mock + CSV 契约 → DB 契约 |
| `tests/test_ssot_static_guard.py` | A5 新增（扫含注释） |

---

### Task A0: data_service 路径修复承接 + tmp_db fixture + trade_id 单点

**Files:**
- Modify: `presentation/server/services/data_service.py`（工作区已改，承接提交）
- Modify: `tests/conftest.py`（加 `tmp_db` fixture）
- Modify: `trading/state_store.py`（加 `build_trade_id`）

**Interfaces:**
- Produces: `tmp_db` fixture（tmp state_store + monkeypatch `_DEFAULT_DB` + account 行）；`state_store.build_trade_id(account_id, symbol, date) -> str`（= `f"{account_id}_{symbol}_{date}"`，消三处复制）

- [ ] **Step 1: 核实 data_service 路径修复已提交（spec §1.4）**

```bash
git log --oneline -1 -- presentation/server/services/data_service.py
# 预期：99f01232 fix(data_service): presentation/ 伞盖收编后项目根上溯四级
# spec §1.4「工作区未提交」前提已不成立（99f01232 已提交）→ A0 跳过承接，只做 tmp_db + build_trade_id
```

- [ ] **Step 2: tests/conftest.py 加 tmp_db fixture**

```python
# tests/conftest.py 追加（与既有 autouse fixture 同文件）
import pytest

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """tmp state_store DB + 默认 account 行（SSoT plan 共享 fixture）。

    物理意图：fill/trade_event/position 读写需 tmp DB 隔离；monkeypatch
    state_store._DEFAULT_DB 让所有未显式传 db_path 的调用落到 tmp。account 行
    预置（FK 引用）。返回 db_path str。
    """
    from trading import state_store
    db = tmp_path / "state.db"
    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(db))
    state_store.init_store(str(db))
    state_store.upsert_account("ACC_TEST", broker="qmt")
    return str(db)
```

- [ ] **Step 3: state_store 加 build_trade_id 单点**

`trading/state_store.py` 加（紧邻 `insert_trade_event`）：

```python
def build_trade_id(account_id: str, symbol: str, date: str) -> str:
    """构造 trade_id（account_symbol_date）。单点（消 engine:636/3126 + submit_order +
    trading_plan:119 三处复制）。date 是【计划生效日】（next_trading_day，非写入日）。"""
    return f"{account_id}_{symbol}_{date}"
```

- [ ] **Step 4: 运行既有测试确认无回归 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/ -q --ignore=tests/e2e_long_cycle -x`
Expected: 既有用例全绿（fixture/build_trade_id 是新增，不改既有）

```bash
git add tests/conftest.py trading/state_store.py
git commit -m "feat(ssot-A0): tmp_db fixture + build_trade_id 单点

- tests/conftest.py 顶层 tmp_db（tmp state_store + account 行）
- state_store.build_trade_id（消三处 trade_id 构造复制）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task A1: submit_order 审计平移 trade_event + fill 加 strategy 列 + 既有用例同改

**Files:**
- Modify: `trading/state_store.py`（fill 表加 strategy 列 + `insert_fill` 接 strategy）
- Modify: `trading/engine.py:3143`（insert_fill 传 strategy）+ `:3200-3213`（删 record_live_trade 调用）
- Modify: `presentation/server/services/trading_service.py:617-693`（submit 平移）+ 加 `_resolve_account_id`
- Modify: `tests/test_trading_service.py:142-160,171,193,225,250`（既有 5 用例 + 新测试同 commit）

**Interfaces:**
- Consumes: `state_store.insert_trade_event(...)` UNIQUE 幂等；`build_trade_id`；`clock.today()`
- Produces: submit 写 trade_event(ORDERED/BLOCKED/REJECTED/DRY_RUN)；fill 表有 strategy 列；engine 不再写 CSV

- [ ] **Step 1: fill 表加 strategy 列 + insert_fill 接 strategy（新断点-4，保 digest 口径）**

`state_store.py:174-186` fill DDL 加列 + `init_store` 迁移：

```python
# fill DDL 加列（与 account_id 迁移同范式 :187-191）
CREATE TABLE IF NOT EXISTS fill (
    ...,
    account_id TEXT REFERENCES account(account_id),
    strategy TEXT   -- 新断点-4：保 digest strategy 过滤口径（原 CSV strategy 列）
)
# init_store 迁移段：
if not _has_column(con, "fill", "strategy"):
    con.execute("ALTER TABLE fill ADD COLUMN strategy TEXT")
```

`insert_fill`（state_store.py:526）签名加 `strategy: str | None = None`，INSERT 补 `strategy` 列。

- [ ] **Step 2: engine insert_fill 传 strategy + 删 record_live_trade 调用**

`engine.py:3143-3145` `insert_fill(order_id, _account_id, traded_time, symbol, direction, float(qty), float(price))` 加 `strategy="neckline"`（与原 record_live_trade :3208 同硬编码；C1 后改从 SIGNAL.meta 取）。

`engine.py:3200-3213` 整块 `try: from ...trading_service import record_live_trade; record_live_trade(...) except ...` 删除（保留下方 NotificationManager 钉钉块）。

- [ ] **Step 3: trading_service 加 _resolve_account_id + submit 平移**

`trading_service.py` 加（注释锁口径）：

```python
def _resolve_account_id() -> str:
    """submit_order 写 trade_event 用（UNIQUE 键）。与 engine._resolve_account_id:455 同口径
    （QMT_ACCOUNT_ID env 优先）——本地实现避免 import engine 循环。改口径须两处同步。"""
    return os.getenv("QMT_ACCOUNT_ID") or "default"
```

`trading_service.py:659-693` submit_order 三处 `record_live_trade` 改 `insert_trade_event`（用 `build_trade_id`）：

```python
from trading import state_store, clock

# :659-674 挡板命中
if decision.blocked:
    _aid = _resolve_account_id()
    if decision.is_dry_run:
        state_store.insert_trade_event(
            _aid, state_store.build_trade_id(_aid, order.symbol, clock.today()),
            order.symbol, "DRY_RUN", qty=float(order.qty),
            price=float(order.price or 0.0), meta=decision.reason)
        return {"order_id": "", "state": "DRY_RUN", "message": decision.reason}
    state_store.insert_trade_event(
        _aid, state_store.build_trade_id(_aid, order.symbol, clock.today()),
        order.symbol, "BLOCKED", qty=float(order.qty),
        price=float(order.price or 0.0), meta=f"{decision.stage}:{decision.reason}")
    raise RuntimeError(decision.reason)

# :677-693 真单成功（双写幂等：engine 已写 ORDERED，UNIQUE 跳过；手动路径首次写）
result: OrderResult = await gw.submit_order(order)
_aid = _resolve_account_id()
_action = "ORDERED" if result.state.name not in ("REJECTED", "FAILED") else "REJECTED"
state_store.insert_trade_event(
    _aid, state_store.build_trade_id(_aid, order.symbol, clock.today()),
    order.symbol, _action, order_id=result.order_id,
    qty=float(order.qty), price=float(order.price or 0.0),
    meta=f"{gw.__class__.__name__}:{result.state.name}:{result.message}")
return {"order_id": result.order_id, "state": result.state.name, "message": result.message}
```

- [ ] **Step 4: 写新测试 + 同 commit 改既有 5 用例**

`tests/test_trading_service.py` 追加（**OrderRequest 直接构造**，参考 :155；**env = QMT_SYMBOL_WHITELIST**；断言 **symbol + meta**，非恒真 action）：

```python
def test_submit_order_blocked_writes_trade_event(tmp_db, monkeypatch):
    """挡板命中（白名单外）→ trade_event(BLOCKED) 落库（断点-1）。"""
    import presentation.server.services.trading_service as svc
    from trading import state_store
    from presentation.server.schemas.trading import OrderRequest  # 路径以实际为准（参考 :155）
    monkeypatch.setattr(svc, "_resolve_account_id", lambda: "ACC_TEST")
    monkeypatch.setenv("QMT_SYMBOL_WHITELIST", "600000.SH")  # 仅 600000 放行
    order = OrderRequest(symbol="600001.SH", qty=100, side="buy", price=10.0)
    import pytest, asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(svc.submit_order(order, dry_run=False, confirm=False))
    import sqlite3
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    hit = con.execute("SELECT symbol, meta FROM trade_event WHERE action='BLOCKED'").fetchone()
    assert hit is not None
    assert hit["symbol"] == "600001.SH"
    assert "whitelist" in (hit["meta"] or "").lower()  # meta 含白名单拒因（symbol 已断言；去 OR 恒真）
```

**既有 5 用例同 commit 改**（`:152,171,193,225,250`）：删 `monkeypatch.setattr(trading_service, "record_live_trade", ...)`（函数 A4 删，本 task 先去 patch 避免中间红）；`:207-232` 真单审计断言改查 `trade_event ORDERED 行`（替代 record_live_trade called）。

- [ ] **Step 5: 运行验证 + 提交（含既有用例，无中间红）**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_trading_service.py -q`
Expected: PASS（新测试 + 既有 5 用例全绿）

```bash
git add trading/state_store.py trading/engine.py presentation/server/services/trading_service.py tests/test_trading_service.py
git commit -m "feat(ssot-A1): submit 审计平移 trade_event + fill strategy 列 + 既有用例同改

- submit BLOCKED/ORDERED/REJECTED/DRY_RUN 平移 trade_event（build_trade_id 单点）
- _resolve_account_id（与 engine 同口径，注释锁）
- fill 表加 strategy 列（新断点-4，保 digest 过滤口径）+ insert_fill 传 strategy
- engine 删 record_live_trade 调用 + 5 既有用例去 mock（无中间红）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task A2: 删 LIVE_TRADE_READ_SOURCE 读回退 + _EXPORT_COLUMNS

**Files:**
- Modify: `trading_service.py:246-304`（aggregate）、`306-376`（export）、`379-496`（query）
- Test: 新增 `tests/server/test_trading_reads_db_only.py`

**Interfaces:**
- Produces: 三函数 DB-only；`export/query` 用 `_EXPORT_COLUMNS`（为 A4 删 `LIVE_TRADE_COLUMNS` 铺路）；DB 异常 → `logger.exception` + 空/降级。

- [ ] **Step 1: 写真红测试（写真实 CSV + LIVE_TRADE_READ_SOURCE=csv，旧代码返非空）**

```python
# tests/server/test_trading_reads_db_only.py
def test_query_trades_no_csv_fallback_when_db_empty(tmp_db, monkeypatch, tmp_path):
    """DB 空 + 磁盘有 CSV 行 + LIVE_TRADE_READ_SOURCE=csv：旧代码回退返 CSV 行；
    新代码（删回退）返空。真红证明 CSV 回退被删。"""
    import presentation.server.services.trading_service as svc
    # 写真实 CSV 行到 tmp LIVE_TRADE_LOG
    csv_log = tmp_path / "live_trades.csv"
    svc._write_csv_row(csv_log, symbol="600000.SH", kind="fill")  # 辅助或直接写 DictWriter
    monkeypatch.setattr(svc, "LIVE_TRADE_LOG", str(csv_log))
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")  # 强制走 CSV 分支
    res = svc.query_trades("2000-01-01", "2099-12-31")
    # 旧代码：CSV 回退返 1 行 → 新代码：DB-only 返 0 行
    assert res["total"] == 0  # 证明 CSV 回退已删

def test_aggregate_fills_db_exception_returns_empty(tmp_db, monkeypatch, tmp_path):
    """patch query_fills 抛错 + 磁盘有 CSV（旧代码回退返净持仓）；新代码返 {}。"""
    from trading import state_store
    import presentation.server.services.trading_service as svc
    csv_log = tmp_path / "live_trades.csv"
    svc._write_csv_row(csv_log, symbol="600000.SH", kind="fill", direction="BUY", shares=100)
    monkeypatch.setattr(svc, "LIVE_TRADE_LOG", str(csv_log))
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    monkeypatch.setattr(state_store, "query_fills", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert svc.aggregate_fills_by_symbol("2000-01-01", "2099-12-31") == {}  # 不回退 CSV
```

（`_write_csv_row` 测试辅助在 conftest 或本文件内补，写 utf-8-sig BOM + DictWriter，与 record_live_trade 同口径）

- [ ] **Step 2: 运行验证真红**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_trading_reads_db_only.py -q`
Expected: FAIL（旧代码 CSV 回退返非空，`total==0` 断言失败）

- [ ] **Step 3: 三函数删 CSV 分支 + 抽 _EXPORT_COLUMNS**

`trading_service.py` 顶部加（A4 删 `LIVE_TRADE_COLUMNS` 前的过渡单点）：

```python
_EXPORT_COLUMNS = ["timestamp", "symbol", "direction", "shares", "price",
                   "strategy", "rationale", "kind"]  # 前端下载契约（A2 抽出，A4 删 LIVE_TRADE_COLUMNS）
```

`aggregate/export/query` 删 `LIVE_TRADE_READ_SOURCE` 分支 + CSV 读口；`export/query` 的格式化改用 `_EXPORT_COLUMNS`（替代 `LIVE_TRADE_COLUMNS`）；DB 异常 → `logger.exception` + 空/仅表头。

- [ ] **Step 4: 运行 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_trading_reads_db_only.py -q`
Expected: PASS（CSV 回退已删，DB-only 返空）

```bash
git add presentation/server/services/trading_service.py tests/server/test_trading_reads_db_only.py
git commit -m "feat(ssot-A2): 删 LIVE_TRADE_READ_SOURCE 回退 + _EXPORT_COLUMNS 抽出

- 三函数 DB-only（aggregate/export/query）
- DB 异常 logger.exception + 空/降级，不回退 CSV
- export/query 抽 _EXPORT_COLUMNS（为 A4 删 LIVE_TRADE_COLUMNS 铺路）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task A3: digest 切 query_fills（保 strategy 过滤）+ 文案清单补全

**Files:**
- Modify: `research/digest.py:189-214,5,16,250`
- Modify: 文案 13 文件（见 step 4）
- Test: `tests/research/test_digest.py:59-71`
- Modify spec: `docs/superpowers/specs/2026-08-05-ssot-final-hardening-design.md` §A3「保留函数签名」→「签名变更」

**Interfaces:**
- Produces: `load_live_fills(db_path=None) -> list[dict]`（**保 strategy 非空过滤**，读 `fill.strategy`——A1 已加列）

- [ ] **Step 1: 改 test_digest（保 strategy 过滤，DB 构造）**

```python
def test_load_live_fills_filters_empty_strategy(tmp_db):
    """load_live_fills 读 fill 表，保 strategy 非空过滤（新断点-4，原 CSV 口径）。
    strategy 空的 fill 不进 digest 样本。"""
    from trading import state_store
    state_store.init_store(tmp_db)
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.insert_fill("O1", "ACC1", "20260805101000", "600000.SH", "BUY", 100, 10.0, strategy="neckline")
    state_store.insert_fill("O2", "ACC1", "20260805101100", "600001.SH", "BUY", 200, 20.0, strategy=None)  # 过滤掉
    from research import digest
    fills = digest.load_live_fills(db_path=tmp_db)
    assert len(fills) == 1 and fills[0]["symbol"] == "600000.SH"
```

- [ ] **Step 2: 改 load_live_fills（签名 csv_path→db_path，保 strategy 过滤）**

`research/digest.py:189-214` 替换：

```python
def load_live_fills(db_path: str | None = None) -> list[dict]:
    """读 state_store.fill 并清洗（A3 切 DB · 保 strategy 非空过滤，新断点-4）。

    签名变更：csv_path → db_path（spec §A3 已同步修订）。fill 表 A1 加 strategy 列，
    本函数保留原 CSV 时代的 strategy 非空过滤（只保留有归因的成交，丢弃补录空行），
    digest 实盘样本口径不变。DB 异常 → []。
    """
    try:
        from trading import state_store
        state_store.init_store(db_path)
        rows = state_store.query_fills("2000-01-01", "2099-12-31", db_path=db_path)
    except Exception:
        logger.exception("load_live_fills 读 state_store 失败，返空")
        return []
    fills = []
    for r in rows:
        strategy = (r.get("strategy") or "").strip()  # A1 fill.strategy 列
        if not strategy:  # 保口径：补录空 strategy 行丢弃
            continue
        tt = str(r.get("traded_time") or "")
        ts = (f"{tt[0:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]}:{tt[12:14]}"
              if len(tt) >= 14 else tt)
        fills.append({
            "timestamp": ts, "symbol": r.get("symbol", ""),
            "direction": (r.get("direction") or "").upper(),
            "shares": r.get("shares"), "price": r.get("price"),
            "strategy": strategy, "rationale": "", "kind": "fill"})
    return fills
```

- [ ] **Step 3: spec §A3「保留签名」同步修订**

`docs/superpowers/specs/2026-08-05-ssot-final-hardening-design.md` A3 段「保留函数签名与去重逻辑」→「保留去重逻辑；签名 `csv_path→db_path`（源从 CSV 变 DB，不可避免）」。

- [ ] **Step 4: 文案清单补全（13 文件，A5 护栏会扫含注释）**

逐文件改「读 logs/live_trades.csv」→「读 state_store.fill」：
- `research/digest.py:5,16,250`、`presentation/server/services/review_service.py:98-109`（保留 csv_text 用户上传语义）、`schemas/review.py:17`、`api/v1/trading.py:83,106`、`api/v1/review.py:28`、`broadcast/__main__.py:169,172`、`trading/engine.py` 相关注释、`trading/state_store.py:5` docstring、`presentation/server/http/config.py:21-22` 注释、`trading/tools/qmt_smoke.py:109` 注释。
- 先 `rg "live_trades\.csv" trading presentation broadcast research --glob '*.py' --glob '!tests/**'` 取全量清单，逐个改。

- [ ] **Step 5: 运行 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/research/test_digest.py -q`

```bash
git add research/digest.py presentation/ broadcast/__main__.py docs/superpowers/specs/2026-08-05-ssot-final-hardening-design.md tests/research/test_digest.py
git commit -m "feat(ssot-A3): digest 切 query_fills 保 strategy 过滤 + 文案清单补全

- load_live_fills 签名 csv_path→db_path（spec §A3 同步）
- 保 strategy 非空过滤（fill.strategy 列，新断点-4，digest 口径不变）
- 13 文件文案/docstring 清理（保留 review.csv_text 上传入口）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task A4: 归档 + 删 record_live_trade 函数 + 测试改造全清单

**Files:**
- Archive: `logs/live_trades.csv`（`mv`，非 git mv）→ `logs/archive/`；`scripts/{migrate,backfill}_*.py`（`git mv`）→ `scripts/archive/`
- Delete: `trading_service.py:37-44`（LIVE_TRADE_LOG/COLUMNS）、`:212-247`（record_live_trade）
- Test 改造（10+ 文件）

| 文件 | 改动 |
|---|---|
| `tests/trading/test_live_trades_csv.py` | 删 → 新建 `test_fill_db_contract.py` |
| `tests/test_backfill_live_trades.py` | 删或改指向 `scripts/archive/backfill_...`（脚本归档后避免 ModuleNotFoundError） |
| `tests/server/test_trading_trades.py` | `LIVE_TRADE_READ_SOURCE=csv` 契约 → tmp state_store.fill 构造 |
| `tests/server/test_review_service_db.py:19-23` | patch LIVE_TRADE_LOG → tmp state_store.fill |
| `tests/test_trading_api.py:37,58` | 删 patch record_live_trade |
| `tests/trading/test_engine.py:1420,1435,1479` | 删 patch record_live_trade（engine 已不调） |
| `tests/trading/test_e2e_trading_flow.py:225` | 删 patch record_live_trade |
| `tests/e2e_long_cycle/conftest.py:29-60` | 删 record_live_trade mock + 改注释 |
| `tests/e2e_long_cycle/test_probabilistic_broker.py:97,178` | 删 patch record_live_trade |

- [ ] **Step 1: 归档（mv vs git mv 区分）**

```bash
mkdir -p logs/archive scripts/archive
[ -f logs/live_trades.csv ] && mv logs/live_trades.csv logs/archive/live_trades.csv.final-20260805  # 被 .gitignore 忽略，用 mv
git mv scripts/migrate_live_trades_csv.py scripts/archive/migrate_live_trades_csv.py  # 被跟踪
git mv scripts/backfill_live_trades_to_state_store.py scripts/archive/backfill_live_trades_to_state_store.py
```

- [ ] **Step 2: 写 fill 契约测试（替代 test_live_trades_csv.py）**

`git rm tests/trading/test_live_trades_csv.py`；新建 `tests/trading/test_fill_db_contract.py`（fill UNIQUE 幂等 + 部分成交各自一行 + strategy 列）。

- [ ] **Step 3: 删 record_live_trade + LIVE_TRADE_LOG/COLUMNS**

确认 `aggregate/export/query` 已用 `_EXPORT_COLUMNS`（A2），无 `LIVE_TRADE_COLUMNS` 引用 → 删 `trading_service.py:37-44,212-247`。

- [ ] **Step 4: 测试改造全清单（按上表，含 test_backfill）**

- [ ] **Step 5: 全量验证 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS

```bash
git add trading/ presentation/ broadcast/ research/ scripts/ tests/ logs/archive/  # 显式（-A 会扫进 docs/ 等无关 untracked）
git commit -m "feat(ssot-A4): 归档 + 删 record_live_trade + 测试改造全清单

- logs/live_trades.csv mv（.gitignore 忽略）+ scripts git mv 到 archive/
- 删 record_live_trade/LIVE_TRADE_LOG/LIVE_TRADE_COLUMNS
- test_backfill + 9 测试文件去 mock + DB 契约
- test_fill_db_contract 替代 test_live_trades_csv

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task A5: 静态护栏（扫含注释）

**Files:** Create `tests/test_ssot_static_guard.py`

- [ ] **Step 1: 写护栏（扫生产代码含注释，BANNED 含 live_trades 全家族）**

```python
"""SSoT 静态护栏：生产代码零 live_trades.csv/LIVE_TRADE_*/record_live_trade 引用（含注释）。

ripgrep 扫生产目录（含注释——文案债也是债），命中即 FAIL。archive/ 与 tests/ 排除。
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROD_DIRS = ["trading", "presentation/server", "broadcast", "research", "scripts"]
BANNED = ["live_trades.csv", "LIVE_TRADE_READ_SOURCE", "record_live_trade", "LIVE_TRADE_LOG"]

def _rg(pattern: str) -> list[str]:
    cmd = ["rg", "-n", "--glob=*.py", "--glob=!**/archive/**", "--glob=!tests/**",
           pattern, *PROD_DIRS]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        out = subprocess.run(["grep", "-rn", "--include=*.py", pattern, *PROD_DIRS],
                             cwd=ROOT, capture_output=True, text=True, check=False).stdout
    return [l for l in out.splitlines() if l.strip()]

def test_no_live_trades_reference():
    hits = []
    for p in BANNED:
        hits.extend(_rg(p))
    assert not hits, "SSoT 护栏命中：\n" + "\n".join(hits)

def test_no_disk_live_trades_csv():
    assert not (ROOT / "logs" / "live_trades.csv").exists()
```

- [ ] **Step 2: 运行 + 提交**

```bash
.venv310/Scripts/python.exe -m pytest tests/test_ssot_static_guard.py -q
git add tests/test_ssot_static_guard.py
git commit -m "feat(ssot-A5): 静态护栏扫含注释，锁生产代码零 CSV 引用

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase A 完成验收

- [ ] `.venv310/Scripts/python.exe -m pytest tests/ -q` 全绿
- [ ] `rg "record_live_trade|LIVE_TRADE_LOG|LIVE_TRADE_READ_SOURCE|live_trades\.csv" trading presentation broadcast research scripts --glob '*.py' --glob '!**/archive/**' --glob '!tests/**'` = 0（含注释）
- [ ] `logs/live_trades.csv` 不存在（仅 `logs/archive/`）
- [ ] submit BLOCKED/ORDERED/REJECTED/DRY_RUN 在 trade_event 可查；engine 路径 ORDERED 幂等（仅 1 行）
- [ ] `export_trades` 输出 shape 与旧 CSV 同构（`_EXPORT_COLUMNS` 表头）

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 删 CSV 后 submit 审计丢失 | A1 平移 trade_event（UNIQUE 幂等双写） |
| digest strategy 口径变化 | A1 fill 加 strategy 列 + A3 保过滤（新断点-4） |
| 测试改造遗漏 AttributeError | A4 全清单 + 全量 pytest |
| 中间红 | A1 既有 5 用例同 commit 改 |

回滚：每 task 一 commit；tag `ssot-phase-a-done`。
