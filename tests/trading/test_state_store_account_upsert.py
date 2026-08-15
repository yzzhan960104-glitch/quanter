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


# ============================================================================
# M2 actual_sid 单 SSoT（2026-08-15 tech-debt · master design §actual_sid）：
# DB account.session_id 是实际 sid 的唯一真相源；logs/engine_session.json 降为
# 运行态快照。get_session_id 是唯一读口（supervisor/ops 消费），set_session_id
# 是轮换/bootstrap 的精准写口（targeted UPDATE，不整行覆盖）。
# ============================================================================
def test_get_session_id_reads_account_row(tmp_path):
    """M2 读口：account 行有 session_id → 返 int（supervisor actual_sid 数据源）。"""
    db = str(tmp_path / "state.db")
    state_store.init_store(db)
    state_store.upsert_account("acc-m2", "qmt", session_id=123460, db_path=db)
    assert state_store.get_session_id("acc-m2", db_path=db) == 123460


def test_get_session_id_none_when_row_or_column_missing(tmp_path):
    """M2 读口诚实语义：无账户行 / 行存在但 session_id 为 NULL → 一律 None。"""
    db = str(tmp_path / "state.db")
    state_store.init_store(db)
    assert state_store.get_session_id("ghost", db_path=db) is None
    state_store.upsert_account("acc-null", "qmt", db_path=db)  # 未传 session_id → NULL
    assert state_store.get_session_id("acc-null", db_path=db) is None


def test_get_session_id_none_when_db_file_missing(tmp_path):
    """M2 读口健壮性：DB 文件不存在 → None，且不得顺手创建空库文件（只读探测）。"""
    db = str(tmp_path / "no_such_dir" / "state.db")
    assert state_store.get_session_id("anyone", db_path=db) is None
    assert not (tmp_path / "no_such_dir").exists()


def test_set_session_id_updates_only_session_column(tmp_path):
    """M2 写口精准性：只 UPDATE session_id 列，不碰 mode/userdata_path 等配置列。

    Why 钉死：旧 L3 回写走 upsert_account(仅传 session_id)——ON CONFLICT 全列
    UPDATE 会把 mode 重置 'dry_run'、userdata_path 清 NULL（bootstrap 期先迁
    .env 全量再回写 sid，等于把刚落库的配置又抹掉）。M2 写口必须列级精准。
    """
    db = str(tmp_path / "state.db")
    state_store.init_store(db)
    state_store.upsert_account("acc-m2", "qmt", userdata_path="E:\qmt_userdata",
                               mode="live", session_id=123459, db_path=db)
    assert state_store.set_session_id("acc-m2", 123461, db_path=db) == 1
    acc = state_store.get_account("acc-m2", db_path=db)
    assert acc["session_id"] == 123461                 # sid 已更新（L2 轮换后 actual）
    assert acc["mode"] == "live"                       # 配置列不被抹（clobber 回归防线）
    assert acc["userdata_path"] == "E:\qmt_userdata"
    # 账户行不存在 → no-op（rowcount 0），不静默造行（行由 _migrate_env_to_account 负责）
    assert state_store.set_session_id("ghost", 1, db_path=db) == 0


def test_set_session_id_no_create_when_db_missing(tmp_path):
    """终审 Minor（2026-08-16）：DB 文件不存在 → 返 0 且不得顺手创建空库文件。

    与 get_session_id 同款 is_file 守卫（M2 读口先例）：写口本就要求账户行已存在，
    库文件都不存在时 UPDATE 必然匹配 0 行——此时 sqlite3.connect 默认建空库只会
    留下「探测垃圾库」（未 init/异 CWD 环境下 broker 轮换或旁路调用的副作用）。
    """
    db = tmp_path / "no_such_dir" / "state.db"
    assert state_store.set_session_id("acc-m2", 123462, db_path=str(db)) == 0
    assert not (tmp_path / "no_such_dir").exists()   # 目录连同空库都不落盘
