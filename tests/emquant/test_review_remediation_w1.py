# -*- coding: utf-8 -*-
"""W1（2026-08-28 全库评审清偿）实盘守卫回归——钉死本次修复的四类缺口。

被测对象是【组装产物】emquant/emquant_neckline_pilot.py（与 test_events_
orchestration 同范式：改 pilot_body.py 后先重跑 build_pilot.py 再跑本文件）。

钉值清单（每条对应评审一个 P0/P1，防回归）：
  1. _has_open_sell 反转语义（P0-1）：全部卖向 purpose（TP2_SHARE/TIME_STOP/
     TP2_EOD_SWEEP/EXPIRE/STOP_LOSS/TP1/TP2/缺失/UNKNOWN）在途 → True；
     买向（OPEN/CHASE）在途 → False。旧正向白名单漏 TP2_SHARE 等三目的
     =反转 regime tp2→tp1 双倍卖出 lot2 的根因。
  2. tp2→tp1 连续冲高双卖防护（P0-1 触发链）：反转 regime 持仓先触 tp2 落 lot2
     卖单（未吸收），次 tick 触 tp1 → _has_open_sell 拦住，不落第二张卖单。
  3. 买向单源（P0-4）：CHASE 在途被 _open_buy_amount 计入；chase 落单进
     placed[today]（check_caps ③「在场效果」占额）。
  4. FILL 逐笔审计（W1-4）：absorb fill 增量 → fill_sink 收到逐笔回调；
     reconcile 注入后 audit CSV 出现 FILL 行。
  5. 跨日清理（W8）：after_close 后 placed/scan_done 只留最近 _STATE_HISTORY_
     KEEP_DAYS 日键。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import time
import sys
from pathlib import Path

import pytest

from tests.emquant.fake_gm import FakeGm

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"
TODAY = "2026-08-21"

TERMINAL = "FILLED"   # 任一终态即可（守卫只看「非终态」）


def _import_artifact():
    spec = importlib.util.spec_from_file_location("emquant_pilot_w1", ARTIFACT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def pilot():
    return _import_artifact()


class _Ctx:
    """gm context 占位（编排层不读其字段）。"""


def _order(purpose, status="SUBMITTED", **over):
    o = {"symbol": "300001.SZ", "date": TODAY, "price": 10.0, "qty": 100,
         "purpose": purpose, "cancel_on": None, "formed_at": None,
         "exec_params": {}, "filled": 0, "status": status,
         "account": "acct", "placed_at": 0.0}
    o.update(over)
    return o


# ============================================================ 1. 守卫反转语义
@pytest.mark.parametrize("purpose", [
    "EXPIRE", "STOP_LOSS", "TP1", "TP2",
    "TP2_SHARE",          # P0-1：R6-6 反转 regime lot2 卖（旧白名单漏）
    "TP2_EOD_SWEEP",      # P0-1：盘后 sweep 次日强平（旧漏）
    "TIME_STOP",          # P0-1：R6-10 时间止损（旧漏；当前快照未启用仍须守卫）
    "UNKNOWN",            # 柜台吸收单保守计入
    None,                 # purpose 缺失保守计入
])
def test_has_open_sell_covers_all_sell_purposes(pilot, tmp_path, purpose):
    rt = pilot.PilotRuntime(FakeGm(), workdir=tmp_path)
    rt.state["orders"]["c1"] = _order(purpose)
    assert rt._has_open_sell("300001.SZ") is True, f"purpose={purpose!r} 必须被卖向守卫覆盖"


@pytest.mark.parametrize("purpose", ["OPEN", "CHASE"])
def test_has_open_sell_excludes_buy_purposes(pilot, tmp_path, purpose):
    rt = pilot.PilotRuntime(FakeGm(), workdir=tmp_path)
    rt.state["orders"]["c1"] = _order(purpose)
    assert rt._has_open_sell("300001.SZ") is False


def test_has_open_sell_ignores_terminal_sell(pilot, tmp_path):
    rt = pilot.PilotRuntime(FakeGm(), workdir=tmp_path)
    rt.state["orders"]["c1"] = _order("TP2_SHARE", status=TERMINAL)
    assert rt._has_open_sell("300001.SZ") is False


# ============================================================ 2. tp2→tp1 双卖防护
def _inverted_position(pilot):
    """反转 regime 持仓：tp1=2H 在 tp2=1.5H 之上（快照实弹形态 tp1_h_mult 2.0 > tp_h_mult 1.5）。"""
    return {"entry_date": "2026-08-01", "entry_price": 9.0, "qty": 1000,
            "remaining_qty": 1000, "stop": 7.0, "tp1_price": 14.0, "tp1_done": False,
            "tp2_price": 13.0, "tp2_done": False, "force_exit": False,
            "trailing": {}, "exec_params": {"tp1_portion": 0.9}}


def test_tp2_then_tp1_no_double_sell(pilot, monkeypatch, tmp_path):
    """P0-1 触发链：tp2 触发落 lot2 卖单（在途未吸收）→ 价格续冲 tp1 →
    on_tick 第二轮必须被 _has_open_sell 拦住（不落第二张卖单）。"""
    fake = FakeGm()
    rt = pilot.PilotRuntime(fake, workdir=tmp_path)
    monkeypatch.setattr(pilot, "_today_str", lambda: TODAY)
    monkeypatch.setattr(pilot, "build_calendar", lambda a, t, lookback_days=500: ["2026-08-20", TODAY])
    rt.state["last_pre_open_date"] = TODAY       # 压制 tick 自愈（不误触发 pre_open）
    rt.state["last_after_close_date"] = TODAY
    rt.state["positions"]["300001.SZ"] = _inverted_position(pilot)
    # 柜台持仓种子（gm 格式键）：对账③「qty 以柜台为准」——无种子则 reconcile 把
    # state 持仓 remaining 清零，测试就不再是目标场景。
    fake.positions["SZSE.300001"] = {
        "account_id": "acct", "symbol": "SZSE.300001", "side": 1,
        "volume": 1000, "vwap": 9.0, "amount": 9000.0, "available": 1000,
        "available_now": 1000, "volume_today": 1000, "market_value": 13500.0}
    # tick 1：px=13.5 ≥ tp2=13.0 → tp2_share 卖 lot2（1000×(1-0.9)=100 股）
    tick_hi = {"symbol": "SZSE.300001", "price": 13.5}
    rt.on_tick(_Ctx(), _Tick(tick_hi))
    sells_1 = [c for c in fake.calls if c.get("api") == "order_volume" and c.get("side") == 2]
    assert len(sells_1) == 1 and sells_1[0]["volume"] == 100
    assert rt.state["positions"]["300001.SZ"]["tp2_done"] is True
    # tick 2：px=14.5 ≥ tp1=14.0（反转 regime lot1 全出价位）——旧代码此处双卖；
    # 修复后 TP2_SHARE 在途被守卫看见 → 本 tick 不落单
    tick_higher = {"symbol": "SZSE.300001", "price": 14.5}
    rt.on_tick(_Ctx(), _Tick(tick_higher))
    sells_2 = [c for c in fake.calls if c.get("api") == "order_volume" and c.get("side") == 2]
    assert len(sells_2) == 1, "lot2 卖单在途未被吸收前，tp1 不得再落卖单（P0-1）"
    # 吸收 lot2 成交后（remaining 900），tp1 才允许全量出 lot1
    oid = sells_1[0].get("cl_ord_id") or _first_sell_id(fake)
    fake.fill_order(oid, volume=100, price=13.0)
    rt.reconcile(_Ctx())
    assert rt.state["positions"]["300001.SZ"]["remaining_qty"] == 900
    rt.on_tick(_Ctx(), _Tick(tick_higher))
    sells_3 = [c for c in fake.calls if c.get("api") == "order_volume" and c.get("side") == 2]
    assert len(sells_3) == 2 and sells_3[-1]["volume"] == 900, "吸收完成后 tp1 全量出 lot1"


class _Tick:
    """TickLikeDict2 形态替身（同 test_events_orchestration._SdkTick）。"""
    get = None

    def __init__(self, fields):
        self._fields = fields

    def __getitem__(self, key):
        return self._fields.get(key)


def _first_sell_id(fake):
    for o in fake.get_orders():
        if o.get("side") == 2:
            return o["cl_ord_id"]
    raise AssertionError("FakeGm 订单簿无卖单")


# ============================================================ 3. 买向单源（P0-4）
def test_open_buy_amount_counts_chase(pilot, tmp_path):
    st = pilot._initial_state()
    st["orders"]["c1"] = _order("CHASE", qty=200, price=10.0)
    assert pilot._open_buy_amount(st) == 2000.0, "CHASE 在途必须计入 CAP 占额（P0-4）"


def test_open_buy_amount_excludes_sells(pilot, tmp_path):
    st = pilot._initial_state()
    st["orders"]["c1"] = _order("TP2_SHARE", qty=100, price=10.0)
    assert pilot._open_buy_amount(st) == 0.0


def test_chase_counts_toward_daily_gate(pilot, monkeypatch, tmp_path):
    """chase 落单进 placed[today] → check_caps ③ 单日闸「在场效果」计数含 chase。"""
    fake = FakeGm()
    rt = pilot.PilotRuntime(fake, workdir=tmp_path)
    monkeypatch.setattr(pilot, "_today_str", lambda: TODAY)
    rt.state["last_pre_open_date"] = TODAY       # 压制 tick 自愈
    rt.state["last_after_close_date"] = TODAY
    # 构造一个已触发 chase 判定的 OPEN 挂单（max_wait 已超 + chase_entry=True）；
    # placed_at=now：R6-13b 竞态宽限窗内（60s）——on_tick 首跳 reconcile 的 absorb②
    # 不把刚挂的单判死（placed_at=0 视同最老单，会先被收敛 CANCELLED，chase 无从触发）
    rt.state["orders"]["old1"] = _order(
        "OPEN", formed_at="2026-07-01",
        exec_params={"max_wait": 1, "chase_entry": True}, qty=100, price=10.0,
        neckline=10.0, bottom=8.0, atr=0.8, placed_at=time.time())
    monkeypatch.setattr(pilot, "build_calendar",
                        lambda a, t, lookback_days=500: ["2026-07-01", "2026-08-20", TODAY])
    rt.on_tick(_Ctx(), _Tick({"symbol": "SZSE.300001", "price": 10.5}))
    chase_ids = [cid for cid, o in rt.state["orders"].items() if o.get("purpose") == "CHASE"]
    assert chase_ids, "构造失败：应已产生 chase 追入单"
    assert any(cid in rt.state["placed"].get(TODAY, []) for cid in chase_ids), \
        "chase 落单必须进 placed[today]（W1-2 单日闸占额）"


# ============================================================ 4. FILL 逐笔审计
def test_fill_sink_emits_per_fill(pilot):
    st = pilot._initial_state()
    api_orders = [{
        "cl_ord_id": "b1", "symbol": "SZSE.300001", "side": 1, "status": 3,
        "volume": 300, "filled_volume": 200, "price": 10.0,
        "filled_vwap": 9.98, "created_at": None, "account_id": "acct"}]
    got = []
    pilot.absorb_reality(st, [], [], fill_sink=lambda **kw: got.append(kw))
    pilot.absorb_reality(st, api_orders, [], fill_sink=lambda **kw: got.append(kw))
    assert got == [{"symbol": "300001.SZ", "side": "buy", "qty": 200,
                    "price": 9.98, "cl_ord_id": "b1", "filled_total": 200}]
    # 幂等：重放同 filled → 无新 FILL
    got.clear()
    pilot.absorb_reality(st, api_orders, [], fill_sink=lambda **kw: got.append(kw))
    assert got == []


def test_reconcile_writes_fill_audit(pilot, monkeypatch, tmp_path):
    fake = FakeGm()
    rt = pilot.PilotRuntime(fake, workdir=tmp_path)
    monkeypatch.setattr(pilot, "_today_str", lambda: TODAY)
    rt.reconcile(_Ctx())          # 首轮空对账
    oid = pilot.place_limit_buy(fake, "300001.SZ", 10.0, 100, "acct")
    assert oid
    fake.fill_order(oid, volume=100, price=10.0)
    rt.reconcile(_Ctx())
    f = tmp_path / "audit" / f"audit_{TODAY.replace('-', '')}.csv"
    rows = list(csv.reader(f.read_text(encoding="utf-8").splitlines()))
    fills = [json.loads(r[2]) for r in rows if r[1] == "FILL"]
    assert fills and fills[0]["qty"] == 100 and fills[0]["side"] == "buy", \
        "reconcile 必须落 FILL 逐笔审计行（W1-4）"


# ============================================================ 5. 跨日清理（W8）
def test_after_close_prunes_history(pilot, monkeypatch, tmp_path):
    fake = FakeGm()
    rt = pilot.PilotRuntime(fake, workdir=tmp_path)
    monkeypatch.setattr(pilot, "_today_str", lambda: TODAY)
    keep = pilot._STATE_HISTORY_KEEP_DAYS
    rt.state["placed"]["2026-01-01"] = ["x"]
    rt.state["placed"][TODAY] = ["y"]
    rt.state["scan_done"] = {"2026-01-01", TODAY}
    rt.after_close(_Ctx())
    assert "2026-01-01" not in rt.state["placed"]
    assert "2026-01-01" not in rt.state["scan_done"]
    assert TODAY in rt.state["placed"] and TODAY in rt.state["scan_done"]
    assert keep >= 1
