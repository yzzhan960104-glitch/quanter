# -*- coding: utf-8 -*-
"""W3.4 post_close 持仓对账口径回归（broker 权威 / fill 归因）。

事故背景（08-04）：
    post_close 兜底以 logs/live_trades.csv 聚合净持仓（aggregate_fills_by_symbol）
    diff position_book 并【以 CSV 为准】重写 qty。网关恢复后 24 行重复 CSV fill
    → 幻影 2400 股 → 止损/止盈基于幻影持仓挂卖单（超卖敞口）。

拷问 3 结论（controller 核实，不可妥协）：
    fill 表/CSV 空可能是「网关断线无回报」而非「真无成交」——post_close 不能用
    fill/CSV 重写 position（否则与柜台漂移）。broker query_stock_positions 才是
    持仓权威（钱的真实归属）；fill 只解释「今日变动归因」。

本文件断言两条红线：
    1. CSV 脏行（重复 fill）不污染 position_book（broker 权威，CSV 降级为归因展示）。
    2. broker 取数失败/返空 → 绝不覆盖 position_book（返空可能是取数失败而非真空仓，
       覆盖会清空真实持仓 → 超卖敞口）+ CRITICAL 告警。
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
from datetime import datetime

import pytest

from trading import engine, position_book
from trading.compute.reconcile import ReconciliationResult


# ---------------------------------------------------------------------------
# 公共 fixture：隔离 position_book/state.db + TRADE_PLAN_DIR（与 test_engine 同口径）
# ---------------------------------------------------------------------------
@pytest.fixture
def _isolated_book(monkeypatch, tmp_path):
    """隔离 position_book DB（防读生产账本 / 误归零真实持仓）。"""
    os.environ["TRADE_PLAN_DIR"] = str(tmp_path)
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    return tmp_path


def _make_fake_rec(is_ok: bool = True):
    """构造 run_reconcile 的 mock 返回（默认无偏差，让测试聚焦在 CSV/broker 路径）。"""
    async def _fake_run_rec(gw, local, tolerance=0.0):
        return ReconciliationResult([], [], [], [], 0.0, is_ok)
    return _fake_run_rec


# ---------------------------------------------------------------------------
# 红线 1：CSV 脏行（重复 fill）不污染 position_book（broker 权威）
# ---------------------------------------------------------------------------
def test_post_close_csv_dirty_rows_do_not_rewrite_position(monkeypatch, _isolated_book):
    """W3.4 红线 1：CSV 24 行重复 BUY 100 → position_book 不得被重写成 2400。

    场景（08-04 事故还原）：
        - position_book 真实持仓 600000.SH = 100（apply_fill 正确记账一次）；
        - broker 真实持仓 600000.SH = 100（query_stock_positions 权威）；
        - 但 CSV 在网关恢复后重放/重复写入 24 行 BUY 100（聚合净=2400）。
    旧逻辑（致命）：aggregate_fills(CSV)=2400 vs position_book=100 drift → 重写 2400。
    新逻辑（W3.4）：CSV 降级为归因展示，position_book 维持 broker 权威口径=100。
    """
    # ① 真实账本：apply_fill 正确记账 100 股 600000.SH
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.apply_fill("ord_clean_1", "600000.SH", "BUY", 100, 10.0,
                             "20990101100000")
    assert position_book.get_local_positions().get("600000.SH") == 100.0

    # ② CSV 写 24 行重复 BUY 100（事故场景：网关恢复后回报重放）
    csv_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "live_trades.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "symbol", "direction", "shares", "price",
                    "strategy", "rationale", "kind"])
        for i in range(24):
            w.writerow([f"{today} 10:00:{i:02d}", "600000.SH", "BUY", 100,
                        10.0, "", "", "fill"])

    # ③ mock broker 权威返 100（与 position_book 一致，无 drift）
    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self, *, tradable_only=True):
            return {"600000.SH": {"volume": 100.0}}
    monkeypatch.setattr("trading.reconcile_job.run_reconcile", _make_fake_rec(is_ok=True))  # W1-B：迁共享模块

    # ④ 跑 post_close
    asyncio.run(engine.post_close(today, gw=_FakeGw(),
                                  local_positions={"600000.SH": 100.0}))

    # ⑤ 红线断言：position_book 维持 100，绝不被 CSV 重写成 2400（超卖敞口）
    assert position_book.get_local_positions().get("600000.SH") == 100.0, (
        "W3.4 红线违反：post_close 用 CSV aggregate_fills 重写了 position_book，"
        "事故场景下会产生幻影 2400 股持仓 → 止损/止盈基于幻影挂卖单（超卖敞口）。")


# ---------------------------------------------------------------------------
# 红线 2：broker 取数失败/返空 → 不覆盖 position_book + CRITICAL 告警
# ---------------------------------------------------------------------------
def test_post_close_broker_failure_does_not_clear_position(monkeypatch, _isolated_book, caplog):
    """W3.4 红线 2：broker 取数失败 → 绝不覆盖 position_book（返空≠真空仓）。

    场景：
        - position_book 真实持仓 300001.SZ = 100（apply_fill 正确记账）；
        - broker query_stock_positions 抛 RuntimeError（断线/未连接）。
    旧逻辑（致命）：_fetch_broker_positions 抛 → sync_positions 抛 →
        run_reconcile 抛 → post_close 走 except 标 drift=True，但若上层用空 dict
        覆盖会清空 position_book。
    新逻辑（W3.4）：broker 取数失败 → 保持 position_book 既有值 + CRITICAL 告警，
        绝不自动归零（超卖敞口红线）。
    """
    # ① 真实账本 300001.SZ = 100
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.apply_fill("ord_clean_2", "300001.SZ", "BUY", 100, 10.0,
                             "20990101100000")
    assert position_book.get_local_positions().get("300001.SZ") == 100.0

    # ② broker 取数失败（模拟 query_stock_positions 抛 RuntimeError，断线场景）
    class _BrokenGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self, *, tradable_only=True):
            raise RuntimeError("QMT 网关未连接，无法对账")
        async def sync_positions(self, local_positions, tolerance=0.0):
            # 真实 BaseExecutionGateway.sync_positions 会调 _fetch_broker_positions
            # 失败时向上抛——这里复刻真实语义（不做软降级）。
            # ⚠️ M-2（Task 8 review fix）：本 mock 仅复刻 sync_positions 抛语义（非真链路）；
            # 真链路 sync_positions 对「broker 返空 dict（不抛）」场景的处理（既有 reconcile
            # 纯函数对空 broker dict 不写 position_book，但隐蔽需测）由 M-3 新测覆盖。
            return await self._fetch_broker_positions()

    # ③ 跑 post_close（run_reconcile 走真路径会抛，被 post_close except 捕获标 drift=True）
    with caplog.at_level(logging.CRITICAL, logger="trading.engine"):
        try:
            result = asyncio.run(engine.post_close(
                today, gw=_BrokenGw(), local_positions={"300001.SZ": 100.0}))
        except Exception:
            pytest.fail("post_close 应软降级不抛（broker 取数失败不应阻断后续段）")

    # ④ 红线断言：position_book 保持 100，绝不被清空
    assert position_book.get_local_positions().get("300001.SZ") == 100.0, (
        "W3.4 红线违反：broker 取数失败时 position_book 被覆盖（清空），"
        "返空可能是取数失败而非真空仓 → 超卖敞口。")

    # ⑤ 断言：至少有 CRITICAL/WARNING 级告警（broker 失败必须可见）
    assert any(r.levelname in ("CRITICAL", "WARNING", "ERROR")
               for r in caplog.records), (
        "W3.4 红线违反：broker 取数失败无任何高级别告警，敞口风险彻底失明。")


def test_post_close_broker_empty_dict_does_not_clear_position(monkeypatch, _isolated_book):
    """W3.4 红线 2 补（M-3）：broker 返空 dict（不抛）→ 账本保持既有值。

    场景（隐蔽形态）：
        - position_book 真实持仓 300002.SZ = 100（apply_fill 正确记账）；
        - broker sync_positions 返 ``{}``（query_stock_positions 查询失败/当日空均可能返空，
          与真空仓不可区分的隐蔽形态——见 broker/qmt.py:_fetch_broker_positions 文档：
          「query_stock_positions 返回 None（查询失败或当日无持仓）→ 返空 dict」）。
    红线：既有 reconcile 纯函数对空 broker dict 不写 position_book（broker 空 dict 表
        「不可知」而非「真空仓」），但此场景隐蔽需测试锁定——防止未来误把空 broker dict
        当「真实空仓」而清零 position_book（超卖敞口红线）。
    """
    # ① 真实账本 300002.SZ = 100
    today = datetime.now().strftime("%Y-%m-%d")
    position_book.apply_fill("ord_clean_3", "300002.SZ", "BUY", 100, 10.0,
                             "20990101100000")
    assert position_book.get_local_positions().get("300002.SZ") == 100.0

    # ② broker 返空 dict（query_stock_positions 返 None → _fetch_broker_positions 返 {}；
    #    sync_positions 模板方法对空 dict 不扁平化、reconcile 纯函数对空 broker dict 不写账本）
    class _EmptyBrokerGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self, *, tradable_only=True):
            # 复刻 broker/qmt.py:624 真链路降级语义：query_stock_positions 返 None → 返 {}
            return {}
        async def sync_positions(self, local_positions, tolerance=0.0):
            # 复刻 BaseExecutionGateway.sync_positions 模板方法真链路（不抛，返 reconcile 结果）
            from trading.compute.reconcile import reconcile
            broker_positions = await self._fetch_broker_positions(tradable_only=False)
            # 空 dict 的 next(iter(...), None) 返 None → 不进 isinstance 扁平化分支 → 原样透传
            if broker_positions and isinstance(next(iter(broker_positions.values()), None), dict):
                broker_positions = {s: p["volume"] for s, p in broker_positions.items()}
            return reconcile(local_positions, broker_positions, tolerance)

    # ③ 跑 post_close：run_reconcile 走真路径（sync_positions 返 reconcile(local, {}, 0)）
    result = asyncio.run(engine.post_close(
        today, gw=_EmptyBrokerGw(), local_positions={"300002.SZ": 100.0}))

    # ④ 红线断言：position_book 保持 100，绝不被空 dict 清零（超卖敞口红线）
    assert position_book.get_local_positions().get("300002.SZ") == 100.0, (
        "W3.4 红线违反：broker 返空 dict 时 position_book 被清零——"
        "空 dict 与真空仓不可区分，覆盖=清空真实持仓 → 超卖敞口。")


def test_post_close_csv_attribution_only_no_qty_rewrite(monkeypatch, _isolated_book):
    """W3.4 补充：CSV 归因展示存在（attribution log），但不重写 qty。

    物理意图：aggregate_fills_by_symbol 降级为「今日成交归因」展示函数，
    position_book 不被其触碰。本测试断言：即使 CSV 有 fill 行且与账本 drift，
    position_book 仍维持原值（CSV 只产日志）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    # 账本空 + CSV 有 1 行 BUY 50（fill）
    csv_path = os.path.join(os.environ["TRADE_PLAN_DIR"], "live_trades.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "symbol", "direction", "shares", "price",
                    "strategy", "rationale", "kind"])
        w.writerow([f"{today} 10:00:00", "000001.SZ", "BUY", 50, 10.0, "", "", "fill"])

    class _FakeGw:
        async def query_asset(self):
            return {"total_asset": 1_000_000.0}
        async def _fetch_broker_positions(self, *, tradable_only=True):
            return {}
    monkeypatch.setattr("trading.reconcile_job.run_reconcile", _make_fake_rec(is_ok=True))  # W1-B：迁共享模块

    asyncio.run(engine.post_close(today, gw=_FakeGw(), local_positions={}))

    # CSV 有 50 股但账本应为空（apply_fill 未记），post_close 不得用 CSV 重写
    assert "000001.SZ" not in position_book.get_local_positions(), (
        "W3.4：CSV 归因展示不应重写 position_book；fill 表只做归因，不做权威。")
