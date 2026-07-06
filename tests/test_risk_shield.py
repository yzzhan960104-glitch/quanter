# -*- coding: utf-8 -*-
"""风控挡板 + 配置层冒烟测试。"""


def test_qmt_gateway_exported():
    """trading 包应导出 QmtExecutionGateway（Task 1 配置层契约）。"""
    from trading import QmtExecutionGateway
    assert QmtExecutionGateway is not None
