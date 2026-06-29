"""持仓对账（Reconciliation）纯函数单测。"""
from trading.execution_gateway import reconcile, ReconciliationResult


def test_reconcile_all_match():
    r = reconcile({"000001.SZ": 100, "600000.SH": 200}, {"000001.SZ": 100, "600000.SH": 200})
    assert r.is_ok is True
    assert len(r.matched) == 2
    assert r.drifted == [] and r.only_local == [] and r.only_broker == []


def test_reconcile_drift_detected():
    r = reconcile({"A": 100}, {"A": 90})
    assert r.is_ok is False
    assert len(r.drifted) == 1
    assert r.drifted[0].delta == -10.0       # broker - local
    assert r.max_abs_drift == 10.0


def test_reconcile_only_local_and_only_broker():
    # A 仅本地有（疑似未成交/丢单）；C 仅券商有（疑似外部成交/手动单）
    r = reconcile({"A": 100, "B": 50}, {"B": 50, "C": 30})
    assert r.is_ok is False
    syms_local = {d.symbol for d in r.only_local}
    syms_broker = {d.symbol for d in r.only_broker}
    assert syms_local == {"A"}
    assert syms_broker == {"C"}
    assert len(r.matched) == 1 and r.matched[0].symbol == "B"


def test_reconcile_tolerance_boundary():
    # tolerance=5：偏差 5 视为 matched，6 视为 drifted
    r = reconcile({"A": 100}, {"A": 105}, tolerance=5.0)
    assert len(r.matched) == 1
    r2 = reconcile({"A": 100}, {"A": 106}, tolerance=5.0)
    assert len(r2.drifted) == 1


def test_reconcile_max_abs_drift_is_global_max():
    r = reconcile({"A": 100, "B": 200}, {"A": 80, "B": 270})
    assert r.max_abs_drift == 70.0           # max(20, 70)
