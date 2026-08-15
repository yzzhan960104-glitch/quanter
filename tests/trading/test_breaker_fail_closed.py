# -*- coding: utf-8 -*-
"""熔断基线缺失 fail-closed 单测（DG-G3 · 2026-08-13）。

物理定位（spec G3 · audit-remediation-design §G3）：
    ``trading/compute/breaker.py:check_daily_loss_limit`` 在基线权益缺失
    （start_equity<=0 或 None）时，**原 fail-open 语义 ``return False``** 会让
    「account_daily 漏采 + T-1 close 也缺」的极端情形静默放行（日内 -3% 熔断失效）。
    DG-G3 裁决：基线链全部缺失 → 不再放行，改为**触发保护**——
        - dry_run（模拟盘）：返 True（C-1 当日停手）+ CRITICAL 告警；
        - live（实盘）：raise _CriticalHalt（engine._critical_guard 捕获 → _halt L1 停调度）。

    不选「仅告警不动作」（那是 P0-2 治之前的纯静默，G3 收 fail-open 语义尾巴）。

行为等价红线（spec G3）：
    有基线路径（start_equity>0）判定逻辑**零变更**——本测试只覆盖基线缺失的新分支，
    回归由既有 ``test_circuit_breaker.py`` 守护（已同步更新为 fail-closed 语义）。

Why monkeypatch ``trading.compute.breaker._mode`` / ``_alert_critical``：
    breaker 在 fail-closed 分支内调 ``_mode()`` / ``_alert_critical()``（DG-G3 副用收口）。
    单测不发真钉钉、用 monkeypatch 拦截 + 断言调用次数。 ``_mode`` 经模块全局名解析，
    patch 模块属性即命中（与 pre_open/post_close 既有的 ``monkeypatch critical._mode`` 同范式，
    但 breaker 模块是副用入口，patch ``trading.compute.breaker._mode`` 才命中本地引用）。
"""
from __future__ import annotations

import pytest

# breaker 真身（functional core · DG-G3 后副用收口，但 import 路径不变）
from trading.compute.breaker import check_daily_loss_limit
# _CriticalHalt 按类身份 catch（L1 致命异常 · engine._critical_guard 捕获停调度）
from trading.critical import _CriticalHalt


# ============================================================================
# 实盘模式：基线缺失 → raise _CriticalHalt（L1 停调度 · DG-G3 裁决）
# ============================================================================

def test_live_baseline_zero_raises_halt(monkeypatch):
    """live + start_equity=0 → raise _CriticalHalt（实盘基线链全失，拒继续下注）。

    物理意图（DG-G3）：live 模式基线缺失 = 日内 -3% 熔断失效 = 真金敞口失控红线，
    必须停调度等人工介入（与 pre_open DB 写失败同 _CriticalHalt 语义）。
    """
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "live")
    # 副用 _alert_critical 不真发钉钉（单测不发真告警，仅断言被调）
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    with pytest.raises(_CriticalHalt):
        check_daily_loss_limit(0, 965_000, limit=-0.03)


def test_live_baseline_none_raises_halt(monkeypatch):
    """live + start_equity=None → raise _CriticalHalt（None 比 0 更明确表「未抓到基线」）。

    物理意图：post_close T-1 兜底也取不到时，会把 None 透传给 breaker（不再 float() 强转
    触发 TypeError），breaker 必须 fail-closed 处理 None（与 0 同语义）。
    """
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "live")
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    with pytest.raises(_CriticalHalt):
        check_daily_loss_limit(None, 965_000, limit=-0.03)  # type: ignore[arg-type]


def test_live_baseline_negative_raises_halt(monkeypatch):
    """live + start_equity<0（脏数据/对账反算负值）→ raise _CriticalHalt（同 0/None fail-closed）。"""
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "live")
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    with pytest.raises(_CriticalHalt):
        check_daily_loss_limit(-100, 965_000, limit=-0.03)


# ============================================================================
# 模拟盘：基线缺失 → 返 True 停手 + CRITICAL 告警（DG-G3 裁决：不抛 halt 进程）
# ============================================================================

def test_dry_run_baseline_zero_stops_and_alerts(monkeypatch):
    """dry_run + start_equity=0 → 返 True（C-1 当日停手）+ 告警被调（不抛 halt）。

    物理意图（DG-G3 裁决「模拟盘=停手+CRITICAL 告警，不抛 halt 进程」）：
        dry_run 是影子观测/回放态，停手即「当日不再下注」（C-1 触发语义），但不应
        抛 _CriticalHalt 中断整个引擎进程（与 live 区分：live 才停调度）。
    """
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "dry_run")
    alert_calls: list[str] = []
    monkeypatch.setattr(
        "trading.compute.breaker._alert_critical", lambda msg: alert_calls.append(msg))
    # start_equity=0：返 True（C-1 熔断停手语义）
    assert check_daily_loss_limit(0, 965_000, limit=-0.03) is True
    assert len(alert_calls) == 1, "基线缺失应推一次 CRITICAL 告警"
    assert "基线缺失" in alert_calls[0] or "fail-closed" in alert_calls[0]


def test_dry_run_baseline_none_stops_and_alerts(monkeypatch):
    """dry_run + start_equity=None → 返 True（None 与 0 fail-closed 语义等价）。"""
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "dry_run")
    monkeypatch.setattr("trading.compute.breaker._alert_critical", lambda msg: None)
    assert check_daily_loss_limit(None, 965_000, limit=-0.03) is True  # type: ignore[arg-type]


# ============================================================================
# 行为等价红线：有基线路径判定逻辑零变更（spec G3「有基线路径行为不变」）
# ============================================================================

def test_valid_baseline_judgment_unchanged(monkeypatch):
    """有基线路径判定逻辑零变更（spec G3 行为等价红线）。

    覆盖既有 test_circuit_breaker.py 的核心契约：-3.5% 触发 / -2% 不触发 / 边界 -3.0% 触发。
    本测试守护 fail-closed 改动不污染正常判定路径。
    """
    # 即便 _mode=live，有基线时也不应 raise / 不应告警
    monkeypatch.setattr("trading.compute.breaker._mode", lambda: "live")
    alert_calls: list[str] = []
    monkeypatch.setattr(
        "trading.compute.breaker._alert_critical", lambda msg: alert_calls.append(msg))
    # -3.5% 触发
    assert check_daily_loss_limit(1_000_000, 965_000, limit=-0.03) is True
    # -2% 不触发
    assert check_daily_loss_limit(1_000_000, 980_000, limit=-0.03) is False
    # 边界 -3.0% 触发（<= 风控宁可多触发）
    assert check_daily_loss_limit(1_000_000, 970_000, limit=-0.03) is True
    # 有基线路径不触达告警
    assert alert_calls == []


# ============================================================================
# CR-4（2026-08-15）：post_close curr_equity 缺失 fail-closed + 收盘快照失败有声
#
# 物理定位（tech-debt CR-4 · 编排层）：G3 治了熔断「基线缺失」方向（breaker 判定核
# fail-closed），但「当前权益缺失」方向在编排层仍 fail-open——post_close 拉不到
# curr_equity（query_asset 返空/异常）时仅 logger.warning + breaker_skipped，live 无
# 告警不停调度。而 curr_equity 缺失恰是断线场景——熔断最该在岗时它缺勤。本组测试
# 对齐 DG-G3 裁决「不选仅告警不动作」：live 停调度（raise _CriticalHalt）+ CRITICAL；
# dry_run 保留 breaker_skipped（无真实资金敞口，保守停手标记）。附带收盘快照失败
# 「有声」：live 推 CRITICAL 但不 raise（快照失败不阻断 post_close 其余闭合段）。
#
# Why patch 物理路径 ``trading.phases.post_close.*``：post_close 顶部 from-import 本地
# 绑定（W1-A/T2 反查切断），patch ``engine.*`` 已失效——patch 调用方模块属性才命中
# （本文件既有测试 patch ``trading.compute.breaker._mode`` 是同一原则的 breaker 版）。
# ============================================================================
import asyncio

from trading.phases.post_close import post_close as _post_close


class _AssetGwStub:
    """最小网关桩：仅暴露 async ``query_asset``（熔断/快照两段在 post_close 的唯一 gw 副用）。

    query_asset 返回值注入：``{}``（断线返空，无 total_asset）/ 有效 dict——复刻真实
    QmtExecutionGateway 断线/锁定时 query_asset 返空 dict 的契约（与 ①对账段「返空≠
    真空仓」的不可区分性同源）。
    """

    def __init__(self, asset):
        self._asset = asset

    async def query_asset(self):
        return self._asset


class _SeqAssetGwStub:
    """按调用序返值网关桩：post_close 内 query_asset 被调两次（熔断段 :314 先、快照段
    :450 后）——首调返 ``first``（熔断段给足有效 curr 不 halt，流程才能走到快照段）、
    次调起返 ``later``（快照段注入「query 成功但 total=None」的无效值路径）。

    Why 不复用单值 ``_AssetGwStub``：恒返 {} 的单值桩会让熔断段先走 CR-4 fail-closed
    （live raise _CriticalHalt），根本测不到快照段无效值分支——快照无效值是「query
    成功而值无效」的独立残留路径，需与熔断段 curr 方向解耦注入。
    """

    def __init__(self, first, later):
        self._first = first
        self._later = later
        self._calls = 0

    async def query_asset(self):
        self._calls += 1
        return self._first if self._calls == 1 else self._later


class _FakeStateStore:
    """post_close 全链路 state_store 桩（隔离 logs/trading_state.db 真库）。

    Why 整对象替换而非逐函数 patch：post_close ⑥ 段直接
    ``_state_store._connect(_state_store._DEFAULT_DB)`` 开真 sqlite 连接——live 引擎
    运行中共写该库，测试必须完全隔离（防误写生产状态真相源）。start_equity 默认给足
    有效基线（1_000_000），让测试聚焦 CR-4 的 curr_equity 方向（与基线缺失方向解耦）。
    """

    # 永不真连：_connect 返空壳连接（execute().fetchall() 恒 []）
    _DEFAULT_DB = "unused.db"

    def __init__(self, start_equity=1_000_000.0, snapshot_exc=None):
        self.start_equity = start_equity
        self.snapshot_exc = snapshot_exc   # 注入收盘快照异常（快照失败有声测试）
        self.close_snapshots = []          # 快照成功调用记录（观测断言用）

    # —— ③ 熔断段：基线读口（start 给足；T-1 兜底不可用，不掺入基线方向变量）——
    def get_start_equity(self, account_id, date, *, db_path=None):
        return self.start_equity

    def get_prev_close_equity(self, account_id, date, *, db_path=None):
        return None

    # —— ⑥ trade_event 段（无活跃 trade / 无 TP 成交 → 全空转，不出事件）——
    def get_account(self, account_id, *, db_path=None):
        return {"account_id": account_id}  # 非 None → 不触发 upsert_account

    def upsert_account(self, *a, **kw):
        return None

    def get_active_trades(self, account_id, *, db_path=None):
        return []

    def get_position(self, account_id, symbol, *, db_path=None):
        return None

    def insert_trade_event(self, *a, **kw):
        return None

    class _NullCon:
        """空壳 sqlite 连接（⑥ 段 TP_FILLED 直查 SQL 的隔离桩，恒返空集）。"""

        def execute(self, *a, **kw):
            return self

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _connect(self, db_path):
        return self._NullCon()

    # —— account_daily 收盘快照段（snapshot_exc 注入失败语义）——
    def snapshot_close_equity(self, account_id, date, close_total_asset,
                              close_cash=None, close_market_value=None, *,
                              db_path=None):
        if self.snapshot_exc is not None:
            raise self.snapshot_exc
        self.close_snapshots.append((account_id, date, close_total_asset))


class _NullBook:
    """position_book 桩（② 归因段 get_local_positions 隔离真账本，恒空仓）。"""

    @staticmethod
    def get_local_positions():
        return {}


def _patch_post_close_env(monkeypatch, *, mode, state_store):
    """post_close 编排层测试公共 patch：隔离账号/state_store/账本/归因/模式/告警。

    返回 ``alert_calls`` 列表（拦截 _alert_critical 副用：单测不发真钉钉 + 可断言）。
    aggregate_fills_by_symbol 拦成空聚合：② 归因段经 gateway_service 真身读真 CSV，
    与本组测试目标（熔断/快照段）无关，隔离防读到 live 引擎当日成交数据。
    """
    monkeypatch.setattr("trading.phases.post_close._resolve_account_id",
                        lambda: "TESTACC")
    monkeypatch.setattr("trading.phases.post_close._state_store", state_store)
    monkeypatch.setattr("trading.phases.post_close._position_book", _NullBook)
    monkeypatch.setattr("trading.gateway_service.aggregate_fills_by_symbol",
                        lambda a, b: {})
    # 固定交易模式（防 env AUTO_TRADE_MODE 漂移）+ 拦截 CRITICAL 告警副用
    monkeypatch.setattr("trading.phases.post_close._mode", lambda: mode)
    alert_calls: list[str] = []
    monkeypatch.setattr("trading.phases.post_close._alert_critical",
                        lambda msg: alert_calls.append(msg))
    return alert_calls


# ---------------------------------------------------------------------------
# 实盘：curr_equity 缺失 → raise _CriticalHalt（停调度）+ CRITICAL 告警
# ---------------------------------------------------------------------------

def test_post_close_curr_equity_missing_live_halts(monkeypatch):
    """live + query_asset 返 {} → raise _CriticalHalt + 推一次 CRITICAL（CR-4 fail-closed）。

    物理意图（CR-4 · DG-G3 对称收口）：curr_equity 缺失恰是断线场景——熔断最该在岗
    时它缺勤。旧 :307 分支仅 logger.warning + breaker_skipped（fail-open）：live 下无
    告警不停调度，敞口在失明状态下过夜。对齐 DG-G3 裁决「不选仅告警不动作」：
    live 停调度（raise 经 :360 except _CriticalHalt re-raise 直传 _critical_guard）。
    """
    store = _FakeStateStore(start_equity=1_000_000.0)  # 基线给足：聚焦 curr 方向
    alert_calls = _patch_post_close_env(monkeypatch, mode="live", state_store=store)
    gw = _AssetGwStub({})  # 断线：query_asset 返空 dict（无 total_asset）
    with pytest.raises(_CriticalHalt):
        asyncio.run(_post_close("2026-08-15", gw=gw, local_positions=None))
    assert len(alert_calls) == 1, "live curr_equity 缺失必须推且仅推一次 CRITICAL"
    assert "熔断评估失效" in alert_calls[0]


def test_post_close_curr_equity_missing_dry_skips(monkeypatch):
    """dry_run + query_asset 返 {} → breaker_skipped=True 不 raise（保留 skipped 语义）。

    物理意图：dry_run 无真实资金敞口（影子观测态），DG-G3 裁决 dry 不抛 halt 进程
    （与基线缺失方向 test_dry_run_baseline_zero_stops_and_alerts 同口径：保守停手 +
    可观测标记）。观测层凭 breaker_skipped=True 知道「未判定」而非「判定未触发」。
    """
    store = _FakeStateStore(start_equity=1_000_000.0)
    alert_calls = _patch_post_close_env(monkeypatch, mode="dry_run", state_store=store)
    gw = _AssetGwStub({})
    # 不 raise（dry_run 保守停手当日，不抛 halt 进程）
    result = asyncio.run(_post_close("2026-08-15", gw=gw, local_positions=None))
    assert result["breaker_skipped"] is True, "dry_run curr 缺失应保留 skipped 观测标记"
    assert result["circuit_breaker"] is False
    assert alert_calls == [], "dry_run 不推钉钉（避免噪音——与对账异常 live-only 口径一致）"


# ---------------------------------------------------------------------------
# 收盘快照失败：live 推 CRITICAL 但不 raise（有声不阻断——CR-4 收口）
# ---------------------------------------------------------------------------

def test_snapshot_close_failure_live_alerts(monkeypatch):
    """live + snapshot_close_equity 抛异常 → 推 CRITICAL 且 post_close 正常返回（不 raise）。

    物理意图（CR-4）：close_total_asset 是次日熔断 T-1 兜底基线
    （get_prev_close_equity 读 account_daily.close）的唯一写入方——静默失败 = 掏空次日
    基线链，次日 pre_open 再漏抓时熔断又变裸奔。但快照失败不阻断 post_close 其余
    闭合段（trade_event 已落 / 清白名单仍走完），故只 alert 不 raise。
    """
    store = _FakeStateStore(start_equity=1_000_000.0,
                            snapshot_exc=RuntimeError("sqlite locked（模拟快照落库失败）"))
    alert_calls = _patch_post_close_env(monkeypatch, mode="live", state_store=store)
    # 熔断段 curr 有效（1_000_000 vs 基线 1_000_000，0% 回撤不触发）→ 走到快照段
    gw = _AssetGwStub({"total_asset": 1_000_000.0, "cash": 100_000.0,
                       "market_value": 900_000.0})
    result = asyncio.run(_post_close("2026-08-15", gw=gw, local_positions=None))
    # 不 raise：post_close 其余闭合段照常走完，正常返回结果 dict
    assert result["date"] == "2026-08-15"
    assert len(alert_calls) == 1, "live 快照失败必须推一次 CRITICAL（防静默掏空次日基线）"
    assert "收盘快照" in alert_calls[0]
    assert store.close_snapshots == [], "异常注入生效：快照确未落库（fail 有声的观测前提）"


def test_snapshot_invalid_total_live_alerts(monkeypatch):
    """live + query_asset 返 {}（query 成功非异常）→ 推 CRITICAL 且 post_close 正常返回。

    物理意图（CR-4 残留 · 收盘快照无效值有声）：快照段旧代码只覆盖「query 抛异常」
    有声，「query 成功而 total_asset=None/≤0」既无日志也无告警静默跳过——同样掏空
    次日 T-1 兜底基线（close 是 get_prev_close_equity 的唯一写入方，「返空≠真空值」）。
    修复后对齐 except 分支口径：live 推 CRITICAL 但不 raise（快照段不阻断其余闭合段）。
    """
    store = _FakeStateStore(start_equity=1_000_000.0)
    alert_calls = _patch_post_close_env(monkeypatch, mode="live", state_store=store)
    # 调用序注入：熔断段首调给有效资产（1_000_000 vs 基线 1_000_000，0% 回撤不触发不
    # halt），快照段次调返 {}（query 成功但 total 缺失——非异常路径）
    gw = _SeqAssetGwStub(first={"total_asset": 1_000_000.0}, later={})
    result = asyncio.run(_post_close("2026-08-15", gw=gw, local_positions=None))
    # 不 raise：快照无效值与快照异常同口径（有声不阻断闭合段），正常返回结果 dict
    assert result["date"] == "2026-08-15"
    assert len(alert_calls) == 1, "live 快照无效值必须推一次 CRITICAL（防静默掏空次日基线）"
    assert "快照无效值" in alert_calls[0]
    assert store.close_snapshots == [], "total 无效：快照确未落库（有声告警的观测前提）"
