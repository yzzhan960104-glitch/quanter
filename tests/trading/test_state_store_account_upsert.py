# -*- coding: utf-8 -*-
"""upsert_account 对"已有子表引用的账户行"必须能更新（2026-08-03 装配炸点）。

背景实证：logs/trading_state.db 中 account 10110356 已存在且 trade_event 有
引用行；`INSERT OR REPLACE` 语义是"先 DELETE 旧行再 INSERT"，DELETE 被
trade_event/order/fill 的 `REFERENCES account(account_id) ON DELETE RESTRICT`
挡住 → `FOREIGN KEY constraint failed` → engine bootstrap 装配失败
（QMT 已连接但 TradingEngine 未装配，2026-08-03 23:01 复现）。
修复：改用 SQLite UPSERT（ON CONFLICT DO UPDATE），更新不删行。
"""
from trading import state_store


def test_upsert_account_updates_row_referenced_by_children(tmp_path):
    """账户已有 trade_event 引用 → upsert_account 更新成功（不触发 FK DELETE 限制）。"""
    db = str(tmp_path / "state.db")
    state_store.init_store(db)
    # 首次插入账户 + 引用它的 trade_event（append-only 事件流）
    state_store.upsert_account("10110356", "qmt", mode="dry_run", db_path=db)
    state_store.insert_trade_event(
        "10110356", "t1", "300001.SZ", "SIGNAL", db_path=db)
    # 再次 upsert（模拟 .env 变更后重启迁移）→ 必须更新成功
    state_store.upsert_account(
        "10110356", "qmt", userdata_path="D:\\x", session_id=123459,
        strategy_name="quanter", mode="live", db_path=db)
    acc = state_store.get_account("10110356", db_path=db)
    assert acc["mode"] == "live"
    assert acc["session_id"] == 123459
    # 子表引用行仍保留（REPLACE 的 DELETE 被 RESTRICT 挡住是旧 bug 的根因）
    from trading.state_store import _connect
    with _connect(db) as con:
        n = con.execute("SELECT COUNT(*) c FROM trade_event WHERE account_id='10110356'"
                        ).fetchone()["c"]
    assert n == 1
