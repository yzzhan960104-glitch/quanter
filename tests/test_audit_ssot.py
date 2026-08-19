# -*- coding: utf-8 -*-
"""A6：audit_ssot 进程拓扑三项（引擎数 / 客户端进程 / 端口属主一致性），弃 wmic。

CR-5（2026-08-15 tech-debt）：补 fill↔position 双向校验（反向漏挂扫描）+ 孤儿
SIGNAL 后续 action 集合口径用例。测试一律用 tmp_path 假库——live 引擎运行中，
对 logs/trading_state.db 只允许只读 SELECT，绝不写生产库。
"""
from __future__ import annotations

import sqlite3

import pytest

import scripts.audit_ssot as a
import ops.process_topology as pt


def _audit_db(tmp_path, fills=(), positions=(), events=(), name="audit"):
    """造最小审计假库（fill/position/trade_event 三表，列对齐 state_store 生产 schema）。

    Why 手工假库而非 state_store.init_store：audit 只读这三张表的少数列，手工建库
    可精确控制「漏挂向」形态（如 position 缺行 / qty=0 残留行），且不引入 FK/account
    依赖（audit 连接 raw sqlite3，与生产巡检同款读法）。name 参数让同一 tmp_path 下
    循环造多个独立库（参数化用例不互相撞表）。
      fills:     [(order_id, symbol, direction, qty)]
      positions: [(symbol, qty)]
      events:    [(trade_id, symbol, action, timestamp)]
    """
    db = tmp_path / f"{name}.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE fill (
        order_id    TEXT NOT NULL,
        traded_time TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        direction   TEXT NOT NULL,
        qty         REAL NOT NULL,
        price       REAL NOT NULL,
        applied_at  TEXT NOT NULL,
        UNIQUE(order_id, traded_time))""")
    con.execute("""CREATE TABLE position (
        account_id TEXT NOT NULL,
        symbol     TEXT NOT NULL,
        qty        REAL NOT NULL,
        avg_price  REAL,
        entry_date TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (account_id, symbol))""")
    con.execute("""CREATE TABLE trade_event (
        event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        trade_id   TEXT NOT NULL,
        symbol     TEXT NOT NULL,
        action     TEXT NOT NULL,
        timestamp  TEXT NOT NULL,
        UNIQUE(account_id, trade_id, action))""")
    for i, (oid, sym, direction, qty) in enumerate(fills):
        con.execute(
            "INSERT INTO fill(order_id, traded_time, symbol, direction, qty, price, applied_at)"
            " VALUES(?, ?, ?, ?, ?, 10.0, 'now')",
            (oid, f"t{i}", sym, direction, qty))
    for sym, qty in positions:
        con.execute(
            "INSERT INTO position(account_id, symbol, qty, avg_price, entry_date, updated_at)"
            " VALUES('ACC1', ?, ?, 10.0, '2026-01-01', 'now')", (sym, qty))
    for tid, sym, action, ts in events:
        con.execute(
            "INSERT INTO trade_event(account_id, trade_id, symbol, action, timestamp)"
            " VALUES('ACC1', ?, ?, ?, ?)", (tid, sym, action, ts))
    con.commit()
    con.close()
    return db


from tests._stubs import FakeProc as _FakeProc


def test_engine_process_count_ok_when_single(monkeypatch):
    """恰好 1 个 -m trading 进程 → None（不告警）。"""
    monkeypatch.setattr(a, "_engine_processes", lambda: [{"pid": 1, "cmdline": "-m trading"}])
    assert a.check_engine_process_count() is None


def test_engine_process_count_fails_when_two(monkeypatch):
    """≥2 个引擎进程 → 告警文案（C-5 单例红线）。"""
    monkeypatch.setattr(a, "_engine_processes", lambda: [
        {"pid": 1, "cmdline": "-m trading"}, {"pid": 2, "cmdline": "-m trading"}])
    msg = a.check_engine_process_count()
    assert msg is not None and "引擎进程数 2 > 1" in msg


def test_engine_processes_parses_powershell_json(monkeypatch):
    """_engine_processes 解析 PowerShell JSON，只留 -m trading / uvicorn main。

    T10（2026-08-15）：查询改取全进程表（Name 过滤移 Python 侧），mock 须带 Name。
    """
    monkeypatch.setattr(pt.subprocess, "run", lambda *args, **kw: _FakeProc(
        '[{"Name": "python.exe", "ProcessId": 11, "ExecutablePath": "x",'
        ' "CommandLine": "python -m trading"},'
        '{"Name": "python.exe", "ProcessId": 22, "ExecutablePath": "x",'
        ' "CommandLine": "python -c print(1)"}]'))
    procs = a._engine_processes()
    assert [p["pid"] for p in procs] == [11]


def test_engine_processes_drops_grandchild_through_spawn_worker(monkeypatch):
    """T10 递归祖先链：经 spawn worker（cmdline 不匹配）挂的 -m trading 孙辈被清。

    2026-08-15 快照实证形态：root(11) → spawn worker(13, 不匹配) → 孙辈(14, 匹配)。
    旧「直接父 ∈ 引擎集合」一级判据漏掉 14（其父 13 不在集合）→ 误计 2 引擎。
    """
    monkeypatch.setattr(pt.subprocess, "run", lambda *args, **kw: _FakeProc(
        '[{"Name": "python.exe", "ProcessId": 11, "ParentProcessId": 0, "ExecutablePath": "x",'
        ' "CommandLine": "E:\\\\quanter\\\\.venv310\\\\Scripts\\\\python.exe -m trading"},'
        '{"Name": "python.exe", "ProcessId": 13, "ParentProcessId": 11, "ExecutablePath": "x",'
        ' "CommandLine": "python -c from multiprocessing.spawn import spawn_main"},'
        '{"Name": "python.exe", "ProcessId": 14, "ParentProcessId": 13, "ExecutablePath": "x",'
        ' "CommandLine": "python -m trading"}]'))
    procs = a._engine_processes()
    assert [p["pid"] for p in procs] == [11]


def test_engine_processes_fallback_uses_anchors_not_all_venv(monkeypatch):
    """CIM 失败 → 只回退到端口/pid 文件锚点，绝不把所有 venv python 当引擎。"""
    def _boom(*args, **kw):
        raise RuntimeError("CIM unavailable")
    monkeypatch.setattr(pt.subprocess, "run", _boom)
    monkeypatch.setattr(pt, "port_holder_pid", lambda port=8000: 79788)
    monkeypatch.setattr(pt, "pid_file_owner", lambda *a, **kw: 79788)
    monkeypatch.setattr(pt, "_pid_alive", lambda pid: True)
    procs = a._engine_processes()
    assert [p["pid"] for p in procs] == [79788]


def test_client_process_ok_when_one(monkeypatch):
    """恰好 1 个 XtMiniQmt → None。"""
    monkeypatch.setattr(a, "_client_status",
                        lambda: {"running": True, "pid": 44044, "count": 1})
    assert a.check_client_process() is None


def test_client_process_fails_when_missing(monkeypatch):
    """0 个客户端 → 告警（不能假装活）。"""
    monkeypatch.setattr(a, "_client_status",
                        lambda: {"running": False, "pid": None, "count": 0})
    msg = a.check_client_process()
    assert msg is not None and "进程数 0 != 1" in msg


def test_client_process_fails_when_probe_error(monkeypatch):
    """探测失败（count=None）→ 显式告警，不假装 0 个。"""
    monkeypatch.setattr(a, "_client_status",
                        lambda: {"running": None, "pid": None, "count": None})
    msg = a.check_client_process()
    assert msg is not None and "探测失败" in msg


def test_port_owner_consistency_ok_when_same(monkeypatch):
    """端口属主 == pid 文件 → None。"""
    monkeypatch.setattr(a, "_port_holder_pid", lambda port=8000: 123)
    monkeypatch.setattr(a, "_pid_file_owner", lambda *x, **kw: 123)
    assert a.check_port_owner_consistency() is None


def test_port_owner_consistency_drift(monkeypatch):
    """端口属主 != pid 文件 → 告警（旧链/非法链）。"""
    monkeypatch.setattr(a, "_port_holder_pid", lambda port=8000: 100)
    monkeypatch.setattr(a, "_pid_file_owner", lambda *x, **kw: 200)
    msg = a.check_port_owner_consistency()
    assert msg is not None and "!= pid 文件" in msg


def test_port_holder_parses_netstat(monkeypatch):
    """netstat 行 → LISTENING PID。"""
    monkeypatch.setattr(pt.subprocess, "run", lambda *args, **kw: _FakeProc(
        "  TCP    0.0.0.0:8000    0.0.0.0:0    LISTENING       27592\n"))
    assert a._port_holder_pid() == 27592


def test_default_port_honors_server_port_env(monkeypatch):
    """端口单一来源：SERVER_PORT env 覆盖缺省 8000（防硬编码漂移）。"""
    monkeypatch.setenv("SERVER_PORT", "9999")
    assert pt.default_port() == 9999
    monkeypatch.setattr(pt.subprocess, "run", lambda *args, **kw: _FakeProc(
        "  TCP    0.0.0.0:9999    0.0.0.0:0    LISTENING       111\n"))
    assert pt.port_holder_pid() == 111


# ===== CR-5：fill↔position 反向扫描（漏挂向观测，2026-08-15 tech-debt）=====

def test_check_fill_position_reverse_scan_missing_row(tmp_path):
    """CR-5 反向扫描：fill 净额=100 而 position 无行 → 必须报 mismatch（漏挂向）。

    物理意图：旧扫描集只扫 ``position WHERE qty != 0``——position 写入路径漏记该
    symbol 时（→止损/止盈漏挂、敞口裸奔），该 symbol 的行根本不存在，从不进循环，
    巡检静默 PASS。这是「防超卖＞防漏挂」三层同向盲区在审计层的缺口：超卖向
    （fill 少记）会炸，漏挂向（position 少记）沉默。
    """
    db = _audit_db(tmp_path, fills=[("o1", "600000.SH", "BUY", 100)])
    msg = a.check_fill_position(db)
    assert msg is not None, "fill 净额 100 而 position 无行必须报漏挂向"
    assert "漏挂向" in msg and "600000.SH" in msg


def test_check_fill_position_reverse_scan_zero_qty_row(tmp_path):
    """CR-5 反向扫描变体：position 行存在但 qty=0（残留行）→ 同样报漏挂向。

    Why：qty=0 行被旧 ``WHERE qty != 0`` 过滤掉——fill 净额 100 而持仓行是 0，
    与「无行」同义（真实敞口 100 股裸奔），巡检不能因行存在就放行。
    """
    db = _audit_db(tmp_path, fills=[("o1", "600000.SH", "BUY", 100)],
                   positions=[("600000.SH", 0.0)])
    msg = a.check_fill_position(db)
    assert msg is not None and "漏挂向" in msg


def test_check_fill_position_zero_net_no_false_positive(tmp_path):
    """CR-5 防误报红线：fill 净额=0（清仓）且 position 无行 → None。

    物理意图：反向扫描只追 |净额|>1e-6 的 symbol——清仓后 position 行删除是正常态
    （apply_fill_to_position 归零删行），净额 0 不构成漏挂。订正不能把正常清仓
    误报成漏挂（否则告警噪音淹没真实断链）。
    """
    db = _audit_db(tmp_path, fills=[("o1", "600000.SH", "BUY", 100),
                                    ("o2", "600000.SH", "SELL", 100)])
    assert a.check_fill_position(db) is None


def test_check_fill_position_balanced_ok(tmp_path):
    """双向平衡回归锁：fill 净额=position.qty → None（既不报超卖向也不报漏挂向）。"""
    db = _audit_db(tmp_path, fills=[("o1", "600000.SH", "BUY", 100)],
                   positions=[("600000.SH", 100.0)])
    assert a.check_fill_position(db) is None


def test_check_fill_position_forward_mismatch_still_detected(tmp_path):
    """既有正向口径（超卖向）回归锁：fill 净额=50 vs position=100 → 仍报不一致。

    Why：加反向扫描不许削弱原有正向扫描——两个方向各自独立成警，防「修漏挂、
    破超卖」的回归。
    """
    db = _audit_db(tmp_path, fills=[("o1", "600000.SH", "BUY", 50)],
                   positions=[("600000.SH", 100.0)])
    msg = a.check_fill_position(db)
    assert msg is not None and "fill累加" in msg


# ===== CR-5：孤儿 SIGNAL 后续 action 集合口径订正（防误报）=====

def test_orphan_signal_with_ordered_followup_not_flagged(tmp_path):
    """CR-5 孤儿口径：SIGNAL 后仅 ORDERED（生产 pre_open/gateway_service 实写）→ 不误报。

    物理意图：旧集合漏 ORDERED/SUBMITTED/TP1_FILLED/TP2_FILLED/STOP_TRIGGERED——
    生产链路 SIGNAL→CONFIRMED→ORDERED 是主干推进（pre_open.py insert_trade_event
    "ORDERED"），旧审计会把每个已下单未成交的 trade 误报成孤儿，告警噪音淹没
    真实断链（审计旁路失效 = 另一种静默）。
    """
    db = _audit_db(tmp_path, events=[("t1", "600000.SH", "SIGNAL", "2020-01-01T09:30:00"),
                                     ("t1", "600000.SH", "ORDERED", "2020-01-01T09:31:00")])
    assert a.check_trade_event_chain(db) is None


def test_orphan_signal_with_partial_fill_followups_not_flagged(tmp_path):
    """CR-5 孤儿口径：SUBMITTED/TP1_FILLED/TP2_FILLED/STOP_TRIGGERED 后续均不误报。

    覆盖集合：旧集合只认 CONFIRMED/VETOED/OPEN/FILLED/CLOSED，漏掉盘中推进事件
    （pre_open "SUBMITTED"、post_close "TP1_FILLED/TP2_FILLED"、stop_loss
    "STOP_TRIGGERED"）——SIGNAL→TP1_FILLED 的已成交链会被误判孤儿。
    T17（2026-08-15）：补下单审计四态 DRY_RUN/BLOCKED/REJECTED/DIRECTION_UNKNOWN
    ——gateway_service/order_state 实写（模拟下单/挡板拒/柜台拒/direction 缺失审计），
    信号走到「尝试下单但被拒」不是断链，漏掉会误报孤儿（T3 遗留补全）。
    """
    for follow in ("SUBMITTED", "TP1_FILLED", "TP2_FILLED", "STOP_TRIGGERED",
                   "DRY_RUN", "BLOCKED", "REJECTED", "DIRECTION_UNKNOWN"):
        db = _audit_db(tmp_path, name=f"audit_{follow}", events=[
            ("t1", "600000.SH", "SIGNAL", "2020-01-01T09:30:00"),
            ("t1", "600000.SH", follow, "2020-01-01T09:31:00")])
        assert a.check_trade_event_chain(db) is None, f"后续 {follow} 不应误报孤儿"


def test_audit_db_connection_is_read_only(tmp_path):
    """T3 ③/T17：巡检连接 mode=ro——对库的写操作必须被 SQLite 协议层拒绝。

    物理意图：审计是读侧消费者，绝不能持生产库写句柄（脚本笔误的 UPDATE/写错表
    会直接落生产库）。mode=ro URI 下任何写尝试直接抛 OperationalError（只读数据库），
    而非静默成功——fail-closed 于「巡检自身不成为污染源」。
    """
    db = _audit_db(tmp_path, fills=[("o1", "600000.SH", "BUY", 100)],
                   positions=[("600000.SH", 100.0)])
    con = a._connect_ro(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 1
        try:
            con.execute("DELETE FROM fill")
        except sqlite3.OperationalError as e:
            assert "readonly" in str(e).lower() or "只读" in str(e)
        else:
            pytest.fail("mode=ro 连接竟然允许 DELETE——只读护栏失效")
    finally:
        con.close()
    # 连接关闭后数据仍在（证明 DELETE 未生效而非静默提交）
    con2 = sqlite3.connect(db)
    try:
        assert con2.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 1
    finally:
        con2.close()


def test_orphan_signal_without_followup_still_flagged(tmp_path):
    """真孤儿回归锁：SIGNAL>7 日无任何后续 → 仍报（订正不能把真断链放跑）。"""
    db = _audit_db(tmp_path, events=[("t1", "600000.SH", "SIGNAL", "2020-01-01T09:30:00")])
    msg = a.check_trade_event_chain(db)
    assert msg is not None and "孤儿 SIGNAL" in msg


# ===== I-3（2026-08-15 终审）：account_daily 闭合检查窗口语义 =====

def _audit_db_account_daily(tmp_path, rows, name="ad"):
    """造仅含 account_daily 的假库：rows = [(date, start, close)]（None 表示 NULL）。

    Why 手工假库（与 _audit_db 同哲学）：audit 只读 date/start/close 三列，手工建库
    可精确控制「窗口外老 NULL / 窗口内新 NULL」两类形态，不引 FK/account 依赖。
    列名与生产 state_store DDL（account_daily）逐字对齐。
    """
    db = tmp_path / f"{name}.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE account_daily (
        account_id          TEXT NOT NULL,
        date                TEXT NOT NULL,
        start_total_asset   REAL,
        start_cash          REAL,
        close_total_asset   REAL,
        close_cash          REAL,
        close_market_value  REAL,
        daily_pnl           REAL,
        daily_pnl_pct       REAL,
        start_snap_at       TEXT,
        close_snap_at       TEXT,
        PRIMARY KEY(account_id, date))""")
    for d, s, c in rows:
        con.execute(
            "INSERT INTO account_daily(account_id, date, start_total_asset, close_total_asset)"
            " VALUES('ACC1', ?, ?, ?)", (d, s, c))
    con.commit()
    con.close()
    return db


def test_account_daily_old_null_outside_window_passes(tmp_path, monkeypatch):
    """I-3 窗口语义：窗口外老 NULL 行（历史 dry_run 存量）不再永久红。

    物理意图：引擎早期 dry_run 日的 NULL 行（接入 query_asset/基线抓取前的存量）在
    「全表任一 NULL 即炸」旧口径下永久红底——审计要抓「当下基线链是否闭合」，不是
    给历史陪葬；持续红把真告警淹没成狼来了（失败推送通道一开就是每轮轰炸）。
    cutoff 钉死 2026-06-01（隔离真实日历/时钟，防用例随日期漂移变 time bomb）：
    1 月的 10 行老 NULL 出窗 → PASS（近端 8 月行全闭合）。
    """
    monkeypatch.setattr(a, "_account_daily_cutoff", lambda n: "2026-06-01")
    rows = [(f"2026-01-{d:02d}", None, None) for d in range(1, 11)]       # 窗口外老 NULL
    rows += [(f"2026-08-{d:02d}", 100.0, 100.5) for d in range(11, 15)]   # 近 4 行闭合
    db = _audit_db_account_daily(tmp_path, rows)
    assert a.check_account_daily_closed(db, window_days=5) is None


def test_account_daily_null_inside_window_still_fails(tmp_path, monkeypatch):
    """I-3 窗口内 NULL 仍 FAIL（窗口订正不得把近端真断链放跑——含 T 日未闭合行）。"""
    monkeypatch.setattr(a, "_account_daily_cutoff", lambda n: "2026-06-01")
    rows = [(f"2026-08-{d:02d}", 100.0, 100.5) for d in range(11, 15)]
    rows.append(("2026-08-15", None, 101.0))   # 最新行 start NULL（窗口内，当日未闭合）
    db = _audit_db_account_daily(tmp_path, rows)
    msg = a.check_account_daily_closed(db, window_days=5)
    assert msg is not None and "2026-08-15" in msg and "近 5 交易日窗口内" in msg


def test_audit_window_days_env_default_and_override(monkeypatch):
    """I-3 窗口 env 口径：默认 30；AUDIT_ACCOUNT_WINDOW_DAYS 覆盖；非法值回落 30。"""
    monkeypatch.delenv("AUDIT_ACCOUNT_WINDOW_DAYS", raising=False)
    assert a._audit_window_days() == 30
    monkeypatch.setenv("AUDIT_ACCOUNT_WINDOW_DAYS", "365")
    assert a._audit_window_days() == 365
    monkeypatch.setenv("AUDIT_ACCOUNT_WINDOW_DAYS", "0")
    assert a._audit_window_days() == 30   # <1 非法 → fail-safe 回默认（不静默关检查）
