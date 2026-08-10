# -*- coding: utf-8 -*-
"""EnginePorts 单测（T1 缝合点 #1）。

物理意图：phases 外迁（T6-T8）后，模块级 pre_open/post_close 无法再反查 engine 实例，
经 EnginePorts 显式注入「实例特有」依赖（gate + 动态白名单读/写/清空）。本测验证三个
回调被正确承载与转发——仅验 Ports 容器契约，不验 engine 真实方法（那由 engine 各单测覆盖）。
"""
from trading.ports import EnginePorts


def test_ports_holds_three_callbacks():
    calls = {"gate": 0, "add": 0, "clr": 0}
    ports = EnginePorts(
        gate=lambda d, gw: (calls.__setitem__("gate", calls["gate"]+1), None)[1],
        whitelist_add=lambda syms: calls.__setitem__("add", calls["add"]+1),
        whitelist_clear=lambda: calls.__setitem__("clr", calls["clr"]+1),
    )
    ports.gate("2026-01-01", None); ports.whitelist_add(["000001"]); ports.whitelist_clear()
    assert calls == {"gate": 1, "add": 1, "clr": 1}
