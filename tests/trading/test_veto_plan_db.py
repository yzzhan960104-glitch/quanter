# -*- coding: utf-8 -*-
"""W2/C3 · veto_plan.veto DB 真相源测试（spec §6 C3：veto 只写 DB VETOED 不落 JSON）。

物理意图（C3 重构）：
    AUTO_CONFIRM_PLAN=true 全自动模式下，eod_plan 写 DB CONFIRMED 放行，pre_open 次日直挂。
    veto 是全自动模式下唯一的人审刹车（opt-out：默认挂，否决才拦）。C3 改 veto 只写
    DB trade_event(VETOED)（spec §6 真相源），不再改 JSON 镜像——pre_open/load_plan 据
    ``get_latest_action(trade_id)=VETOED`` 跳过标的，eod_plan 重跑据 ``!= VETOED`` 不复活
    CONFIRMED。

    错误分级（C-4 沿用）：
        - veto DB 写失败 = 抛错退出（人审刹车不能假成功，否则 pre_open 放行）
        - legacy shim confirm DB 写失败 = 软降级（人审确认不阻断，JSON 已是真相源展示镜像）

C3 测试改造：
    - veto 测试不再断言 JSON 镜像（veto 不再落 JSON）；只验 DB VETOED + load_plan confirmed=False。
    - confirm 测试改用 tests/_legacy_plan_io.confirm_plan_legacy（生产 confirm 已删）。
"""
import json

import pytest

from trading import state_store, trading_plan
from trading.tools.veto_plan import veto
from tests._legacy_plan_io import save_plan_legacy as _save_sample_plan, confirm_plan_legacy


# ---------------------------- 测试夹具 ----------------------------
def _setup_env(tmp_path, monkeypatch, account_id="test_acct"):
    """统一环境：tmp DB + tmp TRADE_PLAN_DIR + account 行 + 写 DB SIGNAL。

    C3：plan 落盘改用 legacy shim（生产 save_plan 已删）+ DB SIGNAL 真相源种子。
    """
    db = str(tmp_path / "ts.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    plan_dir = tmp_path / "plans"
    monkeypatch.setenv("TRADE_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("QMT_ACCOUNT_ID", account_id)
    state_store.init_store(db)
    state_store.upsert_account(account_id, broker="qmt")
    return db, account_id


def _save_sample_plan_with_signals(date: str, symbols: list[str], confirmed: bool = True,
                                    account_id: str = "test_acct") -> None:
    """落盘嵌套 orders（legacy shim JSON 镜像）+ DB SIGNAL 真相源。

    C3：veto 测试需 DB SIGNAL（load_plan DB 优先路径），JSON 镜像保留供回退断言。
    顺序约束：SIGNAL 先写（event_id < 后续 CONFIRMED/VETOED，latest_action 返最新）。
    """
    orders = [
        {"order": {"symbol": s, "qty": 100, "side": "BUY", "price": 10.0},
         "stop_price": 9.5, "take_profit": 11.0, "formed_at": date, "max_wait": 5}
        for s in symbols
    ]
    _save_sample_plan(date, orders, confirmed=confirmed)
    # DB SIGNAL 真相源
    for o in orders:
        sym = o["order"]["symbol"]
        meta_obj = {**o, "plan_date": date, "strategy_name": "neckline",
                    "rationale": f"颈线法@{o.get('formed_at', '')}"}
        tid = state_store.build_trade_id(account_id, sym, date)
        state_store.insert_trade_event(
            account_id, tid, sym, "SIGNAL",
            meta=json.dumps(meta_obj, ensure_ascii=False))
        if confirmed:
            state_store.insert_trade_event(account_id, tid, sym, "CONFIRMED")


# ---------------------------- veto 单否（C3 只写 DB） ----------------------------
def test_veto_single_writes_db_vetoed(tmp_path, monkeypatch):
    """C3：单否只写 DB trade_event(VETOED)（spec §6，不再改 JSON）。"""
    _setup_env(tmp_path, monkeypatch)
    date, sym = "2026-08-05", "300001.SZ"
    _save_sample_plan_with_signals(date, [sym])
    acct = "test_acct"
    trade_id = f"{acct}_{sym}_{date}"

    rc = veto(date, sym)

    assert rc == 0
    # DB 真相源有 VETOED（pre_open _pre_open_impl:869 据此跳过挂单）
    assert state_store.get_latest_action(trade_id) == "VETOED"
    # C3：load_plan 据 latest_action=VETOED → confirmed=False（veto 终局防线）
    plan = trading_plan.load_plan(date)
    assert plan["confirmed"] is False


# ---------------------------- veto 全否（批量，C3 只写 DB） ----------------------------
def test_veto_all_writes_db_vetoed_for_every_symbol(tmp_path, monkeypatch):
    """C3：全否（symbol=None）批量写每个 symbol 的 DB VETOED。

    物理：全否时每个 symbol 写 VETOED（per-trade_id 拦截），load_plan 据任一 VETOED
    → confirmed=False（plan 级整体拦）。
    """
    _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    syms = ["300001.SZ", "300002.SZ", "688001.SH"]
    _save_sample_plan_with_signals(date, syms)
    acct = "test_acct"

    rc = veto(date, None)

    assert rc == 0
    # 每个标的都写 VETOED（per-trade_id 拦截）
    for s in syms:
        tid = f"{acct}_{s}_{date}"
        assert state_store.get_latest_action(tid) == "VETOED", f"缺 VETOED：{s}"
    # C3：load_plan 据任一 VETOED → confirmed=False
    plan = trading_plan.load_plan(date)
    assert plan["confirmed"] is False


# ---------------------------- veto DB 失败抛错 ----------------------------
def test_veto_db_failure_aborts(tmp_path, monkeypatch):
    """C3：DB 写失败 → veto 抛错退出（绝不假成功，人审刹车红线）。

    物理红线：全自动模式下 pre_open 据放行，若 veto 假成功，研究员以为拦了实际次日直挂
    ——人审刹车失效 = 不可逆实盘敞口。故 DB 失败必须抛错。
    """
    _setup_env(tmp_path, monkeypatch)
    date, sym = "2026-08-05", "300001.SZ"
    _save_sample_plan_with_signals(date, [sym], confirmed=True)

    def boom(*a, **k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(state_store, "insert_trade_event", boom)

    with pytest.raises(RuntimeError):
        veto(date, sym)


# ---------------------------- veto 幂等 ----------------------------
def test_veto_idempotent_repeated_calls_no_error(tmp_path, monkeypatch):
    """C3：重复 veto 同一标的不报错（insert_trade_event UNIQUE 幂等）。"""
    _setup_env(tmp_path, monkeypatch)
    date, sym = "2026-08-05", "300001.SZ"
    _save_sample_plan_with_signals(date, [sym, "300002.SZ"])
    acct = "test_acct"
    trade_id = f"{acct}_{sym}_{date}"

    rc1 = veto(date, sym)
    rc2 = veto(date, sym)  # 幂等再否

    assert rc1 == 0 and rc2 == 0
    assert state_store.get_latest_action(trade_id) == "VETOED"


def test_veto_all_idempotent_repeated_calls(tmp_path, monkeypatch):
    """C3：重复全否不报错（DB 幂等）。"""
    _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    syms = ["300001.SZ", "300002.SZ"]
    _save_sample_plan_with_signals(date, syms)

    rc1 = veto(date, None)
    rc2 = veto(date, None)  # 幂等再否

    assert rc1 == 0 and rc2 == 0


# ---------------------------- legacy shim confirm_plan DB CONFIRMED ----------------------------
def test_confirm_plan_legacy_writes_db_confirmed_for_non_vetoed(tmp_path, monkeypatch):
    """C3：legacy shim confirm_plan_legacy 补 DB CONFIRMED（非 VETOED 的标的）。

    物理：人审钉钉确认触发（生产路径已废，仅测试 legacy shim）；JSON confirmed=true 是
    展示镜像，DB CONFIRMED 是 pre_open 据放行的真相源。
    """
    db, acct = _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    syms = ["300001.SZ", "300002.SZ"]
    _save_sample_plan_with_signals(date, syms, confirmed=False)

    ok = confirm_plan_legacy(date)

    assert ok is True
    for s in syms:
        tid = f"{acct}_{s}_{date}"
        assert state_store.get_latest_action(tid) == "CONFIRMED", f"缺 CONFIRMED：{s}"


# ---------------------------- legacy shim confirm_plan 不覆盖 VETOED（veto 保护） ----------------------------
def test_confirm_plan_legacy_does_not_override_vetoed(tmp_path, monkeypatch):
    """C3：先 veto 再 confirm，VETOED 保留（人审否决不被机器确认覆盖）。"""
    _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    vetoed_sym, ok_sym = "300001.SZ", "300002.SZ"
    _save_sample_plan_with_signals(date, [vetoed_sym, ok_sym], confirmed=True)
    acct = "test_acct"
    vetoed_tid = f"{acct}_{vetoed_sym}_{date}"
    ok_tid = f"{acct}_{ok_sym}_{date}"

    # ① 先单否 vetoed_sym（DB 写 VETOED）
    veto(date, vetoed_sym)
    assert state_store.get_latest_action(vetoed_tid) == "VETOED"

    # ② 再 confirm_plan_legacy（模拟研究员钉钉确认整份计划）
    ok_confirm = confirm_plan_legacy(date)
    assert ok_confirm is True

    # ③ vetoed_sym 的 VETOED 保留（confirm_plan_legacy 不覆盖）
    assert state_store.get_latest_action(vetoed_tid) == "VETOED"
    # ④ ok_sym 的 CONFIRMED 正常写入
    assert state_store.get_latest_action(ok_tid) == "CONFIRMED"


# ---------------------------- legacy shim confirm_plan DB 失败软降级 ----------------------------
def test_confirm_plan_legacy_db_failure_soft_degrades(tmp_path, monkeypatch):
    """C3：legacy shim confirm_plan_legacy DB 写失败软降级（JSON 照写，不阻断人审确认流程）。"""
    _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    _save_sample_plan_with_signals(date, ["300001.SZ"], confirmed=False)

    def boom(*a, **k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(state_store, "insert_trade_event", boom)

    # 不抛错（软降级）
    ok = confirm_plan_legacy(date)

    assert ok is True  # JSON 照写返 True
    plan = json.loads(trading_plan._plan_path(date).read_text(encoding="utf-8"))
    assert plan["confirmed"] is True  # JSON 已确认


# ---------------------------- 边界：无计划 ----------------------------
def test_veto_no_plan_returns_1(tmp_path, monkeypatch):
    """C3：无计划 veto 返 1（CLI sys.exit(1)），不抛错。

    无计划定义：DB 无 SIGNAL 且 JSON 回退也无 → load_plan 返 None。
    """
    _setup_env(tmp_path, monkeypatch)
    assert veto("2099-01-01", "300001.SZ") == 1
    assert veto("2099-01-01", None) == 1


def test_confirm_plan_legacy_no_plan_returns_false(tmp_path, monkeypatch):
    """C3：legacy shim confirm_plan_legacy 无计划返 False（防幻觉确认）。"""
    _setup_env(tmp_path, monkeypatch)
    assert confirm_plan_legacy("2099-01-01") is False
