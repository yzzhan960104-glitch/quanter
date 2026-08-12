# -*- coding: utf-8 -*-
"""trading_service 四态心跳 + 熔断幂等 + Phase 1 下单/连接/流水测试。

锁定契约：
1) status 四态严格镜像网关：unavailable / disconnected / live / vetoed_by_risk
2) emergency_halt 幂等：lock_down 已置位时重复调用不重复撤单
3) 网关未装配时 raise RuntimeError（路由层转 503）
4) Phase 1：submit_order dry_run/挡板/真单三分支 + 流水全覆盖
"""
import asyncio

import pytest


def test_status_unavailable_when_no_gateway(monkeypatch):
    """无网关单例（缺 QMT 凭证）→ mode='unavailable'。"""
    from trading import gateway_service as trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    s = trading_service.get_status()
    assert s == {"connected": False, "locked": False, "mode": "unavailable"}


def test_status_disconnected_when_gateway_not_connected(monkeypatch):
    """网关存在但未 connect → mode='disconnected'。"""
    from trading import gateway_service as trading_service
    gw = type("G", (), {"_connected": False, "is_locked": False})()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    s = trading_service.get_status()
    assert s["mode"] == "disconnected" and s["connected"] is False


def test_status_live_when_connected(monkeypatch):
    """已连接且未锁定 → mode='live'。"""
    from trading import gateway_service as trading_service
    gw = type("G", (), {"_connected": True, "is_locked": False})()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    assert trading_service.get_status()["mode"] == "live"


def test_status_vetoed_when_locked(monkeypatch):
    """断线锁定 → mode='vetoed_by_risk'（锁定优先于 connected）。"""
    from trading import gateway_service as trading_service
    gw = type("G", (), {"_connected": True, "is_locked": True})()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    assert trading_service.get_status()["mode"] == "vetoed_by_risk"


def test_emergency_halt_idempotent(monkeypatch):
    """连续两次 emergency_halt：第一次置 lock_down，第二次返'已处于'（不重复撤单）。"""
    from trading import gateway_service as trading_service

    class FakeGW:
        def __init__(self):
            self._lock_down = False
            self._connected = True
            self._orders = {"1": {"state": "SUBMITTED"}, "2": {"state": "FILLED"}}

        @property
        def is_locked(self):
            return self._lock_down

    # 屏蔽告警副作用：mock 消费掉未 await 的 coroutine，避免 RuntimeWarning
    def _swallow_fire_and_forget(coro=None, *a, **kw):
        if coro is not None and hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(trading_service, "fire_and_forget", _swallow_fire_and_forget)

    gw = FakeGW()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)

    r1 = trading_service.emergency_halt()
    assert r1["halted"] is True and gw._lock_down is True

    r2 = trading_service.emergency_halt()
    assert r2["halted"] is True
    assert "已处于" in r2["message"]


def test_emergency_halt_unavailable(monkeypatch):
    """无网关 → raise RuntimeError（路由层转 503）。"""
    from trading import gateway_service as trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    with pytest.raises(RuntimeError):
        trading_service.emergency_halt()


# ============ Phase 1 Task 5：submit_order / connect / 流水 ============
def _fake_gw_connected():
    """造一个已连接、未锁定的假网关，记录 submit_order 调用。"""
    class _FakeGW:
        def __init__(self):
            self._connected = True
            self._lock_down = False
            self._orders = {}
            self.submit_calls = []
            self.connect_called = False

        @property
        def is_locked(self):
            return self._lock_down

        async def connect(self):
            self.connect_called = True
            self._connected = True
            self._lock_down = False

        async def disconnect(self):
            self._connected = False

        async def submit_order(self, order):
            self.submit_calls.append(order)
            from broker.base import OrderResult  # Layer2 阶段6 follow-up #4b：垫片已删，直指 broker.base 真身
            from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身
            return OrderResult(order_id="100", state=OrderState.SUBMITTED, message="ok")

        async def cancel_order(self, order_id):
            from broker.base import OrderResult  # Layer2 阶段6 follow-up #4b：垫片已删，直指 broker.base 真身
            from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身
            return OrderResult(order_id=order_id, state=OrderState.CANCELLED, message="ok")
    return _FakeGW()


def test_connect_gateway(monkeypatch):
    from trading import gateway_service as trading_service
    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    asyncio.run(trading_service.connect_gateway())
    assert gw.connect_called is True


def test_connect_gateway_unavailable(monkeypatch):
    from trading import gateway_service as trading_service
    monkeypatch.setattr(trading_service, "get_gateway", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(trading_service.connect_gateway())


def test_submit_order_dry_run_records_and_returns(tmp_db, monkeypatch):
    """dry_run=True → 不调网关下单，落 DRY_RUN trade_event 事件，返 state=DRY_RUN。

    SSoT Phase A · Task A1：审计从 CSV（record_live_trade）平移到 trade_event 表。
    本测试断言真相源（trade_event DRY_RUN 行），不再依赖 CSV 旁证。
    不 patch get_quote：conftest 假 xtdata.get_full_tick 返 {} → get_quote 返 None，
    挡板跳过涨跌停关（dry_run 在第 2 关即命中，根本到不了第 9 关）。
    """
    from trading import gateway_service as trading_service
    from trading.compute.types import OrderRequest
    import sqlite3

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    monkeypatch.setattr(trading_service, "_resolve_account_id", lambda: "ACC_TEST")

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    r = asyncio.run(trading_service.submit_order(order, dry_run=True))
    assert r["state"] == "DRY_RUN"
    assert gw.submit_calls == []  # 未真下单
    # 真相源断言：trade_event 落 DRY_RUN 行
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    ev = con.execute("SELECT * FROM trade_event WHERE action='DRY_RUN'").fetchone()
    assert ev is not None, "dry_run 未落 trade_event(DRY_RUN) 事件"
    assert ev["symbol"] == "510300.SH"
    con.close()


def test_submit_order_dry_run_meta_kind_is_submit(tmp_db, monkeypatch):
    """DRY_RUN 审计事件 meta_kind=submit（非 fill，防误标成交）。

    物理意图（Fix 3a，用户 final review 抓出）：
        _write_submit_trade_event 默认 ``meta_kind="fill"``，DRY_RUN 调用未显式传
        meta_kind → meta 落 ``kind=fill``。但 DRY_RUN 是**模拟挡板命中**，非真实成交，
        post_close 聚合层按 ``kind=fill`` 闸识别真实成交——若 DRY_RUN 误标 fill，会被
        计入净持仓口径，污染审计真相源。BLOCKED/ORDERED 都显式传 ``meta_kind="submit"``，
        DRY_RUN 与两者语义同源（下单审计），应保持一致。
    """
    from trading import gateway_service as trading_service
    from trading.compute.types import OrderRequest
    import sqlite3

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    monkeypatch.setattr(trading_service, "_resolve_account_id", lambda: "ACC_TEST")

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    asyncio.run(trading_service.submit_order(order, dry_run=True))
    # 断言 trade_event DRY_RUN 行的 meta 含 kind=submit（旧代码默认 fill → FAIL）
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    ev = con.execute("SELECT * FROM trade_event WHERE action='DRY_RUN'").fetchone()
    assert ev is not None, "dry_run 未落 trade_event(DRY_RUN) 事件"
    assert "kind=submit" in (ev["meta"] or ""), (
        f"DRY_RUN meta_kind 应为 submit（非 fill），实际 meta={ev['meta']!r}")
    con.close()


def test_submit_order_blocked_raises(tmp_db, monkeypatch):
    """挡板命中（session 关，A-2 后三闸之一）→ raise RuntimeError + 落 BLOCKED 事件。

    SSoT Phase A · Task A1 平移后：BLOCKED 审计走 trade_event 表真相源（UNIQUE 幂等），
    不再依赖 CSV record_live_trade。
    """
    from trading import gateway_service as trading_service
    from trading.compute.types import OrderRequest
    import sqlite3

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    monkeypatch.setattr(trading_service, "_resolve_account_id", lambda: "ACC_TEST")
    monkeypatch.setattr(trading_service, "_in_a_share_session", lambda now=None: False)

    order = OrderRequest(symbol="000001.SZ", qty=100, side="buy", price=5.0)
    with pytest.raises(RuntimeError):
        asyncio.run(trading_service.submit_order(order, dry_run=False))
    # 真相源断言：trade_event 落 BLOCKED 行 + meta 含 session 拒因
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    hit = con.execute("SELECT * FROM trade_event WHERE action='BLOCKED'").fetchone()
    assert hit is not None, "挡板命中未落 trade_event(BLOCKED) 事件"
    assert hit["symbol"] == "000001.SZ"
    assert "session" in (hit["meta"] or "").lower()  # meta 含 session 拒因
    con.close()


def test_submit_order_live_calls_gateway(tmp_db, monkeypatch):
    """dry_run=False + 全过 → 调网关 submit_order。

    SSoT Phase A · Task A1：审计走 trade_event 表，本用例聚焦「网关调用」断言，
    trade_event 落盘由专门用例覆盖。
    """
    from trading import gateway_service as trading_service
    from trading.compute.types import OrderRequest

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    monkeypatch.setattr(trading_service, "_resolve_account_id", lambda: "ACC_TEST")
    monkeypatch.setattr(trading_service, "_enforce_session", lambda: False, raising=False)

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    r = asyncio.run(trading_service.submit_order(order, dry_run=False))
    assert r["order_id"] == "100"
    assert gw.submit_calls and gw.submit_calls[0].symbol == "510300.SH"


def test_submit_order_live_records_audit(tmp_db, monkeypatch):
    """真单成功路径必须落 trade_event 审计事件（spec §6.3 可追溯性，B-6/应修项1）。

    背景：submit_order docstring 声称「真单/废单/撤单均落审计」，但此前的真单成功
    路径拿到 OrderResult 后直接 return，未落任何审计——真实成交在审计流水中完全缺失，
    违反量化交易审计合规红线。SSoT Phase A · Task A1 平移后：审计走 trade_event 表
    真相源（UNIQUE 幂等），不再依赖 CSV record_live_trade。
    """
    from trading import gateway_service as trading_service
    from trading.compute.types import OrderRequest
    import sqlite3

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    monkeypatch.setattr(trading_service, "_resolve_account_id", lambda: "ACC_TEST")
    monkeypatch.setattr(trading_service, "_enforce_session", lambda: False, raising=False)

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    asyncio.run(trading_service.submit_order(order, dry_run=False))

    # 真相源断言：真单成功必须落 trade_event(ORDERED) 事件（断点-1 双写幂等设计：
    # engine pre_open 与 server-manual 共用 action=ORDERED，UNIQUE 自然跳过双写；
    # 真实 OrderState=SUBMITTED 通过 meta 携带，事后复盘不丢信息）
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    ev = con.execute(
        "SELECT * FROM trade_event WHERE symbol='510300.SH' AND action='ORDERED'").fetchone()
    assert ev is not None, "真单成功未落 trade_event(ORDERED) 审计事件（B-6）"
    # meta 应含网关类名 + 真实 state（便于事后复盘）
    meta = ev["meta"] or ""
    assert "SUBMITTED" in meta
    # qty/price 落盘正确
    assert ev["qty"] == 100
    assert ev["price"] == 5.0
    con.close()


def test_submit_order_disconnected_blocks(tmp_db, monkeypatch):
    """网关未连接 → 挡板 connection 关命中。

    SSoT Phase A · Task A1：审计走 trade_event 表，connection 关 BLOCKED 也落事件。
    """
    from trading import gateway_service as trading_service
    from trading.compute.types import OrderRequest
    import sqlite3

    gw = _fake_gw_connected()
    gw._connected = False
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    monkeypatch.setattr(trading_service, "_resolve_account_id", lambda: "ACC_TEST")

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    with pytest.raises(RuntimeError, match="连接"):
        asyncio.run(trading_service.submit_order(order, dry_run=False))
    # 真相源断言：connection 关 BLOCKED 落事件
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    ev = con.execute(
        "SELECT * FROM trade_event WHERE action='BLOCKED' AND symbol='510300.SH'").fetchone()
    assert ev is not None, "connection 关 BLOCKED 未落 trade_event"
    con.close()


def test_submit_order_blocked_writes_trade_event(tmp_db, monkeypatch):
    """SSoT Phase A · Task A1 新增：挡板命中（白名单外）→ trade_event(BLOCKED) 落库。

    断点-1：submit_order 审计平移 trade_event 的核心断言。独立于既有 test_submit_order_blocked_raises
    （后者断言 RuntimeError 抛出 + symbol/whitelist meta），本测试聚焦真相源行落库 +
    action/symbol/qty/price 字段完整性，确保归因/复盘消费端切 DB 时字段不缺。
    """
    from trading import gateway_service as trading_service
    from trading.compute.types import OrderRequest
    import sqlite3

    # 单点 account_id 注入（与 engine._resolve_account_id 同口径的 server 侧实现）
    monkeypatch.setattr(trading_service, "_resolve_account_id", lambda: "ACC_TEST")
    # session 关拦截（A-2 三闸之一）
    monkeypatch.setattr(trading_service, "_in_a_share_session", lambda now=None: False)
    monkeypatch.setattr(trading_service, "get_gateway", lambda: _fake_gw_connected())

    order = OrderRequest(symbol="600001.SH", qty=100, side="buy", price=10.0)
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        asyncio.run(trading_service.submit_order(order, dry_run=False))

    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    hit = con.execute("SELECT symbol, meta, qty, price FROM trade_event WHERE action='BLOCKED'").fetchone()
    assert hit is not None
    assert hit["symbol"] == "600001.SH"
    assert "session" in (hit["meta"] or "").lower()  # meta 含 session 拒因
    assert hit["qty"] == 100.0
    assert hit["price"] == 10.0
    con.close()


def test_emergency_halt_sets_risk_halt(monkeypatch):
    """emergency_halt → set_risk_halt(True)（#6：风控熔断粘滞标志）。"""
    from trading import gateway_service as trading_service
    from broker.qmt import QmtExecutionGateway

    gw = QmtExecutionGateway(userdata_path="C:/tmp/qmt_test", account_id="TEST_ACC")
    monkeypatch.setattr(trading_service, "get_gateway", lambda: gw)
    def _swallow(coro=None, *a, **kw):
        if coro is not None and hasattr(coro, "close"):
            coro.close()
    monkeypatch.setattr(trading_service, "fire_and_forget", _swallow)

    r = trading_service.emergency_halt()
    assert r["halted"] is True
    assert gw._risk_halted is True, "emergency_halt 必须置 risk_halted"
    assert gw._lock_down is True and gw._connected is False


# ============ Task 9：get_jobs（聚合台账 + catchup 四态） ============
# 设计意图：catchup 状态解析走 duck typing（仅依赖 done/exception/result），
# 测试用 _FakeTask 注入，无需真起 asyncio 事件循环——保持单测极速且无副作用。
class _FakeTask:
    """模拟 asyncio.Task 的最小鸭子类型（仅实现 _resolve_catchup_state 探测的三方法）。

    done/has_exc/result_obj 三参数覆盖四态：
    - not_started：测试侧直接传 catchup_task=None（不走 _FakeTask）
    - running：done=False
    - done：done=True, has_exc=False, result_obj=<dict>
    - failed：done=True, has_exc=True, result_obj=<Exception 实例>
    """

    def __init__(self, *, done: bool, has_exc: bool = False, result_obj=None):
        self._done = done
        self._has_exc = has_exc
        self._result_obj = result_obj

    def done(self):
        return self._done

    def exception(self):
        if not self._done:
            raise RuntimeError("result is not ready")  # 与真 asyncio.Task 同语义
        return self._result_obj if self._has_exc else None

    def result(self):
        if not self._done:
            raise RuntimeError("result is not ready")  # 与真 asyncio.Task 同语义
        if self._has_exc:
            raise self._result_obj  # 失败态取 result 即重抛异常（asyncio.Task 同语义）
        return self._result_obj


def test_get_jobs_catchup_not_started(monkeypatch):
    """catchup_task=None → catchup.state='not_started'，jobs 取台账快照。"""
    from trading import gateway_service as trading_service
    import trading.job_ledger as job_ledger

    fake_jobs = [{"name": "pipeline", "status": "done"}]
    monkeypatch.setattr(job_ledger, "snapshot_for_date", lambda d: fake_jobs)

    out = trading_service.get_jobs("2026-08-02", engine=None, catchup_task=None)
    assert out["date"] == "2026-08-02"
    assert out["jobs"] == fake_jobs
    assert out["catchup"] == {"state": "not_started", "result": None}
    assert "warning" not in out


def test_get_jobs_catchup_running(monkeypatch):
    """catchup_task 未 done → catchup.state='running'。"""
    from trading import gateway_service as trading_service
    import trading.job_ledger as job_ledger

    monkeypatch.setattr(job_ledger, "snapshot_for_date", lambda d: [])
    task = _FakeTask(done=False)

    out = trading_service.get_jobs("2026-08-02", engine=None, catchup_task=task)
    assert out["catchup"] == {"state": "running", "result": None}


def test_get_jobs_catchup_done(monkeypatch):
    """catchup_task done 且无异常 → catchup.state='done'，result 透传 run_startup_catchup 返回 dict。"""
    from trading import gateway_service as trading_service
    import trading.job_ledger as job_ledger

    monkeypatch.setattr(job_ledger, "snapshot_for_date", lambda d: [])
    # 真实 run_startup_catchup 返回结构（spec §C-8）
    expected_result = {
        "pipeline": True,
        "brief": False,
        "pre_open": False,
        "pre_open_note": "",
        "error": None,
    }
    task = _FakeTask(done=True, has_exc=False, result_obj=expected_result)

    out = trading_service.get_jobs("2026-08-02", engine=None, catchup_task=task)
    assert out["catchup"]["state"] == "done"
    assert out["catchup"]["result"] == expected_result


def test_get_jobs_catchup_failed(monkeypatch):
    """catchup_task done 且抛异常 → catchup.state='failed'，result={'error': <str(exc)>}。"""
    from trading import gateway_service as trading_service
    import trading.job_ledger as job_ledger

    monkeypatch.setattr(job_ledger, "snapshot_for_date", lambda d: [])
    boom = RuntimeError("启动补跑崩了")
    task = _FakeTask(done=True, has_exc=True, result_obj=boom)

    out = trading_service.get_jobs("2026-08-02", engine=None, catchup_task=task)
    assert out["catchup"]["state"] == "failed"
    assert out["catchup"]["result"] == {"error": "启动补跑崩了"}


def test_get_jobs_ledger_read_failure_warns(monkeypatch):
    """台账读失败（snapshot_for_date 抛 Exception）→ jobs=[] + warning，不向上抛。

    Why：台账是操作元数据，绝不阻断观测主路径（spec §5.1）——
    即便 SQLite 被锁/文件损坏，GET /trading/jobs 仍要返回 catchup 状态让前端可见。
    """
    from trading import gateway_service as trading_service
    import trading.job_ledger as job_ledger

    monkeypatch.setattr(
        job_ledger, "snapshot_for_date",
        lambda d: (_ for _ in ()).throw(RuntimeError("db locked")),
    )

    out = trading_service.get_jobs("2026-08-02", engine=None, catchup_task=None)
    assert out["jobs"] == []
    assert "warning" in out
    assert "db locked" in out["warning"]
    # catchup 探测不受台账读失败影响
    assert out["catchup"] == {"state": "not_started", "result": None}
