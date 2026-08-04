# 网关重连与单一真相源整改 Implementation Plan（W1-W5）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 2026-08-04 上午生产事故链（pre_open 静默跳过 + 24 笔重复成交回报 + 双引擎进程 + 真相源漂移），让网关在客户端存活时 1 分钟内自愈重连、计划/成交/权益三类真相源收敛到 SQLite 单一写口、消费端不再读脏镜像。

**Architecture:** 不引入新依赖。三类改动：(1) 网关侧把 connect 返回码作为客户端可用性的唯一权威信号，文件 mtime 降级为日志分类；(2) 真相源侧补齐 veto/成交回报/权益快照的 DB 双写，让 pre_open/post_close 的既有 DB 防线生效；(3) 消费端（简报/导出/对账）改读 state_store，CSV 降级为可重建镜像。所有改动遵守 `docs/data-source-of-truth.md`「同一事务同一调用点写」原则。

**Tech Stack:** Python 3.10（`.venv310`）、FastAPI、sqlite3（WAL + foreign_keys）、apscheduler、pytest。QMT 网关 broker/qmt.py（xtquant C++ 扩展，CI 不可用）。

## Global Constraints

- **语言**：所有对话/注释/文档 100% 中文（CLAUDE.md 红线）。注释写「为什么」不只写「是什么」。
- **定位用符号名，禁用行号**：spec 引用的 `[engine.py:2355]` 等行号在 C-9 merge + 08-04 根治后已全面漂移。所有 task 用「函数名 + 语义段」定位（如 `_health_guard` ④ 未就绪分支），执行者用 `codegraph_explore "<symbol>"` 或 Grep 找当前行号。
- **TDD**：每个 task 先写失败测试再改实现。测试用 `.venv310/Scripts/python.exe -m pytest <path> -v`。
- **反魔法**：不引重型量化库；数学/清洗用显式代码。QMT 探针保持纯文件系统检查（不触 xtquant），CI 安全。
- **错误分级沿用 C-4**：DB 真相源写失败 = L1（`_CriticalHalt` 停调度）；单只业务拒单 = L2（聚合 CRITICAL 不停）；告警走 `_alert_critical` + `fire_and_forget`，失败软降级不阻塞主链路。
- **trade_id 口径全链路统一**：`f"{account_id}_{sym}_{date}"`（与 `eod_plan`/`_pre_open_impl` 既有口径一致，不得自造）。
- **回滚**：W1/W2 各自可单独 revert；W3 消费端切 DB 期间保留 CSV 双写，env 开关 `LIVE_TRADE_READ_SOURCE=db|csv`（缺省 db）一键切回。
- **不动**：QMT 客户端登录自动化、CSV 历史脏行回填 SQLite、前端 caisen 死端点、多账户/期权。

## File Structure

| 文件 | 职责 | 本 plan 动作 |
|---|---|---|
| `broker/qmt.py` | QMT 网关：connect/is_client_ready/on_stock_trade | T1 改 is_client_ready、T3 前置清队列 |
| `trading/engine.py` | 触发点编排：_health_guard/_pre_open_impl/_handle_order_update/post_close | T2 告警、T6 成交幂等、T8 对账、T9 权益 |
| `trading/tools/veto_plan.py` | veto CLI（人审刹车） | T4 双写 DB VETOED |
| `trading/trading_plan.py` | save/load/confirm_plan（JSON 落盘） | T4 confirm 补 DB CONFIRMED |
| `trading/state_store.py` | SQLite 真相源（fill/order/trade_event/account_daily） | T10 加 get_ready |
| `trading/clock.py` / `trading/calendar.py` | 时间/交易日历单一源 | T10 用 is_trading_session 判交易时段 |
| `presentation/server/services/trading_service.py` | record_live_trade/query_trades/aggregate_fills_by_symbol | T6 CSV 幂等、T7 切 DB |
| `broadcast/__main__.py` / `brief_trading.py` | 简报消费端 | T7 切 DB + 去重 + 三态 |
| `tests/trading/test_qmt_health_guard.py` 等 | 既有测试 | 各 task 加用例 |
| `docs/superpowers/runbooks/2026-08-04-gateway-ops.md` | 运维 SOP（进程清理/CSV 清理/.env 回正） | T5 新建 |

---

## Phase 0（P0，下个交易日前）

### Task 1: W1.1 is_client_ready 重定义——connect 返回码唯一权威

**Files:**
- Modify: `broker/qmt.py` `is_client_ready` 方法（当前探 `miniqmtShm*Cache*`/`up_queue_win_*` mtime）
- Test: `tests/trading/test_qmt_health_guard.py`（既有 5 用例需更新断言）

**Interfaces:**
- Produces: `is_client_ready(staleness_sec=300) -> bool` 只在「客户端进程/userdata 目录完全不在」时返 False；新增 `_client_staleness_diag() -> str` 供告警文案用（返「进程不在/目录缺失/quoter 陈旧 N 分钟/正常」）。

**背景（为什么这么改）**：08-04 事故根因 = 把客户端启动时一次性生成的共享内存镜像 mtime 当心跳，客户端运行 >5 分钟即判死，`_health_guard` 永不 connect。connect 返回码才是客户端可用性唯一权威信号；文件 mtime 只配做日志分类，绝不做硬前置（否则换探针复发静默跳过）。

- [ ] **Step 1: 写失败测试——进程在但 mtime 陈旧也应判 ready**

在 `tests/trading/test_qmt_health_guard.py` 加（既有 `test_is_client_ready_false_when_files_stale` 的语义需反转）：

```python
def test_is_client_ready_true_when_userdata_dir_exists_even_if_stale(tmp_path, monkeypatch):
    """W1.1: userdata 目录存在即视客户端进程在 → ready，mtime 陈旧不再硬前置。
    物理：connect 返回码才是权威；文件 mtime 只做日志分类，防 08-04 静默跳过复发。"""
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()
    # 故意只放一个老旧缓存文件（旧逻辑会判 stale=False，新逻辑判 True）
    (userdata / "miniqmtShmCache_old").write_text("x")
    import os, time
    old = time.time() - 3600  # 1 小时前
    os.utime(userdata / "miniqmtShmCache_old", (old, old))

    gw = QmtExecutionGateway(userdata_path=str(userdata), account_id="test")
    assert gw.is_client_ready() is True  # 目录存在 → 进程可能在 → 放行 connect
    # 诊断函数能描述「陈旧」供告警用
    assert "陈旧" in gw._client_staleness_diag() or "正常" in gw._client_staleness_diag()


def test_is_client_ready_false_when_userdata_dir_missing(tmp_path):
    """W1.1: userdata 目录不存在 = 客户端必然未起 → False（connect 必失败的唯一场景）。"""
    gw = QmtExecutionGateway(userdata_path=str(tmp_path / "no_such_dir"), account_id="test")
    assert gw.is_client_ready() is False
    assert "不存在" in gw._client_staleness_diag() or "缺失" in gw._client_staleness_diag()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_health_guard.py::test_is_client_ready_true_when_userdata_dir_exists_even_if_stale tests/trading/test_qmt_health_guard.py::test_is_client_ready_false_when_userdata_dir_missing -v`
Expected: FAIL（旧逻辑按 mtime 判 stale=False）

- [ ] **Step 3: 改 is_client_ready + 加 _client_staleness_diag**

用 Grep 定位 `def is_client_ready`（`broker/qmt.py`），替换为：

```python
def is_client_ready(self, staleness_sec: int = 300) -> bool:
    """探测 miniQMT 客户端是否就绪（W1.1 · 2026-08-04 根治后二次重定义）。

    判据（connect 返回码唯一权威原则）：
        userdata 目录存在且非空 → True（客户端进程可能在跑，放行让上层 connect，
        由 trader.connect() 返回码定权威结论：0=成功 / -1=session 残留自愈 / 其他=环境故障）。
        目录缺失/为空 → False（客户端必然未启动，connect 必失败，唯一该挡的场景）。

    ⚠️ 不再用 miniqmtShm*Cache*/up_queue_win_* 的 mtime 做硬前置：
        那是客户端【启动时一次性生成】的共享内存镜像，运行期间不刷新，>5min 即判死 →
        _health_guard 永不 connect（08-04 事故根因）。mtime 降级为 _client_staleness_diag
        的日志分类素材，仅供 health_guard WARNING 文案用，绝不阻断 connect 尝试。

    Why 纯文件检查：不触达 xtquant（C++ 扩展），CI/单测/无 SDK 环境可安全调用。
    """
    if not self._userdata_path or not os.path.isdir(self._userdata_path):
        return False
    # 目录存在但完全空（刚创建未登录）也视未就绪
    try:
        if not any(os.scandir(self._userdata_path)):
            return False
    except OSError:
        return False
    return True

def _client_staleness_diag(self, staleness_sec: int = 300) -> str:
    """客户端活跃度诊断文案（W1.1，仅供 health_guard WARNING 用，不做硬前置）。

    返回：目录缺失/目录空/quoter 陈旧 N 分钟/缓存新鲜——供运维一眼定位断线原因。
    """
    import glob as _glob
    if not self._userdata_path or not os.path.isdir(self._userdata_path):
        return "userdata 目录不存在（客户端未安装/路径错）"
    try:
        if not any(os.scandir(self._userdata_path)):
            return "userdata 目录空（客户端未登录）"
    except OSError:
        return "userdata 目录不可读"
    # 活跃度启发式：quoter 行情目录 + 启动缓存任一新鲜 → 活跃；全老旧 → 陈旧告警
    now = time.time()
    patterns = ("miniqmtShm*Cache*", "up_queue_win_*", "quoter")
    newest = 0.0
    for pat in patterns:
        for f in _glob.glob(os.path.join(self._userdata_path, pat)):
            try:
                m = os.path.getmtime(f)
                if m > newest:
                    newest = m
            except OSError:
                continue
    if newest == 0.0:
        return "无活跃文件（仅目录存在，客户端可能未登录）"
    age_min = int((now - newest) / 60)
    return f"文件最新 mtime 陈旧 {age_min} 分钟" if age_min > staleness_sec // 60 else "正常（文件新鲜）"
```

- [ ] **Step 4: 修既有测试的过时断言**

Grep `test_is_client_ready_false_when_files_stale` / `test_is_client_ready_true_when_file_fresh` 等 5 个既有用例，把「mtime stale→False」语义的断言改为新语义（目录在→True），或删除已被新用例覆盖的冗余用例。保留 `test_is_client_ready_false_when_dir_missing`。

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_health_guard.py -v`
Expected: PASS（全模块绿）

- [ ] **Step 5: Commit**

```bash
git add broker/qmt.py tests/trading/test_qmt_health_guard.py
git commit -m "fix(qmt): W1.1 is_client_ready 改 connect 返回码权威，mtime 降级日志诊断（08-04 静默跳过根治）"
```

---

### Task 2: W1.2 _health_guard ④ 未就绪分支补 WARNING + 限流钉钉

**Files:**
- Modify: `trading/engine.py` `_health_guard` 方法 ④ 段（Grep `ready = gw.is_client_ready()` / `if not ready:`）
- Test: `tests/trading/test_qmt_health_guard.py`（加 caplog 断言）

**Interfaces:**
- Consumes: T1 的 `gw._client_staleness_diag()`
- Produces: ④ 未就绪分支打 WARNING + 每 10 轮推一次钉钉（复用 `_alert_critical` 通道，节流 `% 10`）。

- [ ] **Step 1: 写失败测试——未就绪打 WARNING 且每 10 轮告警**

```python
def test_health_guard_not_ready_warns_and_throttles_alert(caplog, monkeypatch):
    """W1.2: 客户端未就绪 → WARNING 日志 + 诊断文案；连续 10 轮才推一次钉钉（节流）。"""
    import logging
    engine = TradingEngine.__new__(TradingEngine)  # 绕 __init__ 装配
    engine._guard_fail_count = 0
    engine._guard_rounds_since_fail = 0
    engine._guard_client_ready_prev = True
    engine._not_ready_rounds = 0  # W1.2 新增计数

    class _FakeGW:
        _connected = False
        _risk_halted = False
        _reconnecting = False
        def is_client_ready(self): return False
        def _client_staleness_diag(self): return "userdata 目录不存在（客户端未安装/路径错）"

    monkeypatch.setattr("trading.engine.get_gateway", lambda: _FakeGW())
    fired = []
    monkeypatch.setattr("trading.engine._alert_critical", lambda msg: fired.append(msg))

    caplog.set_level(logging.WARNING)
    for i in range(12):
        import asyncio
        asyncio.run(engine._healthGuard_or_stub())  # 见 Step3 说明

    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "客户端未就绪" in r.getMessage()]
    assert len(warns) >= 1, "未就绪必须打 WARNING"
    assert len(fired) == 1, f"12 轮应只推 1 次钉钉（每 10 轮），实际 {len(fired)}"
    assert "客户端未安装" in fired[0] or "目录不存在" in fired[0]
```

> 注：`_health_guard` 是 async 实例方法。测试里若直接 `await engine._health_guard()` 需正确构造 engine；若 `__new__` 绕装配缺属性，改为用既有测试的 engine 构造 helper（Grep `test_health_guard_skips_when_client_not_ready` 看既有构造范式，复用它）。Step3 的 `_healthGuard_or_stub` 是占位名，实际就调 `engine._health_guard()`。

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_health_guard.py::test_health_guard_not_ready_warns_and_throttles_alert -v`
Expected: FAIL（旧 ④ 段静默 return，无 WARNING）

- [ ] **Step 3: 改 _health_guard ④ 段**

Grep `_health_guard` 的 `ready = gw.is_client_ready()`（约 engine.py:2359），把：
```python
    ready = gw.is_client_ready()
    if not ready:
        self._guard_client_ready_prev = False
        return
```
改为：
```python
    ready = gw.is_client_ready()
    if not ready:
        # W1.2（08-04 静默跳过根治）：未就绪必须可见——WARNING + 诊断文案，
        # 连续 10 轮推一次钉钉（节流防风暴，复用 _alert_critical 通道）。
        # Why 必须告警：旧版静默 return 致断线 9 小时无人知，直到 pre_open 失败才暴露。
        self._guard_client_ready_prev = False
        self._not_ready_rounds = getattr(self, "_not_ready_rounds", 0) + 1
        diag = gw._client_staleness_diag() if hasattr(gw, "_client_staleness_diag") else "无诊断"
        logger.warning("health_guard 客户端未就绪，跳过 connect（%s，连续 %d 轮）",
                       diag, self._not_ready_rounds)
        if self._not_ready_rounds % 10 == 0:
            _alert_critical(
                f"health_guard 客户端连续未就绪 {self._not_ready_rounds} 轮（≈{self._not_ready_rounds}min），"
                f"网关无法自愈重连（{diag}）。请人工检查 miniQMT 客户端是否启动/登录")
        return
    # 就绪后清零未就绪计数
    self._not_ready_rounds = 0
```

在 `TradingEngine.__init__` 加 `self._not_ready_rounds = 0`（Grep `_guard_fail_count = 0` 旁边）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_health_guard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_qmt_health_guard.py
git commit -m "fix(engine): W1.2 health_guard 未就绪分支补 WARNING + 限流钉钉（消灭静默断线）"
```

---

### Task 3: W1.3 connect 前置清理本 sid 残留队列

**Files:**
- Modify: `broker/qmt.py` `connect` 方法（当前 `_cleanup_session_files` 仅在 attempt1 返 -1 后补救）
- Test: `tests/trading/test_qmt_gateway.py`（加「connect 前触发清理」断言）

**Interfaces:**
- Produces: `connect()` 在 stop-before-recreate 之后、attempt 循环之前，预防性调一次 `_cleanup_session_files(userdata, sid)`，防 75MB 旧队列重放。

- [ ] **Step 1: 写失败测试——connect 前（非 -1 后）就清理**

```python
def test_connect_cleans_session_files_before_first_attempt(monkeypatch, tmp_path):
    """W1.3: connect 进入循环前预防性清队列，不等 -1 补救（防旧成交回报连上瞬间重放）。"""
    userdata = str(tmp_path / "userdata_mini"); os.makedirs(userdata)
    sid = 123456
    # 残留队列文件（引擎自有会话文件）
    down_q = os.path.join(userdata, f"down_queue_win_{sid}")
    open(down_q, "w").write("stale 75MB payload")

    cleaned = {"called": False, "args": None}
    import broker.qmt as qmt_mod
    real_cleanup = qmt_mod._cleanup_session_files
    def spy_cleanup(path, session_id):
        cleaned["called"] = True; cleaned["args"] = (path, session_id)
        return real_cleanup(path, session_id)
    monkeypatch.setattr(qmt_mod, "_cleanup_session_files", spy_cleanup)

    gw = QmtExecutionGateway(userdata_path=userdata, account_id="t", session_id=sid)
    # mock _bootstrap 让 connect 不真连柜台（仅验证清理时机）
    async def fake_connect(self): 
        self._loop = asyncio.get_running_loop()
        # 触发前置清理逻辑（不调真 xtquant）
    # 见 Step3：清理在 _ensure_xtquant 之后、attempt 循环之前
    # 若 xtquant 不可用（CI），connect 会抛 _ensure_xtquant 异常——改测「清理被调用」用更薄的单元
    # fallback：直接断言 connect 源码里 _cleanup 出现在 for attempt 循环前（结构断言）
```

> CI 环境 xtquant 不可用，无法跑完整 connect。务实方案：**Step3 改完代码后，用 Grep 断言结构**（`_cleanup_session_files` 调用点在 `for attempt in (1, 2):` 之前出现一次 + attempt==1/-1 分支保留一次 = 共 2 处）。或 mock `_ensure_xtquant` + `_bootstrap` 跳过真连，仅验证 spy_cleanup 在 attempt 循环前被调一次。执行者优先选 mock 方案。

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k connect_cleans -v`
Expected: FAIL（当前清理仅在 -1 后）

- [ ] **Step 3: 改 connect——前置预防性清理**

Grep `connect` 方法里 `# 2. 最多两轮` 注释（`for attempt in (1, 2):` 之前），在 stop-before-recreate（`if self._trader is not None: _stop_trader_safely(...)`）之后、attempt 循环之前插入：

```python
        # W1.3（08-04 根治）：connect 前预防性清本 sid 残留队列，不等 -1 补救。
        # Why 前置：旧 down_queue_win_{sid} 残留（事故机 75MB）会在连上瞬间被客户端重放，
        # 把历史成交回报再推一遍 → CSV 重复（W3 幂等是第二道防线，这里是第一道）。
        # 只清 down_queue_win_{sid}（引擎自有会话文件），不动 xtquant/xtmodel 队列。
        try:
            _pre_cleaned = _cleanup_session_files(self._userdata_path, self._session_id)
            if _pre_cleaned:
                logger.info("QMT connect 前置清理本 sid 残留队列：%s", _pre_cleaned)
        except Exception:
            logger.warning("QMT connect 前置清理异常（忽略，继续 attempt）", exc_info=True)
```

保留 attempt==1/-1 分支内的既有清理（兜底）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add broker/qmt.py tests/trading/test_qmt_gateway.py
git commit -m "fix(qmt): W1.3 connect 前置清理 sid 残留队列，防旧成交回报重放"
```

---

### Task 4: W2 veto_plan.veto/confirm 双写 DB（pre_open 既有防线即刻生效）

**Files:**
- Modify: `trading/tools/veto_plan.py` `veto` 函数（Grep `def veto`，约 :42）
- Modify: `trading/trading_plan.py` `confirm_plan`（补 DB CONFIRMED 人工确认路径）
- Test: `tests/trading/test_veto_plan_db.py`（新建）

**Interfaces:**
- Consumes: `state_store.insert_trade_event(account_id, trade_id, symbol, "VETOED")`、`get_latest_action(trade_id)`。trade_id = `f"{account_id}_{sym}_{date}"`（与 eod_plan/pre_open 一致）。
- Produces: veto 命令 DB+JSON 双写；DB 失败则命令报错退出（不产生「看似成功实际只改一半」）。

**关键背景（核准结论，避免重复设计）**：`_pre_open_impl`（engine.py:867-871）**已经查 DB VETOED 跳过挂单**；`eod_plan`（engine.py:637）已查 VETOED 不写 CONFIRMED。本 task 只补 veto_plan 不写 DB 的唯一缺口——防线即刻生效，无需改 pre_open/eod_plan。

- [ ] **Step 1: 写失败测试——veto 写 DB VETOED + 重跑 pre_open 跳过**

```python
# tests/trading/test_veto_plan_db.py
import pytest, json
from trading import state_store, trading_plan
from trading.tools.veto_plan import veto

def test_veto_writes_db_and_pre_open_skips(tmp_path, monkeypatch):
    """W2: veto 双写 DB VETOED；pre_open 既有防线据此跳过被否标的。"""
    db = str(tmp_path / "ts.db"); monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    plan_dir = tmp_path / "plans"; monkeypatch.setenv("TRADE_PLAN_DIR", str(plan_dir))
    acct = "test_acct"; monkeypatch.setenv("QMT_ACCOUNT_ID", acct)
    state_store.init_store(db)
    state_store.upsert_account(acct, broker="qmt")

    date = "2026-08-05"; sym = "300001.SZ"
    trading_plan.save_plan(date, [{"order": {"symbol": sym, "qty": 100, "side": "BUY", "price": 10.0},
                                    "stop_price": 9.5, "take_profit": 11.0}], confirmed=True)
    trade_id = f"{acct}_{sym}_{date}"

    # veto 命令
    veto(date, sym)

    # DB 真相源有 VETOED
    assert state_store.get_latest_action(trade_id) == "VETOED"
    # eod_plan 重跑不复活 CONFIRMED（既有防线，间接验证 veto 落库生效）
    assert state_store.get_latest_action(trade_id) != "CONFIRMED"

def test_veto_db_failure_aborts(tmp_path, monkeypatch):
    """W2: DB 写失败 → veto 抛错退出，不产生半成功（JSON 改了 DB 没记）。"""
    db = str(tmp_path / "ts.db"); monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    plan_dir = tmp_path / "plans"; monkeypatch.setenv("TRADE_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("QMT_ACCOUNT_ID", "test_acct")
    state_store.init_store(db); state_store.upsert_account("test_acct", broker="qmt")
    trading_plan.save_plan("2026-08-05", [{"order": {"symbol": "300001.SZ", "qty": 100,
                                "side": "BUY", "price": 10.0}, "stop_price": 9.5, "take_profit": 11.0}],
                           confirmed=True)
    def boom(*a, **k): raise RuntimeError("DB down")
    monkeypatch.setattr(state_store, "insert_trade_event", boom)
    with pytest.raises(RuntimeError):
        veto("2026-08-05", "300001.SZ")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_veto_plan_db.py -v`
Expected: FAIL（veto 不写 DB）

- [ ] **Step 3: 改 veto_plan.veto 双写**

Grep `trading/tools/veto_plan.py` `def veto`，读当前实现（确认入参 date/symbol 语义）。改为 DB 先写、JSON 后写：

```python
def veto(date: str, symbol: str) -> None:
    """否决某标的（W2 · DB+JSON 双写，DB 真相源先写）。

    物理：研究员 pre_open 前人审刹车。DB trade_event(VETOED) 是真相源——
    pre_open 既有防线（engine._pre_open_impl:867）据此跳过挂单，eod_plan 重跑据此不复活 CONFIRMED。
    JSON 镜像保留供 CLI/钉钉展示。

    失败语义：DB 写失败 → 抛错退出，绝不「JSON 改了 DB 没记」（否则全自动模式下 pre_open 放行）。
    """
    import os
    from trading import state_store, trading_plan
    account_id = os.getenv("QMT_ACCOUNT_ID", state_store._DEFAULT_ACCOUNT_ID)
    trade_id = f"{account_id}_{symbol}_{date}"
    # ① DB 真相源先写（失败即抛，不碰 JSON）
    state_store.init_store()
    if state_store.get_account(account_id) is None:
        state_store.upsert_account(account_id, broker="qmt")
    state_store.insert_trade_event(account_id, trade_id, symbol, "VETOED")  # 幂等 UNIQUE
    # ② JSON 镜像后写（DB 已落，JSON 失败不影响真相源）
    _veto_in_json(date, symbol)  # 既有 JSON 改写逻辑改名调用（见下）
    print(f"已否决 {symbol}（date={date}，DB+JSON 双写）")
```

把原 veto 里改 JSON 的逻辑抽成 `_veto_in_json(date, symbol)`（或保留原行内代码，只在前面加 DB 写入）。**关键不变量**：DB 写在最前，失败抛错不执行 JSON。

- [ ] **Step 4: 改 confirm_plan 补人工确认路径的 DB CONFIRMED**

Grep `trading/trading_plan.py` `def confirm_plan`（:78）。当前只改 JSON。eod_plan 自动确认路径已写 DB CONFIRMED（engine.py:638），但人工 `confirm_plan` 调用（钉钉回复确认触发）没写。补：

```python
def confirm_plan(date: str) -> bool:
    plan = load_plan(date)
    if plan is None:
        return False
    # W2：人工确认路径补 DB CONFIRMED（与 eod_plan auto 路径对齐，真相源不漂移）。
    # DB 失败软降级（JSON 已是真相源的展示镜像，DB 下次补；不阻断人审确认）。
    try:
        import os
        from trading import state_store
        account_id = os.getenv("QMT_ACCOUNT_ID", state_store._DEFAULT_ACCOUNT_ID)
        state_store.init_store()
        if state_store.get_account(account_id) is None:
            state_store.upsert_account(account_id, broker="qmt")
        for o in plan.get("orders", []):
            sym = (o.get("order") or {}).get("symbol")
            if not sym:
                continue
            trade_id = f"{account_id}_{sym}_{date}"
            if state_store.get_latest_action(trade_id) != "VETOED":  # veto 保护不覆盖
                state_store.insert_trade_event(account_id, trade_id, sym, "CONFIRMED")
    except Exception:
        import logging; logging.getLogger(__name__).exception("confirm_plan DB 写 CONFIRMED 失败（软降级）")
    plan["confirmed"] = True
    _plan_path(date).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return True
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_veto_plan_db.py tests/trading/test_engine.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading/tools/veto_plan.py trading/trading_plan.py tests/trading/test_veto_plan_db.py
git commit -m "fix(plan): W2 veto/confirm DB 双写，pre_open 既有 veto 防线即刻生效"
```

---

### Task 5: W1.4 单引擎探测告警 + .env 配置回正 + 运维 SOP

**Files:**
- Create: `docs/superpowers/runbooks/2026-08-04-gateway-ops.md`（运维 SOP）
- Modify: `trading/__main__.py`（启动探测：port 8000 + PID 探活，双命中 CRITICAL 退出）
- Test: `tests/trading/test_main_singleton.py`（新建，断言探测逻辑）

**Interfaces:**
- Produces: `__main__` 启动时若探测到 port 8000 被占用且持有者是存活 python 进程 → `sys.exit` 前 CRITICAL 告警。

**背景**：memory [[qmt-connect-1-rootcause]] 真根因是 `python -m trading` 嵌套子进程抢 session，端口探测拦不住嵌套父子（共享端口归属父）。代码侧只加告警不自动杀（自动杀误杀 schtasks 链风险高）；真根治靠运维清理 + 启动告警。

- [ ] **Step 1: 写失败测试——启动探测命中既有实例则退出**

```python
# tests/trading/test_main_singleton.py
import pytest
def test_main_exits_when_port_held_by_live_process(monkeypatch):
    """W1.4: port 8000 被占用且持有 PID 存活 → CRITICAL + sys.exit(1)。"""
    import trading.__main__ as m
    monkeypatch.setattr(m, "_port_holder_alive", lambda port: 12345)  # 假装探测到存活 PID
    monkeypatch.setattr(m, "_alert_critical", lambda msg: None)
    with pytest.raises(SystemExit) as ei:
        m._assert_single_instance()
    assert ei.value.code == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_main_singleton.py -v`
Expected: FAIL（`_assert_single_instance` 不存在）

- [ ] **Step 3: 在 `trading/__main__.py` 加启动探测**

Grep `trading/__main__.py` 的启动入口（`run_server` 调用前），加：

```python
import os, sys, socket, logging
logger = logging.getLogger(__name__)

def _port_holder_alive(port: int) -> int | None:
    """W1.4: 探测 port 是否被占用；被占用时返持有的 PID（无法定位 PID 返 -1），否则 None。
    注意：嵌套父子进程共享端口归属父，本探测拦不住嵌套——这是告警手段非根治（见 runbook）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        if result == 0:
            return -1  # 端口被占，无法跨进程拿 PID（Windows），返 -1 表示「占用但 PID 未知」
        return None
    except OSError:
        return None

def _assert_single_instance(port: int = 8000) -> None:
    """W1.4: 单引擎硬约束——port 被既有实例占用 → CRITICAL + 退出（防双进程抢 session）。"""
    holder = _port_holder_alive(port)
    if holder is not None:
        msg = (f"端口 {port} 已被既有引擎实例占用（holder_pid={holder}）。"
               f"禁止双引擎并行（QMT session 抢连 connect -1 根因）。"
               f"请先 `taskkill /F /PID <holder>` 或查 schtasks QuanterServer 链，再启动。")
        logger.critical(msg)
        try:
            from trading.engine import _alert_critical
            _alert_critical(msg)
        except Exception:
            pass
        sys.exit(1)
```

在 `run_server` 调用前调 `_assert_single_instance()`。

- [ ] **Step 4: 写运维 SOP runbook**

```markdown
# 2026-08-04 网关/真相源治理运维 SOP

## 1. 多余引擎进程清理（W1.4）
- 现状：系统 Python + venv 多个 `python -m trading` 并存（37168→35736 嵌套）
- 清理：`tasklist /FI "IMAGENAME eq python.exe"` 列出；保留 schtasks `QuanterServer` 拉起的链（查 `schtasks /Query /TN QuanterServer` 的 PID），其余 `taskkill /F /PID <pid>`
- 验证：`netstat -ano | findstr :8000` 只剩一个 PID 持有

## 2. .env 配置回正（spec #7）
- `TRADE_SHADOW_MIN_DAYS=5`（事故机=1，红线 5）
- 确认 `AUTO_TRADE_MODE`/`AUTO_CONFIRM_PLAN` 是否真要全自动 live（人审 veto 是唯一刹车，W2 已修）

## 3. CSV 测试脏行清理（W3.5 前置）
- 备份：`copy logs\live_trades.csv logs\live_trades.csv.bak.20260804`
- 清 `600000.SH/300001.SZ/300002.SZ` 的 `成交回报@` 测试行（按 scripts/migrate_live_trades_csv.py 的 TEST_FILL_SYMBOLS）
- ⚠️ 清理前 must 人工核对 QMT 客户端真实持仓（memory: 茅台 smoke 真成交买入不扣 cash，账户侧可能真有 600000.SH 持仓）

## 4. 75MB 残留队列清理
- 删 `userdata_mini\down_queue_win_123459`（W1.3 后引擎启动自动清，可手动预热）

## 5. 启动顺序（重启 engine 前）
1. 清多余进程（§1）
2. 确认 miniQMT 客户端已启动 + 登录
3. 启动 schtasks QuanterServer（`schtasks /Run /TN QuanterServer`）
4. 观察 `_health_guard` 日志：1 分钟内应见「网关已连接」或「客户端未就绪（诊断文案）」WARNING
```

- [ ] **Step 5: 运行测试确认通过 + Commit**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_main_singleton.py -v`
Expected: PASS

```bash
git add trading/__main__.py tests/trading/test_main_singleton.py docs/superpowers/runbooks/2026-08-04-gateway-ops.md
git commit -m "fix(ops): W1.4 单引擎启动探测告警 + 配置回正 + 运维 SOP runbook"
```

---

## Phase 1（P1，本周）

### Task 6: W3.1 _handle_order_update 成交分支 CSV/钉钉幂等

**Files:**
- Modify: `trading/engine.py` `_handle_order_update` 的 trade 分支（Grep `record_live_trade` 调用点，约 :3046 漂移后）
- Test: `tests/trading/test_engine_order_update_handler.py`（加「重放只写 1 行」断言）

**Interfaces:**
- Consumes: `state_store.insert_fill(...) -> bool`（True=首次写入）
- Produces: trade 分支仅在 `insert_fill` 返 True 时才 `record_live_trade(kind="fill")` + `notify_trade_event`。

**背景**：`record_live_trade` 被 `submit_order`（下单审计 kind=submit，本就该无条件记，不动）+ `_handle_order_update`（成交回报 kind=fill，要幂等）两处调。本 task 只改后者。

- [ ] **Step 1: 写失败测试——重放同 (order_id, traded_time) 只写 1 行 CSV**

```python
def test_trade_replay_writes_csv_only_once(tmp_path, monkeypatch):
    """W3.1: 同 (order_id, traded_time) 成交回报重放 → insert_fill 返 False → CSV/钉钉不重复。"""
    db = str(tmp_path / "ts.db"); monkeypatch.setattr("trading.state_store._DEFAULT_DB", db)
    csv_path = str(tmp_path / "live_trades.csv")
    monkeypatch.setattr("presentation.server.services.trading_service.LIVE_TRADE_LOG", csv_path)
    from trading import state_store
    state_store.init_store(db); state_store.upsert_account("acct", broker="qmt")

    # 读 _handle_order_update 当前实现，构造一个 trade update dict（order_id/traded_time/symbol/...）
    # 调两次 _handle_order_update(update)，断言 csv_path 只 1 行 kind=fill
    # （具体 update dict 形状参照既有 test_engine_order_update_handler.py 的 fixture）
    ...
    assert csv行数 == 1
```

> 执行者第一步：Grep `tests/trading/test_engine_order_update_handler.py` 既有 fixture 看 trade update dict 形状，复用它构造重放场景。

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py -k replay -v`
Expected: FAIL（当前 record_live_trade 在 insert_fill 外无条件调）

- [ ] **Step 3: 改 _handle_order_update trade 分支**

Grep `_handle_order_update` 里 `kind == "trade"` 或成交回报处理段（含 `insert_fill` + `record_live_trade` 调用）。按 spec §3.3.1 骨架重构：

```python
        # 仅 trade 分支（成交回报），order 分支不动
        if update.get("kind") == "trade":
            inserted = _state_store.insert_fill(
                order_id=str(update["order_id"]), account_id=account_id,
                traded_time=str(update["traded_time"]), symbol=update["stock_code"],
                direction=direction, qty=float(update["traded_volume"]),
                price=float(update["traded_price"]))
            if inserted:
                # W3.1：首次写入才写 CSV/钉钉/position（与真相源同一判定点，spec §3.3.1）
                _state_store.apply_fill_to_position(account_id, update["stock_code"], direction,
                    float(update["traded_volume"]), float(update["traded_price"]), str(update["traded_time"]))
                _state_store.insert_trade_event(account_id, trade_id, update["stock_code"], "FILLED",
                    order_id=str(update["order_id"]), qty=float(update["traded_volume"]),
                    price=float(update["traded_price"]))
                record_live_trade(update["stock_code"], direction, float(update["traded_volume"]),
                    float(update["traded_price"]), kind="fill", ...)
                # notify_trade_event 也只在 inserted 时调（首次才推钉钉）
            else:
                logger.info("成交回报重复，跳过 CSV/钉钉/position（order_id=%s traded_time=%s）",
                            update["order_id"], update["traded_time"])
```

**关键**：先把当前 `_handle_order_update` 完整源码读出来（codegraph_explore `_handle_order_update` 拿 body），确认 insert_fill 当前是否已被调用、record_live_trade 与它的位置关系，再按上面骨架把 record_live_trade/notify 挪进 `if inserted:` 块。不要破坏 order 分支（委托回报）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_order_update_handler.py tests/trading/test_e2e_trading_flow.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_engine_order_update_handler.py
git commit -m "fix(engine): W3.1 成交回报 CSV/钉钉幂等（insert_fill 首次成功才写，消灭重放刷屏）"
```

---

### Task 7: W3.2 消费端切 state_store.fill + 简报去重 + 持仓三态

**Files:**
- Modify: `presentation/server/services/trading_service.py` `query_trades`（改读 state_store.fill，CSV 降级）
- Modify: `broadcast/__main__.py` / `brief_trading.py`（简报去重 + 三态持仓）
- Modify: 加 env `LIVE_TRADE_READ_SOURCE=db|csv`（缺省 db，回滚开关）
- Test: `tests/trading/test_query_trades_db.py`（新建）+ 简报 fixture 测试

**Interfaces:**
- Produces: `query_trades(start, end, ...)` 优先读 `state_store.fill`（按 traded_time 日期 + direction），DB 空/异常时按 env 回退 CSV；`build_trading_brief` 对 `(traded_time, symbol, shares, price)` 去重并输出「重放 N 次」段。

- [ ] **Step 1: 写失败测试——query_trades 读 DB fill**

```python
def test_query_trades_reads_db_fill_first(tmp_path, monkeypatch):
    """W3.2: query_trades 优先读 state_store.fill；DB 有数据时不碰 CSV。"""
    db = str(tmp_path / "ts.db"); monkeypatch.setattr("trading.state_store._DEFAULT_DB", db)
    csv_path = str(tmp_path / "live_trades.csv"); monkeypatch.setattr(
        "presentation.server.services.trading_service.LIVE_TRADE_LOG", csv_path)
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "db")
    from trading import state_store
    state_store.init_store(db); state_store.upsert_account("acct", broker="qmt")
    state_store.insert_fill("oid1", "acct", "20260805101000", "300001.SZ", "BUY", 100, 10.5)

    from presentation.server.services.trading_service import query_trades
    res = query_trades("2026-08-05", "2026-08-05")
    assert res["total"] == 1
    assert res["trades"][0]["symbol"] == "300001.SZ"

def test_query_trades_fallback_csv_when_env_set(monkeypatch, tmp_path):
    """W3.2: LIVE_TRADE_READ_SOURCE=csv → 回退 CSV 读口（回滚开关）。"""
    # 写一行 CSV，DB 空，env=csv → query_trades 读 CSV
    ...
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_query_trades_db.py -v`
Expected: FAIL（query_trades 当前只读 CSV）

- [ ] **Step 3: 加 state_store.fill 查询函数 + 改 query_trades**

在 `state_store.py` 加（参照 insert_fill 的 traded_time 口径，YYYYMMDDHHMMSS）：

```python
def query_fills(start: str, end: str, *, symbol: str | None = None,
                direction: str | None = None, db_path: str | None = None) -> list[dict]:
    """查 [start,end]（YYYY-MM-DD）内 fill 表（成交流水真相源，W3.2 消费端读口）。
    traded_time 是 YYYYMMDDHHMMSS 整数串，按日期前缀 [start,end] 闭区间比较。"""
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        sql = ("SELECT order_id, traded_time, symbol, direction, qty, price, account_id"
               " FROM fill WHERE substr(traded_time,1,8) BETWEEN ? AND ?")
        params = [start.replace("-", ""), end.replace("-", "")]
        if symbol:
            sql += " AND symbol=?"; params.append(symbol)
        if direction:
            sql += " AND direction=?"; params.append(direction.upper())
        rows = con.execute(sql + " ORDER BY traded_time", params).fetchall()
    return [{"order_id": r["order_id"], "traded_time": r["traded_time"], "symbol": r["symbol"],
             "direction": (r["direction"] or "").lower(), "shares": float(r["qty"]),
             "price": float(r["price"])} for r in rows]
```

改 `query_trades`：开头加 `if os.getenv("LIVE_TRADE_READ_SOURCE", "db") == "db":` 分支调 `state_store.query_fills`，异常或空且 env=csv 才回退原 CSV 逻辑。保持返回 `{trades, total, limit, offset}` shape 不变（前端契约）。

- [ ] **Step 4: 改简报去重 + 持仓三态**

Grep `broadcast/__main__.py` 读简报消费段（约 :172）+ `brief_trading.py` 的 positions 渲染（约 :91）。
- 简报数据源：`query_trades`（已切 DB）→ `build_trading_brief` 对 `(traded_time, symbol, shares, price)` 去重，N>1 时输出「同一成交重放 N 次」段。
- 持仓三态：`/positions` 409 或网关未连 → 渲染「持仓未知（网关未连接）」，不渲染「当前无持仓」。Grep brief_trading 的 positions 段加分支：`if 仓位获取失败: "持仓未知（网关未连接）"`。

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_query_trades_db.py tests/broadcast/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading/state_store.py presentation/server/services/trading_service.py broadcast/ tests/trading/test_query_trades_db.py
git commit -m "fix(brief): W3.2 消费端切 state_store.fill + 简报去重 + 持仓三态"
```

---

### Task 8: W3.4 post_close 改 broker 权威对账（fill 表只做归因）

**Files:**
- Modify: `trading/engine.py` `post_close`（Grep `aggregate_fills_by_symbol` 调用点，约 :1606 漂移后）
- Test: `tests/trading/test_post_close_reconcile.py`（新建）

**Interfaces:**
- Produces: post_close 持仓对账以 `gw.query_stock_positions`（broker 权威）为准；`aggregate_fills_by_symbol(CSV)` 不再参与账本重写，降级为「今日成交归因」可选展示。

**背景（拷问 3 结论）**：fill 表空可能是「网关断线无回报」而非「真无成交」，post_close 不能用 fill 重写 position（否则与柜台漂移）。broker query_stock_positions 才是持仓权威；fill 只解释「今日变动归因」。

- [ ] **Step 1: 写失败测试——post_close 不以 CSV/fill 重写 position**

```python
def test_post_close_uses_broker_authority_not_csv(tmp_path, monkeypatch):
    """W3.4: post_close 持仓以 broker 为权威；CSV 脏行不污染 position_book。"""
    # mock gw.query_stock_positions 返真实持仓 {300001.SZ: {volume:100,...}}
    # 在 CSV 写 24 行重复 600000.SH BUY 100（事故场景）
    # 调 post_close → 断言 position_book 里 600000.SH 不被重写成 2400
    ...
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_post_close_reconcile.py -v`
Expected: FAIL（当前 post_close 用 aggregate_fills_by_symbol 重写）

- [ ] **Step 3: 改 post_close 对账口径**

Grep `post_close` 里 `aggregate_fills_by_symbol` 调用段。把「聚合 CSV → diff position_book → 重写 qty」改为「`gw.query_stock_positions()`（broker 权威）→ reconcile(position_book, broker) → 仅在 drift 时告警，不自动重写（或仅以 broker 为准覆盖，绝不以 CSV 为准）」。`aggregate_fills_by_symbol` 降级为日志展示「今日成交归因」，不参与账本。

**注意**：先读 post_close 完整源码（codegraph_explore `post_close` 拿 body），确认当前 reconcile 路径与 `sync_positions`/`reconcile` 的关系（blast radius 显示 `sync_positions → reconcile`），尽量复用既有 reconcile 通道，不另造。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_post_close_reconcile.py tests/trading/test_e2e_trading_flow.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_post_close_reconcile.py
git commit -m "fix(engine): W3.4 post_close 改 broker 权威对账，CSV/fill 不再重写持仓"
```

---

### Task 9: W4 account_daily 闭合（pre_open 改调 state_store.snapshot_start_equity）

**Files:**
- Modify: `trading/engine.py` `_pre_open_impl` ②.5 段（Grep `_position_book.snapshot_start_equity`，约 :798）
- Test: `tests/trading/test_engine.py`（加 daily_pnl 闭合 e2e）

**Interfaces:**
- Produces: pre_open 抓熔断基线改调 `_state_store.snapshot_start_equity(account_id, date, total, cash)`（写 account_daily），与 post_close 的 `_state_store.snapshot_close_equity` 同表，`daily_pnl` 闭合。

**背景（发现 2）**：pre_open 当前调 `_position_book.snapshot_start_equity(date, total)`（写 daily_equity 表），post_close 调 `_state_store.snapshot_close_equity`（写 account_daily 表）——两表断链致 daily_pnl 恒 NULL。

- [ ] **Step 1: 写失败测试——daily_pnl 非空**

```python
def test_daily_pnl_closes_after_pre_open_and_post_close(tmp_path, monkeypatch):
    """W4: pre_open 写 account_daily.start，post_close 写 close → daily_pnl 非空（闭合）。"""
    db = str(tmp_path / "ts.db"); monkeypatch.setattr("trading.state_store._DEFAULT_DB", db)
    from trading import state_store
    state_store.init_store(db); state_store.upsert_account("acct", broker="qmt")
    # pre_open 写 start=100000
    state_store.snapshot_start_equity("acct", "2026-08-05", 100000.0, 50000.0)
    # post_close 写 close=101500
    state_store.snapshot_close_equity("acct", "2026-08-05", 101500.0)
    # 读 account_daily 验证 daily_pnl
    with state_store._connect(db) as con:
        row = con.execute("SELECT daily_pnl FROM account_daily WHERE account_id=? AND date=?",
                          ("acct", "2026-08-05")).fetchone()
    assert row and row["daily_pnl"] == 1500.0
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py::test_daily_pnl_closes_after_pre_open_and_post_close -v`
Expected: FAIL（`snapshot_close_equity` 当前已能闭合单表——这个单测应 PASS；真正失败的是 pre_open 没调它。调整测试为 e2e：mock gw.query_asset，跑 pre_open + post_close，断言 account_daily 同 date 有 start+close+pnl）

> 测试调整：本 task 的核心是 **pre_open 调用点切换**。单测 `snapshot_start_equity`+`snapshot_close_equity` 同表闭合本就 PASS（验证函数本身正确）；需补的 e2e 是「pre_open 实际调了 state_store 版而非 position_book 版」。可在 pre_open 改造后用 monkeypatch spy 断言调用的是 `_state_store.snapshot_start_equity`。

- [ ] **Step 3: 改 pre_open ②.5 调用点**

Grep `_pre_open_impl` 里 `_position_book.snapshot_start_equity(today_eq, float(total))`（②.5 抓熔断基线段），改为：

```python
            if total is not None and float(total) > 0:
                # W4（08-04 断链根治）：改调 state_store.snapshot_start_equity 写 account_daily，
                # 与 post_close 的 snapshot_close_equity 同表 → daily_pnl 闭合（原调 position_book
                # 版写 daily_equity 表，两表断链致 daily_pnl 恒 NULL）。
                cash = (asset or {}).get("cash")
                _state_store.snapshot_start_equity(
                    _resolve_account_id(), today_eq, float(total),
                    float(cash) if cash is not None else None)
```

保留 daily_equity 表读口作熔断基线兼容（或确认熔断读 account_daily.start_total_asset，本 task 不强求删 daily_equity——降级读口归 W6）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_engine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_engine.py
git commit -m "fix(engine): W4 pre_open 熔断基线改写 account_daily，daily_pnl 闭合"
```

---

### Task 10: W5 数据就绪单口 get_ready(date, datasets)

**Files:**
- Modify: `trading/state_store.py` 或 `trading/calendar.py`（加纯函数 `get_ready`）
- Modify: `trading/engine.py` `_pre_open_gate` ③ 段、`trading/catchup.py`、播报端（统一调 get_ready）
- Test: `tests/trading/test_data_ready.py`（新建）

**Interfaces:**
- Produces: `get_ready(date: str, datasets: list[str] | None = None) -> bool` = `data_ready` 全绿 AND `job_ledger.latest_status("pipeline", date) == "done"` AND（可选）parquet mtime 新鲜。

- [ ] **Step 1: 写失败测试——三源组合判定**

```python
def test_get_ready_combines_three_sources(monkeypatch, tmp_path):
    """W5: get_ready = data_ready 全绿 AND job_ledger.pipeline(done) AND parquet 新鲜。"""
    # 三源分别 mock，组合验证 True/False
    ...
    assert get_ready("2026-08-05", ["daily_quotes"]) is True  # 三源全绿
    # 任一源红 → False
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_data_ready.py -v`
Expected: FAIL（get_ready 不存在）

- [ ] **Step 3: 加 get_ready 并接入三处消费点**

在 `trading/calendar.py`（或 state_store）加纯函数：

```python
def get_ready(date: str, datasets: list[str] | None = None) -> bool:
    """W5: 数据就绪单口（pre_open gate③ / catchup / 播报统一消费）。
    判定 = data_ready(datasets) 全绿 AND job_ledger.pipeline(done) AND parquet mtime 新鲜。
    任一源失败 → False，并在日志显式暴露差异（消除「台账 done、内容缺、播报 healthy」三张嘴）。"""
    from trading import job_ledger
    from data.data_ready import check_ready  # Grep 确认既有 data_ready 入口名
    try:
        if not check_ready(date, datasets or default_datasets(date)):
            logger.warning("get_ready=False：data_ready 内容校验未全绿 date=%s", date); return False
    except Exception:
        logger.exception("get_ready data_ready 检查异常 date=%s", date); return False
    if job_ledger.latest_status("pipeline", date) != "done":
        logger.warning("get_ready=False：job_ledger.pipeline 非 done date=%s", date); return False
    return True
```

接入：Grep `_pre_open_gate` ③ data-ready 段 + `trading/catchup.py` + 播报端（`ops/brief_all.py` 或 broadcast），把各自的 data_ready/job_ledger 分散判定改为调 `calendar.get_ready(date, datasets)`。播报端保留 mtime 双口径展示（观测健康度），「是否放行挂单」只用 get_ready。

> 执行者先 codegraph_explore `data_ready check_ready job_ledger.latest_status` 确认既有函数名/签名（spec 引用 `[pipeline.py:131]/[data_service.py:94]` 行号已漂移），按实际签名接入。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_data_ready.py tests/trading/test_engine_pre_open_gate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/calendar.py trading/engine.py trading/catchup.py tests/trading/test_data_ready.py
git commit -m "feat(ready): W5 数据就绪单口 get_ready，三处消费统一 + 漂移显式暴露"
```

---

## 验收清单（对应 spec §4 验收）

### Phase 0（T1-T5）
- [ ] `is_client_ready` 对「目录存在」返 True（T1 单测）
- [ ] 客户端运行 >1 小时后重启引擎，`_health_guard` 1 分钟内连上（手动 e2e + runbook §5）
- [ ] 未就绪状态出现 WARNING + 每 10 轮钉钉（T2 caplog 断言）
- [ ] veto 后 pre_open 跳过被否标的（T4 DB+既有防线 e2e）
- [ ] 双引擎启动第二实例时 CRITICAL 退出（T5 单测）
- [ ] `.env TRADE_SHADOW_MIN_DAYS=5` 回正（runbook §2 执行）

### Phase 1（T6-T10）
- [ ] 重放同 (order_id, traded_time) → CSV 仅 1 行、钉钉 1 条（T6 回归）
- [ ] 简报对重复行去重且显示「重放 N 次」（T7 fixture）
- [ ] 断线时简报显示「持仓未知」，非「当前无持仓」（T7 fixture）
- [ ] post_close 不以 CSV 重写 position（T8 mock 脏 CSV）
- [ ] `account_daily` 同 date 有 start+close，`daily_pnl` 非空（T9 e2e）
- [ ] `get_ready` 三源组合判定 + 漂移暴露（T10 单测）

## Self-Review（plan 作者自检）

**1. Spec 覆盖**：W1（T1-T3,T5）/W2（T4）/W3（T6-T8 + T5 CSV 清理在 runbook§3）/W4（T9）/W5（T10）全覆盖。W3.5 CSV 脏清理放 runbook（运维手动，因需人工核对柜台持仓）。W6 治理文档/巡检脚本明确 out of scope（Phase 2 另写）。✅
**2. 占位符扫描**：T7/T8/T10 的「先 codegraph_explore 读当前实现再改」是诚实标注（未核实代码不编造），非占位符；每个都给了目标代码骨架 + 明确符号定位。✅
**3. 类型一致**：trade_id 全链路 `f"{account_id}_{sym}_{date}"`；`snapshot_start_equity` state_store 版签名 `(account_id, date, total, cash)`；`insert_fill -> bool`。✅
**4. 顺序依赖**：T2 依赖 T1 的 `_client_staleness_diag`（Interfaces 标注）；T4 独立；T6-T10 互不依赖（可并行）。✅

---

## Execution Handoff

Plan 完整保存于 `docs/superpowers/plans/2026-08-04-gateway-ssot-hardening.md`。两种执行方式：

**1. Subagent-Driven（推荐）**：每个 task 派一个新 subagent，task 间 review（implementer → reviewer → fix loop），快迭代。适合本 plan——10 个 task 多数独立，T1→T2 有依赖可串行，T4/T6-T10 可并行。

**2. Inline Execution**：本会话内按 executing-plans 批量执行，检查点 review。适合你想全程盯着改。

**建议**：Phase 0（T1-T5）用 Subagent-Driven 串行（P0 风险高，每 task review gate 把关）；Phase 1（T6-T10）可并行 dispatch。先落 Phase 0 再启动 Phase 1（P0 落地后 spec 事实偏差已校准，P1 代码定位更准）。

---

## 实现偏差说明（相对 spec · Fix4 用户两轴 review）

本节诚实标注实现与 spec 的两处偏差，供后续运维/review 一目了然。**功能满足核心契约**，偏差均在 brainstorm 时经用户认可。

### W1.1 两级就绪（spec §3.1.1 描述「进程级（弱）+ 活跃级（强）」）

- **spec 描述**：进程级（userdata 目录存在）= 弱判定 + 活跃级（shm 文件 mtime 新鲜）= 强判定，两级组合 gate connect。
- **实现简化**：userdata 目录存在即 ready + connect 返回码权威。强就绪信号（mtime）未参与判定，仅 `_client_staleness_diag` 作日志分类素材（WARNING 文案用）。
- **设计理由**：connect 返回码是客户端可用性唯一权威；文件 mtime 是启发式不可靠（**08-04 事故根因**：shm 文件是启动时一次性生成不刷新，>5min 即判死 → `_health_guard` 永不 connect → 全天静默跳过）。brainstorm 时用户认可「Plan A+B+W1 = connect 返回码唯一权威」，强就绪信号降级为日志素材防「换探针复发静默跳过」。功能满足核心契约（connect 失败仍由返回码 + 锁态兜底，不会裸跑真单）。

### W1.4 PID 探活（spec §3.1.4 要求 single_instance 锁持有者 PID 探活）

- **spec 要求**：single_instance 锁持有者 PID 探活，区分「自己持有」与「他进程持有」。
- **实现退化**：端口占用探测 + `sys.exit(1)`（docstring 已诚实标注「Windows 跨进程拿不到可靠 PID」）。
- **设计理由**：PID 探活在 Windows 不可靠（嵌套父子共享端口归属父进程，psutil 取到父 PID 仍 probe alive → 假精确）。加了也是假精确反而误导运维。真根治靠 **运维 SOP §1**（树杀双进程）+ 启动期 `_alert_critical` 告警（W1.4 已接线）。功能满足核心契约（双起仍被 single_instance 拒，只是拒因是「端口占用」而非「PID 探活」）。
