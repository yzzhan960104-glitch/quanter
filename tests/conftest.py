"""Pytest 配置与共享 Fixtures"""
import sys
import types
from pathlib import Path

import pytest

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ============ Phase 1 Task 4：全局注入假 xtquant（collection 前生效）============
# Why 全局注入：qmt_gateway 顶部 `from xtquant.xttrader import XtQuantTrader` 在
# 真实 xtquant 可用时会绑定真实 C++ 类（实例化即连真实柜台，测试不可控）。conftest
# 是 pytest 收集时第一个被 import 的模块，在任何 trading.* 之前执行——此处把假
# xtquant 塞进 sys.modules（优先于文件系统查找），使后续所有 import trading.* 拿到
# 假模块，QmtExecutionGateway 可在无真实柜台环境被实例化与单测。CI 无 xtquant 同样生效。
def _install_fake_xtquant() -> None:
    if getattr(sys.modules.get("xtquant"), "_FAKE", False):
        return  # 已注入，避免重复覆盖

    # 假 xtconstant：枚举值与 qmt_gateway._QMT_ORDER_* 字面量契约一致
    fake_xtconstant = types.ModuleType("xtquant.xtconstant")
    fake_xtconstant.STOCK_BUY = 23
    fake_xtconstant.STOCK_SELL = 24
    fake_xtconstant.LATEST_PRICE = 5
    fake_xtconstant.FIX_PRICE = 11
    for _name, _val in [
        ("ORDER_UNREPORTED", 48), ("ORDER_REPORTED", 50), ("ORDER_REPORTED_CANCEL", 51),
        ("ORDER_CANCELED", 54), ("ORDER_PART_SUCC", 55), ("ORDER_SUCCEEDED", 56),
        ("ORDER_JUNK", 57),
    ]:
        setattr(fake_xtconstant, _name, _val)

    # 假 xttype.StockAccount
    fake_xttype = types.ModuleType("xtquant.xttype")

    class _FakeStockAccount:
        def __init__(self, acc_id, acc_type="STOCK"):
            self.account_id = acc_id
            self.account_type = 2  # 柜台内部类型编码，测试不关心具体值
    fake_xttype.StockAccount = _FakeStockAccount

    # 假 xtdata：get_full_tick 默认返空 dict（测试可 monkeypatch md.xtdata 覆盖）
    fake_xtdata = types.ModuleType("xtquant.xtdata")
    fake_xtdata.get_full_tick = lambda codes: {}

    # 假 xttrader：回调基类 + 可配置的 FakeXtQuantTrader
    fake_xttrader = types.ModuleType("xtquant.xttrader")

    class _FakeCallbackBase:
        pass

    class FakeXtQuantTrader:
        """可配置假 Trader：类属性 rc/seq/positions 作默认，实例记录所有调用。

        测试通过 monkeypatch.setattr(FakeXtQuantTrader, 'connect_rc', 1) 配置类级默认，
        或 monkeypatch 实例属性配置单例行为。
        """
        connect_rc = 0
        subscribe_rc = 0
        cancel_rc = 0
        order_seq = 100
        positions = None

        def __init__(self, path, sid):
            self.path, self.sid = path, sid
            self.cb = None
            self.calls = []

        def register_callback(self, cb):
            self.cb = cb
            self.calls.append("register_callback")

        def start(self):
            self.calls.append("start")

        def connect(self):
            self.calls.append("connect")
            return self.connect_rc

        def subscribe(self, acc):
            self.calls.append("subscribe")
            return self.subscribe_rc

        def stop(self):
            self.calls.append("stop")

        def order_stock_async(self, *args):
            self.calls.append(("order_stock_async", args))
            seq = self.order_seq
            self.order_seq += 1
            return seq

        def cancel_order_stock(self, acc, oid):
            self.calls.append(("cancel_order_stock", oid))
            return self.cancel_rc

        def query_stock_positions(self, acc):
            return self.positions

        def query_stock_asset(self, acc):
            return None

    fake_xttrader.XtQuantTrader = FakeXtQuantTrader
    fake_xttrader.XtQuantTraderCallback = _FakeCallbackBase

    # 假 xtquant 包
    fake_xt = types.ModuleType("xtquant")
    fake_xt._FAKE = True
    fake_xt.xtconstant = fake_xtconstant
    fake_xt.xtdata = fake_xtdata

    sys.modules["xtquant"] = fake_xt
    sys.modules["xtquant.xtconstant"] = fake_xtconstant
    sys.modules["xtquant.xttype"] = fake_xttype
    sys.modules["xtquant.xtdata"] = fake_xtdata
    sys.modules["xtquant.xttrader"] = fake_xttrader


_install_fake_xtquant()