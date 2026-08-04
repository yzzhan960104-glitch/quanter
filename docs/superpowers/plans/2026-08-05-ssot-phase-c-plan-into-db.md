# ssot-final-hardening Phase C 实施计划 · 计划内容入 DB（精修版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交易计划内容源从 `logs/trading_plans/plan_<date>.json` 迁到 `trade_event(SIGNAL).meta`——消费方全切 DB，删 `save_plan/confirm_plan`，`load_plan` 降级只读兼容窗口；归因重建从 meta 补（弥补 B2 重启窗口）。

**Architecture:** `SIGNAL.meta`（**C1 补 plan_date/strategy_name/rationale 字段**）升格唯一源；消费方按 **meta.plan_date**（C2a）/ **meta.formed_at**（C2b）查（**非 timestamp**——timestamp 是写入日 T，查计划日 T+1 恒 0）；trade_id 用 `build_trade_id` 单点；`load_plan` DB 优先 + JSON 回退（兼容窗口）。

**Tech Stack:** Python 3.10 / SQLite / pytest / `.venv310`

**关联 spec:** `docs/superpowers/specs/2026-08-05-ssot-final-hardening-design.md` §6 Phase C

**前置依赖:** Phase A + B 完成（`tmp_db` + `build_trade_id` + position.strategy 列由 A/B 提供）。**实施前确认 A/B 已合入。**

## 全局约束

- 全中文注释；测试 `.venv310/Scripts/python.exe -m pytest <path> -q`。
- commit 中文 + `Co-Authored-By: Claude <noreply@anthropic.com>`。
- **致命事实（已核验）**：`trade_event.timestamp` = `clock.now()` 写入时间（state_store.py:337）= T 日盘后，**不是**计划日 T+1。计划日仅在 `trade_id` 后缀（`{aid}_{symbol}_{plan_date}`）+ `meta.formed_at`（信号形成日 T）。**所有按 timestamp 查计划日的代码恒错**。
- **meta 字段事实（已核验 engine.py:605-639）**：当前 order_dict 含 order/stop_price/take_profit/neckline/atr/formed_at/max_wait/tp1/cancel_on/experiment_id/experiment_weight/rr，**无 strategy/rationale/plan_date**——C1 必须补。

## 文件结构

| 文件 | 改动 |
|---|---|
| `trading/engine.py:605-639` | C1 eod_plan meta 补 plan_date/strategy_name/rationale |
| `trading/state_store.py` | C1 `rebuild_position_attribution`（读真实字段）；C2 新增 `list_signals_by_plan_date`/`count_signals_by_plan_date`（trade_id LIKE） |
| `trading/engine.py:318-375` | C2b `_load_recent_plan_symbols` 按 meta.formed_at |
| `trading/engine.py` pre_open/_stoploss | C2c 切 get_trade_plan + build_trade_id |
| `trading/review_report.py:62` + `presentation/server/services/review_service.py` | C2c 复盘读方切 DB（spec 漏 review_report） |
| `broadcast/__main__.py:403-420` | C2a scan_count 按 plan_date（trade_id LIKE） |
| `experiment/cli.py:32-45` | C2d report 按 plan_date（since 过滤） |
| `trading/tools/{trigger_eod_once,smoke_trading_engine}.py` | C2d 切 DB 或归档 |
| `trading/trading_plan.py:41-119` | C3 删 save_plan/confirm_plan；load_plan DB 优先 |

---

### Task C1: SIGNAL.meta 补字段 + 归因重建（读真实字段）

**Files:**
- Modify: `trading/engine.py:605-639`（eod_plan meta 补 plan_date/strategy_name/rationale）
- Add: `trading/state_store.py` `rebuild_position_attribution`
- Modify: `trading/engine.py` lifespan（启动补扫调用）
- Test: `tests/trading/test_state_store.py`、`tests/trading/test_engine.py:142`（eod_plan meta 断言扩展）

**Interfaces:**
- Produces: SIGNAL.meta 含 plan_date/strategy_name/rationale；`rebuild_position_attribution(account_id) -> int`（读真实 strategy_name，非默认）

- [ ] **Step 1: eod_plan meta 补字段（C2 前置）**

`engine.py:638-639` 改（meta 补 plan_date/strategy_name/rationale）：

```python
# 原：meta=json.dumps(o, ensure_ascii=False)
# 新：补 plan_date（=date 计划生效日 T+1）+ strategy_name（当前单策略）+ rationale
meta_obj = {**o, "plan_date": date, "strategy_name": "neckline",
            "rationale": f"颈线法@{o.get('formed_at', '')}"}
_state_store.insert_trade_event(
    account_id, trade_id, sym, "SIGNAL", meta=json.dumps(meta_obj, ensure_ascii=False))
```

`tests/trading/test_engine.py:142` 扩展断言：`meta` 含 `plan_date`/`strategy_name`。

- [ ] **Step 2: 写归因重建测试（真实 meta shape + IS NULL 覆盖）**

`tests/trading/test_state_store.py` 追加：

```python
def test_rebuild_position_attribution_reads_real_meta(tmp_db):
    """rebuild 从 SIGNAL.meta 真实 strategy_name 回填（C1，非默认 neckline）。"""
    from trading import state_store
    import json
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "BUY", 100, 10.0, "20260805")
    # 真实 meta shape（engine.py:605-639 order_dict + C1 补字段）
    state_store.insert_trade_event(
        "ACC_TEST", state_store.build_trade_id("ACC_TEST", "600000.SH", "2026-08-05"),
        "600000.SH", "SIGNAL",
        meta=json.dumps({"order": {"symbol": "600000.SH"}, "formed_at": "2026-08-04",
                         "plan_date": "2026-08-05", "strategy_name": "neckline",
                         "rationale": "颈线法@2026-08-04"}))
    n = state_store.rebuild_position_attribution("ACC_TEST")
    assert n == 1
    row = state_store.get_position("ACC_TEST", "600000.SH")
    assert row["strategy"] == "neckline" and row["entry_rationale"] == "颈线法@2026-08-04"

def test_rebuild_skips_already_attributed(tmp_db):
    """已写归因的行（strategy IS NOT NULL）不被覆盖（IS NULL 守卫）。"""
    from trading import state_store
    import json
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "BUY", 100, 10.0, "20260805")
    state_store.upsert_position_attribution("ACC_TEST", "600000.SH", "manual", "人工")
    state_store.insert_trade_event("ACC_TEST", state_store.build_trade_id("ACC_TEST", "600000.SH", "2026-08-05"),
        "600000.SH", "SIGNAL", meta=json.dumps({"strategy_name": "neckline", "formed_at": "2026-08-04"}))
    state_store.rebuild_position_attribution("ACC_TEST")
    assert state_store.get_position("ACC_TEST", "600000.SH")["strategy"] == "manual"  # 不覆盖
```

- [ ] **Step 3: rebuild_position_attribution（读真实 strategy_name + IS NULL 守卫）**

`state_store.py` 加：

```python
def rebuild_position_attribution(account_id: str, *, db_path: str | None = None) -> int:
    """从 trade_event(SIGNAL).meta 回填 position.strategy/entry_rationale（C1）。

    读 meta 真实 strategy_name/rationale（C1 补字段，非默认）；只回填 strategy IS NULL 的行
    （不覆盖 B2 已写）；无 SIGNAL 跳过。返回回填行数。
    """
    db_path = db_path or _DEFAULT_DB
    n = 0
    with _connect(db_path) as con:
        positions = con.execute(
            "SELECT symbol FROM position WHERE account_id=? AND (strategy IS NULL OR strategy='')",
            (account_id,)).fetchall()
        for p in positions:
            row = con.execute(
                "SELECT meta FROM trade_event WHERE account_id=? AND symbol=? AND action='SIGNAL'"
                " ORDER BY event_id DESC LIMIT 1", (account_id, p["symbol"])).fetchone()
            if not row or not row["meta"]:
                continue
            try:
                meta = json.loads(row["meta"])
            except Exception:
                continue
            strategy = meta.get("strategy_name") or "neckline"  # 真实字段（C1），兜底单策略
            rationale = meta.get("rationale") or f"颈线法@{meta.get('formed_at', '')}"
            con.execute("UPDATE position SET strategy=?, entry_rationale=? WHERE account_id=? AND symbol=? AND (strategy IS NULL OR strategy='')",
                        (strategy, rationale, account_id, p["symbol"]))
            n += 1
    return n
```

- [ ] **Step 4: engine lifespan 启动补扫 + 运行 + 提交**

```python
# lifespan 启动序列（参考 C-8 补跑位置）：
try:
    _n = _state_store.rebuild_position_attribution(_resolve_account_id())
    if _n: logger.info("启动归因重建：从 SIGNAL.meta 回填 %d 个持仓归因", _n)
except Exception:
    logger.exception("启动归因重建失败（不阻断）")
```

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_state_store.py tests/trading/test_engine.py -q`

```bash
git add trading/engine.py trading/state_store.py tests/
git commit -m "feat(ssot-C1): SIGNAL.meta 补 plan_date/strategy_name + 归因重建读真实字段

- eod_plan meta 补 plan_date/strategy_name/rationale（C2 前置）
- rebuild_position_attribution 读真实 strategy_name（非默认）+ IS NULL 守卫
- lifespan 启动补扫归因重建（弥补 B2 重启窗口）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task C2: 消费方切 DB（按 plan_date/formed_at，非 timestamp）

#### C2a: broadcast scan_count 按 plan_date（trade_id LIKE）

**Files:** `broadcast/__main__.py:403-420`、`broadcast/brief_strategy.py:7`（docstring）
**Test:** `tests/test_broadcast_main.py:91-120`

- [ ] **Step 1: scan_count 按 plan_date 测试**

```python
def test_scan_count_by_plan_date(tmp_db, monkeypatch):
    """scan_count = 计划日=next_trading_day(date) 的 SIGNAL 数（按 trade_id 后缀，非 timestamp）。"""
    from trading import state_store
    state_store.insert_trade_event("ACC_TEST", state_store.build_trade_id("ACC_TEST", "A.SH", "2026-08-05"),
        "A.SH", "SIGNAL", meta='{"plan_date":"2026-08-05"}')
    state_store.insert_trade_event("ACC_TEST", state_store.build_trade_id("ACC_TEST", "B.SH", "2026-08-05"),
        "B.SH", "SIGNAL", meta='{"plan_date":"2026-08-05"}')
    import broadcast.__main__ as bm
    monkeypatch.setattr(bm, "_experiment_active_state", lambda: None)
    # 直接调 _fetch_strategy_snapshot（不 patch 未实现的 helper），断言 scan_count 走 DB
    _, _, _ = bm._fetch_strategy_snapshot  # 确保 _fetch_strategy_snapshot 已切 DB（C2a Step 2 改造后）
    scan_count, _, _ = bm._fetch_strategy_snapshot("2026-08-04")  # next_trading_day→2026-08-05
    assert scan_count == 2  # 两个 SIGNAL（A.SH/B.SH 计划日 2026-08-05）
```

- [ ] **Step 2: scan_count 切 trade_id LIKE（非 timestamp）**

`broadcast/__main__.py:403-420` 替换：

```python
scan_count: int | None = None
try:
    from trading import state_store
    from trading.calendar import next_trading_day
    plan_date = next_trading_day(date)  # T 日盘后产出的 T+1 计划
    # 按 trade_id 后缀查（trade_id={aid}_{symbol}_{plan_date}），非 timestamp（写入日 T≠计划日 T+1）
    scan_count = state_store.count_signals_by_plan_date(plan_date)
except Exception:
    logger.exception("scan_count 读 DB 失败，降级 None"); scan_count = None
```

`state_store.count_signals_by_plan_date(plan_date)` 新增（`SELECT COUNT(*) FROM trade_event WHERE action='SIGNAL' AND substr(trade_id, -10) = ?`，param `plan_date`——用 `substr(trade_id,-10)` 而非 `LIKE '%_date'`，避免 `_` 是 LIKE 通配符的歧义；**去重口径 docstring**：同 symbol 单日多信号理论上不出现，COUNT(DISTINCT symbol) 与 COUNT(*) 等价，用 DISTINCT 保守）。

- [ ] **Step 3: 运行 + 提交**

```bash
.venv310/Scripts/python.exe -m pytest tests/test_broadcast_main.py -q
git commit -m "feat(ssot-C2a): scan_count 按 plan_date（trade_id LIKE，非 timestamp）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

#### C2b: engine _load_recent_plan_symbols 按 meta.formed_at

**Files:** `trading/engine.py:318-375`
**Test:** `tests/trading/test_engine_eod_injection.py:328,392`

- [ ] **Step 1: cooldown 按 formed_at 测试**

```python
def test_load_recent_plan_symbols_by_formed_at(tmp_db):
    """cooldown 扫最近 N 日 SIGNAL.symbol（按 meta.formed_at，非 timestamp）。"""
    from trading import state_store
    import json
    state_store.insert_trade_event("ACC_TEST", state_store.build_trade_id("ACC_TEST", "A.SH", "2026-08-05"),
        "A.SH", "SIGNAL", meta=json.dumps({"formed_at": "2026-08-03"}))
    from trading import engine
    syms = engine._load_recent_plan_symbols(days_back=3, today="2026-08-05")
    assert "A.SH" in syms
```

- [ ] **Step 2: _load_recent_plan_symbols 切 formed_at**

`engine.py:318-375` 替换（删 plan_dir/JSON 扫描）：

```python
def _load_recent_plan_symbols(days_back: int, today: str) -> set[str]:
    """扫最近 days_back 自然日 SIGNAL.symbol（C2b，按 meta.formed_at，原扫 plan JSON）。

    formed_at（信号突破日）是 cooldown 锚点（非 timestamp 写入日）。查 trade_event SIGNAL
    按 json_extract(meta,'$.formed_at') 过滤最近 N 自然日。
    """
    from datetime import datetime as _dt, timedelta as _td
    from trading import state_store
    today_dt = _dt.strptime(today, "%Y-%m-%d")
    dates = [(today_dt - _td(days=i)).strftime("%Y-%m-%d") for i in range(days_back)]
    try:
        return state_store.list_signal_symbols_by_formed_at(dates)  # substr(json_extract(meta,'$.formed_at'),1,10) IN dates
    except Exception:
        logger.exception("_load_recent_plan_symbols 读 DB 失败，返空集"); return set()
```

`state_store.list_signal_symbols_by_formed_at(dates)` 新增（`SELECT DISTINCT symbol FROM trade_event WHERE action='SIGNAL' AND substr(json_extract(meta,'$.formed_at'),1,10) IN (...)`）。

**关键**：`meta.formed_at` 是 `"2026-08-03 00:00:00"`（pandas Timestamp 经 `str()` 落盘，带时间——`method_v0.py:268` `W.index[-1]` → `compute/plan.py:158` `str(s.formed_at)`），**非纯日期**。必须 `substr(...,1,10)` 取前 10 字符（YYYY-MM-DD）匹配，否则 `json_extract IN (纯日期)` 恒空——同款查询轴坑。

- [ ] **Step 3: 运行 + 提交**

```bash
.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_eod_injection.py -q
git commit -m "feat(ssot-C2b): _load_recent_plan_symbols 按 meta.formed_at（cooldown）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

#### C2c: pre_open / _stoploss / review_report（含 review_report.py:62）切 get_trade_plan + build_trade_id

**Files:** `trading/engine.py`（grep load_plan 调用点）、`trading/review_report.py:62`（spec 漏）、`presentation/server/services/review_service.py`

- [ ] **Step 1: grep 定位全部 load_plan 调用点（含 review_report）**

```bash
rg "load_plan" trading/ presentation/ --glob '*.py' --glob '!tests/**'
```
确认 `trading/review_report.py:62` + engine pre_open/_stoploss + review_service 全列入清单。

- [ ] **Step 2: 逐调用点切 get_trade_plan + build_trade_id**

```python
# 原：plan = trading_plan.load_plan(date); orders = plan["orders"]
# 新：state_store.list_signals_with_meta_by_plan_date(date)（trade_id LIKE %_{date}）
#     + get_latest_action(build_trade_id(aid, sym, date)) 判 CONFIRMED/VETOED
# pre_open：遍历 SIGNAL，CONFIRMED 才挂；_stoploss：get_trade_plan 读 meta.stop_price；
# review_report:62 + review_service：从 list_signals_with_meta_by_plan_date 拉 meta 列表
```

`state_store.list_signals_with_meta_by_plan_date(plan_date) -> list[dict]` 新增。

- [ ] **Step 3: 测试 + 提交**

```bash
.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py tests/server/test_review_service_db.py -q
git commit -m "feat(ssot-C2c): pre_open/_stoploss/review_report/review_service 切 get_trade_plan

- 含 trading/review_report.py:62（spec 漏）
- trade_id 用 build_trade_id 单点

Co-Authored-By: Claude <noreply@anthropic.com>"
```

#### C2d: experiment/cli report + trigger_eod_once + smoke（plan_date + since 过滤）

**Files:** `experiment/cli.py:32-45`、`trading/tools/trigger_eod_once.py:46-51`、`trading/tools/smoke_trading_engine.py`

- [ ] **Step 1: experiment report 按 plan_date（保留 since 过滤）**

`experiment/cli.py:32-45` `list_plans`（扫 `plan_*.json` + `since` 过滤）改 `state_store.list_signals_with_meta_by_plan_date_range(since, ...)`（按 plan_date，非文件 mtime/timestamp）。

- [ ] **Step 2: trigger_eod_once / smoke 切 DB 或归档**

`trigger_eod_once.py:46-51` 复核改查 `count_signals_by_plan_date`；`smoke_trading_engine.py` 归档 `scripts/archive/`（若一次性 smoke）或切 DB。

- [ ] **Step 3: 运行 + 提交**

```bash
git commit -m "feat(ssot-C2d): experiment report + trigger/smoke 切 plan_date

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task C3: 删 save_plan/confirm_plan + load_plan 只读兼容窗口

**Files:** `trading/trading_plan.py:41-119`、`trading/engine.py:616,650`、`trading/tools/veto_plan.py`（**spec 漏**：veto DB+JSON 双写，C3 改 veto 只写 DB VETOED、不落 JSON，否则兼容窗口被写方破坏）、`broadcast/push.py`（钉钉导出）
**Test:** `tests/trading/test_trading_plan.py`、`tests/trading/test_engine.py`（eod_plan）、`tests/trading/test_veto_plan.py`、新增 VETOED 边界测试

- [ ] **Step 1: load_plan 改 DB 优先 + build_trade_id（无 _account_id() 未定义问题）**

`trading/trading_plan.py:62-75` `load_plan` 改：

```python
def load_plan(date: str) -> dict | None:
    """读计划 · DB 优先（SIGNAL.meta）+ JSON 回退（C3 只读兼容窗口）。

    返回 shape {date, confirmed, orders}（消费方契约不变）。无 SIGNAL 且无 JSON → None。
    confirmed 按 per-symbol get_latest_action（VETOED 晚于 CONFIRMED → 该 symbol 未确认）。
    """
    from trading import state_store
    account_id = os.getenv("QMT_ACCOUNT_ID", state_store._DEFAULT_ACCOUNT_ID)
    try:
        metas = state_store.list_signals_with_meta_by_plan_date(date)
        if metas:
            orders = []
            confirmed_all = True
            for m in metas:
                sym = (m.get("order") or {}).get("symbol", m.get("symbol"))
                tid = state_store.build_trade_id(account_id, sym, date)  # 单点（消 _account_id 未定义）
                action = state_store.get_latest_action(tid)
                if action == "VETOED":  # VETOED 晚于 CONFIRMED → 未确认（veto 终局防线）
                    confirmed_all = False
                elif action != "CONFIRMED":
                    confirmed_all = False
                orders.append(m)
            return {"date": date, "confirmed": confirmed_all, "orders": orders}
    except Exception:
        logger.exception("load_plan 读 DB SIGNAL 失败，回退 JSON")
    p = _plan_path(date)
    if not p.exists(): return None
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: logger.exception("计划损坏 %s", p); return None
```

- [ ] **Step 2: VETOED 晚于 CONFIRMED 边界测试（全自动 veto 终局防线）**

```python
def test_load_plan_vetoed_after_confirmed(tmp_db, monkeypatch):
    """VETOED 事件晚于 CONFIRMED → load_plan 返 confirmed=False（veto 终局）。"""
    from trading import state_store
    import json
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    tid = state_store.build_trade_id("ACC_TEST", "600000.SH", "2026-08-05")
    state_store.insert_trade_event("ACC_TEST", tid, "600000.SH", "SIGNAL", meta=json.dumps({"plan_date":"2026-08-05"}))
    state_store.insert_trade_event("ACC_TEST", tid, "600000.SH", "CONFIRMED")
    state_store.insert_trade_event("ACC_TEST", tid, "600000.SH", "VETOED")  # 晚于 CONFIRMED
    from trading import trading_plan
    plan = trading_plan.load_plan("2026-08-05")
    assert plan["confirmed"] is False  # VETOED 是最新 action
```

- [ ] **Step 3: 删 save_plan/confirm_plan（C2 全切 DB 后）+ 钉钉导出产物化**

确认 grep `save_plan|confirm_plan` 仅 `engine.py:616,650` + `trading_plan.py` 定义。删 `trading_plan.py:41-59`（save_plan）+ `:78-119`（confirm_plan）；`engine.py:616`（save_plan 调用）+ `:650`（confirm_plan 调用）删（DB SIGNAL/CONFIRMED 由 :638/:642 直接写）。钉钉 `push_plan_to_dingtalk` 改从 DB meta 生成 JSON 字符串（不落盘，参考 A2 export_trades 范式）。

**复核 scripts/archive**：smoke/trigger 已归档（C2d），确认 archive 内脚本无 live plan JSON 写依赖。

- [ ] **Step 4: 测试 + 全量验证 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/ -q`

```bash
git commit -m "feat(ssot-C3): 删 save/confirm_plan + load_plan DB 优先兼容窗口

- load_plan DB 优先（build_trade_id 单点）+ JSON 回退（只读兼容）
- VETOED 晚于 CONFIRMED → confirmed=False（veto 终局防线测试）
- 删 save_plan/confirm_plan（消费方已全切 DB）
- 钉钉推送 plan 从 DB 生成（导出产物，不落盘）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase C 完成验收

- [ ] `.venv310/Scripts/python.exe -m pytest tests/ -q` 全绿
- [ ] `rg "save_plan|confirm_plan" trading presentation broadcast --glob '*.py' --glob '!tests/**' --glob '!**/archive/**'` = 0
- [ ] `rg "load_plan" trading presentation --glob '*.py' --glob '!tests/**'` 仅剩 `trading_plan.py` 只读兼容口 + 已切 DB 的调用点（review_report/engine/review_service 全切）
- [ ] C2a scan_count 按 plan_date 查（非 timestamp），实测非 0
- [ ] C2b cooldown 按 meta.formed_at（非 timestamp）
- [ ] 重启后 `rebuild_position_attribution` 跑一次，存量持仓归因从真实 strategy_name 回填
- [ ] VETOED 晚于 CONFIRMED → confirmed=False（边界测试）
- [ ] `audit_ssot.py` 全绿（含 plan 源一致性，B6 扩展）

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| C2a/C2b 按 timestamp 查恒 0（致命） | 按 meta.plan_date / meta.formed_at（trade_id LIKE + json_extract），C1 补字段 |
| pre_open 切 DB 后漏挂 | load_plan JSON 回退兼容窗口 + get_trade_plan None → 保守跳过 |
| meta 缺 formed_at/plan_date | C1 eod_plan 补字段（已核验当前 order_dict 含 formed_at，补 plan_date/strategy_name） |
| VETOED 终局失效 | C3 边界测试（VETOED 晚于 CONFIRMED → confirmed=False） |
| smoke/trigger 归档路径 | C2d + C3 复核 scripts/archive |

回滚：每 task 一 commit；tag `ssot-phase-c-done`。整体回滚点：三 Phase 完成后保留 `load_plan` JSON 兼容窗口一个发布周期，实盘稳定后删回退 + 归档 `logs/trading_plans/`。

## 跨 Phase 总验收（A+B+C）

- [ ] `pytest tests/ -q` 全绿；`audit_ssot.py` 全绿
- [ ] `data-source-of-truth.md` 反映 spec §3.1 目标态（9 数据域唯一真相源）
- [ ] 实盘模拟盘观察一个完整周期（pre_open 挂单 + post_close 复盘 + 播报），SSoT 无漂移
- [ ] **A/B 合入后 C 才开工**（C 依赖 tmp_db/build_trade_id/position.strategy）
