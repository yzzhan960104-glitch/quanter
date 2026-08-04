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
