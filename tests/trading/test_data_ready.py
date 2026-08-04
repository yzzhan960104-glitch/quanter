# -*- coding: utf-8 -*-
"""W5 数据就绪单口 get_ready 单测（spec #13 · 消除「三张嘴」漂移）。

物理意图（spec §4 验收 T10）：
    08-04 问题——数据「就绪」三源无对账：
      ① data_ready 表（内容校验，落盘于 pipeline_then_eod 完成后）；
      ② job_ledger.pipeline 状态（running/done/failed，跨重启运行台账）；
      ③ parquet mtime + .syncing 哨兵（data_service 派生态，观测健康度）。
    三源各自写、各自读 → 台账 done、内容缺、播报 healthy 三方不一致。

    get_ready(date, datasets) 合成 ① + ②（③ 为观测口径，保留双口径展示），
    任一源失败 → False + logger.warning 显式暴露差异（让研究员一眼看见哪源漂移）。

设计约束（反魔法 / Karpathy 极简）：
    - 纯组合函数，复用既有 state_store.get_data_ready + job_ledger.latest_status；
    - 不引重型库；任一源检查异常 → False + warning，不抛出（C-4 软降级语义）。
"""
from __future__ import annotations

import logging

import pytest

from trading import job_ledger
from trading.state_store import get_ready


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def green_db(tmp_path, monkeypatch):
    """构造一个全绿台账环境：data_ready 表写 ok=1 + job_ledger.pipeline=done。

    物理意图：把 state_store 与 job_ledger 的 DB 路径都指到 tmp_path 隔离环境，
    写入「全绿」基线，让 get_ready 在干净环境下返 True。
    """
    state_db = tmp_path / "trading_state.db"
    job_db = tmp_path / "trading_job_run.db"
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(job_db))

    # 写 data_ready 表：date=2026-08-05, dataset=daily, ok=1
    import sqlite3
    con = sqlite3.connect(state_db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS data_ready "
        "(date TEXT, dataset TEXT, ok INTEGER, message TEXT, checked_at TEXT, "
        "PRIMARY KEY (date, dataset))"
    )
    con.execute(
        "INSERT OR REPLACE INTO data_ready (date, dataset, ok, message, checked_at) "
        "VALUES (?, 'daily', 1, '', '2026-08-05T18:00:00')",
        ("2026-08-05",),
    )
    con.commit()
    con.close()

    # 写 job_ledger：pipeline@2026-08-05 = done
    job_ledger.init_db(str(job_db))
    job_ledger.begin_run("pipeline", "2026-08-05", "2026-08-05T17:30:00", path=str(job_db))
    job_ledger.finish_run("pipeline", "2026-08-05", "done", "OK",
                          path=str(job_db))

    return {"state_db": state_db, "job_db": job_db,
            "date": "2026-08-05", "dataset": "daily"}


# --------------------------------------------------------------------------- tests


def test_get_ready_all_green_returns_true(green_db, caplog):
    """三源全绿（data_ready.ok=1 AND job_ledger.pipeline=done）→ get_ready 返 True。"""
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        ok = get_ready("2026-08-05", ["daily"], db_path=str(green_db["state_db"]))
    assert ok is True
    # 全绿不应有 warning（差异暴露只在失败时打）
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_get_ready_data_not_ready_returns_false(green_db, caplog):
    """data_ready 未绿（ok=0 或 None）→ False + warning 显式暴露差异。"""
    import sqlite3
    # 把 data_ready 行改成 ok=0（内容校验未通过）
    con = sqlite3.connect(green_db["state_db"])
    con.execute(
        "UPDATE data_ready SET ok=0, message='内容校验未通过' WHERE date=? AND dataset='daily'",
        ("2026-08-05",),
    )
    con.commit()
    con.close()

    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        ok = get_ready("2026-08-05", ["daily"], db_path=str(green_db["state_db"]))
    assert ok is False
    # warning 必须显式提到 data_ready 未绿（让研究员一眼定位是哪源漂移）
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "data_ready" in msgs or "内容" in msgs or "未就绪" in msgs


def test_get_ready_data_missing_returns_false(green_db, caplog):
    """data_ready 表无该 dataset 记录（None=未采集）→ False + warning。"""
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        # 传一个 green_db 里没写的 dataset key
        ok = get_ready("2026-08-05", ["moneyflow"], db_path=str(green_db["state_db"]))
    assert ok is False
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "data_ready" in msgs or "未采集" in msgs or "未就绪" in msgs


def test_get_ready_job_ledger_not_done_returns_false(green_db, caplog):
    """job_ledger.pipeline 非 done（running/failed/skipped/None）→ False + warning。

    覆盖 spec #13「台账 done、内容缺」反向场景：data_ready 全绿但台账未 done
    → 暴露 pipeline 流程未闭环（采集完了但 eod/brief 没跑完）。
    """
    # 把 job_ledger 改成 running（pipeline 还在跑）
    job_ledger.begin_run("pipeline", "2026-08-05", "2026-08-05T17:30:00",
                         path=str(green_db["job_db"]))

    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        ok = get_ready("2026-08-05", ["daily"], db_path=str(green_db["state_db"]))
    assert ok is False
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "pipeline" in msgs or "job_ledger" in msgs or "台账" in msgs


def test_get_ready_job_ledger_missing_returns_false(green_db, caplog):
    """job_ledger 无记录（None=未跑 pipeline）→ False + warning。"""
    # 清空 job_ledger 表
    import sqlite3
    con = sqlite3.connect(green_db["job_db"])
    con.execute("DELETE FROM job_run")
    con.commit()
    con.close()

    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        ok = get_ready("2026-08-05", ["daily"], db_path=str(green_db["state_db"]))
    assert ok is False
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "pipeline" in msgs or "job_ledger" in msgs


def test_get_ready_data_check_exception_returns_false(tmp_path, caplog):
    """data_ready 检查异常（DB 损坏/文件不存在）→ False，不抛出（C-4 软降级）。

    物理意图：pre_open gate 调 get_ready，若 DB 读异常直接 raise 会阻断 gate；
    应软降级返 False + exception log，让 gate 走「未就绪」分支软降级（不挂单 +
    CRITICAL 由上层 _critical_guard 兜底）。
    """
    # 指向一个不存在的 DB 文件（读 data_ready 表会因表不存在/文件损坏异常）
    bogus_db = tmp_path / "nonexistent.db"
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        ok = get_ready("2026-08-05", ["daily"], db_path=str(bogus_db))
    # data_ready 表不存在 → get_data_ready 实际返 None（表自动建？不会，_connect 不建 data_ready 表）
    # 无论返 None 还是异常，get_ready 都应返 False 不抛
    assert ok is False


def test_get_ready_defaults_dataset_when_none(green_db, caplog):
    """datasets=None 时用默认 dataset=['daily']（A 股日线引擎唯一数据集）。"""
    with caplog.at_level(logging.WARNING, logger="trading.state_store"):
        ok = get_ready("2026-08-05", None, db_path=str(green_db["state_db"]))
    assert ok is True
