# -*- coding: utf-8 -*-
"""BrokerProtocol 契约测试（W2-H1 · master design §5.1）。

物理意图：
    W2-H1 broker 四文件分层后，QmtExecutionGateway 的「上层可依赖面」必须被
    显式契约钉死——本测试用 runtime_checkable isinstance 做**结构化断言**：
    组装后的 QmtExecutionGateway 实例必须满足 trading.broker_ports.BrokerProtocol
    全方法面（submit/cancel/query_asset/query_orders/query_trades/sync_positions/
    probe_account_status + set_order_update_callback 钩子）。

Why isinstance 而非逐方法 hasattr：
    - 单点断言契约完整性（Protocol 增删方法自动收窄/放宽断言面，无需同步改测试）；
    - runtime_checkable 只校验方法存在性（PEP 544 语义），签名/行为由既有五套
      qmt 单测钉死——契约测试只管「面」，不管「语义」，职责分离。

反向断言（负面钉子）：
    - MockExecutionGateway（回测件，缺 query_orders/probe 等实盘面）【不满足】
      本契约——钉死「BrokerProtocol 是实盘网关面，不是所有网关的泛化父集」，
      防未来有人把契约放宽到 Mock 也过（那会稀释实盘语义）。
    - 任意对象（object()）不满足——契约非空面。
"""
from __future__ import annotations

import pytest

from broker.mock import MockExecutionGateway
from broker.qmt import QmtExecutionGateway
from trading.broker_ports import BrokerProtocol


@pytest.fixture
def qmt_gw(monkeypatch, tmp_path):
    """构造 QmtExecutionGateway 实例（纯构造，不 connect——不触达 xtquant/柜台）。

    userdata 指向临时空目录即可：__init__ 只做环境变量回退与状态初始化，
    不做任何 IO（连接惰性在 connect）。
    """
    monkeypatch.setenv("QMT_USERDATA_PATH", str(tmp_path))
    monkeypatch.setenv("QMT_ACCOUNT_ID", "TEST_ACC_CONTRACT")
    return QmtExecutionGateway()


class TestBrokerProtocolContract:
    """QmtExecutionGateway ⇄ BrokerProtocol 结构化契约断言。"""

    def test_qmt_gateway_satisfies_broker_protocol(self, qmt_gw):
        """核心断言：实盘网关实例满足 BrokerProtocol 全方法面（isinstance 结构化）。"""
        assert isinstance(qmt_gw, BrokerProtocol), (
            "QmtExecutionGateway 必须满足 trading.broker_ports.BrokerProtocol——"
            "W2-H1 分层（mixin 组装）不得丢失上层可依赖面"
        )

    def test_protocol_surface_methods_on_assembled_class(self, qmt_gw):
        """契约八方法逐一在组装类上可解析（isinstance 之外的可读性钉子）。

        Why 冗余列举：isinstance 失败时 pytest 只报 False，不报缺哪个方法；
        逐一断言让「缺面」在 CI 输出里直接可归因（哪个方法没搬到/没组装上）。
        """
        for name in (
            "submit_order", "cancel_order", "query_asset", "query_orders",
            "query_trades", "sync_positions", "probe_account_status",
            "set_order_update_callback",
        ):
            assert callable(getattr(qmt_gw, name, None)), (
                f"组装后的 QmtExecutionGateway 缺契约方法 {name}"
            )

    def test_protocol_methods_resolve_to_layer_modules(self, qmt_gw):
        """方法真身归属分层钉子：IO/业务方法的 __module__ 指各自分层文件。

        Why：防未来有人把方法体复制回 broker/qmt.py 造成「双源真理」（re-export
        垫片上是同一对象则 __module__ 恒指真身文件——复制体才会漂移）。
        """
        expect = {
            # 连接层（含 __init__/生命周期/C++ 回调）
            "connect": "broker.qmt_connection",
            "on_stock_order": "broker.qmt_connection",
            # IO 层
            "query_asset": "broker.qmt_io",
            "query_orders": "broker.qmt_io",
            # 业务层
            "submit_order": "broker.qmt_business",
            "_process_order_update": "broker.qmt_business",
        }
        for name, module in expect.items():
            actual = getattr(type(qmt_gw), name).__module__
            assert actual == module, f"{name} 真身应在 {module}，实际 {actual}"

    def test_mock_gateway_does_not_satisfy_live_contract(self):
        """负面钉子：Mock（回测件）不承诺实盘契约面（缺 query_orders/probe 等）。"""
        assert not isinstance(MockExecutionGateway(), BrokerProtocol), (
            "MockExecutionGateway 是回测撮合件，不应满足实盘 BrokerProtocol——"
            "若此断言失败说明契约被放宽到泛化父集，实盘语义被稀释"
        )

    def test_plain_object_does_not_satisfy(self):
        """负面钉子：契约非空面——任意对象不满足。"""
        assert not isinstance(object(), BrokerProtocol)
