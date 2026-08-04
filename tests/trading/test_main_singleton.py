# -*- coding: utf-8 -*-
"""W1.4 单引擎启动探测告警测试（spec §3.3 · [[qmt-connect-1-rootcause]]）。

物理意图：08-04 事故真根因是 ``python -m trading`` 嵌套子进程抢 QMT session
（系统 Python + venv 父子 37168→35736 并存）。本测试覆盖「启动探测命中既有实例
即 sys.exit(1)」的告警路径——**探测只告警退出，绝不自动 taskkill**（误杀 schtasks
QuanterServer 链风险高，真根治靠运维清理 + 启动告警，见 runbook §1）。

探测局限（controller 已核实，docstring 必须诚实标注）：
  - Windows 拿不到跨进程 PID：``_port_holder_alive`` 被占返 -1（PID 未知）；
  - 嵌套父子拦不住：父子共享端口归属父，本探测拦不住嵌套——告警手段非根治。
"""
import pytest


def test_main_exits_when_port_held_by_live_process(monkeypatch):
    """W1.4: port 8000 被占用且持有 PID 存活 → CRITICAL + sys.exit(1)。"""
    import trading.__main__ as m

    # 假装探测到既有实例（返 PID 12345 表示占用且存活）
    monkeypatch.setattr(m, "_port_holder_alive", lambda port: 12345)
    # 屏蔽钉钉告警副作用（_alert_critical 软降级 try/except 即可，这里显式 mock 双保险）
    monkeypatch.setattr(m, "_alert_critical", lambda msg: None)
    with pytest.raises(SystemExit) as ei:
        m._assert_single_instance()
    assert ei.value.code == 1


def test_main_proceeds_when_port_free(monkeypatch):
    """W1.4: port 8000 空闲（探测返 None）→ 不抛 SystemExit，正常放行起 server。"""
    import trading.__main__ as m

    monkeypatch.setattr(m, "_port_holder_alive", lambda port: None)
    monkeypatch.setattr(m, "_alert_critical", lambda msg: None)
    # 不应抛 SystemExit
    m._assert_single_instance()


def test_port_holder_alive_returns_none_when_port_free():
    """W1.4: 真实 socket 探测——空闲端口 connect_ex 非 0 → None（不 mock，验真实逻辑）。"""
    import trading.__main__ as m

    # 取一个几乎不可能被占的高端口探测（避开 8000 真实引擎端口）
    result = m._port_holder_alive(65530)
    assert result is None
