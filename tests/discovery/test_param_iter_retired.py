# -*- coding: utf-8 -*-
"""legacy param_iter 退役守卫单测（2026-08-03 双轨治理）。

物理意图：discovery daemon 已是参数搜索单一真相源（L0-L5），param_iter 继续夜跑
会产生双轨冠军（param_iter_state.json vs experiment.db ACTIVE）。本测试把 param_iter
入口改为 fail-closed：不带 ``--legacy`` 显式开关一律拒绝运行，防任何外部脚本/手动
启动再次拉起 legacy 搜索。PARAM_SPACE 仍被 discovery.sampler 复用（import 不受影响）。
"""
import pytest

from discovery.tools import param_iter


def test_param_iter_main_refuses_without_legacy_flag():
    """不带 --legacy 必须拒绝（退役 fail-closed，防双轨冠军复活）。"""
    with pytest.raises(SystemExit) as ei:
        param_iter.main(["--time-budget", "1"])
    assert ei.value.code != 0


def test_param_iter_main_accepts_explicit_legacy_flag(monkeypatch):
    """显式 --legacy 才放行到执行体（monkeypatch 证明越过守卫而非静默跳过）。"""
    monkeypatch.setattr(
        param_iter, "load_universe",
        lambda: (_ for _ in ()).throw(AssertionError("reached legacy body")))
    with pytest.raises(AssertionError, match="reached legacy body"):
        param_iter.main(["--legacy", "--time-budget", "1"])
