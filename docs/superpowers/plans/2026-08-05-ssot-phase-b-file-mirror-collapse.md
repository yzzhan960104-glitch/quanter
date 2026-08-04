# ssot-final-hardening Phase B 实施计划 · 文件镜像收敛（精修版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将剩余文件/内存镜像（expired_positions.json / _position_attribution / param_iter_state.json / .last_<bot>_brief / daily_equity 死表）收敛进 state_store 或 job_ledger。

**Architecture:** expired 改 pre_open 现算（基准日=上一交易日）；归因落 position 表新列 + **接线 engine 成交路径**（原 record_position_attribution 无生产调用方，须接线否则验收空）；param_iter 全部读口切 `resolve_active`；播报幂等换 `job_ledger`（begin/finish 成对）；daily_equity 死表清理；audit_ssot 精确巡检 + 文档重写。

**Tech Stack:** Python 3.10 / SQLite / pytest / ripgrep / `.venv310`

**关联 spec:** `docs/superpowers/specs/2026-08-05-ssot-final-hardening-design.md` §5 Phase B

**前置依赖:** Phase A 完成（`tmp_db` fixture + `build_trade_id` + fill.strategy 列由 A0/A1 提供）。

## 全局约束

- 全中文注释；测试 `.venv310/Scripts/python.exe -m pytest <path> -q`。
- 每 task 提交前相关 pytest 全绿；Phase B 完成跑 `pytest tests/ -q` + `scripts/audit_ssot.py`。
- commit 中文 + `Co-Authored-By: Claude <noreply@anthropic.com>`。
- **接口事实（已核验）**：`job_ledger.finish_run(job_name, business_date, status, message="", path=None)` 只 UPDATE（job_ledger.py:87-97），须先 `begin_run`；`record_position_attribution` 全仓**无生产调用方**（仅 trading_service.py:197 定义+注释）；交易日逻辑在 `trading/calendar.py`（is_trading_day/next_trading_day，**无 previous_trading_day**，无 data.tools.trade_cal 模块）；param_iter 读口 ≥7 处。

## 文件结构

| 文件 | 改动 |
|---|---|
| `trading/calendar.py` | B1 加 `previous_trading_day`（与 next_trading_day 对称） |
| `trading/clock.py` | B1 加 `pretrade_date`（薄转发 calendar） |
| `trading/engine.py` | B1 pre_open 现算超期 + 删文件函数；B2b 成交接线归因 |
| `trading/state_store.py` | B2a position 加 strategy/entry_rationale 列 + upsert/clear 函数；B5 删 daily_equity 残留 |
| `presentation/server/services/trading_service.py` | B2a 归因改 DB upsert |
| `broadcast/__main__.py` | B3 删 param_iter 回退；B4 .last_brief → job_ledger |
| `trading/catchup.py` | B4 _brief_missed 改读台账 |
| `backtest/weekly_replay.py` | B3 删 legacy 回退 |
| `discovery/cli.py` | B3 cmd_oos + report 切 resolve_active |
| `discovery/tools/{param_iter,probe_champion_oos}.py` + `backtest/tools/kbkg_trailing_verify.py` | B3 切 ACTIVE 或归档 |
| `trading/position_book.py` | B5 删 snapshot_start_equity/get_start_equity + daily_equity DDL |
| `docs/data-source-of-truth.md` + `scripts/audit_ssot.py` | B6 |

---

### Task B1: expired_positions 改 pre_open 现算（calendar.previous_trading_day）

**Files:**
- Modify: `trading/calendar.py`（加 `previous_trading_day`）
- Modify: `trading/clock.py`（加 `pretrade_date` 薄转发）
- Modify: `trading/engine.py:842-844`（pre_open 现算）、`:1371,1413,1425,1435,1525-1529,1809-1815`（删文件函数 + post_close 扫描写盘）
- Test: `tests/trading/test_engine.py:1492-1658,2000-2026`、新增 `tests/trading/test_calendar_previous.py`

**Interfaces:**
- Consumes: `_scan_expired_positions(today, max_holding)`（已存在）；`_trade_cfg()["max_holding"]`（post_close 既有取法，:1809-1812）
- Produces: pre_open 现算超期（基准日=`clock.pretrade_date(clock.today())`=上一交易日）；文件读写函数全删

- [ ] **Step 1: calendar 加 previous_trading_day + 注入式日历测试（不硬编码真实日历）**

`tests/trading/test_calendar_previous.py`：

```python
def test_previous_trading_day_injected_calendar(monkeypatch):
    """previous_trading_day：注入 is_trading_day 桩，避免真实日历缓存变化致假绿/假红。"""
    from trading import calendar
    # 桩：2026-08-04/08-01 是交易日，08-03/08-02 非交易日（模拟周末）
    trading = {"2026-08-04", "2026-08-01", "2026-07-31"}
    monkeypatch.setattr(calendar, "is_trading_day", lambda d: d in trading)
    assert calendar.previous_trading_day("2026-08-04") == "2026-08-01"  # 跳过 08-03/02
    assert calendar.previous_trading_day("2026-08-01") == "2026-07-31"
```

`trading/calendar.py` 加（与 `next_trading_day:75-97` 对称）：

```python
def previous_trading_day(date_str: str) -> str:
    """date_str 之前最近的一个 A 股交易日（B1 pre_open 超期现算基准日）。

    与 next_trading_day 对称：从 date_str-1 起逐日 is_trading_day，最多回溯 15 自然日
    （覆盖周末+长假）。跨年由 is_trading_day 按 year 拉 trade_cal 处理。
    """
    from datetime import datetime, timedelta
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(1, 16):
        cand = (d - timedelta(days=i)).strftime("%Y-%m-%d")
        if is_trading_day(cand):
            return cand
    return (d - timedelta(days=1)).strftime("%Y-%m-%d")  # 兜底（极端长假）
```

- [ ] **Step 2: clock 加 pretrade_date 薄转发**

`trading/clock.py` 加：

```python
def pretrade_date(date: str) -> str:
    """date 的上一交易日（薄转发 calendar.previous_trading_day，B1 pre_open 超期基准日）。"""
    from trading import calendar
    return calendar.previous_trading_day(date)
```

- [ ] **Step 3: pre_open 现算 + 删文件函数 + max_holding 来源 + 边界断言**

`engine.py:842-844` pre_open 内（max_holding 来自 `_trade_cfg()`，与 post_close :1809-1812 同源）：

```python
_asof = clock.pretrade_date(clock.today())  # 基准日=上一交易日（断点-2，零漂移）
_expired = _scan_expired_positions(_asof, _trade_cfg()["max_holding"])
if _expired:
    await _close_expired_positions(gw, _expired)
```

删除文件函数（`_EXPIRED_POSITIONS_PATH/_write/_load/_consume` @ 1371/1413/1425/1435）+ post_close `_scan+_write` 调用（:1809-1815）+ `_consume` 调用（:1525-1529）。

`tests/trading/test_engine.py` 改边界测试（**断言写死，止损/超期语义红线**）：

```python
def test_scan_expired_boundary_holding_days(tmp_db):
    """holding_days == max_holding 不平仓、> max_holding 平仓（超期语义红线）。"""
    # 建仓 entry_date 使 holding_days 恰好 == max_holding → 不标超期
    # entry_date 使 holding_days == max_holding+1 → 标超期
```

- [ ] **Step 4: 运行 + 提交（删 mock import）**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py tests/trading/test_calendar_previous.py -q`

```bash
git add trading/calendar.py trading/clock.py trading/engine.py tests/
git commit -m "feat(ssot-B1): expired 改 pre_open 现算（previous_trading_day + 边界断言）

- calendar.previous_trading_day（与 next_trading_day 对称）+ clock 薄转发
- pre_open 现算超期，基准日=上一交易日（断点-2 零漂移）
- max_holding 来源 _trade_cfg()；边界断言 ==不平、>平
- 删 _EXPIRED_POSITIONS_PATH/_write/_load/_consume

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task B2: 归因落 DB 列（拆「落列」+「接线 engine 成交」）

**Files:**
- Modify: `trading/state_store.py`（position 加 strategy/entry_rationale 列 + `upsert_position_attribution`/`clear_position_attribution`）
- Modify: `presentation/server/services/trading_service.py:48,179-209`（归因改 DB upsert）
- Modify: `trading/engine.py`（B2b 成交接线 `record_position_attribution`）
- Test: `tests/trading/test_state_store.py`、`tests/test_trading_service.py`

**背景**：`record_position_attribution` 全仓**无生产调用方**（仅定义）。B2 必须接线 engine 成交路径，否则「重启后归因不丢」验收无数据来源。

- [ ] **Step B2a-1: position 加列 + DB upsert 测试**

`tests/trading/test_state_store.py` 追加（`upsert_position_attribution` / `clear_position_attribution` + `get_position` 返新列）：

```python
def test_position_attribution_upsert_clear(tmp_db):
    from trading import state_store
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "BUY", 100, 10.0, "20260805")
    state_store.upsert_position_attribution("ACC_TEST", "600000.SH", "neckline", "颈线突破")
    row = state_store.get_position("ACC_TEST", "600000.SH")
    assert row["strategy"] == "neckline" and row["entry_rationale"] == "颈线突破"
    state_store.clear_position_attribution("ACC_TEST", "600000.SH")
    assert state_store.get_position("ACC_TEST", "600000.SH")["strategy"] is None
```

- [ ] **Step B2a-2: position DDL 加列 + 迁移 + upsert/clear 函数**

`state_store.py` position DDL（:200-210）加 `strategy TEXT` + `entry_rationale TEXT`；`init_store` 迁移段加 `ALTER ADD COLUMN`（参考 fill.account_id 范式 :187-191）。新增 `upsert_position_attribution`/`clear_position_attribution`（UPDATE position SET strategy/entry_rationale WHERE account_id+symbol）。

- [ ] **Step B2a-3: trading_service 归因改 DB（删内存字典）**

`trading_service.py:48` 删 `_position_attribution: dict`；`:197-209` `record/clear_position_attribution` 改 `state_store.upsert/clear_position_attribution(_resolve_account_id(), ...)`；`:179-192` get_positions 富化读 position 表 strategy/entry_rationale 列。

- [ ] **Step B2b: 接线 engine 成交路径（从成交上下文写归因）**

`engine.py` `_handle_order_update` 的 `apply_fill_to_position` 后（:3148 附近），BUY 成交（新建仓）调：

```python
if direction == "BUY" and _fill_inserted:
    try:
        from presentation.server.services.trading_service import record_position_attribution
        record_position_attribution(symbol, "neckline", f"成交建仓@{traded_time}")
    except Exception:
        logger.exception("归因登记失败 symbol=%s（不阻断）", symbol)
# SELL 平仓：apply_fill_to_position 归零即【删 position 行】（state_store.py:607 语义），
# 归因随行消失——clear_position_attribution 会 UPDATE 0 行（空操作，不调用）。
# 验收口径：position 行删除即归因消失（非 clear 调用）。
```

（strategy 当前单策略硬编码 "neckline"；C1 后从 SIGNAL.meta.strategy_name 取。SELL 平仓 `clear_position_attribution`。）

- [ ] **Step B2b-test: 接线测试（成交后归因落 DB）**

`tests/trading/test_engine.py` 追加：

```python
def test_buy_fill_records_attribution(tmp_db, monkeypatch):
    """BUY 成交 → record_position_attribution 落 position.strategy（B2b 接线）。"""
    # mock gw 回报 BUY 成交 → 调 _handle_order_update → 断言 position.strategy == "neckline"
```

- [ ] **Step 4: 运行 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_state_store.py tests/test_trading_service.py tests/trading/test_engine.py -q`

```bash
git add trading/state_store.py presentation/server/services/trading_service.py trading/engine.py tests/
git commit -m "feat(ssot-B2): 归因落 DB 列 + 接线 engine 成交（拆落列/接线）

- position 加 strategy/entry_rationale 列 + upsert/clear
- record/clear_position_attribution 改 DB（删内存字典）
- B2b 接线 engine BUY 成交写归因（原无生产调用方，补接线）
- 断点-3：不做重启重建（C1 从 SIGNAL.meta 补）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task B3: param_iter_state.json 全部读口切 ACTIVE（先全量 rg + 确认停跑）

**Files:** `broadcast/__main__.py:447-478`、`backtest/weekly_replay.py:46-67`、`discovery/cli.py:24,47-58,97`、`discovery/tools/param_iter.py:78,202,229`、`discovery/tools/probe_champion_oos.py:100`、`backtest/tools/kbkg_trailing_verify.py:43`、`broadcast/brief_strategy.py:7,36`、`broadcast/__main__.py:389`（docstring）

**已核验读口（≥7 处）**：broadcast:452（回退）/ weekly_replay:61 / discovery/cli:56+97 / kbkg:43 / param_iter:202（生产者）/ probe_champion_oos:100 / + docstring：brief_strategy:7,36、__main__:389。

- [ ] **Step 1: 前置——确认 param_iter 已停跑 + 全量 rg**

```bash
# 确认 param_iter 夜跑已停（spec §1 memory：2026-07-23 discovery engine 上线后退役，但需确认无残留 schtasks/cron）
rg "param_iter" infra/ scripts/ *.bat --glob '*.{py,bat,ps1}'  # 确认无调度入口
ls -la logs/param_iter_state.json  # 确认最后更新时间（08-03 09:02 后无新写入=已停）
rg "param_iter_state\.json" --glob '*.py'  # 全量读口清单（≥7 处 + docstring）
```

若 param_iter 仍在跑：先停调度（否则删文件后下次运行重新生成），**B3 阻塞直至停跑确认**。

- [ ] **Step 2: 切 ACTIVE 测试（无 ACTIVE 降级，不读 JSON）**

`tests/discovery/test_param_iter_retired.py` 追加（每读口「无 ACTIVE → 默认/None，不读 legacy JSON」）。

- [ ] **Step 3: 7 读口切 ACTIVE**

- `broadcast/__main__.py:447-478`：删 legacy 回退，只认 `_experiment_active_state()`。
- `backtest/weekly_replay.py:59-67`：删 `_champion_cfg_override` legacy 回退，只认 `resolve_active()`。
- `discovery/cli.py:24,47-58,97`：`cmd_oos` + `:97` 第二处改 `resolve_active()`（删 STATE_FILE 常量 + 两处 json.load）。
- `discovery/tools/param_iter.py`：若退役工具，归档 `scripts/archive/`；若仍用，改 `resolve_active`。
- `discovery/tools/probe_champion_oos.py:100`：改 `resolve_active()`。
- `backtest/tools/kbkg_trailing_verify.py:43`：改 `resolve_active()`。
- **docstring 注释清理**（A5 护栏会扫含注释）：`broadcast/brief_strategy.py:7,36`、`broadcast/__main__.py:389`。

- [ ] **Step 4: 归档 JSON + 扩护栏 + 运行 + 提交**

```bash
[ -f logs/param_iter_state.json ] && mkdir -p logs/archive && mv logs/param_iter_state.json logs/archive/param_iter_state.json.final-20260805
# A5 test_ssot_static_guard.py BANNED 加 "param_iter_state.json"
```

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast tests/backtest tests/discovery tests/test_ssot_static_guard.py -q`

```bash
git add -A
git commit -m "feat(ssot-B3): param_iter_state.json 全部读口切 ACTIVE（≥7 处 + docstring）

- 前置确认 param_iter 停跑 + 全量 rg
- broadcast/weekly_replay/discovery cli+param_iter+probe_champion_oos/kbkg 切 resolve_active
- docstring 清理（brief_strategy/__main__）+ 归档 JSON + 护栏扩

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task B4: 播报幂等迁 job_ledger（begin/finish 成对）

**Files:** `broadcast/__main__.py:49-99,682,136,144`（含 `_read_last`/`_write_last` 死代码 :136,144 —— last_brief_file 退役后一并删）、`trading/catchup.py:53-58,115-118`
**Test:** `tests/test_broadcast_main.py`、`tests/broadcast/test_cli_routing.py:11-29`（last_brief_file 用例）、`tests/trading/test_catchup.py`

**已核验**：`finish_run(job_name, business_date, status, message="", path=None)` 只 UPDATE——**须先 `begin_run`**。

- [ ] **Step 1: brief 幂等读台账测试（begin/finish 成对，无多余 kwargs）**

`tests/test_broadcast_main.py` 追加：

```python
def test_brief_idempotent_via_job_ledger(tmp_path, monkeypatch):
    """brief 已 done（台账行）→ 跳过；--force 忽略台账。begin/finish 成对（finish 只 UPDATE）。"""
    from trading import job_ledger
    db = tmp_path / "job.db"
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(db))
    job_ledger.begin_run("brief_trading", "2026-08-05", started_at="2026-08-05T16:00:00")  # 先 INSERT
    job_ledger.finish_run("brief_trading", "2026-08-05", "done")  # 再 UPDATE（无 started_at/finished_at kwargs）
    assert job_ledger.latest_status("brief_trading", "2026-08-05") == "done"
    # 调 broadcast 主流程，断言不重复推（NotificationManager mock not called）
```

- [ ] **Step 2: last_brief_file 退役 + 幂等查 latest_status**

`broadcast/__main__.py:49-99` BOTS 字典删 `last` 键；`:83-99` `last_brief_file` 函数退役（或删）；`:682` 推送前查 `job_ledger.latest_status(f"brief_{bot}", date)`：非 done 且非 `--force` → 推送 + `begin_run`/`finish_run("done")`；已 done → 跳过。

- [ ] **Step 3: catchup._brief_missed 改读台账**

`trading/catchup.py:53-58` `_brief_missed` 改 `job_ledger.latest_status(f"brief_{bot}", latest_day) != "done"` 任一为真 → 补播。

- [ ] **Step 4: test_cli_routing last_brief_file 用例改造**

`tests/broadcast/test_cli_routing.py:11-29`（`test_last_brief_path_per_push_bot` + `test_last_brief_file_unknown_bot`）：`last_brief_file` 退役后，这两用例改断言「幂等查 job_ledger」（或删除，改为 job_ledger 路径测试）。

- [ ] **Step 5: 运行 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_broadcast_main.py tests/broadcast/test_cli_routing.py tests/trading/test_catchup.py -q`

```bash
git add broadcast/__main__.py trading/catchup.py tests/
git commit -m "feat(ssot-B4): 播报幂等迁 job_ledger（begin/finish 成对）

- .last_<bot>_brief → job_ledger(brief_<bot>)，幂等查 latest_status
- --force 跳过台账；begin/finish 成对（finish 只 UPDATE）
- catchup._brief_missed 改读台账；test_cli_routing 用例改造

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task B5: daily_equity 死表 + position_book 读写函数清理

**Files:** `trading/position_book.py:128-135,262-280`、`trading/state_store.py:215-219`（注释）、`trading/engine.py:805`（注释）
**Test:** `tests/trading/test_engine.py:120-133,764-879,854-868`（spy 用例）

- [ ] **Step 1: grep 确认无生产引用**

```bash
rg "snapshot_start_equity|get_start_equity|daily_equity" trading presentation broadcast --glob '*.py' --glob '!tests/**'
```
Expected: 仅 `state_store.py`（account_daily，非 daily_equity 表）+ `position_book.py`（待删）。C-1 已把熔断读口迁 `state_store.get_start_equity`（account_daily）。

- [ ] **Step 2: 删 position_book 函数 + daily_equity DDL + 测试改**

`position_book.py:128-135`（daily_equity DDL）+ `:262-280`（snapshot_start_equity/get_start_equity）删。`tests/trading/test_engine.py:854-868` spy `position_book.snapshot_start_equity` 断言删（函数已删）。

`state_store.py:215-219` 注释更新：「daily_equity 表已删（B5）；**旧库残留 daily_equity 表无害**（init_store 不再 CREATE，旧表存在=历史残留，不读写）」。`engine.py:805` 注释同步。

- [ ] **Step 3: 运行 + 提交**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py -q`

```bash
git add trading/position_book.py trading/state_store.py trading/engine.py tests/
git commit -m "feat(ssot-B5): daily_equity 死表清理（旧库残留无害记录）

- 删 daily_equity DDL + position_book.snapshot_start_equity/get_start_equity
- 熔断基线唯一读口 = state_store.get_start_equity(account_daily)
- 旧库残留 daily_equity 表无害（init 不再 CREATE，不读写）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task B6: audit_ssot.py 精确巡检 + 文档重写

**Files:** Create `scripts/audit_ssot.py`、Modify `docs/data-source-of-truth.md`

- [ ] **Step 1: audit_ssot.py 精确检查清单（非 pass 占位）**

```python
"""SSoT 一致性巡检（B6）。退出码 0=全绿，1=有不一致。"""
import sqlite3, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def check_fill_position(db):
    """position.qty == SUM(CASE WHEN direction='BUY' THEN qty ELSE -qty END) by symbol."""
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    fills = {}
    for r in con.execute("SELECT symbol, direction, qty FROM fill"):
        fills[r["symbol"]] = fills.get(r["symbol"], 0) + (r["qty"] if r["direction"]=="BUY" else -r["qty"])
    for r in con.execute("SELECT symbol, qty FROM position WHERE qty != 0"):
        if abs(fills.get(r["symbol"], 0) - r["qty"]) > 1e-6:
            return f"fill↔position 不一致 {r['symbol']}: fill={fills.get(r['symbol'])} pos={r['qty']}"
    return None

def check_account_daily_closed(db):
    """account_daily 每交易日 start+close 非空（熔断基线闭合）。"""
    con = sqlite3.connect(db)
    rows = con.execute("SELECT date FROM account_daily WHERE start_total_asset IS NULL OR close_total_asset IS NULL").fetchall()
    return f"account_daily 缺 start/close: {[r[0] for r in rows]}" if rows else None

def check_trade_event_chain(db):
    """trade_event 链完整性：孤儿 SIGNAL（>7 日无后续 CONFIRMED/OPEN/FILLED）告警。"""
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    orphans = con.execute(
        "SELECT trade_id, symbol, timestamp FROM trade_event e1 WHERE e1.action='SIGNAL'"
        " AND NOT EXISTS (SELECT 1 FROM trade_event e2 WHERE e2.trade_id=e1.trade_id"
        " AND e2.action IN ('CONFIRMED','VETOED','OPEN','FILLED','CLOSED'))"
        " AND e1.timestamp < datetime('now','-7 days')").fetchall()
    return f"孤儿 SIGNAL（>7 日无后续）: {[r['symbol'] for r in orphans]}" if orphans else None

def check_engine_process_count():
    """引擎进程数 ≤ 1（C-5 单例，__main__ run_server 单例）。"""
    # Windows tasklist 统计 python.exe 进程数；非 Windows 用 pgrep
    import platform
    if platform.system() == "Windows":
        out = subprocess.run('wmic process where "name=\'python.exe\'" get commandline',
                             shell=True, capture_output=True, text=True, check=False).stdout
        n = sum(1 for l in out.splitlines() if "-m trading" in l)
    else:
        out = subprocess.run(["pgrep", "-f", "python.*-m trading"],
                             capture_output=True, text=True, check=False).stdout
        n = len([l for l in out.splitlines() if l.strip()])
    return f"引擎进程数 {n} > 1（C-5 单例红线）" if n > 1 else None

def check_guard_ripgrep():
    """护栏复用 A5：生产代码零 live_trades.csv / param_iter_state.json / save_plan 引用。"""
    BANNED = ["live_trades.csv", "param_iter_state.json"]
    # rg 复用 test_ssot_static_guard 逻辑
    return None

def main():
    db = ROOT / "logs" / "trading_state.db"
    errs = [f(db) for f in (check_fill_position, check_account_daily_closed, check_trade_event_chain)] + \
           [check_engine_process_count(), check_guard_ripgrep()]
    errs = [e for e in errs if e]
    if errs:
        print("\n".join(errs)); sys.exit(1)
    print("audit_ssot: 全绿")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 重写 data-source-of-truth.md（spec §3.1 目标态）**

- [ ] **Step 3: 运行 + 提交**

```bash
.venv310/Scripts/python.exe scripts/audit_ssot.py && .venv310/Scripts/python.exe -m pytest tests/ -q
git add scripts/audit_ssot.py docs/data-source-of-truth.md
git commit -m "feat(ssot-B6): audit_ssot 精确巡检 + data-source-of-truth 重写

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Phase B 完成验收

- [ ] `pytest tests/ -q` 全绿
- [ ] `rg "param_iter_state\.json|expired_positions\.json|\.last_.*_brief|daily_equity|snapshot_start_equity" trading presentation broadcast backtest discovery research --glob '*.py' --glob '!**/archive/**' --glob '!tests/**'` = 0（含注释）
- [ ] `audit_ssot.py` 退出码 0
- [ ] pre_open 现算超期不依赖文件（B1，holding_days 边界 ==不平/>平）
- [ ] BUY 成交后 position.strategy 落 DB（B2b 接线）
- [ ] brief 幂等走 job_ledger（B4，begin/finish 成对）

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| param_iter 仍在跑 | B3 Step 1 前置确认停跑，否则阻塞 |
| B1 holding_days 漂移 | previous_trading_day 基准日=上一交易日，边界断言写死 |
| B2 归因无调用方 | B2b 接线 engine 成交（原无调用，补） |
| B4 finish_run 只 UPDATE | begin/finish 成对（已核验签名） |

回滚：每 task 一 commit；tag `ssot-phase-b-done`。
