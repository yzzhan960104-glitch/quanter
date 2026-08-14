# -*- coding: utf-8 -*-
"""G7 告警可观测测试（discovery 4 文件降级点）。

物理意图（task-G7 Part A · 消「监控监控器」盲区）：
    discovery 的 worker/daemon/snapshot/objective 存在大量 ``except Exception:
    pass/return None/continue`` 零日志降级——降级本身正确（单点失败不阻断跑批/主路径，
    spec §7/§8），但「静默降级」让运维无法回答「discovery 跑批是否在某处吞异常 /
    哪只标的反复失败」。本测试套断言：触发降级时 caplog 有 warning 记录，且**控制流
    不变**（仍降级返回原值/空集）——只加可观测，不改降级语义。

TDD RED→GREEN：先写失败测试（当前零日志 → caplog 空 → 断言失败），再实现加 log。
全中文注释（CLAUDE.md 协议）。
"""
from __future__ import annotations

import logging
import types

import pandas as pd
import pytest


# ============================= objective.run_full_scan =============================

def test_run_full_scan_symbol_exception_logged(monkeypatch, caplog):
    """run_full_scan 中 scan_symbol 抛异常 → continue（控制流不变）+ caplog 有 warning。

    物理意图：单标的 scan 异常不应炸整批（spec §8 单 trial 失败不影响 run），但原
    ``except: continue`` 零日志 → 某标的反复失败（数据残缺/内核崩溃）无人知晓。
    加 warning 后运维可定位「哪只标的在吞异常」，消监控盲区。
    """
    import discovery.objective as objective
    # params 必须含全部 ID_KEYS+EXEC_KEYS（run_full_scan 在 try 外构造 id_cfg/exec_cfg，
    # 缺键 KeyError 在循环前）。用 DEFAULTS+EXEC_DEFAULTS 补全（scan 被 mock，值不跑）。
    from strategies.neckline.method_v0 import DEFAULTS
    from strategies.neckline.backtest import EXEC_DEFAULTS
    params = {**DEFAULTS, **EXEC_DEFAULTS}

    def boom(*a, **kw):
        raise RuntimeError("scan_symbol 注入失败")

    monkeypatch.setattr(objective, "scan_symbol", boom)

    # 合成最小 universe（不跑真实 scan，只触发 except 分支）
    sym_df = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1], "amount": [1]},
        index=pd.to_datetime(["2025-01-01"]))
    universe = {"BAD_SYM": sym_df}

    with caplog.at_level(logging.WARNING, logger="discovery.objective"):
        result = objective.run_full_scan(params, universe)

    # 控制流不变：异常标的 skip，返空 list
    assert result == []
    # 可观测：caplog 有 warning 含标的名（定位反复失败的标的）
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("BAD_SYM" in m for m in msgs), f"期望 caplog 含 BAD_SYM 降级告警，实得：{msgs}"


# ============================= snapshot.load_universe =============================

def test_load_universe_parquet_fallback_logged(monkeypatch, caplog):
    """load_universe: filters 推送失败回退全量读（控制流不变）+ caplog 有 warning。

    物理意图：read_parquet 带 filters 推送在异常湖/旧引擎上会抛，回退全量读语义不变
    （仅内存优化失效）。原 ``except: lake = read_parquet(...)`` 零日志 → 内存优化
    静默失效无人知晓（12 worker × 1.3GB 是内存峰值源）。加 warning 让运维可见回退。
    """
    import discovery.snapshot as snap

    # 合成空 MultiIndex lake（回退路径的 index 操作不报错）
    empty_lake = pd.DataFrame(
        {"amount": [], "open": [], "high": [], "low": [], "close": [], "volume": []},
        index=pd.MultiIndex.from_tuples([], names=["date", "symbol"]))

    calls = {"n": 0}

    def fake_read_parquet(path, *a, **kw):
        calls["n"] += 1
        # 第一次（带 filters）抛 → 触发回退；第二次（回退全量读）返空 lake
        if calls["n"] == 1 and kw.get("filters") is not None:
            raise RuntimeError("filters 推送失败（注入）")
        return empty_lake

    monkeypatch.setattr(pd, "read_parquet", fake_read_parquet)

    with caplog.at_level(logging.WARNING, logger="discovery.snapshot"):
        universe = snap.load_universe()

    # 控制流不变：回退全量读（空 lake → 空 universe）
    assert universe == {}
    assert calls["n"] == 2  # 确认走了回退路径
    # 可观测：caplog 有 warning
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert msgs, "期望 parquet filters 回退降级有 warning 日志"


# ============================= worker（memory_cap + _eval_worker） =============================

def test_memory_cap_psutil_exception_logged(monkeypatch, caplog):
    """memory_cap_n_proc: psutil 异常 → 降级返 CPU cap（控制流不变）+ caplog 有 warning。

    物理意图：psutil 不可用（异常环境）时 memory_cap 降级返 CPU cap（只留 CPU 约束）。
    原零日志 → 内存自动降并发机制静默失效（2026-08-03 MemoryError 复发防线之一）。
    """
    import discovery.worker as worker
    import psutil

    def boom():
        raise RuntimeError("psutil 注入失败")

    monkeypatch.setattr(psutil, "virtual_memory", boom)

    with caplog.at_level(logging.WARNING, logger="discovery.worker"):
        n = worker.memory_cap_n_proc()

    # 控制流不变：降级返 CPU cap
    assert n == worker._N_PROC_CPU_CAP
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert msgs, "期望 psutil 异常降级有 warning 日志"


def test_eval_worker_exception_logged(monkeypatch, caplog):
    """_eval_worker: evaluate 抛异常 → 返 None（控制流不变）+ caplog 有 warning。

    物理意图：单 trial 评估异常（spec §8 拷问②）返 None，主进程 filter null，run 继续。
    原零日志 → 某 params 反复让 evaluate 崩溃无人知晓，null 被静默过滤。
    """
    import discovery.worker as worker

    def boom(*a, **kw):
        raise RuntimeError("evaluate 注入失败")

    monkeypatch.setattr(worker, "evaluate", boom)
    # 绕过 initializer：直接设 _WORKER_STATE ready（主进程调 _eval_worker）
    monkeypatch.setattr(worker, "_WORKER_STATE",
                        {"universe": {}, "split": None, "ready": True})

    with caplog.at_level(logging.WARNING, logger="discovery.worker"):
        result = worker._eval_worker({"window": 1})

    # 控制流不变：异常返 None（主进程 filter null）
    assert result is None
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert msgs, "期望 _eval_worker 异常降级有 warning 日志"


# ============================= daemon（eval_outer / notify / auto_publish 三处降级） =====

def _setup_daemon_env(tmp_path):
    """复用 test_daemon.py 的最小 daemon 环境（init_db + meta + split + run_search_fn）。

    造一个最小 RunSummary（避免跑真实 run_search），并模拟 run_search 的 write_search_run
    副作用（daemon read_latest_search_run 依赖此行存在）。top_trial_id="t1" 非空 →
    触发 eval_outer_fn/auto_publish_fn 调用条件。
    """
    from discovery.snapshot import SnapshotMeta
    from discovery.split import holdout_split
    from discovery.store import init_db, connect, write_search_run

    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "d", "2025-01-01")
    split = holdout_split()
    summary = types.SimpleNamespace(
        run_id="r1", frontier_size=3, status="budget_exhausted",
        top_trial_id="t1", top_inner_calmar=1.0, rho=0.9, ei=0.0005,
        snapshot_hash="snap1", n_new_trials=5, convergence_reason="")

    def _rs(*a, **kw):
        with connect(db) as conn:
            write_search_run(conn, run_id=summary.run_id, snapshot_hash="snap1",
                             started_at="t0", ended_at="t0e", n_trials=5,
                             status=summary.status, frontier_size=summary.frontier_size,
                             k_rounds_no_expansion=0, daemon_run_count=0, note="")
        return summary

    return db, meta, split, _rs


def test_daemon_eval_outer_degradation_logged(tmp_path, caplog):
    """eval_outer_fn 抛异常 → outer=None（控制流不变）+ caplog 有 warning。

    物理意图：冠军 outer 去偏失败（trial 不存在/数据缺失）软降级返 None，不阻断 daemon。
    原零日志 → outer 指标静默缺失，研究员以为跑成功实际无 OOS 指标。
    """
    from discovery.daemon import run_daemon_cycle
    db, meta, split, rs_fn = _setup_daemon_env(tmp_path)

    def boom(_trial_id):
        raise RuntimeError("eval_outer 注入失败")

    with caplog.at_level(logging.WARNING, logger="discovery.daemon"):
        out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, eval_outer_fn=boom)

    # 控制流不变：outer 软降级返 None
    assert out["outer"] is None
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("outer" in m.lower() for m in msgs), f"期望 eval_outer 降级告警，实得：{msgs}"


def test_daemon_notify_degradation_logged(tmp_path, caplog):
    """notify_fn 抛异常 → pass（控制流不变）+ caplog 有 warning。

    物理意图：钉钉告警失败不阻断 daemon 主流程。原零日志 → 钉钉通道静默死掉无人知晓
    （「监控监控器」盲区典型：告警系统自己是单点故障源却无观测）。
    """
    from discovery.daemon import run_daemon_cycle
    db, meta, split, rs_fn = _setup_daemon_env(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("notify 注入失败")

    with caplog.at_level(logging.WARNING, logger="discovery.daemon"):
        out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, notify_fn=boom)

    # 控制流不变：daemon 正常返回（notify 异常被吞）
    assert "latest_k" in out
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("notify" in m.lower() or "告警" in m or "通知" in m for m in msgs), \
        f"期望 notify 降级告警，实得：{msgs}"


def test_daemon_auto_publish_degradation_logged(tmp_path, caplog):
    """auto_publish_fn 抛异常 → auto_exp_id=None（控制流不变）+ caplog 有 warning。

    物理意图：自动 publish 桥（实验平台打通）失败软降级，不阻断 daemon。原零日志 →
    新冠军静默未 publish 到实验平台，研究员漏接。
    """
    from discovery.daemon import run_daemon_cycle
    db, meta, split, rs_fn = _setup_daemon_env(tmp_path)

    def boom(_trial_id, _outer):
        raise RuntimeError("auto_publish 注入失败")

    with caplog.at_level(logging.WARNING, logger="discovery.daemon"):
        out = run_daemon_cycle(meta, split, db, run_search_fn=rs_fn, auto_publish_fn=boom)

    # 控制流不变：auto_published_experiment 软降级返 None
    assert out["auto_published_experiment"] is None
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("publish" in m.lower() for m in msgs), f"期望 auto_publish 降级告警，实得：{msgs}"
