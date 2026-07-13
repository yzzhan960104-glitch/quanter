# -*- coding: utf-8 -*-
"""replay_worker 单测：worker 跑完/空数据/取消/异常四条路径（Spec 1 · Task 3）。

物理意图：worker 是同步函数（被 ProcessPoolExecutor submit 在子进程跑），测试用合成
price_data + monkeypatch _load_price_data/_init_worker（跳过真 data_lake 加载），在主进程
直接调 run_replay_worker 验证状态机写回（不真起子进程——状态机逻辑与进程边界无关）。
"""
import multiprocessing as mp

import pandas as pd
import pytest

from caisen import replay_tasks_db, replay_worker


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(replay_tasks_db, "_DEFAULT_DB_PATH", path)
    replay_tasks_db.init_db()
    return path


def _make_task(**req_over):
    """创建一个 SUCCESS 路径用的标准任务（universe 单标的）。"""
    return replay_tasks_db.create_task({
        "start": "2024-01-01", "end": "2024-06-01",
        "universe": ["000001.SZ"], "cfg_override": {}, **req_over,
    })


def _synth_df():
    """合成合法 OHLCV 横盘 DataFrame（DatetimeIndex，让 start/end 日期串可定位）。

    横盘无形态→screener 返空→replay 跑完返零统计 ReplayReport（验状态机，非验命中）。
    DatetimeIndex 必需：_iter_trading_days 对 RangeIndex 会 int(start) 转换，日期串会炸。
    """
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    n = len(idx)
    return pd.DataFrame({
        "close": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
        "volume": [1000.0] * n, "amount": [1e8] * n,
    }, index=idx)


def _patch(monkeypatch, price_data):
    """统一 patch：跳过 _init_worker（不真 load data_lake）+ _load_price_data 注入合成数据。"""
    monkeypatch.setattr(replay_worker, "_init_worker", lambda: None)
    monkeypatch.setattr(replay_worker, "_load_price_data", lambda uni, end: price_data)


def test_worker_success_marks_success(db, monkeypatch):
    """worker 跑完 → mark_success + report 内嵌 + progress=100。"""
    _patch(monkeypatch, {"000001.SZ": _synth_df()})
    task_id = _make_task()
    replay_worker.run_replay_worker(task_id, mp.Event(), mp.Queue(), mp.Queue())
    got = replay_tasks_db.get_task(task_id)
    assert got["status"] == "SUCCESS"
    assert got["progress"] == 100
    assert got["report"] is not None


def test_worker_empty_price_data_marks_failed(db, monkeypatch):
    """price_data 装配空 → 显式 mark_failed（spec §7 data_lake 离线不卡死、不跑空回测）。"""
    _patch(monkeypatch, {})            # 空 price_data
    task_id = _make_task()
    replay_worker.run_replay_worker(task_id, mp.Event(), mp.Queue(), mp.Queue())
    got = replay_tasks_db.get_task(task_id)
    assert got["status"] == "FAILED"
    assert got["error"]


def test_worker_abort_marks_cancelled(db, monkeypatch):
    """abort_flag 已 set → replay 循环顶抛 ReplayAborted → mark_cancelled。"""
    _patch(monkeypatch, {"000001.SZ": _synth_df()})
    task_id = _make_task()
    flag = mp.Event()
    flag.set()
    replay_worker.run_replay_worker(task_id, flag, mp.Queue(), mp.Queue())
    assert replay_tasks_db.get_task(task_id)["status"] == "CANCELLED"


def test_worker_exception_marks_failed(db, monkeypatch):
    """_load_price_data 抛异常 → worker 兜底 mark_failed（不抛出子进程外，不裸崩）。"""
    monkeypatch.setattr(replay_worker, "_init_worker", lambda: None)

    def _boom(uni, end):
        raise RuntimeError("data_lake 爆了")

    monkeypatch.setattr(replay_worker, "_load_price_data", _boom)
    task_id = _make_task()
    replay_worker.run_replay_worker(task_id, mp.Event(), mp.Queue(), mp.Queue())
    got = replay_tasks_db.get_task(task_id)
    assert got["status"] == "FAILED"
    assert "RuntimeError" in got["error"] or "data_lake" in got["error"]


def test_worker_universe_none_is_all_market(db, monkeypatch):
    """universe=None → _load_price_data 收到 None（全市场语义），不炸。"""
    captured = {}
    monkeypatch.setattr(replay_worker, "_init_worker", lambda: None)
    monkeypatch.setattr(
        replay_worker, "_load_price_data",
        lambda uni, end: captured.update(uni=uni, end=end) or {"X": _synth_df()},
    )
    task_id = replay_tasks_db.create_task({
        "start": "2024-01-01", "end": "2024-06-01",
        "universe": None, "cfg_override": {},
    })
    replay_worker.run_replay_worker(task_id, mp.Event(), mp.Queue(), mp.Queue())
    assert captured["uni"] is None              # universe_json=None 还原为 None（全市场）
    assert captured["end"] == "2024-06-01"
    assert replay_tasks_db.get_task(task_id)["status"] == "SUCCESS"
