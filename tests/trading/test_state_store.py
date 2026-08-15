# -*- coding: utf-8 -*-
"""state_store 单测：统一交易状态库（6 张表 + 幂等写入 + 查询）。

物理意图（trading-state-store-redesign spec §2）：把散落在 gw._orders 内存 /
_tp_placed 内存 / position_book / live_trades.csv / trading_plan JSON 的交易状态，
收口到一个事务一致、跨重启、幂等保护的 SQLite 交易状态库。本测试覆盖：
- T1: 6 张表建表 + schema 迁移（account/trade_event/order/fill/position/account_daily）+ FK 引用完整性
- T2: account 表 CRUD + .env 迁移
- T3: trade_event / order / fill 幂等写入（UNIQUE 冲突返 False）
- T4: position（加权 + 归零）+ account_daily（快照 + daily_pnl）
- T5: 查询接口（has_order / get_active_trades / get_pending_orders / get_trade_plan / get_entry_dates / get_latest_action）

约定（对齐 test_position_book.py 风格）：file-based sqlite + per-file _DEFAULT_DB fixture，
async 测试用 asyncio.run（本模块全 sync，无 async）。全中文注释。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from trading import state_store


@pytest.fixture
def db(tmp_path, monkeypatch):
    """每个测试用独立 tmp db（隔离），patch _DEFAULT_DB 让 engine 间接调用也命中 tmp。

    先 init_store 建 6 张表（state_store 是真相源，position_book 的表由 state_store 统一建）。
    """
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    return db_path


def _table_cols(con, table: str) -> set[str]:
    """读表列名集合（PRAGMA table_info）。"""
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(con) -> set[str]:
    """读所有用户表名。"""
    return {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


# ============================= T1：6 张表建表 + FK 引用完整性 =============================

def test_init_store_creates_6_tables(db):
    """init_store 后 6 张表存在（account/trade_event/order/fill/position/account_daily）。"""
    with sqlite3.connect(db) as con:
        tables = _tables(con)
    expected = {"account", "trade_event", "order", "fill", "position", "account_daily"}
    assert expected <= tables, f"缺失表: {expected - tables}"


def test_fill_table_has_account_id(db):
    """fill 表有 account_id 列（spec §7.1 迁移：ALTER ADD COLUMN 兼容既有 fill 数据）。"""
    with sqlite3.connect(db) as con:
        cols = _table_cols(con, "fill")
    assert "account_id" in cols


def test_position_pk_is_composite(db):
    """position PK = (account_id, symbol)（spec §2.2 ⑤：多账户隔离的复合主键）。"""
    with sqlite3.connect(db) as con:
        pk_cols = {
            r[1] for r in con.execute("PRAGMA table_info(position)").fetchall() if r[5] != 0
        }
    # PRAGMA pk 列名（组合键两列均非 0，标记主键序号）
    assert "account_id" in pk_cols
    assert "symbol" in pk_cols


def test_order_idempotent_unique(db):
    """order 表 UNIQUE(account_id, trade_date, symbol, purpose)——重复挂单幂等键存在。"""
    with sqlite3.connect(db) as con:
        # 先插一个 account（FK 引用完整性需要）
        con.execute("INSERT INTO account(account_id, broker, created_at) VALUES('ACC1','qmt','now')")
        con.commit()
        # 同 (account_id, trade_date, symbol, purpose) 第二次插应触发 UNIQUE 冲突
        base = "INSERT INTO \"order\"(order_id, trade_id, account_id, trade_date, symbol, side, purpose, qty, price) VALUES(?, ?, 'ACC1', '2026-07-30', '600000.SH', 'buy', 'OPEN', 100, 10.0)"
        con.execute(base, ("o1", "ACC1_600000.SH_2026-07-30"))
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(base, ("o2", "ACC1_600000.SH_2026-07-30"))  # 同幂等键不同 order_id → 冲突
            con.commit()


def test_foreign_keys_enforced(db):
    """trade_event 引用不存在的 account_id → IntegrityError（PRAGMA foreign_keys=ON 生效）。"""
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA foreign_keys=ON")  # sqlite3 默认 per-connection 关 FK，显式开
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO trade_event(account_id, trade_id, symbol, action, timestamp)"
                " VALUES('GHOST', 'GHOST_X_2026', 'X', 'SIGNAL', 'now')"
            )
            con.commit()


# ============================= T2：account 表 CRUD + .env 迁移 =============================

def test_upsert_account_idempotent(db):
    """upsert_account 同 account_id 覆盖不报错（INSERT OR REPLACE 幂等）。"""
    state_store.upsert_account("ACC1", broker="qmt", name="东北模拟盘")
    state_store.upsert_account("ACC1", broker="qmt", name="东北模拟盘改")  # 覆盖
    acc = state_store.get_account("ACC1")
    assert acc["account_id"] == "ACC1"
    assert acc["broker"] == "qmt"
    assert acc["name"] == "东北模拟盘改"


def test_get_account_returns_none_if_missing(db):
    """读不存在的 account_id → None。"""
    assert state_store.get_account("NOPE") is None


def test_migrate_env_to_account(monkeypatch, db):
    """mock env QMT_* → _migrate_env_to_account 写入 account 表，读取一致。"""
    monkeypatch.setenv("QMT_ACCOUNT_ID", "12345")
    monkeypatch.setenv("QMT_USERDATA_PATH", "C:/userdata")
    monkeypatch.setenv("QMT_SESSION_ID", "999")
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    state_store._migrate_env_to_account()
    acc = state_store.get_account("12345")
    assert acc is not None
    assert acc["userdata_path"] == "C:/userdata"
    assert acc["session_id"] == 999
    assert acc["mode"] == "live"


# ============================= T3：trade_event / order / fill 幂等写入 =============================

def test_insert_trade_event_idempotent(db):
    """同 (account_id, trade_id, action) 重插 → 返 False（UNIQUE 冲突幂等）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    ok1 = state_store.insert_trade_event("ACC1", "ACC1_X_2026", "600000.SH", "SIGNAL")
    ok2 = state_store.insert_trade_event("ACC1", "ACC1_X_2026", "600000.SH", "SIGNAL")
    assert ok1 is True
    assert ok2 is False  # 幂等跳过


def test_insert_order_idempotent(db):
    """同 (account_id, trade_date, symbol, purpose) 重插 → 返 False（重复挂单幂等）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    ok1 = state_store.insert_order(
        "o1", "ACC1_X_2026", "ACC1", "2026-07-30", "600000.SH", "buy", "OPEN", 100, 10.0)
    ok2 = state_store.insert_order(
        "o2", "ACC1_X_2026", "ACC1", "2026-07-30", "600000.SH", "OPEN", 100, 10.0) if False else \
        state_store.insert_order(
            "o3", "ACC1_X_2026", "ACC1", "2026-07-30", "600000.SH", "buy", "OPEN", 100, 10.0)
    assert ok1 is True
    assert ok2 is False


def test_insert_fill_idempotent(db):
    """同 (order_id, traded_time) 重插 → 返 False（成交回报重推幂等）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.insert_order("o1", "ACC1_X_2026", "ACC1", "2026-07-30", "600000.SH", "buy", "OPEN", 100, 10.0)
    ok1 = state_store.insert_fill("o1", "ACC1", "09:30:00", "600000.SH", "BUY", 100, 10.0)
    ok2 = state_store.insert_fill("o1", "ACC1", "09:30:00", "600000.SH", "BUY", 100, 10.0)
    assert ok1 is True
    assert ok2 is False


def test_insert_trade_event_signal_with_meta(db):
    """SIGNAL action 带 meta JSON（计划参数快照，后续 get_trade_plan 读）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    meta = {"stop_price": 9.5, "tp1": 11.0, "take_profit": 12.0}
    state_store.insert_trade_event(
        "ACC1", "ACC1_X_2026", "600000.SH", "SIGNAL", meta=json.dumps(meta))
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT meta FROM trade_event WHERE action='SIGNAL' AND trade_id='ACC1_X_2026'"
        ).fetchone()
    assert json.loads(row[0]) == meta


# ============================= T4：position / account_daily 读写 =============================

def test_apply_fill_to_position_buy_weighted(db):
    """BUY 100@10 + 100@12 → avg=11.0；SELL avg 不变（A 股口径）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 10.0, "09:30:00")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 12.0, "10:00:00")
    pos = state_store.get_position("ACC1", "600000.SH")
    assert pos["qty"] == pytest.approx(200.0)
    assert pos["avg_price"] == pytest.approx(11.0, abs=0.01)
    # SELL 不动 avg
    state_store.apply_fill_to_position("ACC1", "600000.SH", "SELL", 100, 11.5, "11:00:00")
    pos = state_store.get_position("ACC1", "600000.SH")
    assert pos["avg_price"] == pytest.approx(11.0, abs=0.01)
    assert pos["qty"] == pytest.approx(100.0)


def test_apply_fill_to_position_zero_clears(db):
    """归零 → position 行删除（对账并集不被 0 干扰）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 10.0, "09:30:00")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "SELL", 100, 11.0, "15:00:00")
    assert state_store.get_position("ACC1", "600000.SH") is None


# ============================================================================
# 新债清偿 N-T2（2026-08-16）：entry_date 取成交日（traded_time 解析）而非写入日
# ============================================================================
def test_entry_date_cross_midnight_takes_traded_day(db, monkeypatch):
    """跨午夜盘后写入：traded_time=昨日 23:59 → entry_date=昨日（成交日，非写入日）。

    物理意图（tech-debt #6 2026-08-15 Medium）：盘后落账（如 00:05 补写 T-1 日
    23:59 的成交回报）时，entry_date 若取 clock.today()（写入日）会让建仓日漂一天
    ——holding_days（超期平仓基准 pretrade_date 消费 entry_date）与持仓周期归因
    全链被污染。冻结 clock 至 2026-07-03 00:05（跨午夜盘后写入时点），成交回报
    traded_time="2026-07-02 23:59:00"（空格分隔态）→ entry_date 必须锁成交日
    2026-07-02（修复前会错锁写入日 2026-07-03 → 本测试红）。
    """
    from datetime import datetime

    from trading import clock
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 7, 3, 0, 5))
    state_store.upsert_account("ACC1", broker="qmt")  # FK：position.account_id 引用 account
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 10.0,
                                       "2026-07-02 23:59:00")
    pos = state_store.get_position("ACC1", "600000.SH")
    assert pos["entry_date"] == "2026-07-02", (
        f"建仓日应锁成交日 2026-07-02，实取 {pos['entry_date']}（跨午夜漂移复发）")


def test_entry_date_from_traded_time_four_states():
    """_entry_date_from_traded_time 四态解析：ISO T / 空格分隔 / 14 位数字 / 纯时间→None。

    18 个调用点实存四态（2026-08-16 勘探实证）。解析失败必须返 None 而非抛错——
    纯时间态（单测传参 "15:00:00"）是合法入参，由调用方回退 clock.today() 兜底
    （回退口径见 test_entry_date_falls_back_to_today_on_pure_time）。
    """
    f = state_store._entry_date_from_traded_time
    # 态① ISO T 分隔（engine 成交回报路径）
    assert f("2026-07-02T10:00:00") == "2026-07-02"
    # 态② 空格分隔（e2e / gateway 成交回报路径）
    assert f("2026-07-02 09:25:00") == "2026-07-02"
    # 态③ 14 位数字 YYYYMMDDHHMMSS（fill 表 traded_time 契约口径，backfill 落库格式）
    assert f("20260702092500") == "2026-07-02"
    # 态④ 纯时间（单测传参）→ None（解析失败信号，非异常）
    assert f("15:00:00") is None
    # 边界防御：空串 / None / 垃圾串 → None——容错解析不得抛错打断成交落账（红线）
    assert f("") is None
    assert f(None) is None
    assert f("garbage") is None
    # 8 位日期不属四态（N5 补钉）：长度 <14 的数字串走 None → 兜底写入日，
    # 不得截前 8 位重组（防止把半截数字串当日期）。
    assert f("20260728") is None
    # 形状校验钉死（N5 补钉）：垃圾头 + 合法日期尾巴的混种串不得被「取前 10 位」
    # 截出脏日期——"abc-2026-07-02" 的 head[4] 非 "-"，形状闸必须拦下返 None。
    assert f("abc-2026-07-02") is None
    # 全角数字（N5 isascii 守卫）：isdigit() 对全角也返 True——不设 isascii 前置
    # 会截出「２０２６-０７-０２」全角脏日期（下游字符串比较全错位且肉眼难辨）。
    assert f("２０２６０７０２０９２５００") is None


def test_entry_date_falls_back_to_today_on_pure_time(db, monkeypatch):
    """纯时间态回退保护：解析失败 → entry_date=clock.today()（旧行为兼容）。

    Why：tests/trading/test_e2e_trading_flow.py:797 传 "15:00:00" 纯时间（8 位日期
    "20260728" 同理不属四态）——回退保证既有单测口径不破：解析失败不抛错、不写
    脏值，落写入日兜底（与修复前行为完全一致）。
    """
    from datetime import datetime

    from trading import clock
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 7, 3, 0, 5))
    state_store.upsert_account("ACC1", broker="qmt")  # FK：position.account_id 引用 account
    state_store.apply_fill_to_position("ACC1", "600001.SH", "BUY", 100, 10.0, "15:00:00")
    pos = state_store.get_position("ACC1", "600001.SH")
    assert pos["entry_date"] == "2026-07-03"  # clock.today() 兜底（冻结口径下的写入日）


def test_snapshot_start_equity_idempotent(db):
    """INSERT OR REPLACE 同日覆盖（pre_open 崩溃重入安全）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.snapshot_start_equity("ACC1", "2026-07-30", 1_000_000.0, 500_000.0)
    state_store.snapshot_start_equity("ACC1", "2026-07-30", 999_000.0, 499_000.0)
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT start_total_asset FROM account_daily WHERE account_id='ACC1' AND date='2026-07-30'"
        ).fetchone()
    assert row[0] == 999_000.0


def test_snapshot_close_equity_pnl(db):
    """snapshot_close_equity：close_total - start_total = daily_pnl（写收盘快照 + 盈亏）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.snapshot_start_equity("ACC1", "2026-07-30", 1_000_000.0, 500_000.0)
    state_store.snapshot_close_equity(
        "ACC1", "2026-07-30", close_total_asset=1_020_000.0, close_cash=510_000.0,
        close_market_value=510_000.0)
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT daily_pnl, daily_pnl_pct FROM account_daily WHERE account_id='ACC1' AND date='2026-07-30'"
        ).fetchone()
    assert row[0] == pytest.approx(20_000.0)  # 1020000 - 1000000
    assert row[1] == pytest.approx(0.02, abs=1e-6)  # 2%


# ============================= T5：查询接口 =============================

def _seed_for_queries(db):
    """T5 查询测试共用种子数据：建账户 + 一个 OPEN 委托已挂 + SIGNAL 事件。"""
    state_store.upsert_account("ACC1", broker="qmt")
    meta = {"stop_price": 9.5, "tp1": 11.0, "take_profit": 12.0, "atr": 0.4}
    state_store.insert_trade_event(
        "ACC1", "ACC1_600000.SH_2026-07-30", "600000.SH", "SIGNAL", meta=json.dumps(meta))
    state_store.insert_trade_event(
        "ACC1", "ACC1_600000.SH_2026-07-30", "600000.SH", "CONFIRMED")
    state_store.insert_order(
        "o1", "ACC1_600000.SH_2026-07-30", "ACC1", "2026-07-30", "600000.SH", "buy", "OPEN",
        100, 10.0, state="SUBMITTED")


def test_has_order_true_false(db):
    """已挂 OPEN → True；未挂 STOP → False。"""
    _seed_for_queries(db)
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is True
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "STOP") is False


def test_has_order_filters_dead_states(db):
    """C-1 final-review (I-2/?-1)：REJECTED/FAILED/CANCELLED 死态不算已挂，允许重挂。

    防止挂单被拒（资金不足/涨跌停挡板）后 has_order 恒 True → 永久漏挂（pre_open OPEN）
    / 裸奔（stop_loss STOP 被拒不再发卖）/ 永不补挂（TP 被拒）。live 真金致命。
    """
    _seed_for_queries(db)  # o1 OPEN SUBMITTED → has_order True
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is True
    # 三种死态 → has_order False（可重挂）
    for _dead in ("REJECTED", "FAILED", "CANCELLED"):
        state_store.update_order_state("o1", _dead)
        assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is False, _dead
    # 活态恢复（SUBMITTED）→ True
    state_store.update_order_state("o1", "SUBMITTED")
    assert state_store.has_order("ACC1", "2026-07-30", "600000.SH", "OPEN") is True


def test_get_active_trades(db):
    """最新 action 非终态（CLOSED/EXPIRED/VETOED）的 trade 列表。"""
    _seed_for_queries(db)
    # 另一个已 CLOSED 的 trade 不应出现
    state_store.insert_trade_event(
        "ACC1", "ACC1_688001.SH_2026-07-30", "688001.SH", "SIGNAL")
    state_store.insert_trade_event(
        "ACC1", "ACC1_688001.SH_2026-07-30", "688001.SH", "CLOSED")
    active = state_store.get_active_trades("ACC1")
    trade_ids = {t["trade_id"] for t in active}
    assert "ACC1_600000.SH_2026-07-30" in trade_ids
    assert "ACC1_688001.SH_2026-07-30" not in trade_ids


def test_get_pending_orders(db):
    """state IN (PENDING/SUBMITTED/PARTIAL) 的 order（撤单用）。"""
    _seed_for_queries(db)
    # 加一个 FILLED 的（终态，不应返回）
    state_store.insert_order(
        "o2", "ACC1_600001.SH_2026-07-30", "ACC1", "2026-07-30", "600001.SH", "buy", "OPEN",
        100, 10.0, state="FILLED")
    pending = state_store.get_pending_orders("ACC1")
    order_ids = {o["order_id"] for o in pending}
    assert "o1" in order_ids  # SUBMITTED
    assert "o2" not in order_ids  # FILLED 终态


def test_get_trade_plan_from_signal(db):
    """读 trade_event SIGNAL 行的 meta JSON（plan 参数，stop_loss/pre_open 用）。"""
    _seed_for_queries(db)
    plan = state_store.get_trade_plan("ACC1_600000.SH_2026-07-30")
    assert plan is not None
    assert plan["stop_price"] == 9.5
    assert plan["tp1"] == 11.0


def test_get_entry_dates(db):
    """position entry_date 字典（max_holding/trailing 用）。"""
    state_store.upsert_account("ACC1", broker="qmt")
    state_store.apply_fill_to_position("ACC1", "600000.SH", "BUY", 100, 10.0, "09:30:00")
    entries = state_store.get_entry_dates("ACC1")
    assert "600000.SH" in entries


def test_get_latest_action(db):
    """某 trade_id 的最新 action（当前状态）。"""
    _seed_for_queries(db)
    assert state_store.get_latest_action("ACC1_600000.SH_2026-07-30") == "CONFIRMED"


def test_is_vetoed_single_point(db):
    """M2 is_vetoed 单点：vetoed→True；CONFIRMED（未否决）→False；无事件（None）→False。

    物理意图：eod_plan 自动确认闸（auto_confirmed 分支）原散落 ``!= "VETOED"`` 字面量，
    收口为本单点后须钉死三种边界——尤其 None 安全（无任何事件 = 未否决 = 可写 CONFIRMED，
    与旧字面量 ``None != "VETOED"`` → True 语义一致，不改 eod_plan 行为）。
    """
    # FK 前置：trade_event.account_id 引用 account 表（缺行时 insert_trade_event 吞
    # IntegrityError 返 False，事件静默不落盘 → 测不出真语义）。
    state_store.upsert_account("ACC1", broker="qmt")
    tid_vetoed = "ACC1_600000.SH_2026-07-30"
    state_store.insert_trade_event("ACC1", tid_vetoed, "600000.SH", "SIGNAL")
    state_store.insert_trade_event("ACC1", tid_vetoed, "600000.SH", "VETOED")
    assert state_store.is_vetoed(tid_vetoed) is True

    tid_ok = "ACC1_600001.SH_2026-07-30"
    state_store.insert_trade_event("ACC1", tid_ok, "600001.SH", "SIGNAL")
    state_store.insert_trade_event("ACC1", tid_ok, "600001.SH", "CONFIRMED")
    assert state_store.is_vetoed(tid_ok) is False

    # None 安全：完全不存在的 trade_id（get_latest_action 返 None）
    assert state_store.is_vetoed("ACC1_999999.SH_2026-07-30") is False

# ============================================================================
# SSoT Phase A · Task A1：fill 表加 strategy 列（新断点-4，保 digest 过滤口径）
# ============================================================================
def test_insert_fill_with_strategy(tmp_db):
    """fill 表 strategy 列落盘校验（A1 新断点-4，保 digest 过滤口径）。

    物理意图：digest/filter 等消费端按 strategy 过滤成交流水时，需要 fill 表本身
    持久化 strategy 字段。本测试断点切在「insert_fill 调用是否接 strategy 参数 +
    是否写入 fill.strategy 列」——一旦签名漏加或 INSERT 漏字段，断言 row["strategy"]
    会 KeyError 或 None，立刻暴露。
    """
    from trading import state_store
    ok = state_store.insert_fill("O1", "ACC_TEST", "20260805101000", "600000.SH",
                                 "BUY", 100, 10.0, strategy="neckline")
    assert ok is True
    import sqlite3
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    row = con.execute("SELECT strategy FROM fill WHERE order_id='O1'").fetchone()
    assert row is not None
    assert row["strategy"] == "neckline"


def test_insert_fill_strategy_optional_default_null(tmp_db):
    """strategy 参数可选，缺省写 NULL（向后兼容既有调用方）。

    物理意图：engine 既有 insert_fill 调用（pre_open 自动下单路径）会逐步迁到 A1
    后续 task 补 strategy；过渡期缺省必须落 NULL 而非报错，保证迁移非破坏式。
    """
    from trading import state_store
    ok = state_store.insert_fill("O2", "ACC_TEST", "20260805101001", "600000.SH",
                                 "BUY", 100, 10.0)  # 不传 strategy
    assert ok is True
    import sqlite3
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    row = con.execute("SELECT strategy FROM fill WHERE order_id='O2'").fetchone()
    assert row is not None
    assert row["strategy"] is None  # 缺省 NULL


def test_insert_fill_rejects_non_canonical_direction(tmp_db):
    """CR-5：insert_fill 入口校验 direction 仅 BUY/SELL（脏值 ValueError 快速失败）。

    物理意图：fill.direction 即将有 DB 层 CHECK（CR-5 schema 收口）——若无入口
    校验，脏 direction（如小写 'buy'）会撞 DB CHECK 走 IntegrityError→返 False
    分支，被调用方误当「重复成交」静默吞掉（审计断链）；入口先显式 ValueError
    （与 apply_fill_to_position 同款先例），让脏值在写入侧立刻暴露而非静默降级。
    """
    from trading import state_store
    with pytest.raises(ValueError):
        state_store.insert_fill("O9", "ACC_TEST", "20260805101009", "600000.SH",
                                "buy", 100, 10.0)  # 小写脏值
    with pytest.raises(ValueError):
        state_store.insert_fill("O10", "ACC_TEST", "20260805101010", "600000.SH",
                                "TRADE", 100, 10.0)  # 非 BUY/SELL


def test_init_store_strategy_column_migration_idempotent(tmp_db):
    """init_store 多次跑 strategy 列迁移不报错（幂等，ALTER TABLE IF NOT EXISTS 语义）。

    物理意图：engine 启动期会重复调 init_store（boot/lifespan/补跑等多入口），
    _has_column 守卫必须挡住重复 ALTER，否则第二次 ALTER TABLE ADD COLUMN 会抛
    duplicate column name 致 boot 崩。
    """
    from trading import state_store
    # tmp_db fixture 首次 init_store 已建表；再调两次应不报错
    state_store.init_store(tmp_db)
    state_store.init_store(tmp_db)
    import sqlite3
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    cols = {r["name"] for r in con.execute("PRAGMA table_info(fill)").fetchall()}
    assert "strategy" in cols


# ============================================================================
# Task D1（live-mainchain-fixes）：get_order_placed_qty（止盈差额补挂）
# ============================================================================
def test_get_order_placed_qty_excludes_terminal(monkeypatch, tmp_path):
    """get_order_placed_qty：只合计未终态 TP 行（REJECTED/CANCELLED 不计）。"""
    from trading import state_store

    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(tmp_path / "state.db"))
    state_store.init_store()
    aid, d, sym = "TEST_ACC", "2026-08-01", "600000.SH"
    state_store.upsert_account(aid, broker="qmt")
    state_store.insert_order(f"{d}_{sym}_TP2_1", f"{aid}_{sym}_{d}", aid, d, sym, "sell", "TP2", 100, 11.0, state="SUBMITTED")
    state_store.insert_order(f"{d}_{sym}_TP2_2", f"{aid}_{sym}_{d}", aid, d, sym, "sell", "TP2", 100, 11.0, state="REJECTED")
    assert state_store.get_order_placed_qty(aid, d, sym, "TP2") == 100.0


# ============================================================================
# SSoT Phase B · B2a-1：持仓归因落 position 表 strategy/entry_rationale 列
# ============================================================================
# 物理意图（spec §5 B2 断点-3）：
#   原持仓归因存 trading_service._position_attribution 内存字典——进程重启即丢，
#   且与持仓真相源（position 表）分立两处，对账时无法回答「这只票是哪个策略建的仓」。
#   B2 把归因落到 position 表（与 qty/avg_price 同行），重启后归因随持仓行存活。
#   断点-3：B2 只做「落列 + upsert/clear」，不做重启重建（C1 从 SIGNAL.meta 补 rebuild）。
def test_position_attribution_upsert_clear(tmp_db):
    """upsert_position_attribution / clear_position_attribution + get_position 返新列。

    验收口径（brief B2a-1）：
        - BUY 建仓后 upsert 归因 → get_position 返 strategy/entry_rationale
        - clear → 归因置 NULL（持仓行仍在）
    """
    from trading import state_store
    # 先建仓（apply_fill_to_position BUY 100 @ 10.0，date 口径 YYYYMMDD 无横线）
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "BUY", 100, 10.0, "20260805")
    # 落归因
    state_store.upsert_position_attribution("ACC_TEST", "600000.SH", "neckline", "颈线突破")
    row = state_store.get_position("ACC_TEST", "600000.SH")
    assert row is not None
    assert row["strategy"] == "neckline"
    assert row["entry_rationale"] == "颈线突破"
    # 清归因（持仓行不删，仅 strategy/entry_rationale 置 NULL）
    state_store.clear_position_attribution("ACC_TEST", "600000.SH")
    row2 = state_store.get_position("ACC_TEST", "600000.SH")
    assert row2 is not None
    assert row2["strategy"] is None
    assert row2["entry_rationale"] is None


def test_position_attribution_migration_adds_columns(tmp_db):
    """旧库 position 表无 strategy/entry_rationale 列 → init_store ALTER ADD COLUMN 迁移。

    物理意图（向後兼容红线）：生产库已存在 position 表（无 strategy/entry_rationale 列），
    升级到 B2 后 init_store 必须用 ALTER ADD COLUMN 补列（参考 fill.account_id 范式），
    不能 DROP 重建（DROP 会丢既有持仓数据 = 敞口真相失真红线）。
    """
    import sqlite3
    con = sqlite3.connect(tmp_db); con.row_factory = sqlite3.Row
    cols = {r["name"] for r in con.execute("PRAGMA table_info(position)").fetchall()}
    con.close()
    assert "strategy" in cols, "position 表缺 strategy 列（迁移未执行）"
    assert "entry_rationale" in cols, "position 表缺 entry_rationale 列（迁移未执行）"


def test_position_attribution_sell_clears_via_row_delete(tmp_db):
    """SELL 归零 → apply_fill_to_position 删 position 行 → 归因随行消失（不调 clear）。

    断点-3 Resolution（代码事实优先）：
        apply_fill_to_position 在 SELL 归零时执行 ``DELETE FROM position WHERE qty=0``
        （state_store.py:676），归因随行消失——clear_position_attribution 会 UPDATE 0 行
        （空操作）。验收口径：**position 行删除即归因消失（非 clear 调用）**。
    """
    from trading import state_store
    # 建仓 + 落归因
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "BUY", 100, 10.0, "20260805")
    state_store.upsert_position_attribution("ACC_TEST", "600000.SH", "neckline", "颈线突破")
    assert state_store.get_position("ACC_TEST", "600000.SH") is not None
    # SELL 平仓（同量反向）→ apply_fill_to_position 归零删行
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "SELL", 100, 11.0, "20260805")
    # 持仓行被删 → 归因随行消失（get_position 返 None）
    assert state_store.get_position("ACC_TEST", "600000.SH") is None


# ============================================================================
# SSoT Phase C · C1：归因重建（从 SIGNAL.meta 读真实 strategy_name 回填 position）
# ============================================================================
# 物理意图（spec §5 断点-3 弥补）：
#   B2 把归因落到 position 表（重启存活），但**只落列 + upsert/clear，不做重启重建**——
#   进程重启窗口内 BUY 成交（apply_fill_to_position 建行）+ B2 归因未及写就崩，position 行
#   strategy IS NULL 裸奔。C1 从 trade_event(SIGNAL).meta 反查真实 strategy_name/rationale
#   回填，弥补 B2 的重启丢失窗口。
#   红线：IS NULL 守卫——只回填 strategy IS NULL 的行，绝不覆盖 B2 已写归因（人工/算法已写）。
def test_rebuild_position_attribution_reads_real_meta(tmp_db):
    """rebuild 从 SIGNAL.meta 真实 strategy_name 回填（C1，读真实字段非默认 neckline）。

    验收口径（brief Step 2 红线）：
        - apply_fill_to_position 建行（strategy IS NULL）
        - 插真实 shape SIGNAL.meta（C1 补字段：plan_date/strategy_name/rationale）
        - rebuild 读 meta.strategy_name 真实值回填 position（非写死 "neckline"）
        - 返回回填行数 = 1
    """
    from trading import state_store
    import json
    # 建仓（apply_fill_to_position 建 position 行，strategy IS NULL）
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "BUY", 100, 10.0, "20260805")
    # 真实 shape SIGNAL.meta（engine.py:605-639 order_dict + C1 补字段）
    trade_id = state_store.build_trade_id("ACC_TEST", "600000.SH", "2026-08-05")
    state_store.insert_trade_event(
        "ACC_TEST", trade_id, "600000.SH", "SIGNAL",
        meta=json.dumps({"order": {"symbol": "600000.SH", "qty": 100, "side": "BUY", "price": 10.0},
                         "stop_price": 9.5, "take_profit": 11.5, "neckline": 10.5,
                         "formed_at": "2026-08-04",
                         "plan_date": "2026-08-05", "strategy_name": "neckline",
                         "rationale": "颈线法@2026-08-04"}))
    n = state_store.rebuild_position_attribution("ACC_TEST")
    assert n == 1
    row = state_store.get_position("ACC_TEST", "600000.SH")
    assert row is not None
    # 读 meta 真实值（非默认）—— 若 meta strategy_name 改值，回填跟着改（test_rebuild_reads_alternative_meta 验证）
    assert row["strategy"] == "neckline"
    assert row["entry_rationale"] == "颈线法@2026-08-04"


def test_rebuild_reads_alternative_strategy_name(tmp_db):
    """rebuild 读 meta 真实 strategy_name（非写死 neckline 兜底）—— 字段切值验证。

    物理意图：C1 简报红线「读真实 strategy_name（非默认）」。若 rebuild 写死 "neckline"，
    本测试会失败（meta strategy_name='momentum' 但回填 'neckline'）。读真实字段是多策略
    扩展的物理基础（未来多策略并存时，归因必须随 SIGNAL.meta 真实值，不能全归 neckline）。
    """
    from trading import state_store
    import json
    state_store.apply_fill_to_position("ACC_TEST", "000001.SZ", "BUY", 200, 15.0, "20260805")
    trade_id = state_store.build_trade_id("ACC_TEST", "000001.SZ", "2026-08-05")
    # 故意把 strategy_name 写成 "momentum"（非默认 neckline）
    state_store.insert_trade_event(
        "ACC_TEST", trade_id, "000001.SZ", "SIGNAL",
        meta=json.dumps({"formed_at": "2026-08-04", "plan_date": "2026-08-05",
                         "strategy_name": "momentum", "rationale": "动量突破@2026-08-04"}))
    state_store.rebuild_position_attribution("ACC_TEST")
    row = state_store.get_position("ACC_TEST", "000001.SZ")
    assert row is not None
    assert row["strategy"] == "momentum"  # 读真实值，非写死 neckline
    assert row["entry_rationale"] == "动量突破@2026-08-04"


def test_rebuild_skips_already_attributed(tmp_db):
    """已写归因的行（strategy IS NOT NULL）不被覆盖（IS NULL 守卫红线）。

    验收口径（brief Step 2 红线）：
        - apply_fill_to_position 建行 → upsert 写 "manual" 归因（模拟 B2 已写或人工标注）
        - 插 SIGNAL.meta（strategy_name="neckline"）
        - rebuild IS NULL 守卫：UPDATE 命中 0 行（"manual" 行不覆盖）
        - get_position strategy 仍是 "manual"（不被改写为 "neckline"）
    物理意图：B2 落列 + 算法/人工已写归因的行，C1 rebuild 必须保留原值——否则
        重启补扫会把人工标注的 "manual" 覆盖为 SIGNAL.meta 的 "neckline"，归因失真。
    """
    from trading import state_store
    import json
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "BUY", 100, 10.0, "20260805")
    # B2 已写归因（人工/算法路径）
    state_store.upsert_position_attribution("ACC_TEST", "600000.SH", "manual", "人工标注")
    state_store.insert_trade_event(
        "ACC_TEST",
        state_store.build_trade_id("ACC_TEST", "600000.SH", "2026-08-05"),
        "600000.SH", "SIGNAL",
        meta=json.dumps({"strategy_name": "neckline", "formed_at": "2026-08-04",
                         "rationale": "颈线法@2026-08-04"}))
    state_store.rebuild_position_attribution("ACC_TEST")
    row = state_store.get_position("ACC_TEST", "600000.SH")
    assert row is not None
    assert row["strategy"] == "manual"  # IS NULL 守卫：不覆盖已写归因
    assert row["entry_rationale"] == "人工标注"


def test_rebuild_skips_position_without_signal(tmp_db):
    """rebuild 对无 SIGNAL.meta 的持仓行跳过（不报错、不回填、不计入回填行数）。

    物理意图：position 行可能由历史数据迁移/手动建仓（无 SIGNAL 事件）产生，rebuild
        找不到对应 SIGNAL.meta 必须静默跳过（continue），不能 raise 中断整批回填。
        多策略/手动建仓的归因由各自路径处理，不依赖 SIGNAL.meta 回填。
    """
    from trading import state_store
    # 建仓但无 SIGNAL 事件
    state_store.apply_fill_to_position("ACC_TEST", "600000.SH", "BUY", 100, 10.0, "20260805")
    n = state_store.rebuild_position_attribution("ACC_TEST")
    assert n == 0  # 无 SIGNAL → 跳过，回填 0 行
    row = state_store.get_position("ACC_TEST", "600000.SH")
    assert row is not None
    assert row["strategy"] is None  # 不回填，保持 NULL


# ============ C2d：list_signals_with_meta_by_plan_date_range（致命日期轴）============


def _seed_signal(tmp_db, symbol, plan_date, *, experiment_id=None):
    """落一行 trade_event(SIGNAL) helper（C2d 测试共用）。"""
    import json
    from trading import state_store
    tid = state_store.build_trade_id("ACC_TEST", symbol, plan_date)
    meta = {"plan_date": plan_date, "stop_price": 9.0}
    if experiment_id:
        meta["experiment_id"] = experiment_id
    state_store.insert_trade_event("ACC_TEST", tid, symbol, "SIGNAL", meta=json.dumps(meta))


def test_list_signals_with_meta_by_plan_date_range_since_filter(tmp_db):
    """since 区间按 plan_date（trade_id 后缀 substr(-10)），非 timestamp。

    致命日期轴：若误用 timestamp（写入时间 = 测试当下）做 since 过滤，
    since=2026-07-05 会因 now < 2026-07-05 把所有 SIGNAL 滤掉。
    """
    _seed_signal(tmp_db, "600000.SH", "2026-07-01")
    _seed_signal(tmp_db, "600001.SH", "2026-07-03")
    _seed_signal(tmp_db, "600002.SH", "2026-07-05")
    rows = state_store.list_signals_with_meta_by_plan_date_range(since="2026-07-03")
    assert {r["symbol"] for r in rows} == {"600001.SH", "600002.SH"}
    # 每项含 plan_date 字段（C2d experiment report 按 p["date"] 聚合所需）
    assert all("plan_date" in r for r in rows)


def test_list_signals_with_meta_by_plan_date_range_until_filter(tmp_db):
    """until 上界按 plan_date（含）。"""
    _seed_signal(tmp_db, "600000.SH", "2026-07-01")
    _seed_signal(tmp_db, "600001.SH", "2026-07-03")
    _seed_signal(tmp_db, "600002.SH", "2026-07-05")
    rows = state_store.list_signals_with_meta_by_plan_date_range(
        since="2026-07-02", until="2026-07-04")
    assert {r["symbol"] for r in rows} == {"600001.SH"}


def test_list_signals_with_meta_by_plan_date_range_no_filter_returns_all(tmp_db):
    """since/until 均 None → 全量 SIGNAL。"""
    _seed_signal(tmp_db, "600000.SH", "2026-07-01")
    _seed_signal(tmp_db, "600001.SH", "2026-07-05")
    rows = state_store.list_signals_with_meta_by_plan_date_range()
    assert len(rows) == 2
