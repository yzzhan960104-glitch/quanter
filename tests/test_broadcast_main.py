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
    # mock trading 取数四件套（避免 import trading_service 重链路 + 真实网关）
    monkeypatch.setattr(bm, "_fetch_trading_snapshot",
                        lambda d: ([], None, None, {"mode": "live"}))
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


def test_fetch_strategy_snapshot_scan_count_reads_plan_file(monkeypatch, tmp_path):
    """回归（2026-08-02）：scan_count 读 EOD 落盘的 plan_<date>.json。

    旧实现读已停用的 plans/<date>.json → 恒「— 个」；T 日盘后 EOD 把计划写进
    logs/trading_plans/plan_<trading_day>.json（T+1 计划生效日）。07-31 周五盘后
    → plan_2026-08-03.json，2 单 → scan_count=2。
    """
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    (plan_dir / "plan_2026-08-03.json").write_text(
        json.dumps({"date": "2026-08-03", "confirmed": True, "orders": [{"o": 1}, {"o": 2}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADE_PLAN_DIR", str(plan_dir))

    scan_count, _, _ = bm._fetch_strategy_snapshot("2026-07-31")
    assert scan_count == 2


def test_fetch_strategy_snapshot_scan_count_falls_back_same_day_plan(monkeypatch, tmp_path):
    """无 T+1 计划文件时回退同日 plan_<date>.json（兼容补跑/周末）。"""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    (plan_dir / "plan_2026-07-31.json").write_text(
        json.dumps({"date": "2026-07-31", "confirmed": False, "orders": [{"o": 1}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADE_PLAN_DIR", str(plan_dir))

    scan_count, _, _ = bm._fetch_strategy_snapshot("2026-07-31")
    assert scan_count == 1


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

    trades, asset, positions, status = bm._fetch_trading_snapshot("2026-08-03")
    assert status["mode"] == "live"
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

    _, asset, positions, status = bm._fetch_trading_snapshot("2026-08-03")
    assert asset is None
    assert positions == [{"symbol": "300654.SZ", "qty": 12900.0}]
    assert status == {}
