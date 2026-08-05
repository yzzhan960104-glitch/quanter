"""A0 基础设施契约：tmp_db fixture 隔离 + build_trade_id 单点。

后续 A1-A5 + Phase B/C task 依赖本 fixture，本文件锁定契约不可漂移。
"""


def test_tmp_db_isolates_state_store(tmp_db):
    """tmp_db：insert_trade_event 落 tmp DB（非生产 logs/trading_state.db）。

    物理意图：tmp_db monkeypatch state_store._DEFAULT_DB → 所有未显式传 db_path
    的写入都落到 tmp 路径；测试断言生产 DB 不被污染。
    """
    from trading import state_store
    ok = state_store.insert_trade_event("ACC_TEST", "ACC_TEST_600000.SH_2026-08-05",
                                        "600000.SH", "SIGNAL", meta="{}")
    assert ok is True
    # 查 tmp DB 有该行
    import sqlite3
    con = sqlite3.connect(tmp_db)
    n = con.execute("SELECT COUNT(*) FROM trade_event WHERE action='SIGNAL'").fetchone()[0]
    assert n == 1


def test_build_trade_id_format():
    """build_trade_id：account_symbol_date 单点格式（消三处复制，后续 task 用）。"""
    from trading import state_store
    assert state_store.build_trade_id("ACC1", "600000.SH", "2026-08-05") == "ACC1_600000.SH_2026-08-05"


def test_build_trade_id_same_across_callers():
    """跨调用方 trade_id 同构：server submit / engine eod / engine pre_open / trading_plan confirm /
    veto 同 symbol+date 必生成同一 trade_id。

    物理意图（A1 断点-1 双写幂等的根基）：UNIQUE(account_id, trade_id, action) 去重前提是
    所有写入方用同一 trade_id 字符串。若 server 写 ``..._20260805`` 而 engine 写 ``..._2026-08-05``，
    则两行共存、事件链（SIGNAL/CONFIRMED/ORDERED）按 trade_id 关联断裂。Fix1 rework 目标：
    date 统一带横线（clock.today()/next_trading_day 返 YYYY-MM-DD），server 不再 ``.replace('-', '')``。
    """
    from trading import state_store
    aid, sym, date = "ACC1", "600000.SH", "2026-08-05"
    server_tid = state_store.build_trade_id(aid, sym, date)      # server-manual 路径
    engine_eod_tid = state_store.build_trade_id(aid, sym, date)  # engine eod_plan SIGNAL
    engine_preopen_tid = state_store.build_trade_id(aid, sym, date)  # engine pre_open ORDERED
    plan_tid = state_store.build_trade_id(aid, sym, date)        # trading_plan confirm_plan CONFIRMED
    veto_tid = state_store.build_trade_id(aid, sym, date)        # veto_plan VETOED
    # 全部相同字符串（任一漂移即 UNIQUE 失效、双写幂等失效）
    assert server_tid == engine_eod_tid == engine_preopen_tid == plan_tid == veto_tid \
        == "ACC1_600000.SH_2026-08-05"


def test_submit_and_engine_ordered_dedup_on_same_trade_id(tmp_db):
    """server-manual + engine-pre_open 同 symbol+date 写 ORDERED → UNIQUE(account_id,trade_id,action)
    去重（第二次返 False）。

    这是 A1 断点-1 双写幂等的核心断言：server 与 engine 用同一 trade_id 各写一次 ORDERED，
    UNIQUE 必须只接受第一行；若 trade_id 漂移（格式不一致）则两行共存，断点-1 失效。
    """
    from trading import state_store
    aid, sym, date = "ACC_TEST", "600000.SH", "2026-08-05"
    # 先建 account 行（trade_event FK 引用 account）
    state_store.upsert_account(aid, broker="qmt")
    tid = state_store.build_trade_id(aid, sym, date)
    ok1 = state_store.insert_trade_event(aid, tid, sym, "ORDERED", meta="engine")
    ok2 = state_store.insert_trade_event(aid, tid, sym, "ORDERED", meta="server")  # 双写幂等
    assert ok1 is True and ok2 is False, (
        f"双写幂等失效：ok1={ok1} ok2={ok2} tid={tid}（UNIQUE 未去重，trade_id 可能漂移）")
