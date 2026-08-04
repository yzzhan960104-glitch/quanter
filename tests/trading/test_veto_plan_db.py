# -*- coding: utf-8 -*-
"""W2 · veto_plan.veto / confirm_plan DB 双写测试。

物理意图：
    AUTO_CONFIRM_PLAN=true 全自动模式下，eod_plan 落盘即 confirmed=True，pre_open 次日直挂。
    veto 是全自动模式下唯一的人审刹车（opt-out：默认挂，否决才拦）。原 veto 只改 JSON，
    DB trade_event(VETOED) 没写 → pre_open 既有防线（get_latest_action=="VETOED" 跳过挂单）
    形同虚设。本测试验证 veto/confirm 的 DB 双写补丁。

    错误分级（C-4 沿用）：
        - veto DB 写失败 = 抛错退出（人审刹车不能假成功，否则 pre_open 放行）
        - confirm_plan DB 写失败 = 软降级（人审确认不阻断，JSON 已是真相源展示镜像）
"""
import json

import pytest

from trading import state_store, trading_plan
from trading.tools.veto_plan import veto


# ---------------------------- 测试夹具 ----------------------------
def _setup_env(tmp_path, monkeypatch, account_id="test_acct"):
    """统一环境：tmp DB + tmp TRADE_PLAN_DIR + account 行。

    Why monkeypatch _DEFAULT_DB 而非 env：state_store 模块加载时已读 _DEFAULT_DB 常量，
    后续函数默认走该常量；改环境变量不会回填已绑定的默认值。直接 patch 模块属性最稳。
    """
    db = str(tmp_path / "ts.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    plan_dir = tmp_path / "plans"
    monkeypatch.setenv("TRADE_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("QMT_ACCOUNT_ID", account_id)
    state_store.init_store(db)
    state_store.upsert_account(account_id, broker="qmt")
    return db, account_id


def _save_sample_plan(date: str, symbols: list[str], confirmed: bool = True) -> None:
    """落盘嵌套 orders 计划（与 engine.eod_plan 生产侧结构一致）。"""
    orders = [
        {"order": {"symbol": s, "qty": 100, "side": "BUY", "price": 10.0},
         "stop_price": 9.5, "take_profit": 11.0}
        for s in symbols
    ]
    trading_plan.save_plan(date, orders, confirmed=confirmed)


# ---------------------------- veto 单否 ----------------------------
def test_veto_single_writes_db_vetoed(tmp_path, monkeypatch):
    """W2：单否写 DB trade_event(VETOED)，pre_open 既有防线据此跳过。"""
    _setup_env(tmp_path, monkeypatch)
    date, sym = "2026-08-05", "300001.SZ"
    _save_sample_plan(date, [sym])
    acct = "test_acct"
    trade_id = f"{acct}_{sym}_{date}"

    rc = veto(date, sym)

    assert rc == 0  # CLI sys.exit 依赖 0=成功
    # DB 真相源有 VETOED（pre_open _pre_open_impl:869 据此跳过挂单）
    assert state_store.get_latest_action(trade_id) == "VETOED"
    # JSON 镜像同步：orders 删掉该 symbol
    plan = json.loads(trading_plan._plan_path(date).read_text(encoding="utf-8"))
    assert all(o["order"]["symbol"] != sym for o in plan["orders"])


# ---------------------------- veto 全否（批量） ----------------------------
def test_veto_all_writes_db_vetoed_for_every_symbol(tmp_path, monkeypatch):
    """W2：全否（symbol=None）批量写每个 symbol 的 VETOED。

    物理：全否时 confirmed=False 是 plan 级拦截，但 DB VETOED 是标的级镜像——双写
    保证 DB 真相源与 JSON 一致（即使 confirmed 被误改，DB VETOED 仍拦每个标的）。
    """
    _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    syms = ["300001.SZ", "300002.SZ", "688001.SH"]
    _save_sample_plan(date, syms)
    acct = "test_acct"

    rc = veto(date, None)

    assert rc == 0
    plan = json.loads(trading_plan._plan_path(date).read_text(encoding="utf-8"))
    assert plan["confirmed"] is False  # plan 级拦截
    # 每个标的都写 VETOED（标的级镜像，pre_open 防线对每个 trade_id 都拦得住）
    for s in syms:
        tid = f"{acct}_{s}_{date}"
        assert state_store.get_latest_action(tid) == "VETOED", f"缺 VETOED：{s}"


# ---------------------------- veto DB 失败抛错 ----------------------------
def test_veto_db_failure_aborts_without_touching_json(tmp_path, monkeypatch):
    """W2：DB 写失败 → veto 抛错退出，绝不「JSON 改了 DB 没记」。

    物理红线：全自动模式下 pre_open 据放行，若 veto 假成功（JSON 改了 DB 没写），
    研究员以为拦了实际次日直挂——人审刹车失效 = 不可逆实盘敞口。
    DB 必须先写、JSON 后写，失败抛错不碰 JSON。
    """
    _setup_env(tmp_path, monkeypatch)
    date, sym = "2026-08-05", "300001.SZ"
    _save_sample_plan(date, [sym], confirmed=True)

    original_confirmed = json.loads(
        trading_plan._plan_path(date).read_text(encoding="utf-8")
    )["confirmed"]

    def boom(*a, **k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(state_store, "insert_trade_event", boom)

    with pytest.raises(RuntimeError):
        veto(date, sym)

    # JSON 没被碰（confirmed 保持原值，orders 没删）
    plan_after = json.loads(trading_plan._plan_path(date).read_text(encoding="utf-8"))
    assert plan_after["confirmed"] == original_confirmed
    assert any(o["order"]["symbol"] == sym for o in plan_after["orders"])


# ---------------------------- veto 幂等 ----------------------------
def test_veto_idempotent_repeated_calls_no_error(tmp_path, monkeypatch):
    """W2：重复 veto 同一标的不报错（insert_trade_event UNIQUE 幂等）。"""
    _setup_env(tmp_path, monkeypatch)
    date, sym = "2026-08-05", "300001.SZ"
    _save_sample_plan(date, [sym, "300002.SZ"])
    acct = "test_acct"
    trade_id = f"{acct}_{sym}_{date}"

    rc1 = veto(date, sym)
    # 第二次：JSON 里已无该 symbol（单否幂等返 0），DB 重复写幂等不报错
    rc2 = veto(date, sym)

    assert rc1 == 0 and rc2 == 0
    assert state_store.get_latest_action(trade_id) == "VETOED"


def test_veto_all_idempotent_repeated_calls(tmp_path, monkeypatch):
    """W2：重复全否不报错（confirmed 已 False + DB 幂等）。"""
    _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    syms = ["300001.SZ", "300002.SZ"]
    _save_sample_plan(date, syms)

    rc1 = veto(date, None)
    rc2 = veto(date, None)  # 幂等再否

    assert rc1 == 0 and rc2 == 0


# ---------------------------- confirm_plan DB CONFIRMED ----------------------------
def test_confirm_plan_writes_db_confirmed_for_non_vetoed(tmp_path, monkeypatch):
    """W2：confirm_plan 补 DB CONFIRMED（非 VETOED 的标的）。

    物理：人审钉钉确认触发 confirm_plan，JSON confirmed=true 是展示镜像，
    DB CONFIRMED 是 pre_open 据放行的真相源。两写对齐防漂移。
    """
    db, acct = _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    syms = ["300001.SZ", "300002.SZ"]
    _save_sample_plan(date, syms, confirmed=False)

    ok = trading_plan.confirm_plan(date)

    assert ok is True
    plan = json.loads(trading_plan._plan_path(date).read_text(encoding="utf-8"))
    assert plan["confirmed"] is True
    for s in syms:
        tid = f"{acct}_{s}_{date}"
        assert state_store.get_latest_action(tid) == "CONFIRMED", f"缺 CONFIRMED：{s}"


# ---------------------------- confirm_plan 不覆盖 VETOED（veto 保护） ----------------------------
def test_confirm_plan_does_not_override_vetoed(tmp_path, monkeypatch):
    """W2：先 veto 再 confirm，VETOED 保留（人审否决不被机器确认覆盖）。

    物理红线：研究员否决是 opt-out 终局动作。即使后续 confirm_plan 被调（钉钉回复
    「确认」触发 / eod_plan 重跑 auto_confirm），VETOED 必须保留——否则人审刹车
    被覆盖，次日 pre_open 直挂被否标的。
    """
    _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    vetoed_sym, ok_sym = "300001.SZ", "300002.SZ"
    _save_sample_plan(date, [vetoed_sym, ok_sym], confirmed=True)
    acct = "test_acct"
    vetoed_tid = f"{acct}_{vetoed_sym}_{date}"
    ok_tid = f"{acct}_{ok_sym}_{date}"

    # ① 先单否 vetoed_sym（DB 写 VETOED + JSON 删该 order）
    veto(date, vetoed_sym)
    assert state_store.get_latest_action(vetoed_tid) == "VETOED"

    # ② 再 confirm_plan（模拟研究员钉钉确认整份计划）
    ok_confirm = trading_plan.confirm_plan(date)
    assert ok_confirm is True

    # ③ vetoed_sym 的 VETOED 保留（confirm_plan 不覆盖）
    assert state_store.get_latest_action(vetoed_tid) == "VETOED"
    # ④ ok_sym 的 CONFIRMED 正常写入
    assert state_store.get_latest_action(ok_tid) == "CONFIRMED"


# ---------------------------- confirm_plan DB 失败软降级 ----------------------------
def test_confirm_plan_db_failure_soft_degrades_json_still_written(tmp_path, monkeypatch):
    """W2：confirm_plan DB 写失败软降级（JSON 照写，不阻断人审确认流程）。

    物理差异（设计，非不一致）：人审确认是 opt-in，JSON 已是真相源的展示镜像，
    DB 下次 eod 补；不阻断人审流程。与 veto 抛错语义形成对比——veto 是刹车，
    假成功=放行；confirm 是放行，假失败=人审流程被阻（更糟）。
    """
    _setup_env(tmp_path, monkeypatch)
    date = "2026-08-05"
    _save_sample_plan(date, ["300001.SZ"], confirmed=False)

    def boom(*a, **k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(state_store, "insert_trade_event", boom)

    # 不抛错（软降级）
    ok = trading_plan.confirm_plan(date)

    assert ok is True  # JSON 照写返 True
    plan = json.loads(trading_plan._plan_path(date).read_text(encoding="utf-8"))
    assert plan["confirmed"] is True  # JSON 已确认


# ---------------------------- 边界：无计划 ----------------------------
def test_veto_no_plan_returns_1(tmp_path, monkeypatch):
    """W2：无计划 veto 返 1（CLI sys.exit(1)），不抛错。"""
    _setup_env(tmp_path, monkeypatch)
    assert veto("2099-01-01", "300001.SZ") == 1
    assert veto("2099-01-01", None) == 1


def test_confirm_plan_no_plan_returns_false(tmp_path, monkeypatch):
    """W2：无计划 confirm 返 False（防幻觉确认）。"""
    _setup_env(tmp_path, monkeypatch)
    assert trading_plan.confirm_plan("2099-01-01") is False
