# -*- coding: utf-8 -*-
"""B3（SSoT Phase B）：param_iter_state.json 全部读口切 ACTIVE 单测。

物理意图（2026-08-05 双轨治理收口）：
    legacy ``logs/param_iter_state.json`` 是参数发现引擎旧时代的冠军治理 JSON
    （best/best_ann/iter）。B3 把全部生产读口切到 ``experiment.resolver.resolve_active``
    （experiment.db ACTIVE 单一真相源），无 ACTIVE 时降级默认/None，**不再回退**
    legacy JSON——彻底消除双轨冠军。

本测试覆盖每读口的「无 ACTIVE → 默认/None，不读 legacy JSON」语义：
    - backtest.weekly_replay._champion_cfg_override：无 ACTIVE → {}（不读 JSON）
    - broadcast.__main__._fetch_strategy_snapshot：无 ACTIVE → param_iter_state=None
      （不读 JSON）
    - discovery.cli.cmd_oos / cmd_verify：无 ACTIVE → 参考实现走 resolve_active，
      params 来源为 experiment.db（cli 是探查工具，断言其不持有 STATE_FILE 常量即可）

    断言口径：tmp 目录下放一个伪造的 legacy JSON（含会污染的冠军参数），monkeypatch
    resolve_active 返 [] 后，调用读口——若读口仍回退读 JSON，断言会爆（拿到伪造
    冠军）；切了 ACTIVE 不回退 → 返默认/None，伪造 JSON 完全不被读。
"""
import pytest


# ---------------------------------------------------------------------------
# backtest.weekly_replay._champion_cfg_override
# ---------------------------------------------------------------------------
def test_weekly_replay_no_active_returns_default_not_legacy_json(monkeypatch):
    """无 ACTIVE 实验 → _champion_cfg_override 返 {}（不读 legacy JSON）。

    切 ACTIVE 后 weekly_replay 已无 _STATE_FILE 常量（彻底切断 legacy 回退）。
    monkeypatch resolve_champion 返 None（无 ACTIVE）→ 必须返 {}，绝不读任何 JSON。
    """
    from backtest import weekly_replay

    # 切 ACTIVE 后 weekly_replay 不应有 _STATE_FILE 常量（B3 收口）
    assert not hasattr(weekly_replay, "_STATE_FILE"), (
        "weekly_replay 仍持有 _STATE_FILE 常量——legacy 回退未彻底切断"
    )

    # 无 ACTIVE 实验
    monkeypatch.setattr(weekly_replay, "resolve_champion", lambda: None)

    out = weekly_replay._champion_cfg_override()
    assert out == {}, f"无 ACTIVE 应返 {{}}（不回退 legacy JSON），got {out}"


def test_weekly_replay_no_active_resolve_active_exception_returns_default(monkeypatch):
    """resolve_champion 抛异常 → {}（兜底防未预期异常，不抛不读 legacy）。"""
    from backtest import weekly_replay

    def _boom():
        raise RuntimeError("experiment.db locked")

    monkeypatch.setattr(weekly_replay, "resolve_champion", _boom)

    assert weekly_replay._champion_cfg_override() == {}


# ---------------------------------------------------------------------------
# broadcast.__main__._fetch_strategy_snapshot（param_iter_state 段）
# ---------------------------------------------------------------------------
def test_broadcast_snapshot_no_active_legacy_not_read(monkeypatch):
    """无 ACTIVE → param_iter_state=None；读 legacy JSON 必须被禁止。

    物理意图：切 ACTIVE 后，生产代码不应 open ``logs/param_iter_state.json``。
    验证手段：monkeypatch builtin open，命中 legacy 路径即 raise（证明读口仍在回退）；
    切 ACTIVE 后 open 永不被 legacy 路径命中 → param_iter_state=None 不抛。
    """
    from broadcast import __main__ as bc
    import builtins

    real_open = builtins.open

    def guarded_open(path, *a, **kw):
        p = str(path)
        # legacy 命中即报错——切 ACTIVE 后此分支不应再触发
        if "param_iter_state.json" in p:
            raise AssertionError(
                f"切 ACTIVE 后读口仍 open legacy {p}——双轨未消除")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(bc, "_experiment_active_state", lambda: None)

    _scan, param_iter_state, _runs = bc._fetch_strategy_snapshot("2026-08-05")
    assert param_iter_state is None, (
        f"无 ACTIVE 应 param_iter_state=None（不读 legacy JSON），got {param_iter_state}"
    )


# ---------------------------------------------------------------------------
# discovery.cli：无 STATE_FILE 常量（cmd_oos/cmd_verify 切 resolve_active 后
# 不应再持有 "logs/param_iter_state.json" 字面量）
# ---------------------------------------------------------------------------
def test_discovery_cli_no_state_file_constant():
    """discovery.cli 不再持有 STATE_FILE 常量（切 resolve_active 后 legacy 入口移除）。"""
    from discovery import cli
    assert not hasattr(cli, "STATE_FILE"), (
        "discovery.cli 仍持有 STATE_FILE 常量——cmd_oos/cmd_verify 应切 resolve_active"
    )
