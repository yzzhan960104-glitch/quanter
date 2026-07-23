# -*- coding: utf-8 -*-
"""llm_throttle_proxy 节流策略契约：最小请求间隔 + 上游 529 过载退避 + 成功恢复。

物理意图：z.ai coding plan 服务端在 agent 连续密集调 LLM（重自主任务）时返回
529 overloaded，本策略在每次转发前强制最小请求间隔（降 RPM），并在检测到上游 529
时切到更长 cooldown（自适应过载窗口），上游恢复(非 529)后回退——把"撞墙"变成
"主动让速"，用"更慢"换"不撞墙"。

纯逻辑测试，不涉网络/真实时钟：用注入的 now 时间戳驱动，确定性验证间隔/退避/恢复。
（HTTP 流式反代胶水层另起集成冒烟脚本验证，见 scripts/smoke_throttle_proxy.py。）
"""
import sys
from pathlib import Path

# scripts/ 非项目包（无 __init__.py），测试单独把 scripts/ 加 path 才能 import
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pytest


def test_first_request_no_wait():
    """首次请求（无历史）→ 不等待，立即放行。"""
    from llm_throttle_proxy import ThrottlePolicy

    p = ThrottlePolicy(min_interval=8.0)
    assert p.next_sleep(now=100.0) == 0.0


def test_second_request_within_interval_waits():
    """距上次发起 < min_interval → 等够差值凑足间隔（降 RPM 的核心）。"""
    from llm_throttle_proxy import ThrottlePolicy

    p = ThrottlePolicy(min_interval=8.0)
    p.record(now=100.0, upstream_status=200)   # 上次发起于 t=100
    assert p.next_sleep(now=103.0) == pytest.approx(5.0)   # 已过 3s，再等 5s 凑足 8s


def test_second_request_after_interval_no_wait():
    """距上次发起 ≥ min_interval → 不等待（间隔已满足）。"""
    from llm_throttle_proxy import ThrottlePolicy

    p = ThrottlePolicy(min_interval=8.0)
    p.record(now=100.0, upstream_status=200)
    assert p.next_sleep(now=108.0) == 0.0    # 刚好 8s
    assert p.next_sleep(now=200.0) == 0.0    # 远超也不额外等


def test_overload_529_triggers_long_cooldown():
    """上游 529 → 进入过载态，下次用 overload_cooldown(20s) 而非 min_interval(8s)。"""
    from llm_throttle_proxy import ThrottlePolicy

    p = ThrottlePolicy(min_interval=8.0, overload_cooldown=20.0)
    p.record(now=100.0, upstream_status=529)   # 检测到过载
    assert p.next_sleep(now=103.0) == pytest.approx(17.0)   # 20-3=17，而非 8-3=5


def test_recovers_after_success():
    """529 后一次成功(200) → 退避解除，恢复 min_interval（过载窗口已过）。"""
    from llm_throttle_proxy import ThrottlePolicy

    p = ThrottlePolicy(min_interval=8.0, overload_cooldown=20.0)
    p.record(now=100.0, upstream_status=529)   # 进入过载
    p.record(now=120.0, upstream_status=200)   # 成功，应解除退避
    assert p.next_sleep(now=123.0) == pytest.approx(5.0)   # 回到 8-3=5，不是 20-3=17


def test_non_529_error_does_not_trigger_overload():
    """非 529 的错误(如 500)不视为过载 → 仍用 min_interval（避免误退避拖死正常抖动）。"""
    from llm_throttle_proxy import ThrottlePolicy

    p = ThrottlePolicy(min_interval=8.0, overload_cooldown=20.0)
    p.record(now=100.0, upstream_status=500)
    assert p.next_sleep(now=103.0) == pytest.approx(5.0)   # 8-3=5，不是 20-3=17
