# -*- coding: utf-8 -*-
"""__main__ CLI 单测（trading 主流程）：dry-run / 幂等去重 / --force / 推送失败 / 无日期兜底。

market 下线后，默认 bot=trading。trading 分支取数走 _fetch_trading_snapshot（重 import），
测试 mock 它 + build_trading_brief，避免依赖真实 trading_service。

B4（2026-08-05）：幂等源从文件（.last_<bot>_brief）迁到 job_ledger（job_name=brief_<bot>），
begin_run/finish_run 成对 + latest_status 查幂等。旧 last_brief_file/_read_last/_write_last 退役。
台账隔离：monkeypatch.setenv("TRADING_JOB_LEDGER_DB", tmp DB)，不碰真实 logs/trading_job_run.db。
"""
import json

import broadcast.__main__ as bm
from broadcast.brief import BriefResult


def _stub_trading(monkeypatch, date="2026-07-15"):
    """桩：reader/date/取数/brief 渲染，让 main 主流程不依赖真实 IO。"""
    monkeypatch.setattr(bm, "_load_reader", lambda: "fake_reader")
    monkeypatch.setattr(bm, "_latest_trade_date", lambda r: date)
    # mock trading 取数五件套（避免 import trading_service 重链路 + 真实网关；
    # 2026-08-17 增 next_plan 第五件——明日计划 DB 读口一并桩掉）
    monkeypatch.setattr(bm, "_fetch_trading_snapshot",
                        lambda d: ([], None, None, {"mode": "live"}, None))
    # mock brief 渲染（主流程测幂等/push/last，不应依赖 brief 真渲染）
    monkeypatch.setattr(
        bm, "build_trading_brief",
        lambda *a, **k: BriefResult(date=date, markdown="### 每日交易播报\n样例正文"),
    )


def _isolate_job_ledger(monkeypatch, tmp_path):
    """把 job_ledger DB 重定向到 tmp_path（不碰真实 logs/trading_job_run.db）。

    B4 取代旧 _isolate_last_file：幂等源迁 job_ledger 后，测试隔离由文件改 DB 路径。
    """
    from trading import job_ledger
    db = str(tmp_path / "job_run.db")
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", db)
    job_ledger.init_db(db)
    return db


def test_main_dry_run_prints_and_pushes_dry(monkeypatch):
    _stub_trading(monkeypatch)
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append((a, k)) or True)
    rc = bm.main(["--dry-run"])
    assert rc == 0
    assert pushed and pushed[0][1].get("dry_run") is True
    assert "每日交易播报" in pushed[0][0][1]


def test_main_dedup_skips_when_already_broadcast(monkeypatch, tmp_path):
    """B4：台账 brief_trading=done → 跳过推送（幂等查 job_ledger.latest_status）。"""
    from trading import job_ledger
    _stub_trading(monkeypatch, date="2026-07-15")
    _isolate_job_ledger(monkeypatch, tmp_path)
    # 预置台账：brief_trading 已 done（先 begin_run INSERT，再 finish_run UPDATE）
    job_ledger.begin_run("brief_trading", "2026-07-15", started_at="2026-07-15T16:00:00")
    job_ledger.finish_run("brief_trading", "2026-07-15", "done")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main([])
    assert rc == 0
    assert pushed == []                      # 今日已播 → 跳过


def test_main_force_overrides_dedup(monkeypatch, tmp_path):
    """B4：--force 跳过台账检查（强制重推），但推送成功后仍 begin/finish 更新台账。"""
    from trading import job_ledger
    _stub_trading(monkeypatch, date="2026-07-15")
    db = _isolate_job_ledger(monkeypatch, tmp_path)
    # 预置台账：brief_trading 已 done
    job_ledger.begin_run("brief_trading", "2026-07-15", started_at="2026-07-15T16:00:00")
    job_ledger.finish_run("brief_trading", "2026-07-15", "done")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main(["--force"])
    assert rc == 0
    assert pushed == [1]                     # --force 覆盖去重
    # --force 重推后台账仍为 done（finish 写最新 done）
    assert job_ledger.latest_status("brief_trading", "2026-07-15") == "done"


def test_main_success_writes_ledger(monkeypatch, tmp_path):
    """B4：推送成功 → begin_run/finish_run("done") 成对落台账。"""
    from trading import job_ledger
    _stub_trading(monkeypatch, date="2026-07-15")
    _isolate_job_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: True)
    rc = bm.main([])
    assert rc == 0
    assert job_ledger.latest_status("brief_trading", "2026-07-15") == "done"


def test_main_push_failure_no_ledger_done(monkeypatch, tmp_path):
    """B4：推送失败 → 不写 done（台账无 done 记录，下次触发重试）。

    finish_run 只 UPDATE——没先 begin_run 时 0 行受影响，latest_status 仍为 None/非 done。
    """
    from trading import job_ledger
    _stub_trading(monkeypatch, date="2026-07-15")
    _isolate_job_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: False)
    rc = bm.main([])
    assert rc == 2                           # 推送失败
    # 失败 → 不应留下 done（下次重试）
    assert job_ledger.latest_status("brief_trading", "2026-07-15") != "done"


def test_main_no_date_returns_1(monkeypatch):
    _stub_trading(monkeypatch, date=None)
    assert bm.main([]) == 1


def test_brief_idempotent_via_job_ledger(tmp_path, monkeypatch):
    """B4 brief 幂等读台账（begin/finish 成对，无多余 kwargs）。

    红线验证：
    - begin_run/finish_run 成对（finish 只 UPDATE，须先 begin INSERT）；
    - 「未 begin 直接 finish」不产生 done 记录（防误用，幂等失效）；
    - latest_status=="done" → broadcast 主流程跳过推送。
    """
    from trading import job_ledger
    db = tmp_path / "job.db"
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(db))

    # 成对 begin/finish → latest_status=="done"（finish UPDATE 命中 begin INSERT 的行）
    job_ledger.begin_run("brief_trading", "2026-08-05", started_at="2026-08-05T16:00:00")
    job_ledger.finish_run("brief_trading", "2026-08-05", "done")
    assert job_ledger.latest_status("brief_trading", "2026-08-05") == "done"

    # 防误用：未 begin 直接 finish → UPDATE 0 行，无 done 记录（latest_status 仍 None）
    job_ledger.finish_run("brief_data", "2026-08-05", "done")
    assert job_ledger.latest_status("brief_data", "2026-08-05") is None

    # broadcast 主流程：brief_trading 台账 done → 不重复推
    _stub_trading(monkeypatch, date="2026-08-05")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main(["--bot", "trading"])
    assert rc == 0
    assert pushed == []                      # 台账 done → 跳过


def test_scan_count_by_plan_date(tmp_db, monkeypatch):
    """C2a（2026-08-05）：scan_count = 计划日=next_trading_day(date) 的 SIGNAL 数。

    红线（致命日期轴）：按 trade_id 后缀（plan_date）查，**非 timestamp**。
    - trade_event.timestamp = clock.now() 写入时间 = T 日盘后（state_store.insert_trade_event）
      —— 不是计划日 T+1。
    - 计划日仅在 trade_id 后缀（{account_id}_{symbol}_{plan_date}，build_trade_id 单点）。
    - 若误用 timestamp 查计划日：T 日盘后写（timestamp=T）查 T+1 恒 0 → 致命错误。

    数学验证（substr(trade_id,-10)=plan_date）：
    - trade_id = "ACC_TEST_A.SH_2026-08-05"（A 股 ts_code 不含下划线，YYYY-MM-DD 恰 10 字符）
    - substr(trade_id,-10) = "2026-08-05" = plan_date ✓
    """
    from trading import state_store
    # 插 2 个 SIGNAL（A.SH/B.SH 计划日 2026-08-05）
    state_store.insert_trade_event(
        "ACC_TEST", state_store.build_trade_id("ACC_TEST", "A.SH", "2026-08-05"),
        "A.SH", "SIGNAL", meta='{"plan_date":"2026-08-05"}')
    state_store.insert_trade_event(
        "ACC_TEST", state_store.build_trade_id("ACC_TEST", "B.SH", "2026-08-05"),
        "B.SH", "SIGNAL", meta='{"plan_date":"2026-08-05"}')

    # param_iter_state 单口（B3 收口 experiment.db）：无 ACTIVE 返 None，不影响 scan_count
    monkeypatch.setattr(bm, "_experiment_active_state", lambda: None)

    # next_trading_day("2026-08-04") → "2026-08-05"，两 SIGNAL → scan_count=2
    scan_count, param_iter_state, _ = bm._fetch_strategy_snapshot("2026-08-04")
    assert scan_count == 2
    assert param_iter_state is None  # 三件套其他部分不动（B3 收口）


def test_scan_count_by_plan_date_distinct_symbol(tmp_db, monkeypatch):
    """C2a 边界：同 symbol 同 plan_date 多 SIGNAL（理论 UNIQUE 约束防不住多 account）→ COUNT DISTINCT symbol 保守。

    物理意图：UNIQUE(account_id, trade_id, action) 只防同 account 同 trade 同 action 重推；
    多 account 写同 symbol 同 plan_date 不会触发 UNIQUE（trade_id account 段不同）。生产
    只一个 account 不会出现，但 DISTINCT 保守——查到的是「当日选股数」非「事件数」。"""
    from trading import state_store
    # 两个 account 各写一个 A.SH SIGNAL（trade_id 不同 account 段）
    state_store.upsert_account("ACC_OTHER", broker="qmt")
    state_store.insert_trade_event(
        "ACC_TEST", state_store.build_trade_id("ACC_TEST", "A.SH", "2026-08-05"),
        "A.SH", "SIGNAL", meta='{"plan_date":"2026-08-05"}')
    state_store.insert_trade_event(
        "ACC_OTHER", state_store.build_trade_id("ACC_OTHER", "A.SH", "2026-08-05"),
        "A.SH", "SIGNAL", meta='{"plan_date":"2026-08-05"}')

    monkeypatch.setattr(bm, "_experiment_active_state", lambda: None)
    scan_count, _, _ = bm._fetch_strategy_snapshot("2026-08-04")
    # DISTINCT symbol → 1（A.SH 一个标的，不是事件数 2）；「当日选股数」语义对齐
    assert scan_count == 1


def test_scan_count_db_exception_degrades_none(monkeypatch, tmp_db):
    """C2a 边界：DB 异常（count_signals_by_plan_date 抛）→ scan_count 降级 None，不阻断 brief。"""
    from trading import state_store
    monkeypatch.setattr(bm, "_experiment_active_state", lambda: None)
    # 让 count_signals_by_plan_date 抛异常模拟 DB 路径错误
    monkeypatch.setattr(state_store, "count_signals_by_plan_date",
                        lambda plan_date: (_ for _ in ()).throw(RuntimeError("DB boom")))
    scan_count, _, _ = bm._fetch_strategy_snapshot("2026-08-04")
    assert scan_count is None  # 降级，不阻断三件套


def test_scan_count_empty_returns_zero(tmp_db, monkeypatch):
    """C2a 边界：无 SIGNAL（当日未扫描/周末）→ scan_count=0（不是 None）。"""
    monkeypatch.setattr(bm, "_experiment_active_state", lambda: None)
    scan_count, _, _ = bm._fetch_strategy_snapshot("2026-08-04")
    assert scan_count == 0


def test_recent_runs_reads_tasks_db_with_pending_marker(monkeypatch, tmp_path):
    """回归（2026-08-03）：recent_runs 读 replay_tasks.db（当前单一真相源）。

    旧实现读已停更的 replay_runs/index.json → 「近期回测」恒停在 07-14。
    新实现：SUCCESS 任务摘要 + 进行中（PENDING）任务置顶提示。
    """
    import json as _json

    from backtest import tasks_db as replay_tasks_db
    db = str(tmp_path / "tasks.db")
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", db)
    replay_tasks_db.init_db(db)

    done_id = replay_tasks_db.create_task({
        "start": "2026-05-01", "end": "2026-08-02", "universe": None,
        "cfg_override": {},
    }, path=db)
    replay_tasks_db.mark_success(done_id, _json.dumps({
        "n_hits": 2, "win_rate": 0.5, "max_drawdown": -0.5,
        "annualized_return": 0.12,
    }), path=db)
    pending_id = replay_tasks_db.create_task({
        "start": "2026-05-01", "end": "2026-08-03", "universe": None,
        "cfg_override": {},
    }, path=db)  # 更新的一条 PENDING → 应置顶提示

    # 固定 created_at（真实时钟会让断言跨午夜脆弱）：done=08-02，pending=08-03
    import sqlite3 as _sqlite3
    con = _sqlite3.connect(db)
    con.execute("UPDATE replay_tasks SET created_at=? WHERE task_id=?",
                ("2026-08-02T10:00:00", done_id))
    con.execute("UPDATE replay_tasks SET created_at=? WHERE task_id=?",
                ("2026-08-03T10:00:00", pending_id))
    con.commit()
    con.close()

    runs = bm._recent_runs_from_tasks_db()
    assert runs[0]["pending"] is True
    assert runs[1]["run_id"] == "20260802"
    assert runs[1]["n_hits"] == 2
    assert runs[1]["win_rate"] == 0.5
    assert runs[1]["max_drawdown"] == -0.5
    assert runs[1]["annualized_return"] == 0.12


def test_fetch_trading_snapshot_reads_server_api(monkeypatch):
    """回归（2026-08-03）：资金/持仓/网关态走运行中 server 的 API。

    旧实现独立进程自建 QMT 网关（从未 connect → 恒 disconnected 降级）；新实现
    读 server API 并解包 {asset}/{positions} 包装。
    """
    fake = {
        "/api/v1/trading/status": {"connected": True, "locked": False, "mode": "live"},
        "/api/v1/trading/asset": {"asset": {
            "account_id": "10110356", "cash": 721794.74,
            "total_asset": 1002237.74, "market_value": 283152.0}},
        "/api/v1/trading/positions": {"positions": [
            {"symbol": "600519.SH", "qty": 100.0},
            {"symbol": "300654.SZ", "qty": 12900.0}]},
    }
    monkeypatch.setattr(bm, "_server_json", lambda path, timeout=5.0: fake[path])
    monkeypatch.setattr(bm, "_local_positions_fallback", lambda: None)
    # 明日计划读口桩死（防测试依赖生产 logs/trading_state.db 的真实计划行）
    monkeypatch.setattr("trading.trading_plan.load_plan",
                        lambda d: {"date": d, "confirmed": True, "orders": []})
    import trading.calendar as _cal
    monkeypatch.setattr(_cal, "next_trading_day", lambda d: "2026-08-04")

    trades, asset, positions, status, next_plan = bm._fetch_trading_snapshot("2026-08-03")
    assert status["mode"] == "live"
    assert next_plan["date"] == "2026-08-04"
    assert asset["total_asset"] == 1002237.74
    assert positions[0]["symbol"] == "600519.SH"
    assert positions[1]["qty"] == 12900.0


def test_fetch_trading_snapshot_degrades_when_api_down(monkeypatch):
    """server 不在/超时 → 资金降级 None、持仓退回本地账本、网关态空（不阻断播报）。"""
    def _boom(path, timeout=5.0):
        raise RuntimeError("server down")
    monkeypatch.setattr(bm, "_server_json", _boom)
    monkeypatch.setattr(bm, "_local_positions_fallback",
                        lambda: [{"symbol": "300654.SZ", "qty": 12900.0}])
    monkeypatch.setattr("trading.trading_plan.load_plan", lambda d: None)
    import trading.calendar as _cal
    monkeypatch.setattr(_cal, "next_trading_day", lambda d: "2026-08-04")

    _, asset, positions, status, next_plan = bm._fetch_trading_snapshot("2026-08-03")
    assert asset is None
    assert next_plan is None
    assert positions == [{"symbol": "300654.SZ", "qty": 12900.0}]
    assert status == {}
