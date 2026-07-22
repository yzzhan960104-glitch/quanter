# -*- coding: utf-8 -*-
"""caisen.replay_runs 回放结果持久化测试（方案 A：默认全存 + 支持删除）。

物理意图与覆盖节点（CLAUDE.md 量化风控·边界审查）：
  本测试验证蔡森「历史回放」结果的持久化仓储——把每次 run_replay 产出的
  ReplayReportResponse + 触发它的 ReplayRequest 落 JSON 文件，供前端浏览历史 /
  加载对比 / 手动删除。方案 A 明确：相同配置+标的的重复回放【不去重】，每次都存，
  由前端删除按钮做清理（用户决策：结果理论上确定可复现，但保留全部以审计/对比）。

  覆盖节点：
    1. save_run：落盘单次结果，返回 run_id；get_run 读回完整 request+report+summary；
    2. list_runs：读 index.json 摘要列表，按 created_at 降序（最新在前）；
    3. delete_run：删 run 文件 + 从 index 移除，不存在返 False（不抛）；
    4. run_id 格式：YYYYMMDD-HHMMSS-<6hex>（时间序 + 防同秒碰撞）；
    5. 路径遍历防御：run_id 含 "../" 等非法字符 → get_run/delete_run 拒绝（返 None/False）；
    6. 隔离性：每个测试 monkeypatch _REPLAY_RUNS_DIR 指向 tmp_path，绝不污染真实 replay_runs/。

设计要点（CLAUDE.md 极简 + 显式原则）：
  - 镜像 caisen/storage.py 的范式：原子写（tempfile+os.replace）+ 跨进程字节锁 +
    模块级 _REPLAY_RUNS_DIR 常量（测试 monkeypatch 隔离）；
  - _gen_run_id（时间戳+uuid）可 monkeypatch，保证 list_runs 顺序断言确定性；
  - run_id 拼进文件名，须正则白名单校验（同 storage._validate_iso_date 的路径遍历防御）。
"""
from __future__ import annotations

import json
import os
import re

import pytest

from backtest import runs as replay_runs


# ---------------------------------------------------------------------------
# 合成 request / report dict（对齐 ReplayRequest / ReplayReportResponse.model_dump）
# ---------------------------------------------------------------------------
def _mk_request(start="2024-01-01", end="2024-03-31", universe=None,
                cfg_override=None, save=True):
    """构造 ReplayRequest.model_dump 等价 dict（save 默认 True=默认都存）。"""
    return {
        "start": start,
        "end": end,
        "universe": universe,
        "cfg_override": cfg_override or {"min_rr_ratio": 1.5},
        "save": save,
    }


def _mk_report(n_hits=12, win_rate=0.58, avg_rr=1.42, max_drawdown=-0.21,
               annualized_return=0.35, equity_curve=None, trades=None):
    """构造 ReplayReportResponse.model_dump 等价 dict。"""
    return {
        "n_hits": n_hits,
        "win_rate": win_rate,
        "avg_rr": avg_rr,
        "max_drawdown": max_drawdown,
        "pattern_dist": {"w_bottom": 8, "head_shoulder": 4},
        "monthly_returns": {"2024-01": 0.6, "2024-02": 0.82},
        "avg_holding_bars": 6.5,
        "min_rr_ratio_recommendation": "建议保留当前阈值（EV=0.30 > 0.2）。",
        "equity_curve": equity_curve or [{"date": "2024-01-10", "cumulative_rr": 0.1, "equity": 1.001}],
        "trades": trades or [],
        "annualized_return": annualized_return,
        "n_trading_days": 60,
    }


# ---------------------------------------------------------------------------
# 公共 fixture：隔离 replay_runs 目录 + 注入确定性 run_id（防时间碰撞）
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_runs(tmp_path, monkeypatch):
    """每个测试自动隔离：_REPLAY_RUNS_DIR 指向 tmp_path/replay_runs。

    防御性（CLAUDE.md 量化风控·边界审查）：测试落盘的 replay_runs JSON 绝不能写入
    生产目录，避免 CI 脏数据干扰后续真实运行。同时注入确定性 _gen_run_id 计数器，
    保证 list_runs 顺序断言不受真实时间/uuid 随机性影响。
    """
    runs_dir = tmp_path / "replay_runs"
    monkeypatch.setattr(replay_runs, "_REPLAY_RUNS_DIR", str(runs_dir))

    # 确定性 run_id 序列：每次调用递增秒数，保证 created_at 单调递增 + run_id 不碰撞。
    counter = {"i": 0}

    def _fake_gen():
        # 基线 2024-01-01 00:00:00 + i 秒，run_id 与 created_at 同源（顺序一致）
        import datetime as _dt
        base = _dt.datetime(2024, 1, 1, 0, 0, 0) + _dt.timedelta(seconds=counter["i"])
        counter["i"] += 1
        created_at = base.isoformat(timespec="seconds")
        run_id = base.strftime("%Y%m%d-%H%M%S") + "-" + f"{counter['i']:06x}"
        return run_id, created_at

    monkeypatch.setattr(replay_runs, "_gen_run_id", _fake_gen)
    yield


# ---------------------------------------------------------------------------
# 1. save_run：落盘 + 返回 run_id
# ---------------------------------------------------------------------------
class TestSaveRun:
    """save_run(request, report) -> run_id：落盘单次回放结果。"""

    def test_save_run_returns_run_id_and_persists(self):
        """save_run 返回非空 run_id，且 get_run 能读回完整 request + report。"""
        req = _mk_request()
        report = _mk_report()
        run_id = replay_runs.save_run(req, report)

        assert isinstance(run_id, str) and run_id
        # 读回：request + report 完整保留
        loaded = replay_runs.get_run(run_id)
        assert loaded is not None
        assert loaded["request"]["start"] == req["start"]
        assert loaded["report"]["n_hits"] == report["n_hits"]
        assert loaded["report"]["win_rate"] == report["win_rate"]
        # 元信息回填
        assert loaded["run_id"] == run_id
        assert loaded["created_at"]

    def test_save_run_id_format(self):
        """run_id 格式 = YYYYMMDD-HHMMSS-<6hex>（时间序前缀 + 6 位 hex 防同秒碰撞）。

        格式即白名单正则——get_run/delete_run 的路径遍历防御同源（只放行此格式）。
        """
        run_id = replay_runs.save_run(_mk_request(), _mk_report())
        assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{6}$", run_id), \
            f"run_id 格式非法：{run_id}"

    def test_save_run_creates_run_file_and_index(self, tmp_path):
        """save_run 同时落 <run_id>.json 与 index.json。"""
        req = _mk_request()
        report = _mk_report()
        run_id = replay_runs.save_run(req, report)

        runs_dir = replay_runs._REPLAY_RUNS_DIR
        assert os.path.isfile(os.path.join(runs_dir, f"{run_id}.json"))
        assert os.path.isfile(os.path.join(runs_dir, "index.json"))

    def test_save_run_duplicate_keeps_both(self):
        """方案 A 核心：相同配置+标的的重复回放不去重，两次 save 产生两条记录。

        物理意图（用户决策）：相同 request→相同 report（确定性可复现），但保留全部
        以便审计/对比；由前端删除按钮做清理，而非写入时去重。
        """
        req = _mk_request()
        report = _mk_report()
        id1 = replay_runs.save_run(req, report)
        id2 = replay_runs.save_run(req, report)
        assert id1 != id2, "方案 A：重复回放应产生不同 run_id（不去重）"
        assert len(replay_runs.list_runs()) == 2


# ---------------------------------------------------------------------------
# 2. list_runs：index.json 摘要列表
# ---------------------------------------------------------------------------
class TestListRuns:
    """list_runs() -> [summary]：读 index 摘要，按 created_at 降序（最新在前）。"""

    def test_list_runs_empty_when_no_dir(self):
        """无 replay_runs 目录 / 无 index → 返回空列表（不抛异常）。"""
        assert replay_runs.list_runs() == []

    def test_list_runs_returns_summaries_desc(self):
        """存 3 条 → list_runs 返回 3 条摘要，按 created_at 降序（最新在前）。"""
        for i in range(3):
            replay_runs.save_run(_mk_request(), _mk_report(n_hits=10 + i))
        runs = replay_runs.list_runs()
        assert len(runs) == 3
        # 降序：第一条 created_at 最大（fake_gen 递增，最后存的最大）
        assert runs[0]["created_at"] >= runs[1]["created_at"] >= runs[2]["created_at"]
        # 摘要含关键字段（不含完整 trades/equity_curve，保持 list 轻量）
        s = runs[0]
        for k in ("run_id", "created_at", "start", "end", "universe_n",
                  "n_hits", "win_rate", "avg_rr", "annualized_return", "max_drawdown"):
            assert k in s, f"摘要缺字段 {k}"

    def test_list_runs_summary_universe_n(self):
        """universe=None→universe_n=-1（全市场标记）；列表→len。"""
        replay_runs.save_run(_mk_request(universe=None), _mk_report())
        replay_runs.save_run(_mk_request(universe=["A.SZ", "B.SZ", "C.SZ"]), _mk_report())
        runs = replay_runs.list_runs()
        by_n = {r["universe_n"] for r in runs}
        assert -1 in by_n, "universe=None 应记 universe_n=-1（全市场）"
        assert 3 in by_n, "universe=['A','B','C'] 应记 universe_n=3"

    def test_list_runs_does_not_load_heavy_fields(self):
        """list_runs 摘要不含完整 trades/equity_curve（避免大列表读全量）。"""
        big_trades = [{"symbol": f"S{i}.SZ", "rr": 1.0} for i in range(500)]
        replay_runs.save_run(_mk_request(), _mk_report(trades=big_trades))
        runs = replay_runs.list_runs()
        assert len(runs) == 1
        assert "trades" not in runs[0], "摘要不应含完整 trades（list 应轻量）"
        assert "equity_curve" not in runs[0]


# ---------------------------------------------------------------------------
# 3. get_run：单条完整记录
# ---------------------------------------------------------------------------
class TestGetRun:
    """get_run(run_id) -> dict | None：读完整记录（request + report + summary）。"""

    def test_get_run_returns_full_record(self):
        """get_run 返回完整 request + report（含 trades/equity_curve）。"""
        trades = [{"symbol": "X.SZ", "rr": 1.5}]
        replay_runs.save_run(_mk_request(), _mk_report(trades=trades))
        runs = replay_runs.list_runs()
        rid = runs[0]["run_id"]
        loaded = replay_runs.get_run(rid)
        assert loaded is not None
        assert loaded["report"]["trades"] == trades
        assert loaded["request"]["cfg_override"]["min_rr_ratio"] == 1.5

    def test_get_run_nonexistent_returns_none(self):
        """run_id 不存在 → None（路由层转 404）。"""
        assert replay_runs.get_run("20240101-000000-deadbe") is None

    def test_get_run_rejects_path_traversal(self):
        """run_id 含路径跳板字符 → None（不读取目录外文件，路径遍历防御）。"""
        assert replay_runs.get_run("../etc/passwd") is None
        assert replay_runs.get_run("20240101-000000-aaaaaa/../../etc") is None
        assert replay_runs.get_run("") is None


# ---------------------------------------------------------------------------
# 4. delete_run：删除 + index 同步
# ---------------------------------------------------------------------------
class TestDeleteRun:
    """delete_run(run_id) -> bool：删文件 + 从 index 移除。"""

    def test_delete_run_removes_file_and_index(self):
        """save→delete：文件消失，list_runs 不再含，返回 True。"""
        run_id = replay_runs.save_run(_mk_request(), _mk_report())
        assert len(replay_runs.list_runs()) == 1

        ok = replay_runs.delete_run(run_id)
        assert ok is True
        assert replay_runs.get_run(run_id) is None
        assert replay_runs.list_runs() == []

    def test_delete_one_of_many_only_removes_target(self):
        """删一条不影响其它（index 精确移除，非整体重建丢数据）。"""
        ids = [replay_runs.save_run(_mk_request(), _mk_report(n_hits=i))
               for i in range(3)]
        assert replay_runs.delete_run(ids[1]) is True
        remaining = {r["run_id"] for r in replay_runs.list_runs()}
        assert remaining == {ids[0], ids[2]}

    def test_delete_run_nonexistent_returns_false(self):
        """删不存在的 run_id → 返回 False（不抛异常）。"""
        assert replay_runs.delete_run("20240101-000000-deadbe") is False

    def test_delete_run_rejects_path_traversal(self):
        """run_id 含路径跳板字符 → 返回 False（不删目录外文件）。"""
        assert replay_runs.delete_run("../etc/passwd") is False
        assert replay_runs.delete_run("") is False
