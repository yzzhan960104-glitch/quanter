# -*- coding: utf-8 -*-
"""A4 分数 Kelly 仓位单测（2026-08-16 · T1.3）。

覆盖三块：
  ① _trade_cfg 三键：默认零行为变化（fixed/0.25/0.0）；非法 mode 与 fraction 越界
     fail-closed 拒起；hat 越界钳制+CRITICAL（估计量语义，非风控红线语义）；
  ② eod_plan.compute 仓位注入（真链路，tmp_db 隔离 SIGNAL 落库 + 钉钉桩）：
     kelly 模式 min(hat×frac, pos_cap)；hat×frac>pos_cap 封顶回 pos_cap（单向安全）；
     fixed 模式 qty 与改动前口径逐位一致（回归红线，5000 股=1e6×0.05/10 整百手）；
  ③ 影子日志：fixed+hat>0 打对照行但不改变 qty。

实盘安全前提（AUTO_TRADE_MODE=live 在跑）：默认 fixed=零行为变化是本改动唯一合法
上线形态——sizing 三键 env fixture 逐用例还原，绝不泄漏到其他测试。
"""
import asyncio
import logging

import pytest

from trading import critical


@pytest.fixture(autouse=True)
def _clean_sizing_env(monkeypatch):
    """清 sizing 三键 env（防宿主环境污染默认值断言）。"""
    for k in ("TRADE_SIZING_MODE", "TRADE_KELLY_FRACTION", "TRADE_KELLY_HAT"):
        monkeypatch.delenv(k, raising=False)


# ─────────────────────────── ① _trade_cfg 三键 ───────────────────────────

def test_trade_cfg_defaults_zero_behavior_change():
    """默认：sizing_mode=fixed / fraction=0.25 / hat=0.0——部署即零行为变化。"""
    cfg = critical._trade_cfg()
    assert cfg["sizing_mode"] == "fixed"
    assert cfg["kelly_fraction"] == 0.25
    assert cfg["kelly_hat"] == 0.0
    # 其余键不受新增影响（抽查既有键仍在）
    assert cfg["pos_cap"] == pytest.approx(0.05)
    assert cfg["stop_atr_mult"] == pytest.approx(1.0)


def test_sizing_mode_invalid_fails_closed(monkeypatch):
    """非法 mode（拼写错 'kely'）fail-closed 抛错——宁可停单不静默降级。"""
    monkeypatch.setenv("TRADE_SIZING_MODE", "kely")
    with pytest.raises(ValueError, match="TRADE_SIZING_MODE"):
        critical._trade_cfg()


def test_kelly_fraction_over_half_fails_closed(monkeypatch):
    """fraction>0.5 拒起（DG-G5：上限 0.5× 需样本外验证，不可盲设）。"""
    monkeypatch.setenv("TRADE_KELLY_FRACTION", "0.7")
    with pytest.raises(ValueError, match="KELLY_FRACTION"):
        critical._trade_cfg()


def test_kelly_fraction_zero_or_negative_fails_closed(monkeypatch):
    """fraction≤0 拒起（0 = 永远零仓的静默哑弹，属配置笔误）。"""
    monkeypatch.setenv("TRADE_KELLY_FRACTION", "0")
    with pytest.raises(ValueError, match="KELLY_FRACTION"):
        critical._trade_cfg()


def test_kelly_hat_out_of_range_clamps(monkeypatch, caplog):
    """hat 越界钳到 [0,0.5] + CRITICAL 日志（估计量钳制语义，区别于 fraction 拒起）。"""
    monkeypatch.setenv("TRADE_KELLY_HAT", "0.9")
    with caplog.at_level(logging.CRITICAL, logger="trading.critical"):
        cfg = critical._trade_cfg()
    assert cfg["kelly_hat"] == 0.5
    assert any("TRADE_KELLY_HAT" in r.message for r in caplog.records)
    monkeypatch.setenv("TRADE_KELLY_HAT", "-0.3")
    assert critical._trade_cfg()["kelly_hat"] == 0.0


# ─────────────────────── ② compute 仓位注入（真链路） ───────────────────────

def _mk_signal(sym="300001.SZ", entry=10.0):
    """最小 Signal（frozen dataclass，仅填 build_orders 消费的字段）。"""
    from strategies.neckline.signal import Signal
    return Signal(symbol=sym, signal_type="neckline", formed_at="2026-08-14",
                  neckline=10.0, bottom=8.0, entry_price=entry, atr=0.5)


def _run_compute(tmp_db, monkeypatch, env):
    """跑 eod_plan.compute 真链路，从 tmp_db trade_event(SIGNAL) meta 读回订单 qty。

    Why 读 DB 而非返回值：compute 返回 {date,n_orders,mode,auto_confirmed} 无 orders——
    订单细节唯一真相源是 SIGNAL meta（C3 设计），测试同口径读回最真。
    隔离：tmp_db（SIGNAL/CONFIRMED 落库隔离）+ 钉钉推送桩 + QMT 持仓桩（防真实网关）。
    """
    import json as _json
    import sqlite3
    from trading import eod_plan, gateway_service
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(eod_plan.trading_plan, "push_plan_to_dingtalk",
                        lambda *a, **kw: None)
    async def _no_pos():
        return None
    monkeypatch.setattr(gateway_service, "get_positions", _no_pos)
    res = asyncio.run(eod_plan.compute(
        "2026-08-17", [_mk_signal()], {"300001.SZ": 0.5}, capital=1_000_000.0))
    with sqlite3.connect(tmp_db) as con:
        rows = con.execute(
            "SELECT meta FROM trade_event WHERE action='SIGNAL'").fetchall()
    orders = [_json.loads(r[0]) for r in rows]
    return res, orders


def test_compute_fixed_mode_qty_baseline(tmp_db, monkeypatch):
    """回归红线：fixed 模式（默认 env）qty 与改动前逐位一致——1e6×0.05/10.0=5000 股
    （plan.py:141 int(budget/entry/100)*100 整百手），kelly 注入点不得污染 fixed 口径。"""
    res, orders = _run_compute(tmp_db, monkeypatch, {})
    assert res["n_orders"] == 1 and len(orders) == 1
    assert orders[0]["order"]["qty"] == 5000


def test_compute_kelly_mode_shrinks_position(tmp_db, monkeypatch):
    """kelly 模式：hat=0.12×frac=0.25 → pos_cap_eff=0.03 → 1e6×0.03/10.0=3000 股。"""
    res, orders = _run_compute(tmp_db, monkeypatch, {"TRADE_SIZING_MODE": "kelly",
                                                     "TRADE_KELLY_HAT": "0.12"})
    assert orders[0]["order"]["qty"] == 3000


def test_compute_kelly_capped_back_to_pos_cap(tmp_db, monkeypatch):
    """hat×fraction > pos_cap 时封顶回 pos_cap（单向安全：kelly 永不加仓）。

    hat=0.5×frac=0.5=0.25 > 0.05 → 有效 0.05，qty 与 fixed 相同。
    """
    res, orders = _run_compute(tmp_db, monkeypatch, {"TRADE_SIZING_MODE": "kelly",
                                                     "TRADE_KELLY_HAT": "0.5",
                                                     "TRADE_KELLY_FRACTION": "0.5"})
    assert orders[0]["order"]["qty"] == 5000


def test_compute_shadow_log_does_not_change_qty(tmp_db, monkeypatch, caplog):
    """影子日志：fixed+hat>0 只打对照行，qty 仍 fixed 口径（观察面零行为变化）。"""
    with caplog.at_level(logging.INFO, logger="trading.engine"):
        res, orders = _run_compute(tmp_db, monkeypatch, {"TRADE_KELLY_HAT": "0.12"})
    assert orders[0]["order"]["qty"] == 5000
    assert any("sizing-shadow" in r.message for r in caplog.records)


def test_compute_kelly_zero_hat_degenerates_to_min(tmp_db, monkeypatch):
    """hat=0（A4 实证 2022/2023 弱年口径）→ pos_cap_eff=0 → qty=0 → 无单（保守方向）。"""
    res, orders = _run_compute(tmp_db, monkeypatch, {"TRADE_SIZING_MODE": "kelly",
                                                     "TRADE_KELLY_HAT": "0.0"})
    assert res["n_orders"] == 0 and orders == []
