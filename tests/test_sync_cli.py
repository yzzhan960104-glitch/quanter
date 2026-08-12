# -*- coding: utf-8 -*-
"""统一同步 CLI 参数解析 + key 过滤 + fail-soft 测试（Plan Task 9）。

物理意图：把散装 data/tools/sync_*.py 能力收敛到 python -m data.sync，底层复用 sync_dataset。
"""
from unittest.mock import patch
import pytest

from data.sync_cli import parse_args, select_keys, run


def test_parse_args_全量():
    args = parse_args(["--all", "--since", "2021-01-01"])
    assert args.all is True
    assert args.since == "2021-01-01"
    assert args.quota is None


def test_parse_args_指定keys():
    args = parse_args(["--keys", "daily,weekly", "--since", "2021-01-01"])
    assert args.keys == ["daily", "weekly"]  # 逗号分隔解析为列表
    assert args.all is False


def test_parse_args_dry_run():
    args = parse_args(["--keys", "moneyflow", "--dry-run"])
    assert args.dry_run is True


def test_parse_args_quota过滤():
    args = parse_args(["--all", "--quota", "special"])
    assert args.quota == "special"


def test_select_keys_按quota过滤():
    """--quota basic 只选基础桶 key（排除 special 的 moneyflow）。"""
    basic = select_keys(all_keys=True, keys=None, quota="basic")
    assert "stock_basic" in basic
    assert "moneyflow" not in basic  # moneyflow 是 special


def test_select_keys_指定keys():
    # daily 已退役（T13-A 双轨收口），用有效 key 验证指定列表原样返回
    sel = select_keys(all_keys=False, keys=["weekly", "monthly"], quota=None)
    assert sel == ["weekly", "monthly"]


def test_select_keys_退役key被过滤():
    """daily 已退役（T13-A），--keys 含 daily 应被过滤（不 KeyError 崩 CLI）。"""
    sel = select_keys(all_keys=False, keys=["daily", "weekly"], quota=None)
    assert sel == ["weekly"], "退役 key（daily）应被过滤，保留有效 key"


def test_select_keys_退役key配quota不崩():
    """--keys daily --quota basic：daily 退役先被过滤，quota 过滤不 KeyError（review P2）。"""
    sel = select_keys(all_keys=False, keys=["daily", "stock_basic"], quota="basic")
    assert "daily" not in sel, "退役 key 应先被过滤"
    assert "stock_basic" in sel, "quota basic 有效 key 应保留"


def test_select_keys_retired_with_quota_no_keyerror():
    """P2 回归：退役 key + quota 组合不 KeyError（原崩溃路径 --keys daily --quota basic）。

    物理意图：复现 select_keys 修复前的崩溃路径——daily 已退役不在 TUSHARE_DATASETS，
    修复前 quota 过滤对 daily 取 TUSHARE_DATASETS[k] 直接 KeyError（sync_cli.py:73 注释）。
    补强点：指定列表「全为退役 key」+ quota，结果应为空列表，且不崩——
    既有 test_select_keys_退役key配quota不崩 用混合（退役+有效）输入，未覆盖此「全退役→空」边界。
    """
    from data.sync_cli import select_keys
    sel = select_keys(all_keys=False, keys=["daily"], quota="basic")
    assert sel == [], "退役 key 经 quota 过滤后为空，不崩"


def test_select_keys_filters_unavailable_marker(monkeypatch):
    """P3 守卫：_unavailable 标记的 key 一律被过滤，防 select_keys 漏过滤再生。"""
    from data import sync_cli
    fake = {"good": {"quota_type": "basic"}, "bad": {"quota_type": "basic", "_unavailable": True}}
    monkeypatch.setattr(sync_cli, "TUSHARE_DATASETS", fake)
    sel = sync_cli.select_keys(all_keys=True, keys=None, quota=None)
    assert sel == ["good"], "_unavailable key 必须被过滤"


def test_run_单key失败不中断后续(monkeypatch):
    """fail-soft：某 key 抛异常，后续 key 仍跑，汇总 exit code=1。用真实注册表 key（run 访问 cfg）。"""
    calls = []
    def fake_sync(key, start, end, **kw):
        calls.append(key)
        if key == "moneyflow":
            raise RuntimeError("故意失败")
    monkeypatch.setattr("data.sync_cli.sync_dataset", fake_sync)
    rc = run(keys=["stock_basic", "moneyflow", "hs_const_sh"], since="2021-01-01", end="2021-01-02")
    assert rc == 1  # 部分失败
    assert calls == ["stock_basic", "moneyflow", "hs_const_sh"]  # moneyflow 失败后 hs_const_sh 仍执行


def test_run_全成功返0(monkeypatch):
    monkeypatch.setattr("data.sync_cli.sync_dataset", lambda key, start, end, **kw: None)
    rc = run(keys=["stock_basic", "hs_const_sh"], since="2021-01-01", end="2021-01-02")
    assert rc == 0
