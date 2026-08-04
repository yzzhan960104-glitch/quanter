# -*- coding: utf-8 -*-
"""research_digest 生成器原型单测（2026-08-03 · 观察环地基）。

物理意图：长周期 Agent 自主优化的"环 1 观察"输入——每天盘后把实盘成交（fill 去重
归因）、回测期望（replay_tasks.db 最近 SUCCESS）、数据/实验状态组装成结构化
研究摘要，并做粗粒度漂移对比（OK/WARN/CRITICAL/样本不足），供 Agent 提案与人工
审阅。原型阈值（胜率 ±15pp / 均 rr ±0.3）在模块常量，后续标定。
"""
import json

from research import digest


def _live():
    return {
        "n_hits": 10, "win_rate": 0.6, "avg_rr": 1.8,
        "avg_holding_bars": 12, "n_skipped": 3, "same_day_both": 1, "stop_gap": 2,
    }


def _exp():
    return {
        "task_id": "t1", "window": "2026-05-01~2026-08-01",
        "n_hits": 200, "win_rate": 0.52, "avg_rr": 1.6,
        "max_drawdown": -0.12, "annualized_return": 0.18,
    }


def test_build_digest_normal_state_ok():
    """实盘与期望接近 → 漂移状态 OK，摘要含核心字段。"""
    md = digest.build_digest("2026-08-03", _live(), _exp(), data_hash="abc123",
                             active_experiment="neckline_disc_20260725_25c602")
    assert "研究摘要" in md
    assert "2026-08-03" in md
    assert "实盘成交：10 笔" in md
    assert "胜率：60.0%" in md
    assert "期望：成交 200 笔 / 胜率 52.0%" in md
    assert "OK" in md
    assert "abc123" in md
    assert "25c602" in md


def test_build_digest_winrate_drift_critical():
    """胜率显著低于期望（>30pp）→ CRITICAL（fail-closed：冻结自主发布的输入信号）。"""
    live = {**_live(), "win_rate": 0.15}
    md = digest.build_digest("2026-08-03", live, _exp())
    assert "CRITICAL" in md
    assert "胜率" in md


def test_build_digest_insufficient_sample():
    """实盘样本 <5 笔 → 样本不足（不做漂移判定，避免噪声误报）。"""
    live = {**_live(), "n_hits": 3, "win_rate": 0.0}
    md = digest.build_digest("2026-08-03", live, _exp())
    assert "样本不足" in md
    assert "CRITICAL" not in md


def test_load_live_fills_filters_empty_strategy(tmp_db):
    """load_live_fills 读 state_store.fill，保 strategy 非空过滤（新断点-4，原 CSV 口径）。

    strategy 空的 fill 不进 digest 样本。物理意图：原 CSV 时代就过滤 strategy 非空
    （只保留有 neckline 归因的成交，丢弃补录空 strategy 行）；A3 切 DB 后 fill 表
    A1 已加 strategy 列，本测试断言该过滤口径不变——digest 实盘样本不应因切换源而
    混入无归因行（防 n_hits/胜率漂移）。
    """
    from trading import state_store
    # 有归因的成交（neckline）→ 纳入 digest 样本
    state_store.insert_fill("O1", "ACC_TEST", "20260805101000", "600000.SH", "BUY",
                            100, 10.0, strategy="neckline")
    # 补录空 strategy 行 → 过滤掉（保原 CSV 口径，新断点-4）
    state_store.insert_fill("O2", "ACC_TEST", "20260805101100", "600001.SH", "BUY",
                            200, 20.0, strategy=None)
    fills = digest.load_live_fills(db_path=tmp_db)
    assert len(fills) == 1
    assert fills[0]["symbol"] == "600000.SH"


def test_load_backtest_expectation_from_tasks_db(tmp_path):
    """replay_tasks.db 最近 SUCCESS 报告的统计 → 期望 dict（含 task_id 溯源）。"""
    from backtest import tasks_db as replay_tasks_db
    db = str(tmp_path / "tasks.db")
    replay_tasks_db.init_db(db)
    report = {"n_hits": 200, "win_rate": 0.52, "avg_rr": 1.6,
              "max_drawdown": -0.12, "annualized_return": 0.18,
              "avg_holding_bars": 11.0}
    tid = replay_tasks_db.create_task({
        "strategy_name": "neckline", "start": "2026-05-01", "end": "2026-08-01",
        "universe": None, "cfg_override": {},
    }, path=db)
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE replay_tasks SET status='SUCCESS', report_json=? WHERE task_id=?",
                (json.dumps(report), tid))
    con.commit()
    con.close()

    exp = digest.load_backtest_expectation(db_path=db)
    assert exp is not None
    assert exp["task_id"] == tid
    assert exp["win_rate"] == 0.52
    assert exp["n_hits"] == 200


def test_load_backtest_expectation_none_when_no_success(tmp_path):
    """无 SUCCESS 任务 → None（digest 期望段降级「无回测期望」）。"""
    from backtest import tasks_db as replay_tasks_db
    db = str(tmp_path / "tasks.db")
    replay_tasks_db.init_db(db)
    replay_tasks_db.create_task({
        "strategy_name": "neckline", "start": "2026-05-01", "end": "2026-08-01",
        "universe": None, "cfg_override": {},
    }, path=db)   # PENDING，非 SUCCESS
    assert digest.load_backtest_expectation(db_path=db) is None


def _seed_tasks(tmp_path, specs):
    """种子回测任务：specs=[(cfg_override, start, end, report)]。"""
    from backtest import tasks_db as replay_tasks_db
    db = str(tmp_path / "tasks.db")
    replay_tasks_db.init_db(db)
    import sqlite3
    ids = []
    for cfg, start, end, report in specs:
        tid = replay_tasks_db.create_task({
            "strategy_name": "neckline", "start": start, "end": end,
            "universe": None, "cfg_override": cfg,
        }, path=db)
        con = sqlite3.connect(db)
        con.execute("UPDATE replay_tasks SET status='SUCCESS', report_json=? WHERE task_id=?",
                    (json.dumps(report), tid))
        con.commit()
        con.close()
        ids.append(tid)
    return db


def test_load_backtest_expectation_prefers_active_params_task(monkeypatch, tmp_path):
    """2026-08-05：期望必须优先取「用 ACTIVE 参数跑」的回测，而不是任意最近任务。"""
    from experiment.models import ActiveExperiment
    active_params = {"window": 80, "min_rr": 2.0, "max_holding": 20, "tp_h_mult": 2.5}
    monkeypatch.setattr(digest, "resolve_active",
                        lambda: [ActiveExperiment("exp1", "neckline", active_params,
                                                 1.0, "2026-07-27")])
    # 最近任务用默认参数（7146fdce 复现），更早任务用 ACTIVE 参数
    db = _seed_tasks(tmp_path, [
        ({}, "2026-07-01", "2026-08-03",
         {"n_hits": 100, "win_rate": 0.26, "avg_rr": -0.45,
          "max_drawdown": -0.14, "annualized_return": -0.77}),
        (active_params, "2026-05-05", "2026-08-03",
         {"n_hits": 1314, "win_rate": 0.311, "avg_rr": -0.31,
          "max_drawdown": -0.12, "annualized_return": 0.18}),
    ])
    exp = digest.load_backtest_expectation(db_path=db)
    assert exp["params_source"] == "ACTIVE"
    assert exp["win_rate"] == 0.311
    assert exp["n_hits"] == 1314
    assert exp["window"] == "2026-05-05~2026-08-03"


def test_load_backtest_expectation_marks_other_params(monkeypatch, tmp_path):
    """无 ACTIVE 参数任务 → 取最近 SUCCESS 但标注 params_source=OTHER（防误导）。"""
    from experiment.models import ActiveExperiment
    active_params = {"window": 80, "min_rr": 2.0}
    monkeypatch.setattr(digest, "resolve_active",
                        lambda: [ActiveExperiment("exp1", "neckline", active_params,
                                                 1.0, "2026-07-27")])
    db = _seed_tasks(tmp_path, [
        ({}, "2026-07-01", "2026-08-03",
         {"n_hits": 100, "win_rate": 0.26, "avg_rr": -0.45,
          "max_drawdown": -0.14, "annualized_return": -0.77}),
    ])
    exp = digest.load_backtest_expectation(db_path=db)
    assert exp["params_source"] == "OTHER"
    assert exp["win_rate"] == 0.26


def test_load_live_perf_from_state_store_with_filled_pnl(tmp_path):
    """state_store 的 TP_FILLED 事件（含 realized_pnl）→ 已实现盈亏统计（金额口径）。

    诚实性：CLOSED 事件目前 realized_pnl=None（post_close 先标事件、pnl 后续从 fill
    算），无风险基准时胜率/均 rr 返回 None，绝不猜测。pnl_sum 只累计有金额的行。
    """
    from trading.state_store import init_store
    db = str(tmp_path / "state.db")
    init_store(db)
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("INSERT INTO account(account_id, broker, created_at) VALUES('default','qmt','t')")
    rows = [
        ("t1", "300001.SZ", "TP1_FILLED", 100.0),
        ("t2", "300002.SZ", "TP1_FILLED", -50.0),
        ("t3", "300003.SZ", "TP2_FILLED", 200.0),
        ("t4", "300004.SZ", "CLOSED", None),   # 先标事件无 pnl（现状）
    ]
    for tid, sym, action, pnl in rows:
        con.execute(
            "INSERT INTO trade_event(account_id, trade_id, symbol, action, timestamp,"
            " realized_pnl) VALUES('default',?,?,?,?,?)",
            (tid, sym, action, "2026-08-03T15:30:00", pnl))
    con.commit()
    con.close()

    out = digest.load_live_perf_from_state_store(db_path=db)
    assert out["pnl_events"] == 3
    assert out["pnl_sum"] == 250.0
    assert out["n_closed"] == 1
    assert out["win_rate"] is None
    assert out["avg_rr"] is None


def test_load_live_perf_from_state_store_empty(tmp_path):
    """无事件/库缺失 → 默认零值 dict（digest 渲染「—」，不抛）。"""
    out = digest.load_live_perf_from_state_store(db_path=str(tmp_path / "empty.db"))
    assert out == {"n_closed": 0, "pnl_events": 0, "pnl_sum": None,
                   "win_rate": None, "avg_rr": None}


def test_push_digest_uses_builtin_notifier(monkeypatch):
    """push_digest：build_default_manager 装配通道后同步 asyncio.run 推送（进程退出不丢）。"""
    calls = []
    ran = []
    fake_mgr = type("M", (), {"notify_risk_event": lambda self, msg, level="INFO": "CORO"})()
    monkeypatch.setattr(digest, "build_default_manager", lambda: fake_mgr)
    monkeypatch.setattr(digest.asyncio, "run", lambda coro: (ran.append(coro) or []))
    digest.push_digest("# 摘要", calls)
    assert ran == ["CORO"]
    assert calls == ["# 摘要"]


def test_main_push_flag_calls_push_digest(tmp_path, monkeypatch):
    """main(["--push"])：组装 digest 后调 push_digest（落盘 + 推送双写）。"""
    monkeypatch.setattr(digest, "load_live_fills", lambda *a, **k: [])
    monkeypatch.setattr(digest, "summarize_fills", lambda fills: {"n_hits": 0})
    monkeypatch.setattr(digest, "load_backtest_expectation", lambda **k: None)
    monkeypatch.setattr(digest, "load_live_perf_from_state_store", lambda **k: {})
    monkeypatch.setattr(digest, "build_digest", lambda *a, **k: "MD")
    pushed = []
    monkeypatch.setattr(digest, "push_digest", lambda md, *a: pushed.append(md))
    out_path = str(tmp_path / "digest.md")
    md = digest.main(["--push", "--out", out_path])
    assert md == "MD"
    assert pushed == ["MD"]
    assert (tmp_path / "digest.md").exists()
