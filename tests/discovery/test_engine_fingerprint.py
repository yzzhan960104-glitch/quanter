# -*- coding: utf-8 -*-
"""engine_hash 指纹单源守护（P1-3 覆盖守卫 + 单源委托守护）。

出身：2026-08-18 compute_unit 退役（ADR-17）时自 tests/compute_unit/test_hashes.py
迁入——内核覆盖守卫原样保留；双实现一致性断言转型为单源委托守护（防未来再内联
出漂移副本，2026-08-13 三份实现不一致事故的防线）。退役搬迁等价断言（旧
compute_unit.hashes ↔ fingerprint 输出恒等，G1 门 ce16cc4ee4de 留证）已完成使命，
随 T2 删包移除。
"""
from discovery import fingerprint


def test_engine_files_cover_replay_kernel():
    """指纹清单必须罩住完整回测内核：replay/models/strategy/execution/signal/method_v0/objective/price_levels。

    生产教训（P1-3）：指纹曾只含 backtest.py+method_v0.py，execution.py/replay.py 等
    改动漏检——老 trial 不标 stale。T18（2026-08-15）补 price_levels.py——价位数学
    单源是 backtest 的传递依赖，不入清单则改价位公式指纹不变。本测试钉死清单。
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
    assert required <= set(fingerprint.ENGINE_FILES)


def test_runner_and_cli_delegate_to_fingerprint():
    """runner/cli 的 _engine_hash 必须与 fingerprint 单源一致（防再内联漂移副本）。"""
    from discovery.runner import _engine_hash as runner_hash
    from discovery.cli import _engine_hash as cli_hash
    assert runner_hash() == fingerprint.engine_hash()
    assert cli_hash() == fingerprint.engine_hash()
