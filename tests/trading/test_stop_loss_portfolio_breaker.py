# -*- coding: utf-8 -*-
"""CR-3 盘中组合级熔断评估点前移单测（tech-debt CR-3 · 2026-08-15）。

物理定位（tech-debt-full-wave Task 8 · 设计三分支锁定）：
    「日内 -3% 组合熔断」原唯一判定点在 15:30 post_close（盘后闸）——盘中穿线后敞口
    要裸奔至收盘才停手。CR-3 把同一判定（同 ``get_start_equity`` 基线读口 + 同
    ``check_daily_loss_limit`` 纯判定）前移进 stop_loss_monitor 的 30s 巡检（⑤ pending
    撤单后、⑥ 聚合告警前），5min 节流评估。三分支语义（锁定）：
        ① 触发（真实回撤 ≤ -3%）：emergency_halt 粘滞锁 + cancel_all_open_orders +
           CRITICAL——**绝不 raise _CriticalHalt**（停调度会连带杀死止损监控自身，
           盘中绝对不可接受：熔断后仍需监控存活撤残留单/盯持仓）；
        ② 评估失败（query_asset 断线返 {}）：miss_streak 连续计数 ≥3 才推 CRITICAL
           （评估本身 5min 节流，不刷屏）；不停调度不 halt（断线场景 monitor 查持仓
           失败已有 L1 兜底，这里只补「熔断在岗性」观测）；
        ③ 基线缺失（start=None）：live → emergency_halt + CRITICAL（对齐 DG-G3「不选
           仅告警不动作」，但把 breaker 抛的 _CriticalHalt 转换成 halt 形态保监控
           存活）；dry_run → warning（影子态无真金敞口，不中断）。

测试边界（与 test_stop_loss_monitor_decide_exit 同纪律）：
    - 绝不真起 APScheduler、绝不真行情/真单：get_gateway/calendar/qmt_market_data/
      _submit/emergency_halt/cancel_all_open_orders/_alert_critical 全 patch；
    - **state_store 隔离到 tmp DB（autouse）**：CR-3 读 account_daily 基线，live 引擎
      运行中，读生产 logs/trading_state.db = 跨进程 sqlite 锁竞争 + 基线值不可控；
    - W1-A/T2 红线：patch 全部落「调用方模块属性」（trading.phases.stop_loss.X）——
      from…import 本地绑定，patch 真身模块属性不命中。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 被 Io C：monitor 真身（不经 engine re-export，patch 目标与调用方一致）
from trading.phases.stop_loss import stop_loss_monitor
from trading.alerting import PortfolioBreakerThrottle
from trading.ports import EnginePorts

SYM = "300001.SZ"


# ----------------------------------------------------------------------------
# autouse：隔离 state_store DB（CR-3 基线读口绝不触生产 logs/trading_state.db）
# ----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    """tmp DB + init_store：get_start_equity 读 tmp 空库天然返 None（可控），
    测试内再 monkeypatch 返回值构造「有基线/无基线」两态。"""
    from trading import state_store
    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "cr3_state.db"))
    state_store.init_store()


def _make_ports(*, interval: float = 300.0) -> EnginePorts:
    """造 EnginePorts：breaker_throttle 显式注入（interval 可调 0 提速多轮评估）。

    gate/whitelist_* 用 no-op 占位（CR-3 不触达 pre_open 路径）；blackout 用默认实例
    （本文件所有用例 quotes 均有效或非 live，blackout 分支不触发）。
    """
    return EnginePorts(
        gate=lambda d, gw: (True, ""),
        whitelist_add=lambda syms: None,
        whitelist_clear=lambda: None,
        breaker_throttle=PortfolioBreakerThrottle(interval=interval),
    )


def _make_gw(*, asset=None, asset_exc: Exception | None = None) -> MagicMock:
    """最小网关桩：_fetch_broker_positions 空 + query_asset 注入（断线返 {} / -4% 资产）。

    query_orders 返空 → ⑤ pending 巡检空转（cancel_on=99 远高于现价本来也不撤），
    保证跑到 CR-3 接入点前无其他副作用。
    """
    gw = MagicMock()
    gw._fetch_broker_positions = AsyncMock(return_value={})
    # 断线/锁定契约（broker/qmt.py query_asset）：返 {}（无 total_asset）；异常同语义
    gw.query_asset = (AsyncMock(side_effect=asset_exc) if asset_exc is not None
                      else AsyncMock(return_value=asset))
    gw.query_orders = AsyncMock(return_value=[])
    gw.cancel_order = AsyncMock(return_value=None)
    return gw


def _run(gw, ports, *, quotes=None):
    """跑一轮 stop_loss_monitor，patch 全外部副作用 + 固定盘中时段。

    pending_ctx={SYM: 99.0}：cancel_on 远高于现价 → ⑤ 空转，仅让主链路穿过「无配置
    早返」守卫到达 CR-3 接入点（⑤后⑥前）；无持仓 → ④ holding 循环空转零发单。
    """
    async def _fake_submit(order, *, confirm=True):
        return {"state": "FILLED"}

    with patch("trading.phases.stop_loss.get_gateway", return_value=gw), \
         patch("trading.phases.stop_loss.calendar") as cal, \
         patch("trading.phases.stop_loss.qmt_market_data") as qmd, \
         patch("trading.phases.stop_loss._submit", new=_fake_submit):
        cal.is_intraday_session.return_value = True
        cal.is_trading_day.return_value = True
        qmd.get_quotes = AsyncMock(return_value=(
            quotes if quotes is not None
            else {SYM: {"last_price": 10.0, "high": 10.2, "low": 9.8}}))
        return asyncio.run(stop_loss_monitor(
            pending_ctx={SYM: 99.0}, ports=ports))


def _patch_cr3_actions(monkeypatch, *, start_equity: float | None):
    """CR-3 副作用全拦截：emergency_halt / cancel_all / 双层 _alert_critical / 基线读口。

    返回 (halted_mock, cancelled_mock, alerts)。双层告警：phases.stop_loss._alert_critical
    （三分支动作告警）+ trading.compute.breaker._alert_critical（breaker fail-closed
    副用，基线缺失路径自身会推一条——一并拦截防真发钉钉 + 防断言串扰）。
    """
    from trading import state_store
    # 账号口径钉死（隔离 .env QMT_ACCOUNT_ID 漂移；基线读口与 post_close.py:282 同口）
    monkeypatch.setattr("trading.phases.stop_loss._resolve_account_id",
                        lambda: "CR3TEST")
    monkeypatch.setattr(state_store, "get_start_equity",
                        lambda aid, d, **kw: start_equity)
    halted = MagicMock()
    monkeypatch.setattr("trading.phases.stop_loss.emergency_halt", halted)
    cancelled = AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})
    monkeypatch.setattr("trading.phases.stop_loss.cancel_all_open_orders", cancelled)
    alerts: list[str] = []
    monkeypatch.setattr("trading.phases.stop_loss._alert_critical",
                        lambda msg: alerts.append(msg))
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    return halted, cancelled, alerts


# ============================================================================
# 测试 ①：节流——同 5min 窗内第二轮 should_check 返 False（纯状态机单测）
# ============================================================================
def test_breaker_throttle_5min_window():
    """5min 评估节流：首轮必过（last_check_ts=0.0 初值），窗内 False，恰满窗再过。

    物理意图：30s 巡检 × query_asset（柜台同步 C++ 查询投线程池）每轮都评估既打柜台
    又在触发/失明场景刷屏；should_check 在单一 Lock 内 check+mark 原子——并发下至多
    一条线程拿到 True（与 QuoteBlackoutThrottle.fire_if_due 同范式）。
    """
    t = PortfolioBreakerThrottle()
    assert t.interval == 300.0          # CR-3 设计锁定：5min
    assert t.miss_streak == 0           # 初值：无历史失败
    base = 100_000.0                    # time.monotonic 域任意基准
    assert t.should_check(base) is True          # 首轮（now - 0 >= 300）
    assert t.should_check(base + 299.9) is False  # 同窗第二轮 False（已 mark）
    assert t.should_check(base + 300.0) is True   # 恰满 5min 再触发（>= 含边界）


# ============================================================================
# 测试 ②：tripped 三件套——start=100万 curr=95万（-5%）→ halt 套装 + 不抛 + 存活
# ============================================================================
def test_tripped_fires_halt_suite_and_monitor_survives(monkeypatch):
    """-5% 回撤 → emergency_halt + cancel_all + CRITICAL，且 monitor 正常返回不抛。

    物理意图（CR-3 核心）：盘中穿线即时收口敞口——撤所有未终态单 + 置网关粘滞锁拒
    新单；**绝不 raise _CriticalHalt**（停调度=杀死止损监控自身，熔断后持仓仍需监控
    存活兜底，盘中不可接受）。「不抛」由 _run 正常返回 + result 结构完整共同断言。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    halted, cancelled, alerts = _patch_cr3_actions(monkeypatch, start_equity=1_000_000.0)
    gw = _make_gw(asset={"total_asset": 950_000.0})   # -5% < -3% → 触发
    result = _run(gw, _make_ports())
    halted.assert_called_once()                       # 三件套①：emergency_halt
    cancelled.assert_called_once_with(gw)             # 三件套②：撤全部未终态单
    assert len(alerts) == 1 and "回撤" in alerts[0]   # 三件套③：CRITICAL（恰一条）
    assert result["stop_triggered"] == 0              # monitor 存活正常返回（无持仓无卖单）


# ============================================================================
# 测试 ③：curr 缺失 ×3 → _alert_critical 恰一次（节流）+ 不 halt
# ============================================================================
def test_curr_missing_3x_alerts_once_no_halt(monkeypatch):
    """query_asset 断线返 {} 连续 3 轮 → 第 3 轮才推一条 CRITICAL，不 halt 不停调度。

    物理意图（分支②）：评估失败恰是断线场景——monitor 查持仓失败已有 L1 兜底，这里
    只补「熔断在岗性」观测：单次抖动（streak 1/2）不叫醒人工，持续失明 ≥3 轮才升级；
    绝不 halt（断线自愈后熔断评估须自动恢复在岗，halt 会把影子观测一起锁死）。
    interval=0 提速：取消 5min 节流让三轮巡检连续评估（节流语义另由测试①守护）。
    """
    halted, cancelled, alerts = _patch_cr3_actions(monkeypatch, start_equity=1_000_000.0)
    gw = _make_gw(asset={})                            # 断线/锁定契约：返空 dict
    ports = _make_ports(interval=0.0)
    for _ in range(3):
        result = _run(gw, ports)                       # 每轮正常返回（不停调度）
        assert result["checked"] == 0
    halted.assert_not_called()                         # 评估失败只观测不动作
    cancelled.assert_not_called()
    assert len(alerts) == 1                            # 恰一次：streak 1/2 静默，3 才推
    assert "熔断评估" in alerts[0] or "取不到" in alerts[0]


# ============================================================================
# 测试 ④：基线 None + live → breaker _CriticalHalt 被转换成 emergency_halt + CRITICAL
# ============================================================================
def test_baseline_missing_live_converts_halt_to_emergency_halt(monkeypatch):
    """start=None + live → emergency_halt + CRITICAL，_CriticalHalt 不逸出（转换形态）。

    物理意图（分支③ live）：对齐 DG-G3「不选仅告警不动作」，但 breaker 在 live 抛的
    _CriticalHalt 若直传 engine._critical_guard 会停调度——停调度=杀死止损监控自身，
    盘中不可接受。故 catch 转换：粘滞锁拒新单（halt 形态转换）+ CRITICAL 人工介入，
    监控继续巡检。分支语义锁定：基线缺失只 halt 拒新单，不撤单（无敞口判定依据）。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    halted, cancelled, alerts = _patch_cr3_actions(monkeypatch, start_equity=None)
    gw = _make_gw(asset={"total_asset": 950_000.0})
    result = _run(gw, _make_ports())                   # 不抛 _CriticalHalt（核心断言）
    halted.assert_called_once()
    cancelled.assert_not_called()                      # 基线缺失分支不撤单（三分支互斥）
    assert len(alerts) == 1 and "基线缺失" in alerts[0]
    assert result["checked"] == 0                      # monitor 存活正常返回


# ============================================================================
# 测试 ⑤：dry_run gw=None → 全链 no-op（评估点从未触达）
# ============================================================================
def test_dry_run_gw_none_full_noop(monkeypatch):
    """dry_run 网关未装配 → monitor :216-220 守卫早返，CR-3 评估点从未触达。

    物理意图：dry_run 无网关无持仓无真金敞口，评估点前的一切守卫原样兜底（沿用
    :216-220 no-op），throttle 状态零污染（last_check_ts 保持 0.0 = 未评估过）。
    """
    halted, cancelled, alerts = _patch_cr3_actions(monkeypatch, start_equity=1_000_000.0)
    ports = _make_ports()
    with patch("trading.phases.stop_loss.get_gateway", return_value=None), \
         patch("trading.phases.stop_loss.calendar") as cal, \
         patch("trading.phases.stop_loss.qmt_market_data") as qmd:
        cal.is_intraday_session.return_value = True
        qmd.get_quotes = AsyncMock(return_value={})
        result = asyncio.run(stop_loss_monitor(
            pending_ctx={SYM: 99.0}, ports=ports))
    assert result["checked"] == 0
    assert "网关" in result.get("reason", "")
    # 评估点从未触达：节流状态零污染（should_check 未被调，last_check_ts 仍初值）
    assert ports.breaker_throttle.last_check_ts == 0.0
    halted.assert_not_called()
    cancelled.assert_not_called()
    assert alerts == []
