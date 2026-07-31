# C-4 错误分级 + 调度硬化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐 task 执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 给 `TradingEngine` 四触发点的软降级 `except` 建立三级分级（L1 致命硬抛 `_CriticalHalt` + CRITICAL + 停调度 / L2 警告 CRITICAL 不停 / L3 保留），硬化 APScheduler `job_defaults`，收口 C-3 cancel 幂等审计。

**Architecture:** 模块级 `_CriticalHalt` 异常 + `_critical_guard` 装饰器 + `TradingEngine._halt(msg)` 统一停调度原语（幂等：置 `_halted` + `_alert_critical` + `sched.shutdown(wait=False)`）。五 job 全部收编为 `TradingEngine` method 并装 `@_critical_guard`；L1 路径内 `raise _CriticalHalt` 经 wrapper 捕获后调 `_halt`。L1/L2 判定线：**基础设施（DB 写/读异常·网关断线·整批失败）= L1 停；单只业务拒单 = L2 聚合 CRITICAL 不停**（聚合防告警风暴）。

**Tech Stack:** Python 3.10 · APScheduler（AsyncIOScheduler）· pytest + pytest-asyncio · SQLite（state_store）。运行测试：`./.venv310/Scripts/python.exe -m pytest`。

## Global Constraints（每个 task 隐含遵守）

- **全中文注释**（CLAUDE.md）：新增/修改代码块上方单行标注物理意图 / Why / 防范的边界。
- **只改异常形态，不改业务控制流**（spec §2 非目标）：L1 改造 = 把指定 `except` 的 `logger + continue/return` 改成 `raise _CriticalHalt(...) from e`；**不动** plan 读取、白名单注入、挂单循环结构、`decide_exit` 分发逻辑。
- **不重建 C-1 UNIQUE**（spec 非目标）：order/trade_event/fill UNIQUE 主体已闭环；U5 仅补 `account_id` 透传激活既有 `cancel_order_by_broker_oid_db` 回写 + docstring 文档化。
- **不含 live 上线**：本计划是 live 前置；不引入 `AUTO_TRADE_MODE=live` 改动。
- **极简显式**（Karpathy）：`_CriticalHalt` 是裸 `Exception` 子类，不引入重型基类；`_halt` 平铺直叙无魔法。
- **变量别名沿用现状**：engine.py 内 `_state_store` / `_ACTIVE_ENGINE` / `_mode()` / `_alert_critical` / `_resolve_account_id()` / `_cancel_all_open_orders` / `_submit` / `_position_book` / `get_gateway()` 等模块级别名保持不变。

---

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `trading/engine.py` | `_CriticalHalt` + `_critical_guard`（模块级，~line 94 后）；`TradingEngine.__init__`（+ `_halted`、job_defaults、`_pipeline_then_eod` method 替换 add_job）；`_halt(msg)` method；四 method 装饰；`pre_open` / `stop_loss_monitor` 内 L1/L2 改造 | 主体改造 |
| `trading/orchestrate/pipeline.py` | `pipeline_then_eod` 采集 `rc!=0` 改 `raise _CriticalHalt` | 小改 |
| `trading/state_store.py` | 顶部 docstring 补 `(date,symbol,side,operation_type)` 幂等键约定 | 文档 |
| `tests/trading/test_critical_guard.py` | 新建：`_critical_guard` / `_halt` / `_halted` 单测 | 新建 |
| `tests/trading/test_engine_scheduler_hardening.py` | 新建：`job_defaults` 三参数断言 | 新建 |
| `tests/trading/test_pre_open_l1_halt.py` | 新建：pre_open DB 写失败 → `_CriticalHalt` → 停调度 | 新建 |
| `tests/trading/test_stop_loss_l1_halt.py` | 新建：stop_loss 查持仓失败 → 停调度 | 新建 |
| `tests/trading/test_l2_aggregated_critical.py` | 新建：单只失败聚合 CRITICAL 不停调度 | 新建 |
| `docs/superpowers/audits/2026-07-31-cancel-idempotency-audit.md` | 新建：C-3 审计结论 + UNIQUE 覆盖度 grep 记录 | 新建 |

---

## Task 1: APScheduler job_defaults 硬化（U1）

**Files:**
- Modify: `trading/engine.py:1685`（`self.sched = AsyncIOScheduler()`）
- Test: `tests/trading/test_engine_scheduler_hardening.py`（Create）

**Interfaces:**
- Produces: `TradingEngine.sched.job_defaults` 含 `max_instances=1` / `misfire_grace_time=300` / `coalesce=True`。

- [ ] **Step 1: 写失败测试**

创建 `tests/trading/test_engine_scheduler_hardening.py`：
```python
# -*- coding: utf-8 -*-
"""U1：APScheduler job_defaults 三参数硬化断言。

物理意图：裸 AsyncIOScheduler() 无 job_defaults——机器休眠/慢触发会 job 堆积重叠
（pre_open 跑超 9:22 与下次重叠双挂、stop_loss 30s 堆积补跑风暴）。三参数锁死。
"""
import pytest
from trading.engine import TradingEngine


def test_sched_job_defaults_hardened():
    """构造 engine 即断言 sched.job_defaults 含三参数（防回归到裸构造）。"""
    eng = TradingEngine()
    jd = eng.sched.job_defaults
    assert jd.get("max_instances") == 1, f"max_instances 应为 1（防重叠双挂），实得 {jd.get('max_instances')}"
    assert jd.get("misfire_grace_time") == 300, f"misfire_grace_time 应为 300s，实得 {jd.get('misfire_grace_time')}"
    assert jd.get("coalesce") is True, f"coalesce 应为 True（堆积合并一次），实得 {jd.get('coalesce')}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_scheduler_hardening.py -v`
Expected: FAIL（`max_instances` 为 None / 默认值）。

- [ ] **Step 3: 改 scheduler 构造**

`trading/engine.py:1685`，把：
```python
        self.sched = AsyncIOScheduler()
```
改为：
```python
        # C-4 U1：job_defaults 硬化（防 job 堆积重叠 + 休眠补跑风暴）。
        # max_instances=1：每 job 同时只一个实例——pre_open 挂单慢（QMT 限频）跑超 9:22，
        #   下次触发被挡，防重叠双挂；stop_loss 30s 跑超 30s 同理防重叠发卖。
        # misfire_grace_time=300：机器休眠/重启错过触发——5min 内补跑（保盘后 job 不轻易漏），
        #   超 5min 放弃（stop_loss 30s 堆积 10 次只补最近 1 次，防补跑风暴）。
        # coalesce=True：与 misfire 配合，堆积合并成一次（不补跑多次）。
        self.sched = AsyncIOScheduler(job_defaults={
            "max_instances": 1,
            "misfire_grace_time": 300,
            "coalesce": True,
        })
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_scheduler_hardening.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_engine_scheduler_hardening.py
git commit -m "feat(c4-u1): APScheduler job_defaults 硬化（max_instances=1/misfire=300/coalesce=True）"
```

---

## Task 2: 停调度机制（U2 · _CriticalHalt + _halt + _critical_guard + 五 job 收编装饰）

**Files:**
- Modify: `trading/engine.py`（模块级 ~line 120 后加 `_CriticalHalt` + `_critical_guard`；`TradingEngine.__init__` 加 `self._halted` + 新增 `_pipeline_then_eod` method + 改 add_job + 四 method 装饰）
- Test: `tests/trading/test_critical_guard.py`（Create）

**Interfaces:**
- Produces: 模块级 `_CriticalHalt(Exception)`；模块级 `_critical_guard(coro_method) -> wrapper`；`TradingEngine._halted: bool`；`TradingEngine._halt(msg: str) -> None`（幂等）；`TradingEngine._pipeline_then_eod(self) -> coroutine`（被 guard 装饰，内部 await `pipeline_then_eod(self)`）。
- Consumes: `_alert_critical`（engine.py:94，已有）；`self.sched.shutdown(wait=False)`（先例 engine.py:2795）。

- [ ] **Step 1: 写失败测试**

创建 `tests/trading/test_critical_guard.py`：
```python
# -*- coding: utf-8 -*-
"""U2：_critical_guard wrapper + _halt 停调度原语单测。

覆盖：
- raise _CriticalHalt → _halted=True + sched.shutdown 被调 + _alert_critical 被调；
- _halted=True 时被装饰 job 入口即跳过（不执行函数体）；
- _halt 幂等（二次调不重复 shutdown）。
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from trading.engine import TradingEngine, _CriticalHalt, _critical_guard as _apply_guard


@pytest.mark.asyncio
async def test_critical_halt_triggers_halt_and_shutdown():
    """被装饰 method 内 raise _CriticalHalt → _halt 置 _halted + shutdown + alert。"""
    eng = TradingEngine()

    # 直接用真实的 _critical_guard 装饰一个会抛 _CriticalHalt 的协程函数
    @_apply_guard
    async def boom(self):
        raise _CriticalHalt("DB 写入失败 symbol=X")

    with patch("trading.engine._alert_critical") as ac, \
         patch.object(eng.sched, "shutdown") as sd:
        # _critical_guard 捕获 _CriticalHalt 后 _halt + 再 raise（让 apscheduler 顶层记日志）
        with pytest.raises(_CriticalHalt):
            await boom(eng)   # eng 作为 self 传入 wrapper
    assert eng._halted is True
    ac.assert_called_once()
    sd.assert_called_once_with(wait=False)


@pytest.mark.asyncio
async def test_halted_skips_decorated_job():
    """_halted=True → 被装饰 job 入口即 return，函数体不执行。"""
    eng = TradingEngine()
    eng._halted = True
    called = MagicMock()

    async def inner(self):
        called()

    decorated = _apply_guard(inner)
    await decorated(eng)
    called.assert_not_called()


@pytest.mark.asyncio
async def test_halt_is_idempotent():
    """二次 _halt 不重复 shutdown / 不重复 alert。"""
    eng = TradingEngine()
    with patch("trading.engine._alert_critical") as ac, \
         patch.object(eng.sched, "shutdown") as sd:
        eng._halt("第一次致命")
        eng._halt("第二次致命")
    assert eng._halted is True
    assert ac.call_count == 1   # 只告警一次
    assert sd.call_count == 1   # 只 shutdown 一次


# _apply_guard 见文件顶部 import（从 trading.engine 导入真实 _critical_guard 复用，不 reimplement）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_critical_guard.py -v`
Expected: FAIL（`_CriticalHalt` / `_critical_guard` 未定义，import error）。

- [ ] **Step 3: 加模块级 `_CriticalHalt` + `_critical_guard`**

在 `trading/engine.py` 的 `_alert_critical` 函数之后（约 line 120，`# =====...` 分隔线之前）插入：
```python
# C-4 U2：L1 致命异常 + 停调度 wrapper。
class _CriticalHalt(Exception):
    """L1 致命异常：交易关键路径失败（DB 写/读失真·网关断线·整批失败·敞口未明）。

    物理意图（spec §3 L1 + review 补强边界）：
        抛出本异常 = 「继续跑会致真金损失或状态真相源失真」，_critical_guard 捕获后调
        _halt() 停所有 job。与单只业务拒单（RuntimeError，L2 聚合 CRITICAL 不停）区分。

    边界判定线（review 补强 · 基础设施 > 单只计数）：
        - DB 写异常（insert_order/update_order_state/insert_trade_event/insert_fill 抛错）= L1
          （哪怕只挂一只，DB 真相源失真优先于「单只」语义，硬抛）；
        - 单只 _submit RuntimeError（业务拒单：涨跌停/资金不足/限频）= L2（不抛本异常）。
    """


def _critical_guard(coro_method):
    """L1 路径 wrapper：_halted 检查 + 捕获 _CriticalHalt → _halt 停调度。

    in-flight 语义（review 补强）：
        - 当前 job：raise _CriticalHalt → 异常向上传播，当前 job 在 raise 处立即中断
          后续写（不会 continue 把半截状态写完）；本 wrapper except 捕获后 _halt + 再 raise
          （APScheduler 顶层吞 job 异常记日志，不影响其他 job）。
        - 其他 job / 下一轮：_halted flag 在本 wrapper 顶 if 兜底——max_instances=1 下，
          被触发或堆积补跑的 job 入口即跳过，不再写。
        即 raise 中断「当前轮」，_halted 防「下一轮/其他 job」，覆盖 in-flight 全部窗口。
    """
    import functools
    @functools.wraps(coro_method)
    async def wrapped(self, *a, **kw):
        if getattr(self, "_halted", False):
            logger.warning("引擎已停调度（_halted），跳过 %s", coro_method.__name__)
            return
        try:
            return await coro_method(self, *a, **kw)
        except _CriticalHalt as e:
            self._halt(f"[{coro_method.__name__}] {e}")
            raise   # 再抛：APScheduler 顶层记 job 异常日志；_halt 已生效
    return wrapped
```

- [ ] **Step 4: `__init__` 加 `_halted` + 新增 `_halt` method**

在 `TradingEngine.__init__` 内，`self._guard_fail_count` 初始化附近（约 line 1757 后）加：
```python
        # C-4 U2：停调度 flag（_halt=True 后所有被 _critical_guard 装饰的 job 入口即跳过）。
        # Why 进程内存（不持久化）：致命停调度需人工介入重启，重启后 _halted=False 重新就绪；
        #   持久化反而让重启后仍锁死（与「人工确认恢复」语义冲突）。
        self._halted: bool = False
```

在 `_health_guard` method 之后（或 `_sanity_check_date_alignment` 之前的合适位置）新增 method：
```python
    def _halt(self, msg: str) -> None:
        """L1 统一停调度原语：置 _halted + CRITICAL + sched.shutdown（幂等）。

        物理意图（spec §5 双层保障）：
            sched.shutdown 停「新触发」+ _halted flag 防「in-flight job 继续写」。
            幂等：已 _halted 时直接返回（多路径同时致命不重复 shutdown/alert）。

        Why shutdown(wait=False) 而非 pause()（review 决议）：
            致命场景下「带病跑不如停」——pause 可被误恢复，留口子；shutdown 硬停 + CRITICAL
            唤醒人工，是 live 真金保护取向（spec R4）。
        """
        if self._halted:
            return
        self._halted = True
        _alert_critical(f"致命停调度 {msg}")
        try:
            self.sched.shutdown(wait=False)   # 先例 engine.py shutdown()
        except Exception:
            # shutdown 自身抛（如 scheduler 未 start / 已 shutdown）→ _halted 已置，
            # 被 _critical_guard 装饰的 job 顶检查兜底，不再写。
            logger.exception("sched.shutdown 失败（_halted 已置，job 顶检查兜底）")
```

- [ ] **Step 5: 五 job 收编为 method + 装饰**

(a) 在 `TradingEngine` 内（`_pre_open` / `_stoploss` / `_post_close` / `_health_guard` 四个 method 定义上方）各加 `@_critical_guard`。例如 `_pre_open`（engine.py:2276）：
```python
    @_critical_guard
    async def _pre_open(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        # ... 原逻辑不变
```
对 `_stoploss`（:2283）、`_post_close`（:2418）、`_health_guard`（:1906）同样加 `@_critical_guard`。

(b) 新增 `_pipeline_then_eod` method（让外部函数也过 guard；放在 `_post_close` 附近）：
```python
    @_critical_guard
    async def _pipeline_then_eod(self) -> None:
        """C-4 U2：pipeline_then_eod 收编为 method（过 _critical_guard）。

        Why 包装：pipeline_then_eod 是 orchestrate/pipeline.py 的外部函数（编排层，
        不该塞进 engine），但需要与其他四 job 同享 L1 停调度语义。包一层 method 让
        五 job 统一被 guard 装饰（满足验收标准 2），pipeline 内 raise _CriticalHalt
        经本 wrapper 捕获 → _halt。
        """
        from trading.orchestrate.pipeline import pipeline_then_eod
        await pipeline_then_eod(self)
```

(c) 改 `__init__` 的 add_job（engine.py:1699-1703），把：
```python
        from trading.orchestrate.pipeline import pipeline_then_eod
        self.sched.add_job(
            pipeline_then_eod, CronTrigger.from_crontab(
                os.getenv("ENGINE_PIPELINE_CRON", "0 18 * * 1-5")),
            args=[self], id="pipeline_then_eod",
        )
```
改为：
```python
        # C-4 U2：pipeline 收编为 _pipeline_then_eod method（过 _critical_guard），
        # 替代原外部函数 + args=[self] 形式（五 job 统一装饰）。
        self.sched.add_job(
            self._pipeline_then_eod, CronTrigger.from_crontab(
                os.getenv("ENGINE_PIPELINE_CRON", "0 18 * * 1-5")),
            id="pipeline_then_eod",
        )
```

- [ ] **Step 6: 跑测试确认通过**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_critical_guard.py -v`
Expected: 3 PASS。

- [ ] **Step 7: 跑既有 engine 测试确认零退化**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_bootstrap.py tests/trading/test_engine_pre_open_gate.py -v`
Expected: 全 PASS（装饰器 + add_job 改动未破坏既有 bootstrap/gate 行为）。

- [ ] **Step 8: Commit**

```bash
git add trading/engine.py tests/trading/test_critical_guard.py
git commit -m "feat(c4-u2): _CriticalHalt + _critical_guard + _halt 停调度机制，五 job 收编装饰"
```

---

## Task 3: pre_open L1 路径改造（U3a · DB 读写异常 → raise _CriticalHalt）

**Files:**
- Modify: `trading/engine.py` 内 `pre_open(date)`（:595-832）四个 DB 读写点
- Test: `tests/trading/test_pre_open_l1_halt.py`（Create）

**Interfaces:**
- Consumes: `_CriticalHalt`（U2 产出）；`_state_store.insert_order` / `update_order_state` / `insert_trade_event` / `has_order` / `get_latest_action` / `get_account` / `upsert_account`（既有）。
- Produces: pre_open 内 DB 读写异常向上抛 `_CriticalHalt` → `_pre_open`（被 guard 装饰）→ `_halt`。

**判定线（review 补强，实现严守）：**
- `load_plan` 返 None / `plan.confirmed=False` = **正常业务态**（非错误），保持 return，**不抛**。
- gate 未通过 = **前置条件**（可恢复），保持现有 return + CRITICAL，**不抛**（与 load_plan 抛 sqlite 异常区分）。
- DB **写**异常（insert_order/update_order_state/insert_trade_event）= L1 硬抛。
- DB 幂等**读**异常（has_order/get_latest_action）= L1 硬抛（读失败→「可能重复挂/重复发」=真金损失，spec §3 L1「state_store 关键写入/读失败」）。

- [ ] **Step 1: 写失败测试**

创建 `tests/trading/test_pre_open_l1_halt.py`：
```python
# -*- coding: utf-8 -*-
"""U3a：pre_open DB 写失败 → raise _CriticalHalt → _pre_open 被 guard 捕获 → _halt。

物理意图：原 insert_order(OPEN) except 软降级（logger+继续挂单）= DB 没记但柜台真挂了
→ 对账幽灵单。改 L1：DB 写异常立即停整批，绝不带病继续挂下一只。
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from trading.engine import TradingEngine, _CriticalHalt


@pytest.mark.asyncio
async def test_pre_open_insert_order_failure_raises_critical_halt(monkeypatch):
    """insert_order(OPEN) 抛异常 → pre_open raise _CriticalHalt（不再软降级继续）。"""
    eng = TradingEngine()
    today = "2026-07-31"

    # 让 pre_open 走到挂单循环（plan 已确认 + gate 绿 + 撤单/基线/白名单全 mock）
    with patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.engine._cancel_all_open_orders", new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.engine._load_expired_positions", return_value=[]), \
         patch("trading.engine._ACTIVE_ENGINE", eng), \
         patch("trading.engine._mode", return_value="dry_run"):
        # plan：1 只标的，已确认
        plan = {"confirmed": True, "orders": [{
            "order": {"symbol": "300214.SZ", "qty": 100, "side": "buy", "price": 10.0},
            "formed_at": None}]}
        # gate 绿（_pre_open_gate mock）
        monkeypatch.setattr(eng, "_pre_open_gate", AsyncMock(return_value=(True, "")))
        with patch("trading.engine.trading_plan") as tp:
            tp.load_plan.return_value = plan
            # insert_order 抛异常 → 应 raise _CriticalHalt
            with patch("trading.engine._state_store") as ss:
                ss.get_account.return_value = MagicMock()
                ss.get_latest_action.return_value = None
                ss.has_order.return_value = False
                ss.insert_order.side_effect = RuntimeError("sqlite locked")
                from trading.engine import pre_open
                with pytest.raises(_CriticalHalt, match="insert_order"):
                    await pre_open(today)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_pre_open_l1_halt.py -v`
Expected: FAIL（pre_open 当前 `except: logger.exception` 吞掉，不 raise `_CriticalHalt`）。

- [ ] **Step 3: 改 pre_open 四个 DB 读写点**

(a) **insert_order(OPEN) 写异常 → L1**（engine.py:776-782），把：
```python
        try:
            _order_id = f"{date}_{od['symbol']}_OPEN_1"
            _state_store.insert_order(
                _order_id, trade_id, account_id, date, od["symbol"], od["side"], "OPEN",
                float(od["qty"]), float(od["price"]), state="PENDING")
        except Exception:
            logger.exception("pre_open insert_order(OPEN) 失败 symbol=%s（不阻断挂单）", od["symbol"])
```
改为：
```python
        # C-4 U3a：insert_order 是 DB 真相源写入——失败=柜台可能挂了但 DB 没记=对账幽灵单。
        # 升 L1（review 补强：单只层面 DB 写异常 > 单只计数，硬抛停调度，绝不带病挂下一只）。
        try:
            _order_id = f"{date}_{od['symbol']}_OPEN_1"
            _state_store.insert_order(
                _order_id, trade_id, account_id, date, od["symbol"], od["side"], "OPEN",
                float(od["qty"]), float(od["price"]), state="PENDING")
        except Exception as e:
            raise _CriticalHalt(
                f"pre_open insert_order(OPEN) 失败 symbol={od['symbol']}（DB 真相源失真）") from e
```

(b) **DB 幂等读异常（get_latest_action/has_order）→ L1**（engine.py:762-771），把：
```python
        try:
            if _state_store.get_latest_action(trade_id) == "VETOED":
                logger.info("pre_open 跳过 vetoed 标的 symbol=%s", od["symbol"])
                continue
            if _state_store.has_order(account_id, date, od["symbol"], "OPEN"):
                logger.info("pre_open 跳过已挂 OPEN（DB 幂等）symbol=%s", od["symbol"])
                continue
        except Exception:
            # DB 查询失败不阻断挂单（主路径是真实挂单，DB 是对账层，软降级）
            logger.exception("pre_open DB 幂等检查失败 symbol=%s（不阻断，可能重复挂）", od["symbol"])
```
改为：
```python
        # C-4 U3a：幂等读失败=「不知是否已挂过」→ 继续挂=可能重复挂（双倍成交，真金损失）。
        # 升 L1（spec §3 state_store 关键读失败 = L1）。宁可停整批不盲挂。
        try:
            if _state_store.get_latest_action(trade_id) == "VETOED":
                logger.info("pre_open 跳过 vetoed 标的 symbol=%s", od["symbol"])
                continue
            if _state_store.has_order(account_id, date, od["symbol"], "OPEN"):
                logger.info("pre_open 跳过已挂 OPEN（DB 幂等）symbol=%s", od["symbol"])
                continue
        except Exception as e:
            raise _CriticalHalt(
                f"pre_open DB 幂等读失败 symbol={od['symbol']}（敞口未明，拒继续挂）") from e
```

(c) **回填 SUBMITTED/ORDERED 写异常 → L1**（engine.py:800-810），把 `except Exception: logger.exception(...)` 改为：
```python
            except Exception as e:
                # C-4 U3a：柜台挂成功了但 DB 没回 SUBMITTED=对账以为没挂 → 幽灵单/重复挂。
                # 升 L1（单只层面 DB 写异常 > 单只）。
                raise _CriticalHalt(
                    f"pre_open 回填 SUBMITTED/ORDERED 失败 symbol={od['symbol']}（DB 真相源失真）") from e
```

(d) **account 行写异常 → L1**（engine.py:741-745），把 `except Exception: logger.exception(...)` 改为：
```python
        except Exception as e:
            # C-4 U3a：account 行写失败=后续 insert_order FK 全失败=DB 真故障，升 L1。
            raise _CriticalHalt(f"pre_open 确保 account 行失败 account={account_id}（DB 真故障）") from e
```

**保持不变（L3 软降级）：** 撤昨日单 except（:677-679，单笔已在 cancel_all 内吞）、抓熔断基线 except（:704-705，不影响挂单）、死态回填 except（:818-819/793-794，不影响正确性）。

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_pre_open_l1_halt.py -v`
Expected: PASS。

- [ ] **Step 5: 跑 pre_open 既有测试确认非业务路径零退化**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_pre_open_gate.py tests/trading/ -k "pre_open" -v`
Expected: 既有 PASS（gate skip / plan None / vetoed 等业务态路径不受影响）。

- [ ] **Step 6: Commit**

```bash
git add trading/engine.py tests/trading/test_pre_open_l1_halt.py
git commit -m "feat(c4-u3a): pre_open DB 读写异常升 L1（insert_order/幂等读/回填/account 硬抛 _CriticalHalt）"
```

---

## Task 4: stop_loss_monitor L1 路径改造（U3b · 查持仓/DB 异常 → raise _CriticalHalt）

**Files:**
- Modify: `trading/engine.py` 内 `stop_loss_monitor`（:838-1159）三个点
- Test: `tests/trading/test_stop_loss_l1_halt.py`（Create）

**Interfaces:**
- Consumes: `_CriticalHalt`（U2）。
- Produces: stop_loss_monitor 内查持仓/DB 写异常 → raise `_CriticalHalt` → `_stoploss`（被 guard）→ `_halt`。

**判定线：**
- 查持仓 `_fetch_broker_positions` 异常 = L1（敞口未明，spec §3）。
- `_record_stop` 内 insert_order(STOP)/insert_trade_event 异常 = L1（发了卖单但 DB 没记=幽灵单+重复发卖）。
- `_stop_already_placed` 幂等读异常 = L1（不知是否发过=可能重发=双倍卖）。
- `decide_exit` 异常 → D12 fallback（**保持 L2，不升 L1**，盘中不裸奔是已定设计）。
- 单只 `_submit` 卖出 RuntimeError = L2（U4 聚合 CRITICAL）。

- [ ] **Step 1: 写失败测试**

创建 `tests/trading/test_stop_loss_l1_halt.py`：
```python
# -*- coding: utf-8 -*-
"""U3b：stop_loss 查持仓失败 → raise _CriticalHalt（不再 return checked:0 软降级）。

物理意图：查持仓失败=敞口完全未明，原 return 软降级只是本轮跳过，下轮 30s 后继续盲跑。
升 L1：停调度，CRITICAL 唤醒人工（敞口未明继续跑=盲卖致命）。
"""
import pytest
from unittest.mock import patch, AsyncMock
from trading.engine import stop_loss_monitor, _CriticalHalt


@pytest.mark.asyncio
async def test_fetch_positions_failure_raises_critical_halt():
    """_fetch_broker_positions 抛异常 → raise _CriticalHalt。"""
    gw = AsyncMock()
    gw._fetch_broker_positions.side_effect = RuntimeError("柜台断线")
    with pytest.raises(_CriticalHalt, match="查持仓"):
        await stop_loss_monitor(
            stop_prices={"300214.SZ": 9.0}, gw=gw,
            monitor_ctx={"300214.SZ": {"state": {"stop": 9.0}, "cfg": {}}})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss_l1_halt.py -v`
Expected: FAIL（当前 `except: return {"checked":0, "reason":...}` 软降级）。

- [ ] **Step 3: 改 stop_loss_monitor 三个点**

(a) **查持仓异常 → L1**（engine.py:935-940），把：
```python
    try:
        positions = await gw._fetch_broker_positions()  # {symbol: {volume, ...}}（T7 扩展）
    except Exception:
        # 持仓查询失败绝不下卖出单（敞口未明即操作 = 盲卖，违反风控）
        logger.exception("stop_loss_monitor 查持仓失败（拒发任何卖出单）")
        return {"checked": 0, "reason": "查持仓异常，拒发卖出单"}
```
改为：
```python
    # C-4 U3b：查持仓失败=敞口完全未明——原 return 软降级只是本轮跳过，下轮 30s 继续盲跑。
    # 升 L1：停调度（spec §3 查持仓/DB 失败=L1），CRITICAL 唤醒人工。
    try:
        positions = await gw._fetch_broker_positions()
    except Exception as e:
        raise _CriticalHalt("stop_loss_monitor 查持仓失败（敞口未明，拒继续盲跑）") from e
```

(b) **`_stop_already_placed` 幂等读异常 → L1**（engine.py:904-910），把：
```python
    def _stop_already_placed(sym: str) -> bool:
        """查 DB 是否已挂 STOP 委托（幂等检查）。"""
        try:
            return _state_store.has_order(_aid, _today, sym, "STOP")
        except Exception:
            logger.exception("查 DB has_order(STOP) 失败 symbol=%s（回退非幂等，可能重发）", sym)
            return False
```
改为：
```python
    def _stop_already_placed(sym: str) -> bool:
        """查 DB 是否已挂 STOP 委托（幂等检查）。失败升 L1（不知是否发过=可能重发=双倍卖）。"""
        try:
            return _state_store.has_order(_aid, _today, sym, "STOP")
        except Exception as e:
            raise _CriticalHalt(
                f"stop_loss 查 has_order(STOP) 失败 symbol={sym}（幂等读失真，拒继续盲发）") from e
```

(c) **`_record_stop` 写异常 → L1**（engine.py:912-926），把：
```python
        except Exception:
            logger.exception("record_stop 落 DB 失败 symbol=%s（不阻断卖出）", sym)
```
改为：
```python
        except Exception as e:
            # C-4 U3b：卖单已发但 DB 没记=幽灵单+下轮重发=双倍卖。升 L1。
            raise _CriticalHalt(
                f"stop_loss record_stop 落 DB 失败 symbol={sym}（卖单已发，DB 真相源失真）") from e
```

**保持不变（L2/L3）：** decide_exit fallback（:993-1001）、单只 _submit RuntimeError（:1038-1042/:1082-1086，U4 聚合）、查可撤单（:1101-1104，L3 pending 巡检）、pending 撤单失败（:1151-1153，L3）。

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss_l1_halt.py -v`
Expected: PASS。

- [ ] **Step 5: 跑既有 stop_loss 测试确认零退化**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_stop_loss_monitor_decide_exit.py -v`
Expected: 既有 PASS（decide_exit 主路径 / D12 fallback 行为不变）。

- [ ] **Step 6: Commit**

```bash
git add trading/engine.py tests/trading/test_stop_loss_l1_halt.py
git commit -m "feat(c4-u3b): stop_loss 查持仓/DB 异常升 L1（敞口未明/幽灵单/双倍卖硬抛停调度）"
```

---

## Task 5: pipeline_then_eod 采集失败（U3c · raise _CriticalHalt）

**Files:**
- Modify: `trading/orchestrate/pipeline.py:64-71`（采集子进程 `proc.wait()` 后）
- Test: `tests/trading/test_pipeline_then_eod.py`（已存在，扩展 1 个用例）

**Interfaces:**
- Consumes: `_CriticalHalt`（从 `trading.engine` import）。
- Produces: 采集 `rc!=0` → `raise _CriticalHalt` → 经 engine `_pipeline_then_eod`（U2 包装）guard 捕获 → `_halt`。

- [ ] **Step 1: 写失败测试**

在 `tests/trading/test_pipeline_then_eod.py` 末尾追加：
```python
@pytest.mark.asyncio
async def test_pipeline_collect_failure_raises_critical_halt(monkeypatch):
    """采集子进程 rc!=0 → raise _CriticalHalt（T+1 计划失真，停调度）。"""
    import asyncio
    from trading.engine import _CriticalHalt
    from trading.orchestrate import pipeline as pl

    class _FakeProc:
        async def wait(self):
            return 1   # 采集失败
    async def _fake_exec(*a, **kw):
        return _FakeProc()
    monkeypatch.setattr(pl.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pl, "is_trading_day", lambda d: True)
    monkeypatch.setattr(pl, "resolve_active", lambda: [])
    monkeypatch.setattr(pl, "expected_latest_trade_day", lambda now: "2026-07-31")

    eng = object()   # 占位 engine（raise 在调 engine._eod 之前，不会被触达）
    with pytest.raises(_CriticalHalt, match="采集子进程失败"):
        await pl.pipeline_then_eod(eng)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_pipeline_then_eod.py::test_pipeline_collect_failure_raises_critical_halt -v`
Expected: FAIL（当前 rc!=0 无处理，直接走到 keys 解析）。

- [ ] **Step 3: 改 pipeline_then_eod**

`trading/orchestrate/pipeline.py`，在 `rc = await proc.wait()`（:69）后、`finally` 前，把：
```python
        rc = await proc.wait()
    finally:
        log_fh.close()
```
改为：
```python
        rc = await proc.wait()
    finally:
        log_fh.close()
    # C-4 U3c：采集子进程失败（rc!=0）= T 日增量未落湖 → 用 T-1 数据算 T+1 计划 = 时序 bug
    # （[[eod-date-offbyone-fix]] 同源风险）。升 L1：raise _CriticalHalt → engine _halt 停调度，
    # 绝不用陈旧数据产废信号（spec §3 pipeline 采集失败=L1）。
    if rc != 0:
        from trading.engine import _CriticalHalt
        raise _CriticalHalt(f"采集子进程失败 rc={rc}（T 日增量未落湖，拒产 T+1 计划）")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_pipeline_then_eod.py -v`
Expected: 全 PASS（含新用例 + 既有数据未就绪/正常路径用例）。

- [ ] **Step 5: Commit**

```bash
git add trading/orchestrate/pipeline.py tests/trading/test_pipeline_then_eod.py
git commit -m "feat(c4-u3c): pipeline 采集 rc!=0 升 L1（拒用陈旧数据产 T+1 计划，raise _CriticalHalt）"
```

---

## Task 6: L2 CRITICAL 聚合铺设（U4 · 单只失败聚合告警，防风暴）

**Files:**
- Modify: `trading/engine.py`：`pre_open`（单只挂单失败聚合）、`stop_loss_monitor`（单只止损发卖失败聚合）
- Test: `tests/trading/test_l2_aggregated_critical.py`（Create）

**Interfaces:**
- Consumes: `_alert_critical`（既有）。
- Produces: pre_open / stop_loss 单只业务失败（RuntimeError）聚合一条 CRITICAL，**不**停调度。

**Why 聚合而非逐只（防告警风暴，spec R3）：** N 只全拒时逐只 CRITICAL 会风暴；聚合「pre_open N/M 只挂单被拒」一条，研究员知情 + 不刷屏。整批 submitted=0 已有 CRITICAL（:828，保留）。

- [ ] **Step 1: 写失败测试**

创建 `tests/trading/test_l2_aggregated_critical.py`：
```python
# -*- coding: utf-8 -*-
"""U4：单只业务失败聚合 CRITICAL，不停调度（_halted 保持 False）。"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from trading.engine import TradingEngine


@pytest.mark.asyncio
async def test_pre_open_partial_reject_aggregates_critical_not_halt(monkeypatch):
    """部分挂单被拒 → 聚合一条 CRITICAL；_halted=False（L2 不停调度）。"""
    eng = TradingEngine()
    with patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.engine._cancel_all_open_orders", new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
         patch("trading.engine._load_expired_positions", return_value=[]), \
         patch("trading.engine._ACTIVE_ENGINE", eng), \
         patch("trading.engine._mode", return_value="live"), \
         patch("trading.engine._alert_critical") as ac, \
         patch("trading.engine._submit", new=AsyncMock(side_effect=[
             {"state": "SUBMITTED", "order_id": "seq1"},   # 第 1 只挂成
             RuntimeError("涨停拒单")])),                    # 第 2 只业务拒单 → 部分拒触发 L2 聚合
         patch("trading.engine._state_store") as ss, \
         patch("trading.engine.trading_plan") as tp:
        ss.get_account.return_value = MagicMock()
        ss.get_latest_action.return_value = None
        ss.has_order.return_value = False
        tp.load_plan.return_value = {"confirmed": True, "orders": [
            {"order": {"symbol": "300214.SZ", "qty": 100, "side": "buy", "price": 10.0}, "formed_at": None},
            {"order": {"symbol": "300215.SZ", "qty": 100, "side": "buy", "price": 10.0}, "formed_at": None}]}
        monkeypatch.setattr(eng, "_pre_open_gate", AsyncMock(return_value=(True, "")))
        from trading.engine import pre_open
        result = await pre_open("2026-07-31")
    assert result["submitted"] == 1   # 第 1 只挂成、第 2 只业务拒单（部分拒触发 L2 聚合）
    # 聚合 CRITICAL 含「被拒」语义，_halted 保持 False（L2 不停调度）
    assert any("被拒" in str(c) for c in ac.call_args_list)
    assert eng._halted is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_l2_aggregated_critical.py -v`
Expected: FAIL（当前单只失败只 `logger.warning`，未调 `_alert_critical` 聚合）。

- [ ] **Step 3: pre_open 加聚合 CRITICAL**

(a) 在挂单循环前初始化计数（engine.py:738 `n_expired = 0` 后）加：
```python
    n_rejected = 0   # C-4 U4：单只业务拒单计数（L2 聚合 CRITICAL 用）
```

(b) 单只 `_submit` RuntimeError 分支（engine.py:785-795），在 `continue` 前加 `n_rejected += 1`：
```python
        except Exception as exc:
            # 挡板命中（资金不足/涨跌停/不在白名单等）会 raise RuntimeError —— 单只 L2，不炸整批。
            logger.warning("pre_open 挂单失败 symbol=%s 原因=%s", od["symbol"], exc)
            n_rejected += 1   # C-4 U4：聚合 L2 CRITICAL 计数
            # C-1 final-review fix (I-2)：失败时把残留 PENDING 行标 REJECTED ...
            try:
                _state_store.update_order_state(_order_id, "REJECTED")
            except Exception:
                logger.exception("pre_open 失败回填 REJECTED 失败 symbol=%s", od["symbol"])
            continue
```
对「未成功（REJECTED/FAILED）」分支（:811-813）同样在 warning 后加 `n_rejected += 1`。

(c) 循环末尾（engine.py:821 `logger.info("pre_open 完成..."` 后、:828 submitted=0 CRITICAL 前）加聚合 CRITICAL：
```python
    # C-4 U4：部分拒单（L2）聚合一条 CRITICAL——单只研究员要知情，但整批继续不炸。
    # Why 聚合非逐只：防 N 只全拒告警风暴（spec R3）。整批 submitted=0 已有下方 CRITICAL（保留）。
    if n_rejected > 0 and _mode() == "live" and n_submitted > 0:
        _alert_critical(
            f"pre_open 部分挂单被拒 rejected={n_rejected}/{len(plan['orders'])} "
            f"submitted={n_submitted} date={date}（查挡板日志：涨跌停/资金/白名单）")
```

- [ ] **Step 4: stop_loss 加聚合 CRITICAL**

(a) 在计数初始化（engine.py:955 `n_pending_cancelled = 0` 后）加：
```python
    n_submit_failed = 0   # C-4 U4：单只止损发卖失败计数（L2 聚合）
```

(b) 两处单只 `_submit` RuntimeError 分支（:1038 主路径 / :1082 fallback），在 `logger.warning` 后加 `n_submit_failed += 1`（主路径在 `result = {"state":"FAILED"}` 前；fallback 在 `continue` 前）。

(c) 函数末尾 `logger.info("stop_loss_monitor 完成..."`（:1155）前加：
```python
    # C-4 U4：止损发卖失败聚合 L2 CRITICAL——漏止损真金损失，研究员须知情（但整批监控不停）。
    if n_submit_failed > 0 and _mode() == "live":
        _alert_critical(
            f"stop_loss 部分卖出失败 submit_failed={n_submit_failed} checked={n_checked}"
            f"（查 gw 挡板/lock_down 日志，漏止损须人工补单）")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_l2_aggregated_critical.py -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add trading/engine.py tests/trading/test_l2_aggregated_critical.py
git commit -m "feat(c4-u4): L2 单只失败聚合 CRITICAL（pre_open/stop_loss 部分拒单/发卖失败，防风暴）"
```

---

## Task 7: C-3 cancel 幂等审计收口（U5 · 补 account_id + docstring + 覆盖度 grep）

**Files:**
- Modify: `trading/engine.py:665`（pre_open 调 `_cancel_all_open_orders` 补 `account_id`）
- Modify: `trading/state_store.py`（顶部 docstring 补幂等键约定）
- Create: `docs/superpowers/audits/2026-07-31-cancel-idempotency-audit.md`
- Test: 扩展 `tests/trading/test_engine_pre_open_gate.py` 或新建 `tests/trading/test_cancel_all_account_id.py`

**审计结论（基于代码现状，U5 落地此结论）：**
- `cancel_all_open_orders`（breaker.py:48）柜台路径 `_cancel_via_broker_query`（:95）**已回写** `state_store.cancel_order_by_broker_oid_db(broker_oid)`（:122）→ order.state=CANCELLED，**但仅当 `account_id` 提供**（`if account_id:` :120）。
- pre_open 当前调 `_cancel_all_open_orders(gw)`（engine.py:665）**未传 account_id** → 柜台路径不回写 DB → 撤了昨日单但 DB 仍记 SUBMITTED → T+1 对账幽灵单。
- **结论：补传 `account_id=_resolve_account_id()` 激活既有回写路径；无需新增 `purpose='CANCEL'` 行**（spec §6.1 判据决策树：撤单落 DB → 免 CANCEL 行）。此为最小改动，符合极简 + 不重建 C-1。

- [ ] **Step 1: 写失败测试**

创建 `tests/trading/test_cancel_all_account_id.py`：
```python
# -*- coding: utf-8 -*-
"""U5：pre_open 撤昨日单补传 account_id → 激活柜台路径 CANCELLED 回写（消幽灵单）。"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from trading.engine import TradingEngine


@pytest.mark.asyncio
async def test_pre_open_cancel_passes_account_id(monkeypatch):
    """pre_open 调 _cancel_all_open_orders 时透传 account_id（激活 CANCELLED 回写）。"""
    eng = TradingEngine()
    captured = {}
    async def _spy_cancel(gw, account_id=None):
        captured["account_id"] = account_id
        return {"cancelled": 0, "unconfirmed": 0}
    with patch("trading.engine.get_gateway", return_value=AsyncMock()), \
         patch("trading.engine._cancel_all_open_orders", new=_spy_cancel), \
         patch("trading.engine._resolve_account_id", return_value="ACC_QMT_001"), \
         patch("trading.engine._load_expired_positions", return_value=[]), \
         patch("trading.engine._ACTIVE_ENGINE", eng), \
         patch("trading.engine._mode", return_value="live"), \
         patch("trading.engine.trading_plan") as tp:
        tp.load_plan.return_value = {"confirmed": True, "orders": []}  # 空 orders，只测撤单段
        monkeypatch.setattr(eng, "_pre_open_gate", AsyncMock(return_value=(True, "")))
        from trading.engine import pre_open
        await pre_open("2026-07-31")
    assert captured.get("account_id") == "ACC_QMT_001", \
        "pre_open 必须透传 account_id 激活柜台路径 CANCELLED 回写（消幽灵单）"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_cancel_all_account_id.py -v`
Expected: FAIL（`account_id` 为 None）。

- [ ] **Step 3: pre_open 补传 account_id**

engine.py:665，把：
```python
            _cancel_res = await _cancel_all_open_orders(gw)
```
改为：
```python
            # C-4 U5：补传 account_id 激活柜台路径 cancel_order_by_broker_oid_db 回写
            # order.state=CANCELLED（breaker._cancel_via_broker_query 在 account_id 提供时才回写）。
            # Why 必传：不传则撤了昨日单 DB 仍记 SUBMITTED → T+1 对账幽灵单（spec §6.1 判据）。
            # 此为 C-3 审计结论的最小修（无需 purpose='CANCEL' 行，既有回写路径已够）。
            _cancel_res = await _cancel_all_open_orders(gw, account_id=_resolve_account_id())
```

- [ ] **Step 4: state_store.py 顶部补幂等键约定 docstring**

在 `trading/state_store.py` 顶部模块 docstring 末尾追加一节（若顶部无 docstring 则在 `logger = ...` 前新增模块 docstring）：
```python
"""...（保留原 docstring）...

C-3 幂等键约定（2026-07-31 文档化，非重建）：
    交易状态写入的幂等唯一键统一约定为 ``(date, symbol, side, operation_type)``：
      - order 表：UNIQUE(account_id, trade_date, symbol, purpose) —— purpose 隐含 side+op
        （OPEN/STOP/TP1/TP2），side 显式进 UNIQUE 为冗余（follow-up，本期不做）。
      - trade_event 表：UNIQUE(account_id, trade_id, action) —— action=ORDERED/VETOED/
        STOP_TRIGGERED/...，trade_id=`{account_id}_{symbol}_{date}` 三元组锚定。
      - fill 表：UNIQUE(order_id, traded_time)。
    撤单幂等：cancel_all_open_orders 柜台路径（breaker._cancel_via_broker_query）在 account_id
    提供时调 cancel_order_by_broker_oid_db 把 order.state 回写 CANCELLED（对账一致），
    无需额外 purpose='CANCEL' 行（审计结论见 docs/superpowers/audits/2026-07-31-cancel-idempotency-audit.md）。
"""
```

- [ ] **Step 5: 跑测试确认通过**

Run: `./.venv310/Scripts/python.exe -m pytest tests/trading/test_cancel_all_account_id.py -v`
Expected: PASS。

- [ ] **Step 6: 写审计文档（含 UNIQUE 覆盖度 grep 结论）**

创建 `docs/superpowers/audits/2026-07-31-cancel-idempotency-audit.md`：
```markdown
# C-3 cancel 幂等审计结论

- **日期**：2026-07-31
- **结论**：pre_open 撤昨日单调 `_cancel_all_open_orders(gw, account_id=_resolve_account_id())`，
  激活柜台路径 `cancel_order_by_broker_oid_db` 把 order.state 回写 CANCELLED。
  **不新增 purpose='CANCEL' 行**（判据：撤单落 DB 即免 CANCEL 行）。

## 判据决策树（spec §6.1）
- 撤单确认是否落 DB？
  - 是（cancel_order_by_broker_oid_db 回写 CANCELLED，account_id 提供时）→ 免 CANCEL 行 ✓ 本结论
  - 否（account_id=None / 内存回退路径）→ 幽灵单风险，须补 CANCEL 行或 trade_event(action=CANCEL)

## UNIQUE 覆盖度 grep（执行命令 + 结论）
执行（实现时跑，贴输出）：
    grep -nE "UNIQUE\(" trading/state_store.py
预期命中：
  - order UNIQUE(account_id, trade_date, symbol, purpose)
  - trade_event UNIQUE(account_id, trade_id, action)
  - fill UNIQUE(order_id, traded_time)
  - account_daily UNIQUE(account_id, trade_date)
结论：所有交易写入路径（insert_order/insert_trade_event/insert_fill）均过 UNIQUE，无遗漏。
```

- [ ] **Step 7: 跑覆盖度 grep 确认无遗漏**

Run: `grep -nE "UNIQUE\(|def (insert_order|insert_trade_event|insert_fill|update_order_state)" trading/state_store.py`
Expected: 命中 3 处 UNIQUE + 4 处函数定义；把输出贴进审计文档。

- [ ] **Step 8: Commit**

```bash
git add trading/engine.py trading/state_store.py tests/trading/test_cancel_all_account_id.py docs/superpowers/audits/2026-07-31-cancel-idempotency-audit.md
git commit -m "fix(c4-u5): C-3 cancel 审计收口——pre_open 补 account_id 激活 CANCELLED 回写 + 幂等键 docstring"
```

---

## Task 8: 全量回归 + e2e 事件链 gate（U6）

**Files:**
- 无代码改动；仅验证。

- [ ] **Step 1: 全量回归**

Run: `./.venv310/Scripts/python.exe -m pytest tests/ -q`
Expected: 基线 1125 passed（spec §8）零退化；新增 U1-U5 测试全绿。若退化，定位到具体测试修复（不放宽断言）。

- [ ] **Step 2: e2e 事件链 gate（手工/脚本）**

确认 `sched.shutdown` 在 job 内自调用不抛（先例 engine.py:2795 已验证）：触发一次 `_halt`（如 mock `insert_order` 抛异常跑 pre_open），断言 `_halted=True` + 后续 `_stoploss`/`_post_close` job 入口即 skip（日志「引擎已停调度（_halted），跳过」）。

- [ ] **Step 3: spec §9 验收标准对照**

逐条核对（实现者填）：
1. ☐ job_defaults 三参数（U1）
2. ☐ 五 job 装 `@_critical_guard`（U2：四 method + `_pipeline_then_eod` 收编）
3. ☐ L1 路径全改 `raise _CriticalHalt`（U3a/U3b/U3c）
4. ☐ L2 路径调 `_alert_critical` 不停（U4 聚合）
5. ☐ `_halted` + `sched.shutdown` 双层生效（U2 单测 + 集成）
6. ☐ L3 软降级保留不动（U3a「保持不变」段显式列出）
7. ☐ C-3 cancel 审计有结论（U5：补 account_id，不新建 CANCEL 行）+ docstring 入 state_store
8. ☐ 全量回归零退化（U6 Step 1）

- [ ] **Step 4: 最终 Commit（若 Step 1/2 发现需补的测试或注释）**

```bash
git add -A
git commit -m "test(c4-u6): 全量回归 + e2e gate 通过（spec §9 验收 8 条全绿）"
```

---

## 风险提示（实现期风控官关注）

- **U3 最高风险**（spec §10）：逐路径改 except 形态时严守「只改异常形态，不动业务控制流」。单只挂单路径必须**分层 try**：内层 DB 写单独捕获→`raise _CriticalHalt`（L1）；外层 `_submit` 业务结果→被拒走聚合 `_alert_critical`（L2）。两层不能合并（合并则业务拒单误升 L1）。
- **`_stop_already_placed` 升 L1 的频率风险**：stop_loss 30s 高频，若 DB 偶发抖动会立即停整引擎。这是 spec 的明确取向（双倍卖致命 > 停调度代价），但实现后须在模拟盘观察 DB 抖动频率；若高频误停，follow-up 加「连续 N 次 DB 失败才升 L1」的退避（**本期不做，避免过度设计**）。
- **`sched.shutdown` 在 job 内自调用**：`wait=False` 先例（engine.py:2795）已验证可用；wrapper 内 `try/except` 兜 `SchedulerAlreadyRunning/ShuttingDown` 异常，`_halted` flag 已置兜底。
- **聚合 CRITICAL 的 live 触发条件**：U4 所有聚合告警均 `_mode() == "live"` 守卫，避免 dry_run/测试误告警。
