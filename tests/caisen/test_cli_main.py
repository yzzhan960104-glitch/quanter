# -*- coding: utf-8 -*-
"""CLI 离线入口测试（蔡森形态学流水线 Phase 2 · Task 11）。

物理意图与覆盖节点（CLAUDE.md 极简 + 显式至上）：
    本测试验证 `python -m caisen screen/replay` 两个子命令的入口正确性——
    不依赖真实 data_lake parquet（CI 环境无数据），用 monkeypatch 注入合成
    价格数据 + 拦截 DataLakeReader 路径，验证 CLI 编排链路与 cfg-override。

    核心红线：
        - cfg-override JSON 解析失败 / 未知字段 → ValueError（防脏参数静默走默认）；
        - 数据湖缺失 → screen/replay 退出码 1 + 提示用户先同步数据（不静默跑空）；
        - screen 合成 W 底 → 生成 plans JSON + 候选表打印（端到端 happy path）；
        - cfg-override 真实生效（覆盖默认参数，diff 输出准确）。

覆盖节点：
    - test_parser_requires_subcommand：argparse 子命令必填校验；
    - test_apply_cfg_override_valid：合法 JSON 字段覆盖生效（model_copy）；
    - test_apply_cfg_override_bad_json：非法 JSON → ValueError；
    - test_apply_cfg_override_unknown_field：未知字段 → ValueError + 列出合法字段；
    - test_cfg_diff_summary：diff 准确列出被覆盖字段（新旧值对照）；
    - test_screen_offline_no_parquet：parquet 缺失 → 退出码 1 + 提示；
    - test_screen_synthetic_w_bottom：monkeypatch 合成 W 底 → 命中 + plans JSON；
    - test_resolve_symbol_loose_match：宽松代码匹配（"600519" 命中 "600519.SH"）。

合成序列复用 Task 6/8 的 _build_standard_w_bottom（已验证默认 cfg 能识别）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from caisen import __main__ as cli
from caisen.config import StrategyConfig


# ---------------------------------------------------------------------------
# argparse 入口
# ---------------------------------------------------------------------------
def test_parser_requires_subcommand(capsys):
    """无子命令 → argparse error（required=True）。"""
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args([])
    # argparse 缺必填参数退出码 2
    assert exc.value.code == 2


def test_parser_screen_args_parsed():
    """screen 子命令参数正确解析。"""
    args = cli._build_parser().parse_args([
        "screen", "--date", "2024-06-01",
        "--universe", "600519,000858",
        "--cfg-override", '{"confirm_bars":2}',
    ])
    assert args.command == "screen"
    assert args.date == "2024-06-01"
    assert args.universe == "600519,000858"
    assert args.cfg_override == '{"confirm_bars":2}'


def test_parser_replay_args_parsed():
    """replay 子命令参数正确解析（universe 可缺省）。"""
    args = cli._build_parser().parse_args([
        "replay", "--start", "2023-01-01", "--end", "2024-06-01",
    ])
    assert args.command == "replay"
    assert args.start == "2023-01-01"
    assert args.end == "2024-06-01"
    assert args.universe is None


# ---------------------------------------------------------------------------
# cfg-override 解析
# ---------------------------------------------------------------------------
def test_apply_cfg_override_no_override_returns_same():
    """无 override → 返回原 cfg 实例（identity）。"""
    base = StrategyConfig()
    out = cli._apply_cfg_override(base, None)
    assert out is base
    out2 = cli._apply_cfg_override(base, "")
    assert out2 is base


def test_apply_cfg_override_valid():
    """合法 JSON → 覆盖指定字段，其余字段保持默认。"""
    base = StrategyConfig()
    out = cli._apply_cfg_override(base, '{"confirm_bars":2,"min_rr_ratio":1.5}')
    assert out.confirm_bars == 2
    assert out.min_rr_ratio == 1.5
    # 未覆盖字段保持默认
    assert out.zigzag_threshold_atr == base.zigzag_threshold_atr
    # 原实例未被污染（model_copy 浅拷贝）
    assert base.confirm_bars == 3


def test_apply_cfg_override_bad_json():
    """非法 JSON → ValueError（防脏参数静默走默认）。"""
    base = StrategyConfig()
    with pytest.raises(ValueError, match="JSON 解析失败"):
        cli._apply_cfg_override(base, "{bad json}")


def test_apply_cfg_override_unknown_field():
    """未知字段 → ValueError + 提示合法字段（防拼写错误）。"""
    base = StrategyConfig()
    with pytest.raises(ValueError, match="未知字段"):
        cli._apply_cfg_override(base, '{"nonexistent_param":1}')


def test_apply_cfg_override_non_object():
    """顶层非 JSON 对象（如数组）→ ValueError。"""
    base = StrategyConfig()
    with pytest.raises(ValueError, match="JSON 对象"):
        cli._apply_cfg_override(base, '[1,2,3]')


def test_cfg_diff_summary_empty():
    """两 cfg 完全相同 → diff 空。"""
    base = StrategyConfig()
    out = cli._apply_cfg_override(base, None)
    assert cli._cfg_diff_summary(base, out) == []


def test_cfg_diff_summary_lists_overrides():
    """diff 准确列出被覆盖字段（新旧值对照）。"""
    base = StrategyConfig()
    out = cli._apply_cfg_override(base, '{"confirm_bars":2}')
    diffs = cli._cfg_diff_summary(base, out)
    assert len(diffs) == 1
    assert "confirm_bars" in diffs[0]
    assert "3" in diffs[0] and "2" in diffs[0]


# ---------------------------------------------------------------------------
# 宽松代码匹配
# ---------------------------------------------------------------------------
def test_resolve_symbol_exact():
    """完全相等 → 命中。"""
    assert cli._resolve_symbol("600519.SH", ["600519.SH", "000858.SZ"]) == "600519.SH"


def test_resolve_symbol_numeric_suffix():
    """数字代码 → 后缀匹配标准形式（最常见场景）。"""
    assert cli._resolve_symbol("600519", ["600519.SH", "000858.SZ"]) == "600519.SH"


def test_resolve_symbol_contains_fallback():
    """contains 兜底匹配。"""
    assert cli._resolve_symbol("0519", ["600519.SH"]) == "600519.SH"


def test_resolve_symbol_no_match():
    """无匹配 → None。"""
    assert cli._resolve_symbol("999999", ["600519.SH"]) is None


# ---------------------------------------------------------------------------
# 离线降级：parquet 缺失
# ---------------------------------------------------------------------------
def test_screen_offline_no_parquet(monkeypatch, capsys):
    """parquet 缺失 → screen 退出码 1 + stderr 提示先同步数据。"""
    monkeypatch.setattr(cli, "_load_daily_parquet", lambda: None)
    args = cli._build_parser().parse_args([
        "screen", "--date", "2024-06-01", "--universe", "600519",
    ])
    rc = cli._cmd_screen(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "数据湖未就绪" in captured.err
    assert "sync_data_lake" in captured.err


def test_replay_offline_no_parquet(monkeypatch, capsys):
    """parquet 缺失 → replay 退出码 1 + stderr 提示。"""
    monkeypatch.setattr(cli, "_load_daily_parquet", lambda: None)
    args = cli._build_parser().parse_args([
        "replay", "--start", "2024-01-01", "--end", "2024-06-01",
    ])
    rc = cli._cmd_replay(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "数据湖未就绪" in captured.err


# ---------------------------------------------------------------------------
# screen happy path：合成 W 底 → plans JSON
# ---------------------------------------------------------------------------
def _build_synthetic_lake_w_bottom() -> pd.DataFrame:
    """构造合成数据湖（MultiIndex date,symbol）含一只标准 W 底标的。

    复用 test_screener._build_standard_w_bottom 的序列，转成 MultiIndex DataFrame。
    """
    # 标准合成 W 底序列（20 根，depth=0.467，默认 cfg 可识别）
    close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0, 7.5,
         8.0, 8.5, 9.0, 10.0, 11.0,
         10.0, 9.0, 8.0,
         9.0, 10.0, 11.0, 13.0,
         12.5, 12.0], dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = pd.Series(200.0, index=range(len(close)))
    vol.iloc[5] = 300.0   # 左底放量
    vol.iloc[13] = 100.0  # 右底缩量
    vol.iloc[17] = 500.0  # 突破日放量
    n = len(close)
    dates = pd.bdate_range("2024-01-01", periods=n)
    # 注：构造 MultiIndex DataFrame 时显式传 index= 会让 pandas 把 Series 的
    # DatetimeIndex 与 MultiIndex 对齐 → NaN。这里取 .values（纯 ndarray）绕开对齐。
    df = pd.DataFrame({
        "open": close.values, "high": high.values, "low": low.values, "close": close.values,
        "volume": vol.values, "amount": (vol * 1e6).values,   # amount = vol × 1e6 ≥ liquidity_min_amount
    }, index=pd.MultiIndex.from_arrays([dates, ["TEST001.SZ"] * n], names=["date", "symbol"]))
    return df


def test_screen_synthetic_w_bottom(monkeypatch, tmp_path, capsys):
    """合成 W 底 → screen 命中 + plans JSON 写入 + 候选表打印。

    覆盖端到端 happy path：
        monkeypatch _load_daily_parquet 返回合成湖 → _cmd_screen 走完整链路
        → 候选 ≥ 1 + plans JSON 文件存在 + 终端打印候选表。
    """
    lake = _build_synthetic_lake_w_bottom()
    monkeypatch.setattr(cli, "_load_daily_parquet", lambda: lake)
    # 重定向 plans 输出到临时目录，避免污染仓库
    monkeypatch.setattr(cli, "_PLANS_DIR", str(tmp_path / "plans"))

    last_date = lake.index.get_level_values("date").max().strftime("%Y-%m-%d")
    # 注：合成 20 根 W 底需与 test_screener._mk_cfg 一致的参数（confirm_bars=2、
    # zigzag_threshold_atr=0.5）才能在 pivot 末段保留 P4，触发形态命中。此处用
    # cfg-override 同时验证 CLI 的参数覆盖链路与形态识别端到端 happy path。
    args = cli._build_parser().parse_args([
        "screen", "--date", last_date, "--universe", "TEST001",
        "--cfg-override", json.dumps({
            "confirm_bars": 2, "zigzag_threshold_atr": 0.5,
            "pattern_tension_ratio": 0.01, "w_price_tolerance": 0.1,
            "abc_wave_detect": False,   # 合成 W 底右脚抬升(P3>P1)，与 ABC"P3最低"冲突
            "min_rr_ratio": 0.5,        # 承 Task 9 rr 张力：标准突破 rr≈1.0，默认 3.0 会过滤掉
        }),
    ])
    rc = cli._cmd_screen(args)
    assert rc == 0
    captured = capsys.readouterr()

    # 候选表打印
    assert "CANDIDATES" in captured.out
    assert "TEST001.SZ" in captured.out
    assert "w_bottom" in captured.out
    # plans JSON 写入
    plan_file = tmp_path / "plans" / f"{last_date}.json"
    assert plan_file.exists()
    payload = json.loads(plan_file.read_text(encoding="utf-8"))
    assert payload["n_plans"] >= 1
    assert payload["plans"][0]["symbol"] == "TEST001.SZ"
    assert payload["plans"][0]["pattern_type"] == "w_bottom"
    # rr_ratio > 0（合成 W 底 plan 应通过盈亏比校验）
    assert payload["plans"][0]["rr_ratio"] > 0


def test_screen_empty_universe_error(monkeypatch, capsys):
    """universe 全未命中 → 退出码 1（合成湖含 TEST001，查 999999 不命中）。"""
    lake = _build_synthetic_lake_w_bottom()
    monkeypatch.setattr(cli, "_load_daily_parquet", lambda: lake)
    args = cli._build_parser().parse_args([
        "screen", "--date", "2024-01-29", "--universe", "999999",
    ])
    rc = cli._cmd_screen(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "全部未命中" in captured.err


def test_screen_cfg_override_takes_effect(monkeypatch, capsys):
    """cfg-override 真实生效：用极严 liquidity_min_amount 否决合成标的。"""
    lake = _build_synthetic_lake_w_bottom()
    monkeypatch.setattr(cli, "_load_daily_parquet", lambda: lake)
    monkeypatch.setattr(cli, "_PLANS_DIR", "/tmp/caisen_test_plans_unreachable")

    last_date = lake.index.get_level_values("date").max().strftime("%Y-%m-%d")
    args = cli._build_parser().parse_args([
        "screen", "--date", last_date, "--universe", "TEST001",
        "--cfg-override", json.dumps({"liquidity_min_amount": 1e15}),
    ])
    rc = cli._cmd_screen(args)
    assert rc == 0   # 命中 0 不是错误
    captured = capsys.readouterr()
    # 应被流动性淘汰（liquidity_min_amount=1e15 远超合成 amount=vol*1e6）
    assert "FAIL" in captured.out
    assert "命中候选：0" in captured.out
