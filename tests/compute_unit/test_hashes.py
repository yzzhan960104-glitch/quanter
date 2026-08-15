# -*- coding: utf-8 -*-
"""engine_hash 指纹覆盖测试（P1-3 · 指纹必须罩住完整回测内核）。"""
from compute_unit import hashes


def test_engine_files_cover_replay_kernel():
    """指纹文件清单必须含回测内核：replay/models/strategy/execution/signal/method_v0/objective/price_levels。

    生产改动：compute_unit v2 支持 replay 模式后，指纹若仍只含 backtest.py+method_v0.py，
    execution.py/replay.py 等改动会漏检跨机漂移——本测试钉死清单。
    T18（2026-08-15）：补 price_levels.py——价位数学单源是 backtest 的传递依赖，
    不入清单则改价位公式指纹不变（T7 遗留，06-tech-debt 波次遗留清单登记项）。
    """
    required = {
        "strategies/neckline/backtest.py",
        "strategies/neckline/price_levels.py",
        "strategies/neckline/method_v0.py",
        "strategies/neckline/strategy.py",
        "strategies/neckline/execution.py",
        "strategies/neckline/signal.py",
        "backtest/replay.py",
        "backtest/models.py",
        "discovery/objective.py",
    }
    assert required <= set(hashes.ENGINE_FILES)


def test_engine_hash_matches_discovery_runner():
    """Win discovery.runner 与 compute_unit 的指纹算法必须一致（同一内核同指纹）。"""
    from discovery.runner import _engine_hash as discovery_hash
    assert hashes._engine_hash() == discovery_hash()
